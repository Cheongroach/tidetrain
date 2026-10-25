"""One learner process per visible CUDA device; bounded node lifetime."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--gpus-per-node", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=2700)
    args = parser.parse_args()
    node = int(os.environ["ARNOLD_ID"])
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    hardware = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,driver_version",
                               "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
    print(json.dumps({"event": "scale_node_hardware", "node": node, "hardware": hardware.stdout,
                      "returncode": hardware.returncode, "python": sys.version}), flush=True)
    if hardware.returncode != 0:
        raise RuntimeError("GPU hardware enumeration failed")
    processes = []
    for local in range(args.gpus_per_node):
        worker = f"w{node * args.gpus_per_node + local:04d}"
        directory = root / worker
        directory.mkdir(exist_ok=True)
        log = (directory / "worker.log").open("wb")
        process = subprocess.Popen([sys.executable, "-m", "lowband_training.scale_worker",
            "--endpoint", args.endpoint, "--worker", worker, "--root", str(directory),
            "--device", f"cuda:{local}"], stdout=log, stderr=subprocess.STDOUT)
        processes.append((worker, process, log, directory))
    deadline = time.monotonic() + args.seconds
    try:
        while any(process.poll() is None for _, process, _, _ in processes):
            if time.monotonic() >= deadline:
                raise TimeoutError("Node capacity run lifetime")
            time.sleep(1)
    finally:
        for worker, process, log, directory in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.close()
            print(json.dumps({"event": "scale_node_worker_exit", "worker": worker,
                              "returncode": process.returncode,
                              "log_tail": (directory / "worker.log").read_text(errors="replace")[-12000:]}),
                  flush=True)
    if any(process.returncode != 0 for _, process, _, _ in processes):
        raise SystemExit(1)
    print(json.dumps({"event": "scale_node_complete", "node": node,
                      "actual_gpu_learners": len(processes)}), flush=True)


if __name__ == "__main__":
    main()
