"""Versioned synchronous local-SGD rounds with optional timeout quorum.

The coordinator is single-writer. A worker slot also has a single active owner.
All accepted optimizer/RNG/cursor states are referenced in the round commit;
unaccepted work never advances the durable data cursor. Full optimizer uploads
are deliberate for recovery correctness and MUST count toward traffic metrics.
"""
from __future__ import annotations

import hashlib
import math
import time

import torch

from .storage import IntegrityError, canonical


def round_name(number):
    return f"rounds/{number:06d}.json"


def config_hash(config):
    return hashlib.sha256(canonical(config)).hexdigest()


def cpu_tree(value):
    if isinstance(value, torch.Tensor):
        return value.detach().cpu().clone()
    if isinstance(value, dict):
        return {key: cpu_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(cpu_tree(item) for item in value)
    return value


def validate_config(config):
    workers = config["workers"]
    if not workers or len(set(workers)) != len(workers):
        raise ValueError("Unique worker slots required")
    import re
    if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", w) for w in workers):
        raise ValueError("Invalid worker ID")
    if not 1 <= config["quorum"] <= len(workers):
        raise ValueError("Invalid quorum")
    for name in ("local_steps", "rounds", "seq_len", "batch_size", "round_timeout", "max_seconds"):
        if config[name] <= 0:
            raise ValueError(f"{name} must be positive")
    if not 0 <= config["outer_momentum"] < 1 or config["outer_lr"] <= 0:
        raise ValueError("Invalid outer optimizer settings")


def initialize(store, config, parameters):
    validate_config(config)
    fingerprint = config_hash(config)
    if store.exists("run.json"):
        run = store.read_json("run.json")
        if run["config_hash"] != fingerprint:
            raise IntegrityError("Run config changed on resume")
    else:
        run = {"config": config, "config_hash": fingerprint,
               "deadline": time.time() + config["max_seconds"]}
        store.publish_json("run.json", run)
    store.deadline = run["deadline"]
    if not store.exists(round_name(0)):
        state = {"parameters": cpu_tree(parameters),
                 "momentum": {key: torch.zeros_like(value, device="cpu", dtype=torch.float32)
                              for key, value in parameters.items()}}
        reference = store.put(state)
        store.publish_json(round_name(0), {
            "round": 0, "config_hash": fingerprint, "state": reference,
            "accepted": {}, "workers": {}, "effective_tokens": 0,
            "created_at": time.time(),
        })
    return store.read_json(round_name(0))


def latest(store):
    # Ignore orphan .pt/.partial uploads. Checkpoints are visible only at commit.
    number = 0
    while store.exists(round_name(number + 1)):
        number += 1
    return store.read_json(round_name(number))


def submit(store, base, worker, delta, recovery, token_count, metrics):
    run = store.read_json("run.json")
    if worker not in run["config"]["workers"]:
        raise IntegrityError("Unknown worker slot")
    if token_count <= 0:
        raise IntegrityError("Empty update")
    path = f"updates/{base['round']:06d}/{worker}.json"
    identity = {"base_round": base["round"], "base_sha256": base["state"]["sha256"],
                "worker": worker, "config_hash": run["config_hash"]}
    if store.exists(path):
        previous = store.read_json(path)
        if any(previous[key] != value for key, value in identity.items()):
            raise IntegrityError("Conflicting worker retry")
        return previous  # same worker/round is one logical update, never counted twice
    reference = store.put({"delta": cpu_tree(delta)})
    recovered = store.put(cpu_tree(recovery))
    record = {**identity, "delta": reference, "recovery": recovered,
              "tokens": token_count, "metrics": metrics}
    store.publish_json(path, record)  # commit last: both blobs are already complete
    return record


def validate_update(record, worker, base, run, state, store):
    expected = {"worker": worker, "base_round": base["round"],
                "base_sha256": base["state"]["sha256"], "config_hash": run["config_hash"]}
    if any(record.get(key) != value for key, value in expected.items()):
        raise IntegrityError("Stale or mismatched update")
    config = run["config"]
    expected_tokens = config["local_steps"] * config["batch_size"] * config["seq_len"]
    if record["tokens"] != expected_tokens:
        raise IntegrityError("Unexpected completed token count")
    delta = store.get(record["delta"])["delta"]
    if delta.keys() != state["parameters"].keys():
        raise IntegrityError("Parameter name mismatch")
    for name, value in delta.items():
        if value.shape != state["parameters"][name].shape or not torch.isfinite(value).all():
            raise IntegrityError("Nonfinite or wrong-shaped delta: " + name)
    cursor = store.get(record["recovery"])["cursor"]
    prior = base["workers"].get(worker)
    old_cursor = store.get(prior)["cursor"] if prior else 0
    expected_cursor = old_cursor + config["local_steps"] * config["batch_size"]
    if cursor != expected_cursor:
        raise IntegrityError("Invalid data cursor advancement")
    return delta


def nesterov(parameters, velocity, gradients, lr, momentum):
    # Same recurrence as torch.optim.SGD(momentum=mu, nesterov=True),
    # with g = theta_global - mean(theta_local); zero initial velocity.
    next_velocity = {key: momentum * velocity[key] + gradients[key] for key in parameters}
    next_parameters = {key: parameters[key].float() - lr * (gradients[key] + momentum * next_velocity[key])
                       for key in parameters}
    if any(not torch.isfinite(value).all() for value in next_parameters.values()):
        raise IntegrityError("Nonfinite outer update")
    return {"parameters": next_parameters, "momentum": next_velocity}


def advance(store, base, force_timeout=False):
    run = store.read_json("run.json")
    config = run["config"]
    store.deadline = run["deadline"]
    store.check_deadline()
    destination = round_name(base["round"] + 1)
    if store.exists(destination):
        result = store.read_json(destination)
        if result.get("parent_sha256") != base["state"]["sha256"]:
            raise IntegrityError("Conflicting round ancestry")
        return result
    records = {}
    for worker in config["workers"]:
        name = f"updates/{base['round']:06d}/{worker}.json"
        if store.exists(name):
            records[worker] = store.read_json(name)
    all_ready = len(records) == len(config["workers"])
    timed_out = force_timeout or time.time() >= base["created_at"] + config["round_timeout"]
    if not all_ready and not (timed_out and len(records) >= config["quorum"]):
        return None
    state = store.get(base["state"])
    gradients = {key: torch.zeros_like(value, dtype=torch.float32) for key, value in state["parameters"].items()}
    total = sum(record["tokens"] for record in records.values())
    if total <= 0:
        raise IntegrityError("Invalid total tokens")
    for worker, record in records.items():
        delta = validate_update(record, worker, base, run, state, store)
        for key in gradients:
            gradients[key].add_(delta[key].float(), alpha=-record["tokens"] / total)
        del delta
    result = nesterov(state["parameters"], state["momentum"], gradients,
                      config["outer_lr"], config["outer_momentum"])
    reference = store.put(result)
    workers = dict(base["workers"])
    workers.update({worker: record["recovery"] for worker, record in records.items()})
    commit = {"round": base["round"] + 1, "config_hash": run["config_hash"],
              "parent_sha256": base["state"]["sha256"], "state": reference,
              "accepted": records, "workers": workers,
              "effective_tokens": base["effective_tokens"] + total,
              "created_at": time.time()}
    store.publish_json(destination, commit)
    return commit
