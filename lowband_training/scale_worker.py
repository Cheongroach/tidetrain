"""Real GPU learner for sharded protocol capacity; tiny != Qwen quality."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import resource
import socket
import time
from urllib.error import URLError
from urllib.parse import urlencode
import uuid

import torch

from .http_transport import Client, RemoteError
from .ledger import Ledger
from .shards import Shards, references
from .storage import canonical, digest_file
from .train import create_model, load_data, train_round


class CapacityClient(Client):
    """Retry transient overload on idempotent routes within a request budget."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transient_retries = 0
        self.jitter = random.SystemRandom()

    def request(self, *args, **kwargs):
        expiry = min(time.time() + 120, self.deadline or float("inf"))
        original_timeout = self.timeout
        try:
            for attempt in range(8):
                remaining = expiry - time.time()
                if remaining <= 0:
                    raise TimeoutError("Transient request retry budget exhausted")
                self.timeout = min(original_timeout, remaining)
                try:
                    return super().request(*args, **kwargs)
                except RemoteError as exc:
                    if exc.status not in (429, 500, 502, 503, 504):
                        raise
                    failure = exc
                except (URLError, TimeoutError, ConnectionError) as exc:
                    failure = exc
                if attempt == 7:
                    raise failure
                self.transient_retries += 1
                time.sleep(max(0, min(expiry - time.time(),
                                      self.jitter.uniform(0, min(10, 2 ** attempt)))))
        finally:
            self.timeout = original_timeout


def fetch_tree(client, shards, tree):
    for ref in references(tree):
        path = shards.path(ref)
        if path.exists():
            shards.verify(ref)
            continue
        raw = client.request("POST", "/fetch", ref, binary=True)
        if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("Fetched shard does not match manifest")
        temporary = path.with_suffix(".partial")
        with temporary.open("wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)


def upload_tree(client, shards, tree, uploaded):
    for ref in references(tree):
        if ref["sha256"] in uploaded:
            continue
        raw = shards.verify(ref).read_bytes()
        client.request("POST", "/chunk", raw, {"X-Blob-SHA256": ref["sha256"]})
        uploaded.add(ref["sha256"])


def wait_for_cohort(client, lease, hardware, config):
    wait_deadline = min(config["deadline"], time.time() + config["rendezvous_seconds"])
    client.deadline = wait_deadline
    while time.time() < wait_deadline:
        lease = client.request("POST", "/renew", {"lease": lease})
        ready = client.request("POST", "/ready", {"lease": lease, "hardware": hardware})
        if ready["training_window"] is not None:
            client.deadline = ready["training_window"]["deadline"]
            return lease, client.deadline
        interval = config.get("rendezvous_poll_seconds", 30)
        time.sleep(min(max(0, wait_deadline - time.time()),
                       client.jitter.uniform(interval * .5, interval * 1.5)))
    raise TimeoutError("Allocated node rendezvous window expired")


def run(endpoint, token, worker, root, device):
    started = time.time()
    client = CapacityClient(endpoint, token, timeout=30, deadline=time.time() + 300)
    snapshot_route = "/snapshot?" + urlencode({"worker": worker})
    snapshot = client.request("GET", snapshot_route)
    config = snapshot["config"]
    client.deadline = config["deadline"]
    shards = Shards(Path(root) / "shards", config["chunk_bytes"])
    owner = uuid.uuid4().hex
    lease = client.request("POST", "/lease", {"scope": "worker:" + worker, "owner": owner, "seconds": 600})
    dev = torch.device(device)
    torch.set_num_threads(1)
    if dev.type == "cuda":
        torch.cuda.set_device(dev)
        if not torch.cuda.is_bf16_supported():
            raise RuntimeError("BF16 GPU required")
    model, data = create_model(config, dev), load_data(config)
    deadline = config["deadline"]
    if config.get("require_rendezvous"):
        hardware = {"device": str(dev), "host": socket.gethostname(),
                    "gpu": torch.cuda.get_device_name(dev) if dev.type == "cuda" else None,
                    "gpu_uuid": str(torch.cuda.get_device_properties(dev).uuid)
                    if dev.type == "cuda" and hasattr(torch.cuda.get_device_properties(dev), "uuid") else None}
        lease, deadline = wait_for_cohort(client, lease, hardware, config)
    uploaded, submitted, rounds, rejected = set(), set(), [], 0
    while time.time() < deadline:
        base = client.request("GET", snapshot_route)["base"]
        if base["round"] >= config["rounds"]:
            report = {"worker": worker, "host": socket.gethostname(), "owner": owner,
                      "started_at": started, "finished_at": time.time(), "rounds": rounds,
                      "final_round": base["round"], "accepted_total_tokens": base["effective_tokens"],
                      "sent_bytes": client.sent_bytes, "received_bytes": client.received_bytes,
                      "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                      "device": str(dev), "torch": torch.__version__,
                      "gpu": torch.cuda.get_device_name(dev) if dev.type == "cuda" else None,
                      "gpu_uuid": str(torch.cuda.get_device_properties(dev).uuid)
                      if dev.type == "cuda" and hasattr(torch.cuda.get_device_properties(dev), "uuid") else None,
                      "purpose": "protocol_capacity" if config["model"] == "tiny" else "model_training",
                      "rejected_updates": rejected, "transient_retries": client.transient_retries}
            client.request("POST", "/worker-report", report)
            Path(root, "report.json").write_bytes(canonical(report))
            print(canonical({"event": "scale_worker_complete", **report}).decode(), flush=True)
            return
        lease = client.request("POST", "/renew", {"lease": lease})
        if base["round"] in submitted:
            time.sleep(.5)
            continue
        for tree in base["state"].values():
            fetch_tree(client, shards, tree)
        state = {key: shards.restore(tree) for key, tree in base["state"].items()}
        previous = None
        if worker in base["workers"]:
            recovery = base["workers"][worker]["recovery"]
            fetch_tree(client, shards, recovery)
            previous = shards.restore(recovery)
        delta, recovery, tokens, metrics = train_round(model, data, config, worker, state,
                                                      previous, dev, deadline)
        record = {"worker": worker, "base_round": base["round"], "epoch": base["epoch"],
                  "base_sha256": Ledger.state_hash(base["state"]), "fence": lease["fence"],
                  "tokens": tokens, "cursor": recovery["cursor"],
                  "delta": shards.tree(delta), "recovery": shards.tree(recovery), "metrics": metrics}
        upload_tree(client, shards, record["delta"], uploaded)
        upload_tree(client, shards, record["recovery"], uploaded)
        try:
            client.request("POST", "/submit", {"lease": lease, "record": record})
            rounds.append({"base_round": base["round"], "tokens": tokens, **metrics,
                           "resumed_cursor": previous["cursor"] if previous else 0,
                           "cursor": recovery["cursor"]})
            submitted.add(base["round"])
        except RemoteError as exc:
            if exc.status != 409:
                raise
            rejected += 1
            if client.request("GET", snapshot_route)["base"]["round"] == base["round"]:
                raise
        del state, delta, recovery, previous
    raise TimeoutError("Scale experiment deadline")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    Path(args.root).mkdir(parents=True, exist_ok=True)
    run(args.endpoint, os.environ["TIDETRAIN_TOKEN"], args.worker, args.root, args.device)


if __name__ == "__main__":
    main()
