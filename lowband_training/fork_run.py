"""Fork only committed training history into a new bounded HDFS run.

The read bridge may access the parent's immutable blobs; all new publication
remains in the child root. Dispatch records are copied only for accepted jobs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from .protocol import config_hash, round_name
from .storage import IntegrityError


def committed_history(parent):
    parent = Path(parent).resolve()
    run_bytes = (parent / "run.json").read_bytes()
    run = json.loads(run_bytes)
    if run["config_hash"] != config_hash(run["config"]):
        raise IntegrityError("Parent configuration fingerprint differs")
    manifests, refs = {}, {}
    number = 0
    previous = None
    while (parent / round_name(number)).is_file():
        name = round_name(number)
        commit = json.loads((parent / name).read_bytes())
        if commit["round"] != number or commit["config_hash"] != run["config_hash"]:
            raise IntegrityError("Invalid committed history")
        if previous and (commit["parent_sha256"] != previous["state"]["sha256"] or
                         commit["effective_tokens"] != previous["effective_tokens"] +
                         sum(record["tokens"] for record in commit["accepted"].values())):
            raise IntegrityError("Parent ancestry or token accounting differs")
        manifests[name] = commit
        refs[commit["state"]["sha256"]] = commit["state"]
        for worker, record in commit["accepted"].items():
            update_name = f"updates/{number - 1:06d}/{worker}.json"
            if json.loads((parent / update_name).read_bytes()) != record:
                raise IntegrityError("Accepted update differs from durable record")
            manifests[update_name] = record
            for key in ("delta", "recovery"):
                refs[record[key]["sha256"]] = record[key]
            if "transport" in record:
                dispatch_name = f"dispatch/{number - 1:06d}/{worker}/{record['transport']['generation']:04d}.json"
                assignment = json.loads((parent / dispatch_name).read_bytes())
                if assignment["job_id"] != record["transport"]["job_id"] or assignment["instance"] != record["transport"]["instance"]:
                    raise IntegrityError("Accepted dispatch differs")
                manifests[dispatch_name] = assignment
        previous = commit
        number += 1
    if previous is None:
        raise IntegrityError("Parent has no initial commit")
    for ref in refs.values():
        blob = parent / "blobs" / (ref["sha256"] + ".pt")
        if not blob.is_file() or blob.stat().st_size != ref["bytes"]:
            raise IntegrityError("Inherited blob missing or wrong size")
    # In-flight dispatch, failed attempts, unaccepted updates, and HTTP run IDs
    # are deliberately absent. Blob SHA256 is verified on every child export.
    return run, hashlib.sha256(run_bytes).hexdigest(), previous, manifests, refs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-mount", type=Path, required=True)
    parser.add_argument("--parent-uri", required=True)
    parser.add_argument("--child-uri", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0 or args.child_uri.rstrip("/") == args.parent_uri.rstrip("/"):
        raise ValueError("Require a distinct child and a positive time bound")
    from .hdfs_store import HdfsStore
    run, parent_sha, base, manifests, refs = committed_history(args.parent_mount)
    store = HdfsStore(args.child_uri, args.cache)
    if store.exists("run.json") or store.exists("fork.json"):
        raise IntegrityError("Child run already exists; never replace its deadline")
    deadline = time.time() + args.seconds
    lineage = {"parent_uri": args.parent_uri.rstrip("/"), "parent_run_sha256": parent_sha,
               "parent_deadline": run["deadline"], "fork_round": base["round"],
               "base_state": base["state"], "inherited_effective_tokens": base["effective_tokens"],
               "child_uri": args.child_uri.rstrip("/"), "deadline": deadline,
               "inherited_manifests": sorted(manifests), "inherited_blobs": refs,
               "read_only_parent_blob_mount": str(args.parent_mount.resolve() / "blobs"),
               "created_at": time.time()}
    store.deadline = deadline
    store.publish_json("fork.json", lineage)
    store.publish_json("run.json", {**run, "deadline": deadline, "lineage": {
        "parent_uri": lineage["parent_uri"], "parent_run_sha256": parent_sha,
        "fork_round": base["round"], "base_sha256": base["state"]["sha256"]}})
    for name, value in manifests.items():
        store.publish_json(name, value)
    store.publish_json("fork-ready.json", {"fork_round": base["round"], "base_state": base["state"],
                                            "parent_run_sha256": parent_sha, "deadline": deadline})
    args.output.write_text(json.dumps(lineage, indent=2) + "\n")
    print(json.dumps({"event": "committed_history_forked", "round": base["round"],
                      "effective_tokens": base["effective_tokens"], "deadline": deadline,
                      "manifests": len(manifests), "inherited_blob_bytes": sum(r["bytes"] for r in refs.values()),
                      "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
