"""Bounded local CUDA probes; only select a library path after actual GPU work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def gpu_probe():
    import torch
    record = {"torch": torch.__version__, "cuda_runtime": torch.version.cuda, "ok": False}
    try:
        torch.cuda.init()
        record.update(gpu=torch.cuda.get_device_name(0), device_count=torch.cuda.device_count(),
                      bf16=torch.cuda.is_bf16_supported())
        if not record["bf16"]:
            raise RuntimeError("BF16 unsupported")
        value = torch.ones((32, 32), dtype=torch.bfloat16, device="cuda")
        output = value @ value
        torch.cuda.synchronize()
        if not bool((output == 32).all().item()):
            raise RuntimeError("CUDA BF16 matmul mismatch")
        record["ok"] = True
    except Exception as exc:
        record["error"] = str(exc)
    record["libcuda_maps"] = sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                                    if "libcuda." in line})
    print(json.dumps(record), flush=True)
    return record["ok"]


def select_environment(python, report_path):
    original = os.environ.get("LD_LIBRARY_PATH", "")
    # CUDA compat and stub libraries must not shadow the mounted host driver.
    filtered = ":".join(p for p in original.split(":") if p and "compat" not in p and "stubs" not in p)
    host = [p for p in ("/usr/local/nvidia/lib64", "/usr/local/nvidia/lib",
                       "/usr/lib/x86_64-linux-gnu", "/usr/lib64")
            if list(Path(p).glob("libcuda.so*"))]
    candidates = [("original", original), ("without_compat", filtered),
                  ("host_first", ":".join([*host, filtered]))]
    report = {"attempts": [], "selected": None, "ready": False}
    kernel_version = Path("/proc/driver/nvidia/version")
    if kernel_version.exists():
        report["kernel_driver"] = kernel_version.read_text()[:2048]
        if "470.129.06" in report["kernel_driver"]:
            matching = Path("/opt/lowband-drivers/470.129.06")
            if (matching / "libcuda.so.1").exists():
                candidates.append(("matching_470", str(matching) + ":" + filtered))
    try:
        smi = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        report["nvidia_smi"] = smi.stdout.strip()[:2048]
        report["nvidia_smi_error"] = smi.stderr.strip()[:2048]
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["nvidia_smi_error"] = str(exc)
    tried = set()
    selected_env = dict(os.environ)
    for label, value in candidates:
        if value in tried:
            continue
        tried.add(value)
        env = {**os.environ, "LD_LIBRARY_PATH": value}
        probe_env = {k: v for k, v in env.items() if k != "LOWBAND_HTTP_TOKEN"}
        try:
            proc = subprocess.run([python, "-m", "lowband_training.cuda_bootstrap", "probe"],
                                  env=probe_env, capture_output=True, text=True, timeout=45)
            lines = proc.stdout.strip().splitlines()
            record = json.loads(lines[-1]) if lines else {"ok": False}
            record.update(candidate=label, library_path=value, returncode=proc.returncode,
                          stderr=proc.stderr[-4096:])
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            record = {"candidate": label, "ok": False, "error": str(exc)[:4096]}
        report["attempts"].append(record)
        if record.get("ok"):
            report.update(selected=label, ready=True)
            selected_env = env
            break
    Path(report_path).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"event": "cuda_bootstrap", **report}), flush=True)
    selected_env["LOWBAND_CUDA_REPORT"] = str(report_path)
    return selected_env


def main():
    if sys.argv[1] == "probe":
        raise SystemExit(0 if gpu_probe() else 1)
    report_path, command = sys.argv[1], sys.argv[2:]
    env = select_environment(command[0], report_path)
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
