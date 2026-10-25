import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from .cuda_bootstrap import select_environment


class CudaBootstrapTest(unittest.TestCase):
    def run_case(self, successes):
        seen = []

        def run(argv, **kwargs):
            if argv[0] == "nvidia-smi":
                return subprocess.CompletedProcess(argv, 0, "A10, 550.54.14\n", "")
            env = kwargs["env"]
            self.assertNotIn("LOWBAND_HTTP_TOKEN", env)
            seen.append(env["LD_LIBRARY_PATH"])
            ok = len(seen) in successes
            return subprocess.CompletedProcess(argv, 0 if ok else 1, json.dumps({"ok": ok}), "")

        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "LD_LIBRARY_PATH": "/usr/local/cuda/compat:/runtime/lib",
                "LOWBAND_HTTP_TOKEN": "test-secret"}), patch("subprocess.run", side_effect=run):
            report = Path(root) / "report.json"
            env = select_environment(sys_python(), report)
            return env, json.loads(report.read_text()), seen

    def test_preserves_working_original(self):
        env, report, seen = self.run_case({1})
        self.assertEqual(report["selected"], "original")
        self.assertEqual(len(seen), 1)
        self.assertIn("/usr/local/cuda/compat", env["LD_LIBRARY_PATH"])

    def test_selects_only_verified_filtered_path(self):
        env, report, seen = self.run_case({2})
        self.assertTrue(report["ready"])
        self.assertEqual(report["selected"], "without_compat")
        self.assertEqual(env["LD_LIBRARY_PATH"], "/runtime/lib")

    def test_retains_failure_evidence(self):
        env, report, seen = self.run_case(set())
        self.assertFalse(report["ready"])
        self.assertIsNone(report["selected"])
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn("/usr/local/cuda/compat", env["LD_LIBRARY_PATH"])


def sys_python():
    import sys
    return sys.executable


if __name__ == "__main__":
    unittest.main()
