"""Dorado preflight: HTTP upload/download plus optional controller-side HDFS commit.

Uses only Python's standard library. A successful probe is transport evidence,
not a CUDA/training benchmark. Endpoints are explicit, never guessed by GPU count.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

from .http_transport import Client, canonical, reference


def probe(endpoint, token, hdfs_root=None, hdfs_command=("hdfs", "dfs"), size=16 << 20):
    started = time.monotonic()
    if not 0 < size <= 256 << 20:
        raise ValueError("Probe size must be <=256 MiB")
    client = Client(endpoint, token)
    health = client.health()
    client.instance = health["instance"]
    with tempfile.TemporaryDirectory(prefix="lowband-http-probe-") as temp:
        root = Path(temp)
        source = root / "probe.bin"
        with source.open("wb") as dst:
            remaining = size
            while remaining:
                chunk = secrets.token_bytes(min(1 << 20, remaining))
                dst.write(chunk)
                remaining -= len(chunk)
        ref = client.upload(source)
        downloaded = client.download(ref, root / "download")
        result = {"scope": "HTTP transport probe; not training", "instance": health["instance"],
                  "worker": health["worker"], "blob": ref, "http_verified": reference(downloaded) == ref,
                  "http_sent_bytes": client.sent_bytes, "http_received_bytes": client.received_bytes,
                  "hdfs_verified": False}
        if hdfs_root:
            if not hdfs_root.startswith("hdfs://"):
                raise ValueError("HDFS root URI required")
            destination = hdfs_root.rstrip("/") + "/probe-" + uuid.uuid4().hex
            def run(*args):
                process = subprocess.run([*hdfs_command, *args], capture_output=True, timeout=60)
                if process.returncode:
                    raise OSError(process.stderr.decode(errors="replace")[-2000:])
            run("-mkdir", "-p", destination)
            run("-put", str(downloaded), destination + "/blob.partial")
            run("-mv", destination + "/blob.partial", destination + "/blob.bin")
            back = root / "hdfs-readback.bin"
            run("-get", destination + "/blob.bin", str(back))
            if reference(back) != ref:
                raise ValueError("HDFS readback checksum mismatch")
            result.update(hdfs_verified=True, hdfs_directory=destination)
            manifest = root / "completed.json"
            manifest.write_bytes(canonical(result))
            run("-put", str(manifest), destination + "/completed.partial")
            run("-mv", destination + "/completed.partial", destination + "/completed.json")
        result["seconds"] = time.monotonic() - started
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--hdfs-root")
    parser.add_argument("--hdfs-command", default='["hdfs", "dfs"]')
    parser.add_argument("--bytes", type=int, default=16 << 20)
    args = parser.parse_args()
    print(json.dumps(probe(args.endpoint, os.environ["LOWBAND_HTTP_TOKEN"], args.hdfs_root,
                           json.loads(args.hdfs_command), args.bytes), indent=2))
