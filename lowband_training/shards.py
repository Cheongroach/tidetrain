"""Content-addressed bounded tensor chunks, including optimizer/RNG trees.

CPU reducers hold only a few chunks. GPU learners can materialize recovery.
Manifests preserve dict keys and tuple/list types; blobs use safetensors,
never pickle. This changes memory usage, not the number of checkpoint bytes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import torch
from safetensors.torch import load_file, save_file

from .storage import IntegrityError, canonical, digest_file


class Shards:
    def __init__(self, root, chunk_bytes=16 << 20):
        if chunk_bytes < 8 or chunk_bytes > 64 << 20:
            raise ValueError("Chunk size must be between 8 bytes and 64 MiB")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunk_bytes = chunk_bytes
        self.read_bytes = self.write_bytes = self.peak_chunk_bytes = 0

    def path(self, ref):
        if not re.fullmatch("[0-9a-f]{64}", ref["sha256"]):
            raise IntegrityError("Malformed blob hash")
        if not isinstance(ref["bytes"], int) or not 0 < ref["bytes"] <= self.chunk_bytes + 4096:
            raise IntegrityError("Malformed blob size")
        return self.root / (ref["sha256"] + ".safetensors")

    def verify(self, ref):
        path = self.path(ref)
        if path.stat().st_size != ref["bytes"] or digest_file(path) != ref["sha256"]:
            raise IntegrityError("Shard size/hash mismatch")
        return path

    def put_chunk(self, value):
        value = value.detach().to("cpu").contiguous()
        size = value.numel() * value.element_size()
        if size > self.chunk_bytes:
            raise ValueError("Tensor chunk exceeds bound")
        self.peak_chunk_bytes = max(self.peak_chunk_bytes, size)
        fd, temporary = tempfile.mkstemp(dir=self.root, suffix=".partial")
        os.close(fd)
        try:
            save_file({"value": value}, temporary)
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            ref = {"sha256": digest_file(temporary), "bytes": Path(temporary).stat().st_size}
            path = self.path(ref)
            if path.exists():
                self.verify(ref)
            else:
                os.replace(temporary, path)
                self.write_bytes += ref["bytes"]
                self.sync_directory()
            return ref
        finally:
            Path(temporary).unlink(missing_ok=True)

    def sync_directory(self):
        fd = os.open(self.root, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def get_chunk(self, ref):
        value = load_file(str(self.verify(ref)))["value"]
        self.read_bytes += ref["bytes"]
        self.peak_chunk_bytes = max(self.peak_chunk_bytes, value.numel() * value.element_size())
        return value

    def tensor(self, value):
        # Slice before a device-to-host copy, even for the large embedding.
        flat = value.detach().reshape(-1)
        elements = self.chunk_bytes // value.element_size()
        chunks = [self.put_chunk(flat[start:start + elements])
                  for start in range(0, flat.numel(), elements)]
        return {"kind": "tensor", "shape": list(value.shape),
                "dtype": str(value.dtype).removeprefix("torch."), "chunks": chunks}

    def tree(self, value):
        if isinstance(value, torch.Tensor):
            return self.tensor(value)
        if isinstance(value, dict):
            return {"kind": "dict", "items": [[self.tree(key), self.tree(item)]
                                             for key, item in value.items()]}
        if isinstance(value, (tuple, list)):
            return {"kind": type(value).__name__, "items": [self.tree(item) for item in value]}
        if value is None or isinstance(value, (str, bool, int, float)):
            canonical(value)
            return {"kind": "scalar", "value": value}
        raise TypeError("Unsupported recovery type: " + type(value).__name__)

    def restore(self, manifest):
        kind = manifest["kind"]
        if kind == "tensor":
            shape, dtype = manifest["shape"], getattr(torch, manifest["dtype"])
            output = torch.empty(math.prod(shape), dtype=dtype)
            offset = 0
            for ref in manifest["chunks"]:
                chunk = self.get_chunk(ref)
                if chunk.dtype != dtype or chunk.ndim != 1 or offset + chunk.numel() > output.numel():
                    raise IntegrityError("Shard layout mismatch")
                output[offset:offset + chunk.numel()].copy_(chunk)
                offset += chunk.numel()
            if offset != output.numel():
                raise IntegrityError("Incomplete tensor")
            return output.reshape(shape)
        if kind == "dict":
            return {self.restore(key): self.restore(item) for key, item in manifest["items"]}
        if kind in ("list", "tuple"):
            values = [self.restore(item) for item in manifest["items"]]
            return tuple(values) if kind == "tuple" else values
        if kind == "scalar":
            return manifest["value"]
        raise IntegrityError("Unknown manifest kind")

    def verify_tree(self, manifest):
        for ref in references(manifest):
            self.verify(ref)

    def manifest(self, value):
        # Canonical bytes make retries independent of Python dict insertion order.
        encoded = canonical(value)
        return {"sha256": hashlib.sha256(encoded).hexdigest(), "tree": value}


def references(tree):
    if isinstance(tree, dict):
        if tree.get("kind") == "tensor":
            yield from tree["chunks"]
        else:
            for value in tree.values():
                yield from references(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from references(value)


def tensor_map(tree):
    if tree["kind"] != "dict":
        raise IntegrityError("Expected named tensor map")
    result = {}
    for key, value in tree["items"]:
        if key["kind"] != "scalar" or not isinstance(key["value"], str) or value["kind"] != "tensor":
            raise IntegrityError("Expected string/tensor entries")
        if key["value"] in result:
            raise IntegrityError("Duplicate tensor name")
        result[key["value"]] = value
    return result


def reduce_nesterov(shards, base, updates, lr, momentum):
    """Synchronous token-weighted outer SGD, one tensor chunk at a time.

    Callers must validate admission/cursors before entering. Worker iteration
    order must match the baseline for bitwise equivalence in FP32.
    """
    if not updates or lr <= 0 or not 0 <= momentum < 1:
        raise ValueError("Invalid reduction")
    params, velocity = tensor_map(base["parameters"]), tensor_map(base["momentum"])
    deltas = [tensor_map(update["delta"]) for update in updates]
    total = sum(update["tokens"] for update in updates)
    if total <= 0 or any(update["tokens"] <= 0 for update in updates):
        raise IntegrityError("Invalid token weights")
    if any(delta.keys() != params.keys() for delta in deltas) or velocity.keys() != params.keys():
        raise IntegrityError("Parameter names differ")
    next_params, next_velocity = {}, {}
    for name, layout in params.items():
        related = [velocity[name], *(delta[name] for delta in deltas)]
        if layout["dtype"] != "float32" or any(
                item["shape"] != layout["shape"] or item["dtype"] != layout["dtype"] or
                len(item["chunks"]) != len(layout["chunks"]) for item in related):
            raise IntegrityError("Incompatible tensor layout")
        p_refs, v_refs = [], []
        for index, ref in enumerate(layout["chunks"]):
            theta, old_velocity = shards.get_chunk(ref), shards.get_chunk(velocity[name]["chunks"][index])
            gradient = torch.zeros_like(theta)
            for update, delta in zip(updates, deltas):
                value = shards.get_chunk(delta[name]["chunks"][index])
                if value.shape != theta.shape or not torch.isfinite(value).all():
                    raise IntegrityError("Nonfinite or wrong-shaped delta")
                gradient.add_(value, alpha=-update["tokens"] / total)
                del value
            new_velocity = momentum * old_velocity + gradient
            new_theta = theta - lr * (gradient + momentum * new_velocity)
            if not torch.isfinite(new_theta).all() or not torch.isfinite(new_velocity).all():
                raise IntegrityError("Nonfinite outer state")
            p_refs.append(shards.put_chunk(new_theta))
            v_refs.append(shards.put_chunk(new_velocity))
        next_params[name] = {**layout, "chunks": p_refs}
        next_velocity[name] = {**layout, "chunks": v_refs}
    encode = lambda values: {"kind": "dict", "items": [
        [{"kind": "scalar", "value": name}, value] for name, value in values.items()]}
    return {"parameters": encode(next_params), "momentum": encode(next_velocity)}
