"""Real localhost HTTP/process regressions; these are not Dorado/GPU results."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError

import torch

from .http_controller import Controller
from .http_transport import Client, RemoteError, canonical, reference
from .protocol import latest
from .storage import Store, IntegrityError
from .test_correctness import tiny_config


class HttpTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        self.temp = tempfile.TemporaryDirectory(prefix="lowband-http-")
        self.root = Path(self.temp.name)
        self.token = secrets.token_hex(24)
        self.config = {**tiny_config(), "rounds": 2, "max_seconds": 90}
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            process.stdout.close()
        self.temp.cleanup()

    def launch(self, worker, lease=5):
        directory = self.root / (worker + "-" + str(len(self.processes)))
        env = {**os.environ, "LOWBAND_HTTP_TOKEN": self.token, "LOWBAND_CPU_THREADS": "1",
               "OMP_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""}
        process = subprocess.Popen([sys.executable, "-m", "lowband_training.http_worker", "serve",
             "--config", str(self.config_path), "--worker", worker, "--assets", str(self.root),
             "--root", str(directory), "--port", "0", "--device", "cpu", "--max-seconds", "120",
             "--lease-seconds", str(lease)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.processes.append(process)
        import select
        if not select.select([process.stdout], [], [], 10)[0]:
            raise RuntimeError("Worker startup timed out")
        line = process.stdout.readline()
        ready = json.loads(line)
        endpoint = f"http://127.0.0.1:{ready['port']}"
        return process, Client(endpoint, self.token, ready["instance"], timeout=2, chunk_size=1024)

    def controller(self, suffix, endpoints):
        store = Store(self.root / suffix, self.root / (suffix + "-cache"))
        return Controller(store, self.config, lambda: endpoints, self.token, self.root, suffix, chunk_size=4096)

    def drive(self, controller, until_round=None):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            result = controller.step()
            if result or (until_round is not None and latest(controller.store)["round"] >= until_round):
                return result
            time.sleep(.05)
        raise TimeoutError("HTTP CPU smoke did not finish")

    def test_runtime_readiness_detects_removed_dependency(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from .http_worker import Jobs
        (self.root / "model").mkdir()
        (self.root / "data").mkdir()
        (self.root / "model/config.json").write_text("{}")
        (self.root / "data/manifest.json").write_text("{}")
        dependency = self.root / "transformers-init.py"
        dependency.write_text("# dependency")
        jobs = Jobs(self.root / "runtime", {"model": "qwen"}, "hl", self.root, "cpu")
        try:
            with patch("importlib.util.find_spec", return_value=SimpleNamespace(origin=str(dependency))):
                self.assertTrue(all(jobs.runtime_integrity().values()))
                dependency.unlink()
                self.assertFalse(jobs.runtime_integrity()["transformers"])
                self.assertTrue(jobs.runtime_integrity()["model"])
            with patch("importlib.util.find_spec", return_value=None):
                self.assertFalse(jobs.runtime_integrity()["transformers"])
        finally:
            jobs.close()

    def test_upload_resume_duplicate_and_download_resume(self):
        _, client = self.launch("hl")
        source = self.root / "bytes.bin"
        source.write_bytes(bytes(range(256)) * 35)
        ref = reference(source)
        route = f"/v1/blobs/{ref['sha256']}?offset=0&size={ref['bytes']}"
        chunk = source.read_bytes()[:1024]
        header = {"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()}
        self.assertEqual(client.request("PUT", route, chunk, header)["offset"], 1024)
        # Lost response retry is idempotent, rather than appending the chunk twice.
        self.assertEqual(client.request("PUT", route, chunk, header)["offset"], 1024)
        client.upload(source, ref)
        downloaded = self.root / "download"
        request = client.request
        calls = 0
        def disconnect(method, path, *args, **kwargs):
            nonlocal calls
            if kwargs.get("binary"):
                calls += 1
                if calls == 2:
                    raise URLError("injected disconnection")
            return request(method, path, *args, **kwargs)
        client.request = disconnect
        with self.assertRaises(URLError):
            client.download(ref, downloaded)
        cold = Client(client.endpoint, self.token, client.instance, chunk_size=1024)
        result = cold.download(ref, downloaded)
        self.assertEqual(result.read_bytes(), source.read_bytes())
        self.assertEqual(cold.received_bytes, ref["bytes"] - 1024)

    def test_checksums_auth_and_incarnation(self):
        _, client = self.launch("hl")
        source = self.root / "blob"
        source.write_bytes(b"checkpoint")
        ref = reference(source)
        with self.assertRaises(RemoteError) as error:
            client.request("PUT", f"/v1/blobs/{ref['sha256']}?offset=0&size={ref['bytes']}",
                           b"checkpoint", {"X-Chunk-SHA256": "0" * 64})
        self.assertEqual(error.exception.status, 400)
        client.instance = "old-incarnation"
        with self.assertRaises(RemoteError) as error:
            client.upload(source)
        self.assertEqual(error.exception.status, 409)
        client.token = "wrong-token"
        with self.assertRaises(RemoteError) as error:
            client.upload(source)
        self.assertEqual(error.exception.status, 401)

    def test_lost_job_response_and_lease_expiry(self):
        self.config["local_steps"] = 10000
        self.config_path.write_text(json.dumps(self.config))
        _, client = self.launch("hl", lease=.6)
        controller = self.controller("lease", {"hl": client.endpoint})
        base = latest(controller.store)
        assignment = controller.assignment(base, "hl", client.endpoint, client.health())
        client.upload(controller.store.export_file(assignment["input_state"]))
        first = client.request("POST", "/v1/jobs", assignment["request"])
        second = client.request("POST", "/v1/jobs", assignment["request"])
        self.assertEqual(first["job_id"], second["job_id"])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.request("GET", "/v1/jobs/" + first["job_id"])
            if status["status"] == "failed":
                break
            time.sleep(.1)
        self.assertEqual(status["status"], "failed")
        self.assertIn("Lease expired", status["error"])
        controller.close()

    def test_two_workers_and_cold_restart_match_uninterrupted_training(self):
        hl, hclient = self.launch("hl")
        _, lclient = self.launch("lf")
        endpoints = {"hl": hclient.endpoint, "lf": lclient.endpoint}
        baseline = self.controller("baseline", endpoints)
        try:
            self.drive(baseline)
            expected = baseline.store.get(latest(baseline.store)["state"])["parameters"]
        finally:
            baseline.close()
        trial = self.controller("restart", endpoints)
        try:
            paused = trial.run(poll_seconds=.05, pause_after_round=1)
            self.assertEqual(paused, {"paused": True, "round": 1})
            self.assertFalse(trial.store.exists("dispatch/000001/hl/0000.json"))
            self.assertFalse(trial.store.exists("completed.json"))
            trial.step()  # dispatch the next round, then interrupt in-flight work
            self.assertTrue(trial.store.exists("dispatch/000001/hl/0000.json"))
        finally:
            trial.close()
        hl.terminate()
        hl.wait(timeout=10)
        _, replacement = self.launch("hl")
        self.assertNotEqual(replacement.instance, hclient.instance)
        endpoints["hl"] = replacement.endpoint
        # New controller object, separate empty cache; persistent commits alone survive.
        store = Store(self.root / "restart", self.root / "cold-controller-cache")
        resumed = Controller(store, self.config, lambda: endpoints, self.token, self.root, "restart", chunk_size=4096)
        try:
            completed = self.drive(resumed)
            final = latest(store)
            actual = store.get(final["state"])["parameters"]
            for key in expected:
                torch.testing.assert_close(expected[key], actual[key], rtol=0, atol=0)
            self.assertEqual(completed["effective_tokens"], 256)
            self.assertEqual({w: store.get(r)["cursor"] for w, r in final["workers"].items()}, {"hl": 8, "lf": 8})
        finally:
            resumed.close()

    def test_superseded_assignment_is_rejected_before_fetch(self):
        _, client = self.launch("hl")
        controller = self.controller("fence", {"hl": client.endpoint})
        base = latest(controller.store)
        assignment = controller.assignment(base, "hl", client.endpoint, client.health())
        controller.store.publish_json("dispatch/000000/hl/0001.json", {"instance": "replacement"})
        result = {key: assignment[key] for key in ("job_id", "instance", "worker", "base_round", "base_sha256", "config_hash")}
        with self.assertRaisesRegex(IntegrityError, "Superseded"):
            controller.accept(base, assignment, result, client)
        controller.close()

    def test_replayed_cleanup_preserves_later_job_artifacts(self):
        from .http_worker import Jobs
        jobs = Jobs(self.root / "depot", self.config, "hl", self.root, "cpu")
        self.addCleanup(jobs.close)
        first = self.root / "first"
        first.write_bytes(b"old")
        ref1 = jobs.depot.add(first)
        jobs.jobs["first"] = {"status": "failed", "request": {"input_state": ref1, "previous": None}}
        jobs.release("first")
        second = self.root / "second"
        second.write_bytes(b"new recovery")
        ref2 = jobs.depot.add(second)
        jobs.jobs["second"] = {"status": "succeeded", "request": {"input_state": ref2, "previous": None},
                               "result": {"delta": ref2, "recovery": ref2}}
        jobs.release("first")
        self.assertEqual(jobs.depot.verified(ref2).read_bytes(), b"new recovery")

    def test_http_evaluation_matches_full_local_tail(self):
        from .evaluate import evaluate_model
        from .train import create_model, load_data
        _, client = self.launch("hl")
        controller = self.controller("evaluate", {"hl": client.endpoint})
        self.addCleanup(controller.close)
        base = latest(controller.store)
        assignment = controller.assignment(base, "hl", client.endpoint, client.health())
        client.upload(controller.store.export_file(assignment["input_state"]))
        request = {**assignment["request"], "operation": "evaluate", "batch_size": 3}
        client.request("POST", "/v1/jobs", request)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            status = client.request("GET", "/v1/jobs/" + request["job_id"])
            if status["status"] != "running":
                break
            time.sleep(.05)
        self.assertEqual(status["status"], "succeeded", status)
        expected = evaluate_model(create_model(self.config, "cpu"),
                                  load_data(self.config)["validation"], self.config, "cpu", batch_size=3)
        actual = status["result"]["metrics"]
        self.assertEqual(actual["validation_tokens"], 1023)
        self.assertTrue(actual["full_validation"])
        self.assertAlmostEqual(actual["validation_loss"], expected["validation_loss"], places=6)

    def test_local_recovery_survives_remote_loss_during_durable_publication(self):
        self.config["rounds"] = 1
        self.config_path.write_text(json.dumps(self.config))
        _, hclient = self.launch("hl")
        _, lclient = self.launch("lf")
        controller = self.controller("publication-loss", {"hl": hclient.endpoint, "lf": lclient.endpoint})
        self.addCleanup(controller.close)
        original = controller.store.import_file
        injected = False

        def import_after_remote_loss(local, ref):
            nonlocal injected
            if not injected:
                runtime = hclient.request("GET", "/v1/runtime")
                depot = Path(runtime["storage"]["root"])
                for item in runtime["storage"]["files"]:
                    (depot / item["name"]).unlink(missing_ok=True)
                injected = True
            return original(local, ref)

        controller.store.import_file = import_after_remote_loss
        completed = self.drive(controller)
        self.assertTrue(injected)
        self.assertEqual(completed["effective_tokens"], self.config["local_steps"] *
                         self.config["batch_size"] * self.config["seq_len"] * len(self.config["workers"]))
        self.assertEqual(set(latest(controller.store)["workers"]), {"hl", "lf"})

    def test_missing_success_artifact_is_fenced_and_reassigned(self):
        _, client = self.launch("hl")
        controller = self.controller("missing", {"hl": client.endpoint})
        self.addCleanup(controller.close)
        base = latest(controller.store)
        assignment = controller.assignment(base, "hl", client.endpoint, client.health())
        status = {"status": "succeeded", "result": {}}
        from unittest.mock import patch
        original = Client.request

        def query(owner, method, path, *args, **kwargs):
            if method == "GET" and path.startswith("/v1/jobs/"):
                return status
            return original(owner, method, path, *args, **kwargs)

        with patch.object(Client, "request", query), patch.object(
                controller, "accept", side_effect=RemoteError(404, "missing")):
            controller.step()
        failure = controller.store.read_json("failed/" + assignment["job_id"] + ".json")
        self.assertIn("missing artifact", failure["error"])
        replacement = controller.assignment(base, "hl", client.endpoint, client.health())
        self.assertEqual(replacement["generation"], 1)
        self.assertNotEqual(replacement["job_id"], assignment["job_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
