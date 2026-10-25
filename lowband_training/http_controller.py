"""Dorado-side single-owner controller: push, poll, pull, then durable commit.

The endpoint provider is a callable returning {worker_slot: explicit_http_url}.
It can refresh Bernard discovery without moving stateful calls between replicas.
Do not run this loop inside independently retried/speculative Spark tasks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.error import URLError

import torch

from .http_transport import Client, Conflict, RemoteError, canonical
from .http_worker import local_config
from .protocol import advance, config_hash, initialize, latest, validate_update
from .storage import Store, IntegrityError
from .train import create_model, parameters


class LeaseKeeper:
    def __init__(self, client, job_id, seconds):
        self.client = Client(client.endpoint, client.token, client.instance,
                             timeout=min(10, seconds / 3), deadline=client.deadline)
        self.job_id = job_id
        self.stop = threading.Event()
        self.interval = max(.05, seconds / 3)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.wait(self.interval):
            try:
                self.client.request("POST", f"/v1/jobs/{self.job_id}/renew")
            except (RemoteError, OSError, TimeoutError, Conflict):
                pass  # main loop decides retry/reassignment; never grant a local lease

    def close(self):
        self.stop.set()
        self.thread.join(timeout=11)


class Controller:
    def __init__(self, store, config, endpoints, token, assets, run_id, max_attempts=4, chunk_size=4 << 20):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid run ID")
        self.store, self.config, self.endpoints, self.token = store, config, endpoints, token
        self.run_id, self.max_attempts, self.chunk_size = run_id, max_attempts, chunk_size
        self.keepers = {}
        self.clients = {}
        self.events = []
        if store.exists("rounds/000000.json"):
            initialize(store, config, {})
        else:
            model = create_model(local_config(config, assets), "cpu")
            initialize(store, config, parameters(model))
        store.publish_json("http-run.json", {"run_id": run_id, "protocol": 1, "config_hash": config_hash(config)})

    def close(self):
        for keeper in self.keepers.values():
            keeper.close()
        self.keepers.clear()

    def emit(self, event, **values):
        record = {"event": event, "time": time.time(), **values}
        self.events.append(record)
        print(json.dumps(record), flush=True)

    def input_reference(self, base):
        name = f"inputs/{base['round']:06d}.json"
        if not self.store.exists(name):
            state = self.store.get(base["state"])
            # Outer momentum remains controller-side; GPUs only need parameters.
            ref = self.store.put({"parameters": state["parameters"]})
            self.store.publish_json(name, ref)
        return self.store.read_json(name)

    def assignment(self, base, worker, endpoint, health):
        prefix = f"dispatch/{base['round']:06d}/{worker}/"
        generation = 0
        previous = None
        while self.store.exists(prefix + f"{generation:04d}.json"):
            previous = self.store.read_json(prefix + f"{generation:04d}.json")
            generation += 1
        if (previous and previous["instance"] == health["instance"] and
                not self.store.exists("failed/" + previous["job_id"] + ".json")):
            return previous
        if previous:
            old = self.keepers.pop(previous["job_id"], None)
            if old:
                old.close()
        if generation >= self.max_attempts:
            raise RuntimeError(f"Retry budget exhausted: round {base['round']} worker {worker}")
        job_id = hashlib.sha256(canonical({"run": self.run_id, "round": base["round"],
                    "worker": worker, "generation": generation, "instance": health["instance"]})).hexdigest()
        request = {"job_id": job_id, "instance": health["instance"], "worker": worker,
                   "base_round": base["round"], "base_sha256": base["state"]["sha256"],
                   "config_hash": config_hash(self.config), "input_state": self.input_reference(base),
                   "previous": base["workers"].get(worker),
                   "deadline": min(self.store.deadline, health["deadline"] - .1)}
        # Dispatch intent is committed before POST; lost POST responses can resume.
        assignment = {**request, "endpoint": endpoint, "generation": generation, "request": request}
        self.store.publish_json(prefix + f"{generation:04d}.json", assignment)
        self.emit("assigned", worker=worker, round=base["round"], generation=generation,
                  instance=health["instance"], job_id=job_id)
        return assignment

    def accept(self, base, assignment, result, client):
        worker, job_id = assignment["worker"], assignment["job_id"]
        for key in ("job_id", "instance", "worker", "base_round", "base_sha256", "config_hash"):
            if result.get(key) != assignment[key]:
                raise IntegrityError("Mismatched HTTP result: " + key)
        # Fence results from superseded assignments, including a late old endpoint.
        prefix = f"dispatch/{base['round']:06d}/{worker}/"
        if self.store.exists(prefix + f"{assignment['generation'] + 1:04d}.json"):
            raise IntegrityError("Superseded worker assignment")
        if latest(self.store)["round"] != base["round"]:
            raise IntegrityError("Stale result after round commit")
        local_artifacts = {}
        # Pull the complete recovery pair before slower durable publication:
        # Spot-local files may vanish while the controller writes the first blob.
        for field in ("delta", "recovery"):
            started = time.monotonic()
            received_before = client.received_bytes
            local = client.download(result[field], self.store.cache / "http-downloads")
            self.emit("artifact_downloaded", worker=worker, round=base["round"],
                      field=field, bytes=result[field]["bytes"], seconds=time.monotonic() - started,
                      http_bytes=client.received_bytes - received_before)
            local_artifacts[field] = local
        for field in ("delta", "recovery"):
            started = time.monotonic()
            self.store.import_file(local_artifacts[field], result[field])
            self.emit("artifact_persisted", worker=worker, round=base["round"],
                      field=field, bytes=result[field]["bytes"], seconds=time.monotonic() - started)
        record = {key: result[key] for key in ("worker", "base_round", "base_sha256", "config_hash",
                                                "delta", "recovery", "tokens", "metrics")}
        record["transport"] = {"job_id": job_id, "instance": assignment["instance"],
                               "generation": assignment["generation"],
                               "http_sent_bytes": client.sent_bytes, "http_received_bytes": client.received_bytes}
        started = time.monotonic()
        self.emit("update_validation_started", worker=worker, round=base["round"], job_id=job_id)
        validate_update(record, worker, base, self.store.read_json("run.json"),
                        self.store.get(base["state"]), self.store)
        self.emit("update_validated", worker=worker, round=base["round"],
                  job_id=job_id, seconds=time.monotonic() - started)
        self.store.publish_json(f"updates/{base['round']:06d}/{worker}.json", record)
        self.emit("update_persisted", worker=worker, round=base["round"], job_id=job_id)
        keeper = self.keepers.pop(job_id, None)
        if keeper:
            keeper.close()
        # The result is durable now; losing this best-effort cleanup response is safe.
        try:
            client.request("DELETE", "/v1/jobs/" + job_id)
        except (RemoteError, OSError, Conflict):
            pass

    def step(self):
        self.store.check_deadline()
        base = latest(self.store)
        if base["round"] >= self.config["rounds"]:
            result = {"round": base["round"], "state": base["state"],
                      "effective_tokens": base["effective_tokens"]}
            self.store.publish_json("completed.json", result)
            return result
        endpoints = self.endpoints()
        for worker in self.config["workers"]:
            if self.store.exists(f"updates/{base['round']:06d}/{worker}.json"):
                continue
            endpoint = endpoints.get(worker)
            if not endpoint:
                continue
            try:
                discover = Client(endpoint, self.token, deadline=self.store.deadline)
                health = discover.health()
                if health["worker"] != worker or health["config_hash"] != config_hash(self.config):
                    raise IntegrityError("Discovered endpoint has wrong slot or config")
                assignment = self.assignment(base, worker, endpoint, health)
                job_id = assignment["job_id"]
                client = self.clients.setdefault(job_id, Client(endpoint, self.token, health["instance"],
                                                 deadline=self.store.deadline, chunk_size=self.chunk_size))
                if job_id not in self.keepers:
                    self.keepers[job_id] = LeaseKeeper(client, job_id, health["lease_seconds"])
                try:
                    status = client.request("GET", "/v1/jobs/" + job_id)
                except RemoteError as exc:
                    if exc.status != 404:
                        raise
                    for field, ref in (("parameters", assignment["input_state"]),
                                       ("recovery", assignment["previous"])):
                        if ref:
                            started = time.monotonic()
                            source = self.store.export_file(ref)
                            source_seconds = time.monotonic() - started
                            started = time.monotonic()
                            sent_before = client.sent_bytes
                            client.upload(source, ref)
                            self.emit("artifact_uploaded", worker=worker, round=base["round"],
                                      field=field, bytes=ref["bytes"], seconds=time.monotonic() - started,
                                      http_bytes=client.sent_bytes - sent_before,
                                      source_seconds=source_seconds)
                    status = client.request("POST", "/v1/jobs", assignment["request"])
                if status["status"] == "succeeded":
                    try:
                        self.accept(base, assignment, status["result"], client)
                    except RemoteError as exc:
                        if exc.status != 404:
                            raise
                        failure = {**status, "status": "failed",
                                   "error": "Succeeded result references a missing artifact"}
                        self.store.publish_json("failed/" + job_id + ".json", failure)
                        self.keepers.pop(job_id).close()
                        self.emit("attempt_failed", worker=worker, round=base["round"],
                                  job_id=job_id, reason=failure["error"])
                elif status["status"] in ("failed", "released"):
                    self.store.publish_json("failed/" + job_id + ".json", status)
                    self.keepers.pop(job_id).close()
                    self.emit("attempt_failed", worker=worker, round=base["round"], job_id=job_id)
            except RemoteError as exc:
                if exc.status not in (404, 408, 409, 429, 500, 502, 503, 504):
                    raise
                self.emit("endpoint_retry", worker=worker, reason=str(exc))
            except (URLError, ConnectionError, TimeoutError, Conflict) as exc:
                self.emit("endpoint_retry", worker=worker, reason=type(exc).__name__)
        committed = advance(self.store, base)
        if committed:
            # Stop renewing any unaccepted task from this round (timeout quorum).
            self.close()
            self.emit("round_committed", round=committed["round"], effective_tokens=committed["effective_tokens"])
        return None

    def run(self, poll_seconds=2, pause_after_round=0):
        try:
            while True:
                completed = self.step()
                if completed:
                    return completed
                if pause_after_round and latest(self.store)["round"] >= pause_after_round:
                    self.emit("controller_pause", round=latest(self.store)["round"])
                    return {"paused": True, "round": latest(self.store)["round"]}
                time.sleep(poll_seconds)
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    endpoint_group = parser.add_mutually_exclusive_group(required=True)
    endpoint_group.add_argument("--endpoints", help="JSON {worker: HTTP URL}; reread every poll")
    endpoint_group.add_argument("--services", help="JSON with psm, services {worker: service_id}, region, cluster, env_type")
    endpoint_group.add_argument("--deployment-file", help="Verified Spot deployment IDs, image, CLI and absolute deadline")
    parser.add_argument("--root", required=True, help="Local directory or controller-accessible hdfs:// URI")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--assets", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pause-after-round", type=int, default=0,
                        help="Exit after a durable commit for controller recovery validation")
    parser.add_argument("--hdfs-command", default='["hdfs", "dfs"]', help="JSON argv prefix, never shell text")
    args = parser.parse_args()
    token = os.environ.get("LOWBAND_HTTP_TOKEN", "")
    if len(token) < 24:
        parser.error("Set LOWBAND_HTTP_TOKEN")
    torch.set_num_threads(int(os.environ.get("LOWBAND_CPU_THREADS", "2")))
    if args.root.startswith("hdfs://"):
        from .hdfs_store import HdfsStore
        store = HdfsStore(args.root, args.cache, json.loads(args.hdfs_command))
    else:
        store = Store(args.root, args.cache)
    # This prevents accidental duplicates on one host. The Dorado workflow must
    # enforce one live controller globally; no claim of distributed leader election.
    import fcntl
    with (store.cache / "http-controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads(Path(args.config).read_text())
        if args.deployment_file:
            from .spot_task_endpoints import SpotTaskEndpoints
            settings = json.loads(Path(args.deployment_file).read_text())
            if set(settings["deployments"]) != set(config["workers"]):
                raise ValueError("Deployment slots must match training configuration")
            provider = SpotTaskEndpoints(**settings, token=token, config_hash=config_hash(config))
        elif args.services:
            from .dorado_discovery import BernardEndpoints
            provider = BernardEndpoints(**json.loads(Path(args.services).read_text()))
        else:
            provider = lambda: json.loads(Path(args.endpoints).read_text())
        controller = Controller(store, config, provider, token, args.assets, args.run_id)
        if args.deployment_file:
            store.deadline = min(store.deadline, settings["deadline"])
        started = time.monotonic()
        try:
            print(json.dumps(controller.run(pause_after_round=args.pause_after_round)), flush=True)
        finally:
            controller.emit("controller_session_end", seconds=time.monotonic() - started,
                            storage_read_bytes=store.read_bytes, storage_write_bytes=store.write_bytes,
                            storage_transfer_seconds=store.transfer_seconds,
                            storage_command_count=getattr(store, "command_count", None),
                            storage_command_seconds=getattr(store, "command_seconds", None),
                            http_sent_bytes=sum(client.sent_bytes for client in controller.clients.values()),
                            http_received_bytes=sum(client.received_bytes for client in controller.clients.values()))


if __name__ == "__main__":
    main()
