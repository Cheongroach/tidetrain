"""Real local HTTP checks for slot identity and rollout filtering; no GPU."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from .spot_task_endpoints import SpotTaskEndpoints


class SpotEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.token = "test-only-" + "x" * 32
        self.health = {"worker": "hl", "device": "cuda", "api": 1,
                       "config_hash": "a" * 64, "instance": "b" * 32,
                       "deadline": time.time() + 3600}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.send_error(401)
                    return
                body = json.dumps(owner.health).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.image = "hub.byted.org/test/image:verified"
        self.data = {
            "config": {"service_id": "hl-service", "service_psm": "test.psm",
                       "latest_replicaset_name": "new-rs"},
            "detail": {"infoSpec": {"image": self.image}, "tasks": [
                {"version": "old", "replicaset": "old-rs", "containerID": "old",
                 "state": "TASK_RUNNING", "host": "127.0.0.1", "ports": [1]},
                {"version": "new", "replicaset": "new-rs", "containerID": "new",
                 "state": "NotReady", "host": "127.0.0.1",
                 "ports": [self.server.server_port]},
            ]},
        }
        self.provider = SpotTaskEndpoints(
            "/test/cli", "test.psm", {"hl": {"deployment_id": "123", "service_id": "hl-service"}},
            self.image, self.token, "a" * 64, time.time() + 60, refresh_seconds=0)
        self.cli = patch("lowband_training.spot_task_endpoints.subprocess.run",
                         side_effect=lambda *a, **k: subprocess.CompletedProcess(
                             a[0], 0, json.dumps({"data": self.data}), ""))
        self.cli.start()
        self.addCleanup(self.cli.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def test_authenticated_notready_http_is_usable_and_old_rollout_is_ignored(self):
        self.assertEqual(self.provider(), {"hl": f"http://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(self.provider.counts, {"hl": 1})
        self.data["detail"]["tasks"].pop()
        self.assertEqual(self.provider(), {})

    def test_wrong_worker_config_and_image_are_not_selected(self):
        for field, value in [("worker", "lf"), ("config_hash", "c" * 64), ("device", "cpu")]:
            original = self.health[field]
            self.health[field] = value
            self.assertEqual(self.provider(), {})
            self.health[field] = original
        self.data["detail"]["infoSpec"]["image"] = self.image + "-old"
        self.assertEqual(self.provider(), {})
        self.assertEqual(self.provider.errors, {"hl": "image_not_deployed"})

    def test_cross_service_deployment_is_rejected(self):
        self.data["config"]["service_id"] = "unrelated-service"
        with self.assertRaisesRegex(ValueError, "authorized service"):
            self.provider()

    def test_failed_cuda_probe_excludes_http_healthy_worker(self):
        self.health["cuda_ready"] = False
        self.assertEqual(self.provider(), {})
        self.assertEqual(self.provider.errors, {"hl": "cuda_probe_failed"})
        self.health["cuda_ready"] = True
        self.assertIn("hl", self.provider())

    def test_missing_runtime_files_excludes_cuda_ready_worker(self):
        self.health.update(cuda_ready=True, runtime_ready=False)
        self.assertEqual(self.provider(), {})
        self.assertEqual(self.provider.errors, {"hl": "runtime_files_missing"})
        self.health["runtime_ready"] = True
        self.assertIn("hl", self.provider())

    def test_cached_endpoint_is_removed_at_absolute_deadline(self):
        self.assertIn("hl", self.provider())
        self.provider.refresh_seconds = 100
        self.provider.deadline = time.time() - 1
        self.assertEqual(self.provider(), {})
        self.assertEqual(self.provider.counts, {"hl": 0})

    def test_expired_and_short_lived_workers_are_not_assigned(self):
        for remaining in (-1, 300):
            self.health["deadline"] = time.time() + remaining
            self.assertEqual(self.provider(), {})
            self.assertEqual(self.provider.errors, {"hl": "worker_lifetime_too_short"})

    def test_cached_worker_lifetime_is_rechecked(self):
        now = time.time()
        self.provider.deadline = now + 7200
        self.assertIn("hl", self.provider())
        self.provider.refresh_seconds = 7200
        with patch("lowband_training.spot_task_endpoints.time.time", return_value=now + 3601):
            self.assertEqual(self.provider(), {})


if __name__ == "__main__":
    unittest.main()
