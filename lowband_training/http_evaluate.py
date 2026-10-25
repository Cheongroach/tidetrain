"""Evaluate a durable checkpoint on one explicitly discovered GPU endpoint."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import torch

from .hdfs_store import HdfsStore
from .http_controller import LeaseKeeper
from .http_transport import Client, canonical
from .protocol import config_hash, round_name
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--hdfs-command", default='["hdfs", "dfs"]', help="JSON argv prefix, never shell text")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--bf16", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(2)
    store = HdfsStore(args.root, args.cache, json.loads(args.hdfs_command)) if args.root.startswith("hdfs://") else Store(args.root, args.cache)
    token = (Path(__file__).parent / ".secrets/http-token").read_text().strip()
    run = store.read_json("run.json")
    checkpoint = store.read_json(round_name(args.round))
    client = Client(args.endpoint, token, timeout=30)
    health = client.health()
    if health["worker"] != args.worker or health["config_hash"] != run["config_hash"]:
        raise ValueError("Evaluation endpoint identity differs from run")
    if health.get("cuda_ready") is False:
        raise RuntimeError("CUDA boot probe failed")
    client = Client(args.endpoint, token, health["instance"], timeout=30, deadline=health["deadline"] - 1)
    # Reuse training's immutable parameter-only input where available.
    input_name = f"inputs/{args.round:06d}.json"
    if store.exists(input_name):
        ref = store.read_json(input_name)
    else:
        state = store.get(checkpoint["state"])
        ref = store.put({"parameters": state["parameters"]})
        store.publish_json(input_name, ref)
    request = {"instance": health["instance"], "worker": args.worker, "operation": "evaluate",
               "base_round": args.round, "base_sha256": checkpoint["state"]["sha256"],
               "config_hash": config_hash(run["config"]), "input_state": ref, "previous": None,
               "deadline": health["deadline"] - 1, "batch_size": args.batch_size,
               "max_batches": args.max_batches, "bf16": args.bf16}
    request["job_id"] = hashlib.sha256(canonical(request)).hexdigest()
    print(json.dumps({"event": "evaluation_upload", "round": args.round, "bytes": ref["bytes"]}), flush=True)
    client.upload(store.export_file(ref), ref)
    keeper = LeaseKeeper(client, request["job_id"], health["lease_seconds"])
    try:
        client.request("POST", "/v1/jobs", request)
        while True:
            status = client.request("GET", "/v1/jobs/" + request["job_id"])
            if status["status"] == "succeeded":
                result = {**status["result"], "checkpoint_sha256": checkpoint["state"]["sha256"],
                          "effective_training_tokens": checkpoint["effective_tokens"],
                          "data_manifest_sha256": run["config"].get("data_manifest_sha256"),
                          "http_sent_bytes": client.sent_bytes, "http_received_bytes": client.received_bytes}
                args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result), flush=True)
                client.request("DELETE", "/v1/jobs/" + request["job_id"])
                break
            if status["status"] in ("failed", "released"):
                args.output.with_suffix(".error.json").write_text(json.dumps(status, indent=2) + "\n")
                raise RuntimeError(status)
            time.sleep(3)
    finally:
        keeper.close()


if __name__ == "__main__":
    main()
