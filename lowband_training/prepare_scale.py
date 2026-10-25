"""Prepare reviewable, pinned Merlin payloads; this command never submits."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shlex
import time
import zlib

from .storage import canonical


FILES = ("__init__.py", "storage.py", "protocol.py", "train.py", "shards.py",
         "ledger.py", "http_transport.py", "scale_worker.py", "scale_node.py", "cuda_bootstrap.py")


def configuration(members, deadline, runtime_seconds=2700, rendezvous_seconds=21600):
    return {"model": "tiny", "seed": 44, "workers": [f"w{i:04d}" for i in range(members)],
            "quorum": members, "local_steps": 4, "batch_size": 1, "seq_len": 2,
            "rounds": 2, "inner_lr": .0001, "weight_decay": .01,
            "outer_lr": .7, "outer_momentum": .9, "round_timeout": 1200,
            "max_seconds": runtime_seconds, "deadline": deadline, "chunk_bytes": 256 << 10,
            "rendezvous_seconds": rendezvous_seconds,
            "schedule_seconds": 7 * 86400,
            "io_concurrency": 16, "purpose": "protocol_capacity"}


def prepare(root, nodes, endpoint, token, cluster, group, queue, image_vid, seconds,
            gpus_per_node=8, gpu_type="NVIDIA_H20", cpu=32, memory_mb=131072,
            preemptible=False, dependency_wheel=None, schedule_seconds=7 * 86400,
            rendezvous_seconds=21600):
    if min(nodes, gpus_per_node, cpu, memory_mb) < 1 or seconds <= 120:
        raise ValueError("Positive resource sizes and lifetime over 120 seconds required")
    if schedule_seconds <= 0 or rendezvous_seconds <= 0:
        raise ValueError("Positive scheduling and rendezvous windows required")
    if dependency_wheel is None:
        raise ValueError("An offline safetensors wheel is required")
    wheel = Path(dependency_wheel)
    if not wheel.name.startswith("safetensors-0.4.5-cp311-cp311-") or not wheel.name.endswith(".whl"):
        raise ValueError("Expected pinned safetensors 0.4.5 CPython 3.11 wheel")
    wheel_bytes = wheel.read_bytes()
    wheel_hash = hashlib.sha256(wheel_bytes).hexdigest()
    wheel_packed = base64.b64encode(wheel_bytes).decode()
    members = nodes * gpus_per_node
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent
    content = {name: base64.b64encode((source / name).read_bytes()).decode() for name in FILES}
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in FILES}
    packed = base64.b64encode(zlib.compress(canonical(content))).decode()
    bootstrap = f"""import base64,hashlib,io,json,sys,zipfile,zlib
from pathlib import Path
assert sys.version_info[:2]==(3,11),'Offline dependency requires CPython 3.11'
root=Path('/tmp/tidetrain-scale/code/lowband_training')
root.mkdir(parents=True,exist_ok=True)
wheel=base64.b64decode({wheel_packed!r})
assert hashlib.sha256(wheel).hexdigest()=={wheel_hash!r},'Dependency hash mismatch'
with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
 for name in archive.namelist():
  assert not Path(name).is_absolute() and '..' not in Path(name).parts
 archive.extractall(root.parent)
blobs=json.loads(zlib.decompress(base64.b64decode({packed!r})))
hashes={hashes!r}
for name,blob in blobs.items():
 data=base64.b64decode(blob)
 assert hashlib.sha256(data).hexdigest()==hashes[name],name
 (root/name).write_bytes(data)
print(json.dumps({{'event':'source_verified','hashes':hashes}}),flush=True)
import safetensors
assert safetensors.__version__=='0.4.5'
print(json.dumps({{'event':'offline_dependency_verified','version':safetensors.__version__,
 'wheel_sha256':{wheel_hash!r}}}),flush=True)
"""
    entry = f"""#!/bin/bash
