"""Pinned WikiText-2 raw data; contiguous windows, stable per-slot sharding."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import uuid

if __package__:
    from .prepare_model import open_with_backoff
else:
    from prepare_model import open_with_backoff

DATASET = "Salesforce/wikitext"
REVISION = "b08601e04326c79dfdd32d625aee71d232d685c3"
# Official LFS records for this exact dataset revision.
UPSTREAM = {
    "train": (6357543, "e83889baabc497075506f91975be5fac0d45c5290b6b20582c8cd1e853d0c9f7"),
    "validation": (657209, "204929b7ff9d6184953f867dedb860e40aa69c078fc1e54b3baaa8fb28511c4c"),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--config-template", type=Path, required=True)
    parser.add_argument("--config-output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.max_seconds <= 0:
        parser.error("Positive download time budget required")
    deadline = time.monotonic() + args.max_seconds
    import pyarrow.parquet as pq
    import torch
    from transformers import AutoTokenizer
    model_manifest = json.loads((args.model / "source_manifest.json").read_text())
    if not model_manifest.get("all_assets_upstream_hash_verified"):
        raise RuntimeError("Tokenizer assets must be verified against the pinned model revision")
    for name in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"):
        if sha(args.model / name) != model_manifest["files"][name]["sha256"]:
            raise RuntimeError("Tokenizer asset hash mismatch: " + name)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
    args.destination.mkdir(parents=True, exist_ok=True)
    proxy = os.environ.get("HTTPS_PROXY", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {}))
    result, sources = {}, {}
    for split in ("train", "validation"):
        filename = f"{split}-00000-of-00001.parquet"
        path = args.destination / filename
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/wikitext-2-raw-v1/{filename}"
        if not path.exists():
            temporary = path.with_name(path.name + f".partial-{uuid.uuid4().hex}")
            with open_with_backoff(opener, url, deadline) as response, temporary.open("xb") as stream:
                while chunk := response.read(1 << 20):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Dataset download time budget reached")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.stat().st_size != UPSTREAM[split][0] or sha(temporary) != UPSTREAM[split][1]:
                raise RuntimeError("Pinned dataset SHA256 mismatch: " + split)
            temporary.replace(path)
        if path.stat().st_size != UPSTREAM[split][0] or sha(path) != UPSTREAM[split][1]:
            raise RuntimeError("Pinned dataset SHA256 mismatch: " + split)
        lines = pq.read_table(path, columns=["text"])["text"].to_pylist()
        text = "\n".join(line for line in lines if line.strip())
        ids = tokenizer.encode(text, add_special_tokens=False)
        result[split] = torch.tensor(ids, dtype=torch.int32)
        sources[split] = {"url": url, "sha256": sha(path), "tokens": len(ids),
                          "upstream_sha256_verified": True}
    token_file = args.destination / "tokens.pt"
    temporary = token_file.with_suffix(".partial")
    with temporary.open("wb") as stream:
        torch.save(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(token_file)
    manifest = {"dataset": DATASET, "revision": REVISION, "configuration": "wikitext-2-raw-v1",
                "sources": sources, "tokens_file": token_file.name, "tokens_sha256": sha(token_file),
                "tokenizer_model": model_manifest["model"], "tokenizer_revision": model_manifest["revision"],
                "validation_policy": "separate official validation split; fixed first batch is smoke only"}
    manifest_path = args.destination / "manifest.json"
    temporary = manifest_path.with_suffix(".partial")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(manifest_path)
    config = json.loads(args.config_template.read_text())
    config["model_path"] = str(args.model)
    config["data_manifest"] = str(manifest_path)
    config["data_manifest_sha256"] = sha(manifest_path)
    args.config_output.parent.mkdir(parents=True, exist_ok=True)
    args.config_output.write_text(json.dumps(config, indent=2) + "\n")
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
