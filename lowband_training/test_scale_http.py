"""CPU subprocess HTTP rounds, including compact-ledger recovery hydration."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from .http_transport import Client
from .prepare_scale import configuration


class ScaleHTTPTest(unittest.TestCase):
    def test_two_rounds_restore_optimizer_and_cursor(self):
        with tempfile.TemporaryDirectory(prefix="tidetrain-http-") as temporary:
            root = Path(temporary)
            config = configuration(2, time.time() + 120)
            config["round_timeout"] = 60
            config.update(require_rendezvous=True, rendezvous_poll_seconds=.1)
            (root / "config.json").write_text(json.dumps(config))
            token = secrets.token_hex(24)
            (root / "token").write_text(token)
            with socket.socket(socket.AF_INET6) as sock:
                sock.bind(("::1", 0))
                port = sock.getsockname()[1]
            env = {**os.environ, "TIDETRAIN_TOKEN": token, "OMP_NUM_THREADS": "1",
                   "OPENBLAS_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""}
            processes, handles = [], []
            try:
                log = (root / "gateway.log").open("wb")
                handles.append(log)
                gateway = subprocess.Popen([sys.executable, "-m", "lowband_training.scale_gateway",
                    "--root", str(root / "gateway"), "--config", str(root / "config.json"),
                    "--host", "::1", "--port", str(port), "--token-file", str(root / "token")],
                    env=env, stdout=log, stderr=subprocess.STDOUT)
                processes.append(gateway)
                client = Client(f"http://[::1]:{port}", token, timeout=2)
                startup = time.monotonic() + 30
                while True:
                    try:
                        if client.health()["ready"]:
                            break
                    except OSError:
                        pass
                    if gateway.poll() is not None or time.monotonic() > startup:
                        self.fail((root / "gateway.log").read_text())
                    time.sleep(.1)
                for worker in config["workers"]:
                    log = (root / (worker + ".log")).open("wb")
                    handles.append(log)
                    processes.append(subprocess.Popen([sys.executable, "-m", "lowband_training.scale_worker",
                        "--endpoint", client.endpoint, "--worker", worker,
                        "--root", str(root / worker), "--device", "cpu"],
                        env=env, stdout=log, stderr=subprocess.STDOUT))
                for worker, process in zip(config["workers"], processes[1:]):
                    self.assertEqual(process.wait(timeout=70), 0, (root / (worker + ".log")).read_text())
                    report = json.loads((root / worker / "report.json").read_text())
                    self.assertEqual(report["final_round"], 2)
                    self.assertEqual(report["accepted_total_tokens"], 32)
                    self.assertEqual(report["rounds"][1]["resumed_cursor"], 4)
                    self.assertEqual(report["rounds"][1]["cursor"], 8)
                snapshot = client.request("GET", "/snapshot")["base"]
                self.assertEqual(snapshot["round"], 2)
                self.assertEqual(snapshot["workers"], {})
                metrics = json.loads((root / "gateway/metrics.json").read_text())
                self.assertEqual(sum(e["kind"] == "cohort_started" for e in metrics["events"]), 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
                for handle in handles:
                    handle.close()


if __name__ == "__main__":
    unittest.main()
