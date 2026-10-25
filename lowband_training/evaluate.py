"""Evaluate committed checkpoints, including the final partial validation window."""
import argparse
from contextlib import nullcontext
import json
import math
from pathlib import Path
import time
import torch
from .http_worker import local_config
from .storage import Store
from .protocol import round_name
from .train import batch, create_model, load_data, load_parameters, loss_for


def evaluate_model(model, data, config, device, max_batches=0, batch_size=1, bf16=False):
    device = torch.device(device)
    if batch_size <= 0 or max_batches < 0 or len(data) < 2:
        raise ValueError("Require positive batch size and a nonempty validation stream")
    if bf16 and device.type != "cuda":
        raise ValueError("BF16 evaluation requires CUDA")
    model.eval()
    length = config["seq_len"]
    full, tail = divmod(len(data) - 1, length)
    available = full + bool(tail)
    count = min(available, max_batches) if max_batches else available
    amp = (lambda: torch.autocast("cuda", dtype=torch.bfloat16)) if bf16 else nullcontext
    loss_sum, token_count = 0.0, 0
    started = time.monotonic()
    with torch.inference_mode():
        cursor = 0
        while cursor < min(full, count):
            size = min(batch_size, full - cursor, count - cursor)
            tokens = batch(data, cursor, size, length).to(device)
            with amp():
                loss = loss_for(model, tokens, config)
            if not torch.isfinite(loss):
                raise FloatingPointError("Nonfinite validation loss")
            weight = size * length
            loss_sum += float(loss) * weight
            token_count += weight
            cursor += size
        if tail and count > full:
            tokens = data[full * length:].long().unsqueeze(0).to(device)
            with amp():
                loss = loss_for(model, tokens, config)
            if not torch.isfinite(loss):
                raise FloatingPointError("Nonfinite validation tail loss")
            loss_sum += float(loss) * tail
            token_count += tail
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    loss = loss_sum / token_count
    return {"validation_loss": loss, "perplexity": math.exp(loss),
            "validation_tokens": token_count, "validation_stream_tokens": len(data),
            "validation_windows": count, "full_validation": token_count == len(data) - 1,
            "seq_len": length, "batch_size": batch_size,
            "precision": "bf16_autocast_fp32_weights" if bf16 else "fp32",
            "seconds": time.monotonic() - started, "device": str(device)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--assets")
    parser.add_argument("--hdfs-command", default='["hdfs", "dfs"]')
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--bf16", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(2)
    if args.root.startswith("hdfs://"):
        from .hdfs_store import HdfsStore
        store = HdfsStore(args.root, args.cache, json.loads(args.hdfs_command))
    else:
        store = Store(args.root, args.cache)
    run = store.read_json("run.json")
    config = local_config(run["config"], args.assets) if args.assets else run["config"]
    model = create_model(config, args.device)
    data = load_data(config)["validation"]
    checkpoint = store.read_json(round_name(args.round))
    load_parameters(model, store.get(checkpoint["state"])["parameters"])
    result = {"round": args.round, "checkpoint_sha256": checkpoint["state"]["sha256"],
              "effective_training_tokens": checkpoint["effective_tokens"],
              "data_manifest_sha256": config.get("data_manifest_sha256"),
              **evaluate_model(model, data, config, args.device, args.max_batches,
                               args.batch_size, args.bf16)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
