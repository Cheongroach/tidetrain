"""Local HTTP control-plane load: synthetic updates, no GPU/training claims."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import secrets
import socket
import subprocess
import sys
import threading
import time

import torch

from .http_transport import RemoteError
from .ledger import Ledger
from .prepare_scale import configuration
from .scale_worker import CapacityClient, upload_tree
from .shards import Shards
from .storage import canonical
from .train import create_model, parameters


def process_sample(pid):
    values = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"VmRSS", "VmHWM"}:
            values[key] = int(value.split()[0]) * 1024
        elif key == "Threads":
            values[key] = int(value.strip())
    return values


def run(root, clients, concurrency, seconds):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "gateway/ledger.sqlite3").exists():
        raise ValueError("Use a new root for each benchmark")
    torch.set_num_threads(1)
    config = configuration(clients, time.time() + seconds)
    config.update(require_rendezvous=True, max_seconds=seconds, rendezvous_seconds=seconds)
    (root / "config.json").write_bytes(canonical(config))
    token = secrets.token_hex(24)
    (root / "token").write_text(token)
    with socket.socket(socket.AF_INET6) as sock:
        sock.bind(("::1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://[::1]:{port}"
    log = (root / "gateway.log").open("wb")
    gateway = subprocess.Popen([sys.executable, "-m", "lowband_training.scale_gateway",
        "--root", str(root / "gateway"), "--config", str(root / "config.json"),
        "--host", "::1", "--port", str(port), "--token-file", str(root / "token")],
        env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
             "CUDA_VISIBLE_DEVICES": ""}, stdout=log, stderr=subprocess.STDOUT)
    stop = threading.Event()
    peak = {"VmRSS": 0, "VmHWM": 0, "Threads": 0}

    def sample():
        while not stop.wait(.1):
            try:
                current = process_sample(gateway.pid)
            except FileNotFoundError:
                return
            for key, value in current.items():
                peak[key] = max(peak[key], value)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.monotonic()
    stage_times, request_seconds = {}, []
    stats_lock = threading.Lock()
    peers = {}
    actual_record_bytes = 0

    def timed(stage, function):
        before = time.monotonic()
        result = function()
        stage_times[stage] = time.monotonic() - before
        print(canonical({"event": "control_stage_complete", "stage": stage,
                         "seconds": stage_times[stage]}).decode(), flush=True)
        return result

    try:
        master = CapacityClient(endpoint, token, timeout=30, deadline=config["deadline"])
        while True:
            try:
                if master.health()["ready"]:
                    break
            except OSError:
                pass
            if gateway.poll() is not None or time.time() >= config["deadline"]:
                raise RuntimeError("Gateway startup failed")
            time.sleep(.1)
        shards = Shards(root / "synthetic-shards", config["chunk_bytes"])
        named = parameters(create_model(config, torch.device("cpu")))
        delta = shards.tree({name: torch.full_like(value, .00001) for name, value in named.items()})
        # Shapes and manifest structure match TinyLM Adam state, while values
        # are synthetic and shared between logical clients. Blob traffic is
        # deduplicated setup, not a 1000-worker data-plane benchmark.
        optimizer = {"state": {i: {"step": torch.tensor(4.), "exp_avg": torch.zeros_like(value),
                                  "exp_avg_sq": torch.zeros_like(value)}
                               for i, value in enumerate(named.values())},
                     "param_groups": [{"params": list(range(len(named)))}]}
        recoveries = [shards.tree({"cursor": 4 * (index + 1), "optimizer": optimizer,
                                  "rng": {"cpu": torch.get_rng_state(), "python": random.getstate()}})
                      for index in range(config["rounds"])]
        uploaded = set()
        for tree in [delta, *recoveries]:
            upload_tree(master, shards, tree, uploaded)

        def acquire(worker):
            before = time.monotonic()
            client = CapacityClient(endpoint, token, timeout=30, deadline=config["deadline"])
            lease = client.request("POST", "/lease",
                                   {"scope": "worker:" + worker, "owner": worker, "seconds": 600})
            client.request("POST", "/ready", {"lease": lease,
                           "hardware": {"device": "cpu", "purpose": "synthetic_control_plane"}})
            with stats_lock:
                peers[worker] = (client, lease)
                request_seconds.append(time.monotonic() - before)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            timed("leases_and_ready", lambda: list(executor.map(acquire, config["workers"])))
            cohort = master.request("POST", "/ready", {"lease": peers[config["workers"][0]][1],
                                    "hardware": {"device": "cpu", "purpose": "synthetic_control_plane"}})
            if cohort["ready_workers"] != clients or cohort["training_window"] is None:
                raise AssertionError("Cohort failed to start")
            for version in range(config["rounds"]):
                base = master.request("GET", "/snapshot")["base"]
                if base["round"] != version:
                    raise AssertionError("Unexpected version")
                base_hash = Ledger.state_hash(base["state"])

                def submit(worker):
                    nonlocal actual_record_bytes
                    before = time.monotonic()
                    client, lease = peers[worker]
                    prior = client.request("GET", "/snapshot?worker=" + worker)["base"]
                    if version:
                        info = prior["workers"][worker]
                        if info["cursor"] != version * 4 or info["recovery"] != recoveries[version - 1]:
                            raise AssertionError("Recovery hydration mismatch")
                    record = {"worker": worker, "base_round": version, "epoch": base["epoch"],
                              "base_sha256": base_hash, "fence": lease["fence"], "tokens": 8,
                              "cursor": (version + 1) * 4, "delta": delta,
                              "recovery": recoveries[version], "metrics": {
                                  "purpose": "synthetic_control_plane", "gpu_training": False}}
                    result = client.request("POST", "/submit", {"lease": lease, "record": record})
                    if not result["inserted"]:
                        raise AssertionError("New logical update was not accepted")
                    with stats_lock:
                        actual_record_bytes = max(actual_record_bytes, len(canonical(record)))
                        request_seconds.append(time.monotonic() - before)
                    if worker == config["workers"][0]:
                        result = client.request("POST", "/submit", {"lease": lease, "record": record})
                        if result["inserted"]:
                            raise AssertionError("Retry counted twice")
                    return record

                records = timed(f"submit_round_{version}", lambda: list(
                    executor.map(submit, config["workers"])))
                while master.request("GET", "/snapshot")["base"]["round"] == version:
                    if time.time() >= config["deadline"] or gateway.poll() is not None:
                        raise TimeoutError("Commit did not finish")
                    time.sleep(.2)
                # The accepted previous version must be rejected afterwards.
                worker = config["workers"][0]
                try:
                    peers[worker][0].request("POST", "/submit",
                                            {"lease": peers[worker][1], "record": records[0]})
                except RemoteError as exc:
                    if exc.status != 409:
                        raise
                else:
                    raise AssertionError("Stale update was accepted")
            final = master.request("GET", "/snapshot")["base"]
            if final["effective_tokens"] != clients * 16 or final["round"] != 2:
                raise AssertionError("Synthetic token accounting mismatch")
            ledger = Ledger(root / "gateway/ledger.sqlite3")
            events = ledger.events()
            if sum(event["kind"] == "cohort_started" for event in events) != 1:
                raise AssertionError("Cohort started more than once")
            if sum(event["kind"] == "round_committed" for event in events) != 2:
                raise AssertionError("Duplicate commit")
        stop.set()
        sampler.join(timeout=5)
        request_seconds.sort()
        metrics = json.loads((root / "gateway/metrics.json").read_text())
        report = {"purpose": "local_http_synthetic_control_plane",
                  "gpu_training": False, "physical_gpu_count": 0,
                  "logical_clients": clients, "client_concurrency": concurrency,
                  "server_io_concurrency": config["io_concurrency"],
                  "synthetic_accounted_tokens": final["effective_tokens"],
                  "rounds": final["round"], "stage_seconds": stage_times,
                  "total_seconds": time.monotonic() - started,
                  "logical_operation_p95_seconds": request_seconds[int(.95 * (len(request_seconds) - 1))],
                  "peak_gateway_rss_bytes": peak["VmRSS"], "peak_gateway_hwm_bytes": peak["VmHWM"],
                  "peak_gateway_threads": peak["Threads"], "max_record_bytes": actual_record_bytes,
                  "transient_retries": sum(client.transient_retries for client, _ in peers.values()),
                  "round_metrics": metrics["rounds"], "sqlite_bytes": (root / "gateway/ledger.sqlite3").stat().st_size,
                  "limitations": ["localhost_only", "synthetic_shared_deltas_and_recovery",
                                  "deduplicated_blob_setup_not_data_plane_throughput",
                                  "no_gpu_training_or_quality_result"]}
        (root / "report.json").write_bytes(canonical(report))
        print(canonical({"event": "control_plane_complete", **report}).decode(), flush=True)
        return report
    finally:
        stop.set()
        sampler.join(timeout=5)
        if gateway.poll() is None:
            gateway.terminate()
            gateway.wait(timeout=30)
        log.close()
        (root / "token").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--clients", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    if min(args.clients, args.concurrency) < 1 or args.seconds <= 30:
        parser.error("Positive sizes and a runtime over 30 seconds required")
    run(args.root, args.clients, args.concurrency, args.seconds)


if __name__ == "__main__":
    main()
