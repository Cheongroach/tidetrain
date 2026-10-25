"""Three-process CPU smoke; optional real worker termination and cold-cache restart."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .test_correctness import tiny_config
from .train import create_model, parameters
from .protocol import initialize
from .storage import Store
import torch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-worker", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lowband-smoke-") as directory:
        root = Path(directory)
        config = tiny_config()
        config.update(local_steps=8, rounds=3)
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config))
        processes, logs = [], {}
        started = time.monotonic()
        def launch(role, worker=None, suffix=""):
            name = (worker or role) + suffix
            logs[name] = (root / (name + ".log")).open("w")
            argv = [sys.executable, "-m", "lowband_training.train", role,
                    "--root", str(root / "shared"), "--cache", str(root / name)]
            argv += ["--worker", worker] if worker else ["--config", str(config_path)]
            process = subprocess.Popen(argv, stdout=logs[name], stderr=subprocess.STDOUT)
            processes.append(process)
            return process
        try:
            killed_code = None
            if args.restart_worker:
                torch.set_num_threads(1)
                store = Store(root / "shared", root / "init-cache")
                initialize(store, config, parameters(create_model(config, torch.device("cpu"))))
                initial = launch("worker", "hl", "-before-restart")
                manifest = root / "shared/updates/000000/hl.json"
                while not manifest.exists():
                    if time.monotonic() - started > 10 or initial.poll() is not None:
                        raise RuntimeError("Worker failed before restart injection")
                    time.sleep(0.1)
                initial.terminate()
                killed_code = initial.wait(timeout=2)
            launch("coordinator")
            launch("worker", "lf")
            launch("worker", "hl", "-after-restart" if args.restart_worker else "")
            for process in processes:
                if process.poll() is None:
                    process.wait(timeout=max(1, 24 - (time.monotonic() - started)))
            for log in logs.values():
                log.flush()
            active_codes = [process.returncode for process in processes[(1 if args.restart_worker else 0):]]
            assert active_codes == [0, 0, 0], active_codes
            completed = json.loads((root / "shared/completed.json").read_text())
            assert completed["effective_tokens"] == 1536
            store = Store(root / "shared", root / "inspect")
            commit = store.read_json("rounds/000003.json")
            cursors = {worker: store.get(ref)["cursor"] for worker, ref in commit["workers"].items()}
            assert cursors == {"hl": 48, "lf": 48}, cursors
            result = {"scope": "CPU-only same-host separate processes", "restart_injected": args.restart_worker,
                      "terminated_worker_returncode": killed_code, "final_exit_codes": active_codes,
                      "seconds": time.monotonic() - started, "completed": completed,
                      "durable_worker_cursors": cursors,
                      "logs": {name: (root / (name + ".log")).read_text() for name in logs}}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps({key: value for key, value in result.items() if key != "logs"}))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
            for log in logs.values():
                log.close()


if __name__ == "__main__":
    main()