set -euo pipefail
export PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export PYTHONPATH=/tmp/tidetrain-scale/code
mkdir -p /tmp/tidetrain-scale
python3 - <<'TIDETRAIN_SOURCE'
{bootstrap}
TIDETRAIN_SOURCE
exec timeout --signal=TERM --kill-after=30s {rendezvous_seconds + seconds + 90}s python3 -m lowband_training.cuda_bootstrap \
/tmp/tidetrain-scale/cuda.json python3 -m lowband_training.scale_node \
--endpoint {shlex.quote(endpoint)} --root /tmp/tidetrain-scale/results --gpus-per-node {gpus_per_node} --seconds {rendezvous_seconds + seconds}
"""
    config = configuration(members, time.time() + schedule_seconds + rendezvous_seconds + seconds,
                           seconds, rendezvous_seconds)
    config["schedule_seconds"] = schedule_seconds
    config["schedule_deadline"] = time.time() + schedule_seconds
    config["require_rendezvous"] = True
    (root / "config.json").write_bytes(canonical(config))
    (root / "source-manifest.json").write_bytes(canonical(hashes))
    (root / "dependency-manifest.json").write_bytes(canonical({
        "filename": wheel.name, "sha256": wheel_hash, "bytes": len(wheel_bytes),
        "python": "3.11", "version": "0.4.5"}))
    payload = {
        "name": f"tidetrain-capacity{members}-20261025",
        "job_type": "train", "description": "Real GPU tiny-LM AdamW, two synchronous sharded rounds; capacity only",
        "attachments": [], "request_context": {"is_from_yaml": False},
        "job_config": {"job_template_config": {
            "entrypoint_full_script": entry,
            "env_map": {"TIDETRAIN_TOKEN": token},
            "image_meta": {"image_source": "vid", "image_vid": image_vid}}},
        "resource_config": {"resource_backend": "arnold", "arnold_resource_config": {
            "cluster_ids": [cluster], "cluster_select_mode": "single", "group_id": group,
            "roles": [{"name": "worker", "num": nodes, "gpu": gpus_per_node, "gpu_type": gpu_type,
                       "cpu": cpu, "memory_mb": memory_mb, "ports": 1, "queue_name": queue,
                       "advanced": {"is_preemptible": preemptible, "is_use_unguaranteed_quota": False,
                                    "is_disable_gang_scheduler": True, "is_stateless": True,
                                    "pod_no_restart_state": "any",
                                    "is_fail_trial_on_pod_failure": True}}]}}}
    (root / "create.json").write_bytes(canonical(payload))
    (root / "precheck.json").write_bytes(canonical({
        "node_type": "merlin_job_run", "check_point": "create", "job_run_context": payload}))
    print(canonical({"prepared": str(root), "gpus": members, "nodes": nodes,
                     "source_sha256": hashlib.sha256(canonical(hashes)).hexdigest()}).decode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--nodes", type=int, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--cluster", type=int, required=True)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--image-vid", required=True)
    parser.add_argument("--seconds", type=int, default=2700)
    parser.add_argument("--gpus-per-node", type=int, default=8)
    parser.add_argument("--gpu-type", default="NVIDIA_H20")
    parser.add_argument("--cpu", type=int, default=32)
    parser.add_argument("--memory-mb", type=int, default=131072)
    parser.add_argument("--preemptible", action="store_true")
    parser.add_argument("--dependency-wheel", required=True)
    parser.add_argument("--schedule-seconds", type=int, default=7 * 86400)
    parser.add_argument("--rendezvous-seconds", type=int, default=21600)
    args = parser.parse_args()
    prepare(args.root, args.nodes, args.endpoint, Path(args.token_file).read_text().strip(),
            args.cluster, args.group, args.queue, args.image_vid, args.seconds,
            args.gpus_per_node, args.gpu_type, args.cpu, args.memory_mb, args.preemptible,
            args.dependency_wheel, args.schedule_seconds, args.rendezvous_seconds)


if __name__ == "__main__":
    main()
