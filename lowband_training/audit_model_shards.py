"""Real checkpoint shapes/values, synthetic updates: CPU memory/math audit.

This is not a GPU training or validation-quality experiment.
"""
import argparse
import json
from pathlib import Path
import resource
import time

import torch
from safetensors import safe_open

from .shards import Shards, reduce_nesterov, tensor_map
from .storage import canonical, digest_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    torch.set_num_threads(2)
    shards = Shards(args.root, 16 << 20)
    started = time.monotonic()
    parameter_count = 0
    params, velocity, delta = {}, {}, {}
    with safe_open(args.model, framework="pt", device="cpu") as source:
        for name in source.keys():
            value = source.get_tensor(name)
            flat = value.reshape(-1)
            parameter_count += value.numel()
            refs, zeros, deltas = [], [], []
            for begin in range(0, flat.numel(), shards.chunk_bytes // 4):
                part = flat[begin:begin + shards.chunk_bytes // 4].float()
                refs.append(shards.put_chunk(part))
                zeros.append(shards.put_chunk(torch.zeros_like(part)))
                deltas.append(shards.put_chunk(torch.full_like(part, .00001)))
            layout = {"kind": "tensor", "shape": list(value.shape), "dtype": "float32"}
            params[name] = {**layout, "chunks": refs}
            velocity[name] = {**layout, "chunks": zeros}
            delta[name] = {**layout, "chunks": deltas}
    encode = lambda values: {"kind": "dict", "items": [
        [{"kind": "scalar", "value": name}, value] for name, value in values.items()]}
    base = {"parameters": encode(params), "momentum": encode(velocity)}
    updates = [{"tokens": 65536, "delta": encode(delta)},
               {"tokens": 32768, "delta": encode(delta)}]
    result = reduce_nesterov(shards, base, updates, .7, .9)
    count, max_error = 0, 0.
    for kind in ("parameters", "momentum"):
        actual = tensor_map(result[kind])
        for name, layout in params.items():
            for i, ref in enumerate(layout["chunks"]):
                theta = shards.get_chunk(ref)
                d = shards.get_chunk(delta[name]["chunks"][i])
                g = torch.zeros_like(theta)
                g.add_(d, alpha=-2 / 3)
                g.add_(d, alpha=-1 / 3)
                expected = theta - .7 * (g + .9 * g) if kind == "parameters" else g
                value = shards.get_chunk(actual[name]["chunks"][i])
                max_error = max(max_error, float((value - expected).abs().max()))
                if not torch.equal(value, expected):
                    raise AssertionError("Bitwise mismatch: " + name)
                count += 1
    report = {"purpose": "real_checkpoint_cpu_math_memory_audit_with_synthetic_deltas",
              "checkpoint_sha256": digest_file(args.model), "tensor_count": len(params),
              "stored_parameter_count": parameter_count, "checked_chunks": count,
              "max_abs_error": max_error, "chunk_bytes": shards.chunk_bytes,
              "peak_chunk_bytes": shards.peak_chunk_bytes,
              "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              "read_bytes": shards.read_bytes, "written_bytes": shards.write_bytes,
              "seconds": time.monotonic() - started}
    Path(args.output).write_bytes(canonical(report))
    print(canonical(report).decode(), flush=True)


if __name__ == "__main__":
    main()
