"""CPU checks for committed-only forks and read-only parent blob fallback."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .fork_run import committed_history
from .hdfs_mounted_reader import main as read_bridge
from .protocol import config_hash
from .storage import IntegrityError


class ForkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / "parent"
        self.child = self.root / "child"
        self.parent.mkdir()
        self.child.mkdir()
        self.ref = {"sha256": "a" * 64, "bytes": 7}
        (self.parent / "blobs").mkdir()
        (self.parent / "blobs" / (self.ref["sha256"] + ".pt")).write_bytes(b"payload")
        self.config = {"workers": ["hl"], "rounds": 4}
        fingerprint = config_hash(self.config)
        self.write("run.json", {"config": self.config, "config_hash": fingerprint, "deadline": 123})
        self.write("rounds/000000.json", {"round": 0, "config_hash": fingerprint,
                   "state": self.ref, "accepted": {}, "workers": {}, "effective_tokens": 0})
        self.record = {"tokens": 64, "delta": self.ref, "recovery": self.ref,
                       "transport": {"generation": 1, "job_id": "accepted", "instance": "original"}}
        self.write("rounds/000001.json", {"round": 1, "config_hash": fingerprint,
                   "parent_sha256": self.ref["sha256"], "state": self.ref,
                   "accepted": {"hl": self.record}, "workers": {"hl": self.ref}, "effective_tokens": 64})
        self.write("updates/000000/hl.json", self.record)
        self.write("dispatch/000000/hl/0001.json", {"job_id": "accepted", "instance": "original"})
        self.write("dispatch/000000/hl/0000.json", {"job_id": "failed"})
        self.write("dispatch/000001/hl/0000.json", {"job_id": "in-flight"})
        self.write("updates/000001/hl.json", {"tokens": 1000})
        self.write("http-run.json", {"run_id": "old"})

    def write(self, name, value):
        path = self.parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def argv(self, *command):
        return ["bridge", "--uri-prefix", "hdfs://test/child", "--mount-prefix", str(self.child),
                "--parent-blob-mount", str(self.parent / "blobs"), *command]

    def test_only_committed_history_and_accepted_dispatch_survive(self):
        run, digest, base, manifests, refs = committed_history(self.parent)
        self.assertEqual(base["effective_tokens"], 64)
        self.assertEqual(len(digest), 64)
        self.assertEqual(set(manifests), {"rounds/000000.json", "rounds/000001.json",
                         "updates/000000/hl.json", "dispatch/000000/hl/0001.json"})
        self.assertEqual(refs, {self.ref["sha256"]: self.ref})
        self.assertEqual(run["deadline"], 123)

    def test_broken_ancestry_and_missing_blob_reject_fork(self):
        name = "rounds/000001.json"
        original = json.loads((self.parent / name).read_text())
        self.write(name, {**original, "parent_sha256": "b" * 64})
        with self.assertRaises(IntegrityError):
            committed_history(self.parent)
        self.write(name, original)
        (self.parent / "blobs" / (self.ref["sha256"] + ".pt")).unlink()
        with self.assertRaises(IntegrityError):
            committed_history(self.parent)

    def test_read_parent_blob_without_writing_parent_or_overwriting_destination(self):
        output = self.root / "download"
        command = ("-get", "hdfs://test/child/blobs/" + self.ref["sha256"] + ".pt", str(output))
        with patch("sys.argv", self.argv(*command)):
            read_bridge()
        self.assertEqual(output.read_bytes(), b"payload")
        with patch("sys.argv", self.argv(*command)), self.assertRaises(FileExistsError):
            read_bridge()
        self.assertFalse((self.child / "blobs").exists())

    def test_metadata_writes_and_non_blob_reads_never_use_parent(self):
        for command in [("-cat", "hdfs://test/child/run.json"),
                        ("-put", "local", "hdfs://test/child/new"),
                        ("-get", "hdfs://test/child/run.json", str(self.root / "out")),
                        ("-get", "hdfs://test/child/blobs/not-a-hash.pt", str(self.root / "out"))]:
            with patch("sys.argv", self.argv(*command)), patch("os.execvp") as execute:
                read_bridge()
                execute.assert_called_once_with("hdfs", ["hdfs", "dfs", *command])

    def test_traversal_and_parent_symlink_escape_rejected(self):
        command = ("-get", "hdfs://test/child/../blobs/out.pt", str(self.root / "out"))
        with patch("sys.argv", self.argv(*command)), self.assertRaises(ValueError):
            read_bridge()
        outside = self.root / "outside"
        outside.write_bytes(b"payload")
        blob = self.parent / "blobs" / (self.ref["sha256"] + ".pt")
        blob.unlink()
        blob.symlink_to(outside)
        command = ("-get", "hdfs://test/child/blobs/" + self.ref["sha256"] + ".pt", str(self.root / "out"))
        with patch("sys.argv", self.argv(*command)), self.assertRaises(ValueError):
            read_bridge()


if __name__ == "__main__":
    unittest.main()
