"""Bounded, resumable download of pinned public model weights."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid

MODEL = "Qwen/Qwen2.5-0.5B"
REVISION = "060db6499f32faf8b98477b0a26969ef7d8b9987"
FILES = ("config.json", "generation_config.json", "merges.txt", "model.safetensors",
         "tokenizer.json", "tokenizer_config.json", "vocab.json", "LICENSE", "README.md")


def open_with_backoff(opener, request, deadline):
    """Honor public Hub rate limits, bounded by a caller-selected deadline."""
    for attempt in range(8):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Download time budget reached; rerun to resume")
        try:
            return opener.open(request, timeout=min(20, remaining))
        except urllib.error.HTTPError as error:
            if error.code not in (429, 502, 503, 504):
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else min(60, 5 * 2 ** attempt)
            error.close()
            delay = max(1, delay)
            if time.monotonic() + delay >= deadline:
                raise TimeoutError("Server retry delay exceeds remaining download time")
            print(json.dumps({"event": "download_backoff", "status": error.code,
                              "retry_seconds": delay}), flush=True)
            until = time.monotonic() + delay
            while time.monotonic() < until:
                time.sleep(min(1, until - time.monotonic()))
    raise TimeoutError("Download retry budget exhausted")


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


def validate(path, name):
    if name.endswith(".json"):
        json.loads(path.read_text())
    if name == "model.safetensors":
        with path.open("rb") as stream:
            length = int.from_bytes(stream.read(8), "little")
            if not 2 <= length < 10 << 20:
                raise RuntimeError("Invalid safetensors header")
            header = json.loads(stream.read(length))
        end = max(value["data_offsets"][1] for key, value in header.items() if key != "__metadata__")
        if end + 8 + length != path.stat().st_size:
            raise RuntimeError("Truncated safetensors tensors")


def verify(path, name, metadata):
    """Check every asset against the pinned Hub revision, including git blobs."""
    if path.stat().st_size != metadata["size"]:
        raise RuntimeError(f"Unexpected size for {name}")
    actual_sha256 = sha256(path)
    if "lfs" in metadata:
        if actual_sha256 != metadata["lfs"]["sha256"]:
            raise RuntimeError(f"Pinned upstream SHA256 mismatch: {name}")
        upstream = {"algorithm": "sha256", "value": metadata["lfs"]["sha256"]}
    else:
        h = hashlib.sha1(f"blob {metadata['size']}\0".encode())
        with path.open("rb") as stream:
            while chunk := stream.read(4 << 20):
                h.update(chunk)
        if h.hexdigest() != metadata["blobId"]:
            raise RuntimeError(f"Pinned upstream git blob mismatch: {name}")
        upstream = {"algorithm": "git-blob-sha1", "value": metadata["blobId"]}
    validate(path, name)
    return {"bytes": path.stat().st_size, "sha256": actual_sha256,
            "upstream_hash": upstream}


def publish(source, destination):
    """Copy complete files across filesystems, then publish in one directory."""
    temporary = destination.with_name(destination.name + f".publish-{uuid.uuid4().hex}")
    try:
        with source.open("rb") as inp, temporary.open("xb") as out:
            shutil.copyfileobj(inp, out, 4 << 20)
            out.flush()
            os.fsync(out.fileno())
        if sha256(temporary) != sha256(source):
            raise RuntimeError("Published copy hash mismatch")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--staging-directory", type=Path,
                        help="Resumable local/NAS storage; complete assets are copied to destination")
    parser.add_argument("--source", choices=("huggingface", "modelscope"), default="huggingface",
                        help="Model/tokenizer transport; all assets are verified against the pinned Hugging Face revision")
    parser.add_argument("--max-mib", type=int, default=64, help="Network budget for one invocation; rerun to resume")
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.max_mib <= 0 or args.max_seconds <= 0:
        parser.error("Download byte and time budgets must be positive")
    deadline = time.monotonic() + args.max_seconds
    args.destination.mkdir(parents=True, exist_ok=True)
    staging = args.staging_directory or args.destination
    staging.mkdir(parents=True, exist_ok=True)
    lock = (staging / ".download.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    proxy = os.environ.get("HTTPS_PROXY", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    metadata_url = f"https://huggingface.co/api/models/{MODEL}/revision/{REVISION}?blobs=true"
    with open_with_backoff(opener, metadata_url, deadline) as response:
        info = json.load(response)
    if info["sha"] != REVISION or info["id"] != MODEL:
        raise RuntimeError("Unexpected upstream model revision")
    metadata = {item["rfilename"]: item for item in info["siblings"]}
    budget = args.max_mib << 20
    files = {}
    for name in FILES:
        path = args.destination / name
        asset = metadata[name]
        if path.exists():
            files[name] = verify(path, name, asset)
            continue
        staged, partial = staging / name, staging / (name + ".partial")
        old_partial = args.destination / (name + ".partial")
        if staging != args.destination and not partial.exists() and not staged.exists() and old_partial.exists():
            publish(old_partial, partial)
        if staged.exists():
            files[name] = verify(staged, name, asset)
            publish(staged, path)
            continue
        if args.source == "modelscope" and name not in ("LICENSE", "README.md"):
            url = f"https://modelscope.cn/models/{MODEL}/resolve/master/{name}"
        elif "lfs" not in asset:
            url = f"https://huggingface.co/{MODEL}/raw/{REVISION}/{name}"
        else:
            url = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}"
        if not path.exists():
            while True:
                offset = partial.stat().st_size if partial.exists() else 0
                if offset == asset["size"]:
                    files[name] = verify(partial, name, asset)
                    partial.replace(staged)
                    if staged != path:
                        publish(staged, path)
                    break
                if offset > asset["size"]:
                    raise RuntimeError("Partial exceeds pinned asset size")
                if budget <= 0:
                    print("DOWNLOAD_INCOMPLETE_RERUN", flush=True)
                    return
                chunk_bytes = min(budget, 32 << 20, asset["size"] - offset)
                request = urllib.request.Request(
                    url,
                    headers={"Range": f"bytes={offset}-{offset + chunk_bytes - 1}"})
                with open_with_backoff(opener, request, deadline) as response:
                    if response.status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match or int(match[1]) != offset:
                            raise RuntimeError("Unexpected Content-Range")
                        expected, total = int(match[2]) - offset + 1, int(match[3])
                    elif response.status == 200 and offset == 0:
                        total = expected = int(response.headers["Content-Length"])
                    else:
                        raise RuntimeError("Server ignored resumable Range request")
                    if total != asset["size"] or expected <= 0 or offset + expected > total:
                        raise RuntimeError("Response size disagrees with pinned upstream asset")
                    received = 0
                    with partial.open("ab") as out:
                        while chunk := response.read(min(4 << 20, max(1, budget))):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Download time budget reached; rerun to resume")
                            out.write(chunk)
                            budget -= len(chunk)
                            received += len(chunk)
                            if budget <= 0:
                                break
                        out.flush()
                        os.fsync(out.fileno())
                    if received < expected and budget > 0:
                        raise RuntimeError("Short download response; rerun to resume")
                size = partial.stat().st_size
                print(json.dumps({"file": name, "downloaded": size, "total": total}), flush=True)
                if size > total:
                    raise RuntimeError("Download exceeds expected size")
    manifest = {"model": MODEL, "revision": REVISION, "files": files,
                "weight_source": args.source, "tokenizer_source": args.source,
                "upstream_metadata_url": metadata_url,
                "all_assets_upstream_hash_verified": True,
                "upstream_weight_sha256_verified": True}
    temporary = staging / "source_manifest.json.partial"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    publish(temporary, args.destination / "source_manifest.json")
    temporary.unlink()
    print("MODEL_DOWNLOAD_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
