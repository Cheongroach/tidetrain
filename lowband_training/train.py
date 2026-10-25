"""One device per island; inner AdamW + outer Nesterov, with round recovery."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import fcntl
import json
import os
from pathlib import Path
import random
import time

import torch
from torch import nn

from .protocol import advance, cpu_tree, initialize, latest, submit
from .storage import Store, canonical, digest_file, IntegrityError


class TinyLM(nn.Module):
    """Small causal Transformer for CPU protocol tests, not performance evidence."""
    def __init__(self):
        super().__init__()
        self.embed = nn.Embedding(32, 32)
        self.position = nn.Embedding(512, 32)
        layer = nn.TransformerEncoderLayer(32, 4, 64, dropout=0, batch_first=True)
        self.layers = nn.TransformerEncoder(layer, 2, enable_nested_tensor=False)
        self.head = nn.Linear(32, 32, bias=False)

    def forward(self, tokens):
        length = tokens.shape[1]
        x = self.embed(tokens) + self.position(torch.arange(length, device=tokens.device))
        mask = torch.triu(torch.ones(length, length, device=tokens.device, dtype=torch.bool), diagonal=1)
        return self.head(self.layers(x, mask=mask))


def create_model(config, device):
    torch.manual_seed(config["seed"])
    if config["model"] == "tiny":
        model = TinyLM()
    else:
        from transformers import AutoModelForCausalLM
        manifest_path = Path(config["model_path"]) / "source_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if manifest["revision"] != config["model_revision"] or not manifest["upstream_weight_sha256_verified"]:
            raise IntegrityError("Pinned model manifest missing or mismatched")
        for filename in ("config.json", "model.safetensors"):
            if digest_file(Path(config["model_path"]) / filename) != manifest["files"][filename]["sha256"]:
                raise IntegrityError("Model asset hash mismatch: " + filename)
        model = AutoModelForCausalLM.from_pretrained(
            config["model_path"], local_files_only=True, trust_remote_code=False,
            torch_dtype=torch.float32, attn_implementation="sdpa")
        model.config.use_cache = False
        if config.get("gradient_checkpointing", True):
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    return model.to(device)


def parameters(model):
    return {name: value.detach().cpu().float().clone() for name, value in model.named_parameters()}


def load_parameters(model, values):
    named = dict(model.named_parameters())
    if named.keys() != values.keys():
        raise IntegrityError("Model parameter names do not match checkpoint")
    with torch.no_grad():
        for name, value in values.items():
            if named[name].shape != value.shape:
                raise IntegrityError("Parameter shape mismatch")
            named[name].copy_(value)


def load_data(config):
    if config["model"] == "tiny":
        return {"train": torch.arange(4096).remainder(31).add(1),
                "validation": torch.arange(512, 1536).remainder(31).add(1)}
    manifest = json.loads(Path(config["data_manifest"]).read_text())
    if digest_file(config["data_manifest"]) != config["data_manifest_sha256"]:
        raise IntegrityError("Dataset manifest changed")
    path = Path(config["data_manifest"]).parent / manifest["tokens_file"]
    if digest_file(path) != manifest["tokens_sha256"]:
        raise IntegrityError("Dataset tokens changed")
    return torch.load(path, map_location="cpu", weights_only=True)


def batch(data, cursor, batch_size, seq_len, rank=0, world=1):
    windows = (len(data) - 1) // seq_len
    owned = (windows - rank + world - 1) // world
    if owned < 1:
        raise ValueError("Dataset too small for worker shards")
    starts = [((cursor + row) % owned * world + rank) * seq_len for row in range(batch_size)]
    return torch.stack([data[start:start + seq_len + 1] for start in starts]).long()


def loss_for(model, tokens, config):
    inputs, targets = tokens[:, :-1], tokens[:, 1:]
    output = model(inputs) if config["model"] == "tiny" else model(input_ids=inputs).logits
    return nn.functional.cross_entropy(output.float().reshape(-1, output.shape[-1]), targets.reshape(-1))


def rng_state(device):
    state = {"cpu": torch.get_rng_state(), "python": random.getstate()}
    if device.type == "cuda":
        state["cuda"] = torch.cuda.get_rng_state(device)
    return state


def restore_rng(state, device):
    torch.set_rng_state(state["cpu"])
    random.setstate(state["python"])
    if device.type == "cuda" and "cuda" in state:
        torch.cuda.set_rng_state(state["cuda"], device)


def train_round(model, data, config, worker, base_state, previous=None, device="cpu", deadline=None):
    device = torch.device(device)
    load_parameters(model, base_state["parameters"])
    optimizer = torch.optim.AdamW(model.parameters(), lr=config["inner_lr"],
                                  betas=(0.9, 0.95), weight_decay=config["weight_decay"])
    cursor = 0
    rank = config["workers"].index(worker)
    if previous:
        optimizer.load_state_dict(previous["optimizer"])
        cursor = previous["cursor"]
        restore_rng(previous["rng"], device)
    else:
        torch.manual_seed(config["seed"] + rank + 1)
        random.seed(config["seed"] + rank + 1)
    start = time.monotonic()
    losses = []
    model.train()
    amp = (lambda: torch.autocast("cuda", dtype=torch.bfloat16)) if device.type == "cuda" else nullcontext
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    for _ in range(config["local_steps"]):
        if deadline and time.time() >= deadline:
            raise TimeoutError("Run deadline during local training")
        tokens = batch(data["train"], cursor, config["batch_size"], config["seq_len"], rank, len(config["workers"])).to(device)
        optimizer.zero_grad(set_to_none=True)
        with amp():
            loss = loss_for(model, tokens, config)
        if not torch.isfinite(loss):
            raise FloatingPointError("Nonfinite local loss")
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
        optimizer.step()
        cursor += config["batch_size"]
        losses.append(float(loss.detach()))
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    compute_seconds = time.monotonic() - start
    # Capture training RNG before evaluation to make restart behavior explicit.
    recovered = {"optimizer": cpu_tree(optimizer.state_dict()), "cursor": cursor,
                 "rng": cpu_tree(rng_state(device))}
    model.eval()
    with torch.no_grad(), amp():
        validation = batch(data["validation"], 0, config["batch_size"], config["seq_len"]).to(device)
        validation_loss = float(loss_for(model, validation, config))
    final = parameters(model)
    delta = {name: final[name] - value for name, value in base_state["parameters"].items()}
    metrics = {"mean_train_loss": sum(losses) / len(losses), "first_train_loss": losses[0],
               "last_train_loss": losses[-1], "local_validation_loss": validation_loss,
               "compute_seconds": compute_seconds,
               "peak_allocated_bytes": torch.cuda.max_memory_allocated(device) if device.type == "cuda" else None}
    return delta, recovered, config["local_steps"] * config["batch_size"] * config["seq_len"], metrics


def emit(event, **values):
    print(json.dumps({"event": event, "time": time.time(), **values}, allow_nan=False), flush=True)


def poll(store):
    store.check_deadline()
    time.sleep(0.5)


def run_coordinator(args, store):
    config = json.loads(Path(args.config).read_text())
    if not store.path("rounds/000000.json").exists():
        model = create_model(config, torch.device("cpu"))
        initialize(store, config, parameters(model))
        del model
    else:
        initialize(store, config, {})  # validates config and preserves the original deadline
    emit("coordinator_started", deadline=store.deadline)
    while True:
        base = latest(store)
        if base["round"] >= config["rounds"]:
            store.publish_json("completed.json", {"round": base["round"], "state": base["state"],
                                "effective_tokens": base["effective_tokens"]})
            emit("complete", round=base["round"], effective_tokens=base["effective_tokens"],
                 blob_read_bytes=store.read_bytes, blob_write_bytes=store.write_bytes)
            return
        commit = advance(store, base)
        if commit:
            emit("round_committed", round=commit["round"], workers=list(commit["accepted"]),
                 effective_tokens=commit["effective_tokens"], blob_read_bytes=store.read_bytes,
                 blob_write_bytes=store.write_bytes, transfer_seconds=store.transfer_seconds)
        else:
            poll(store)


def run_worker(args, store):
    # Startup wait is bounded even before run.json exists.
    startup_deadline = time.time() + args.startup_timeout
    store.deadline = startup_deadline
    while not store.path("rounds/000000.json").exists():
        poll(store)
    run = store.read_json("run.json")
    config, store.deadline = run["config"], run["deadline"]
    if args.worker not in config["workers"]:
        raise ValueError("Worker is not a configured slot")
    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_bf16_supported():
        raise RuntimeError("CUDA BF16 support required")
    model, data = create_model(config, device), load_data(config)
    while True:
        base = latest(store)
        if base["round"] >= config["rounds"]:
            emit("worker_complete", worker=args.worker, round=base["round"])
            return
        name = f"updates/{base['round']:06d}/{args.worker}.json"
        if store.path(name).exists():
            poll(store)  # committed upload survives process death; never train it twice
            continue
        state = store.get(base["state"])
        prior_ref = base["workers"].get(args.worker)
        previous = store.get(prior_ref) if prior_ref else None
        delta, recovery, tokens, metrics = train_round(model, data, config, args.worker, state,
                                                      previous, device, store.deadline)
        # If quorum advanced while we were training, discard without advancing durable cursor.
        if latest(store)["round"] != base["round"]:
            emit("discarded_stale_compute", worker=args.worker, round=base["round"], tokens=tokens)
            continue
        submit(store, base, args.worker, delta, recovery, tokens, metrics)
        emit("update_submitted", worker=args.worker, round=base["round"], tokens=tokens,
             blob_read_bytes=store.read_bytes, blob_write_bytes=store.write_bytes,
             transfer_seconds=store.transfer_seconds, **metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("coordinator", "worker"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--config")
    parser.add_argument("--worker")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--bandwidth-mib", type=float, default=0)
    parser.add_argument("--startup-timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.bandwidth_mib < 0 or args.startup_timeout <= 0:
        parser.error("Nonnegative bandwidth and positive startup timeout required")
    if args.role == "coordinator" and not args.config:
        parser.error("Coordinator requires --config")
    if args.role == "worker" and not args.worker:
        parser.error("Worker requires --worker")
    torch.set_num_threads(int(os.environ.get("LOWBAND_CPU_THREADS", "2")))
    store = Store(args.root, args.cache, args.bandwidth_mib)
    # Local guard only. Orchestrator must enforce ONE replica per slot globally.
    lock_name = args.role if args.role == "coordinator" else "worker-" + args.worker
    with (store.cache / (lock_name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.role == "coordinator":
            run_coordinator(args, store)
        else:
            run_worker(args, store)


if __name__ == "__main__":
    main()
