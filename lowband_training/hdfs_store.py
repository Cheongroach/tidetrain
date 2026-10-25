"""Controller-only HDFS store using the runtime's existing hdfs dfs client.

One live Dorado controller per run is required. HDFS rename publishes immutable
manifests. No GPU worker imports this backend or receives HDFS credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import uuid

import torch

from .http_transport import check_ref
from .storage import Store, IntegrityError, canonical, digest_file


class HdfsStore(Store):
    def __init__(self, root, cache, command=("hdfs", "dfs"), deadline=None):
        if not root.startswith("hdfs://"):
            raise ValueError("HDFS URI required")
        self.root = root.rstrip("/")
        self.cache = Path(cache).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.command = list(command)
        self.deadline = deadline
        self.read_bytes = self.write_bytes = 0
        self.transfer_seconds = 0.0
        self.bandwidth = 0
        # Only positive results are cached: published objects are immutable.
        # A new process starts empty and reads durable HDFS commits again.
        self._present = set()
        self._manifests = {}
        self.command_count = 0
        self.command_seconds = 0.0
        self._run("-mkdir", "-p", self.root)

    def _uri(self, name):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not name:
            raise ValueError("Object path outside run root")
        return self.root + "/" + name

    def _run(self, *args, missing_ok=False):
        self.check_deadline()
        import time
        limit = 900 if args and args[0] in ("-get", "-put") else 300
        timeout = min(limit, max(.1, self.deadline - time.time())) if self.deadline else limit
        started = time.monotonic()
        try:
            result = subprocess.run([*self.command, *args], capture_output=True, timeout=timeout)
        finally:
            self.command_count += 1
            self.command_seconds += time.monotonic() - started
        if result.returncode:
            error = result.stderr.decode(errors="replace")
            # A permission/network error must never be treated as an absent commit.
            if missing_ok and ("No such file or directory" in error or "File does not exist" in error):
                return None
            raise OSError(f"HDFS {args[0]} failed ({result.returncode}): {error[-2000:]}")
        return result.stdout

    def path(self, name):
        raise TypeError("HDFS has no local path; use exists/read_json/export_file")

    def exists(self, name):
        self.check_deadline()
        if name not in self._present:
            if self._run("-stat", "%b", self._uri(name), missing_ok=True) is None:
                return False
            self._present.add(name)
        return True

    def read_json(self, name):
        self.check_deadline()
        if name not in self._manifests:
            data = self._run("-cat", self._uri(name))
            if len(data) > 1 << 20:
                raise IntegrityError("Oversized manifest")
            json.loads(data)
            self._manifests[name] = data
            self._present.add(name)
        return json.loads(self._manifests[name])

    def _publish_file(self, name, local):
        destination = self._uri(name)
        parent = destination.rsplit("/", 1)[0]
        self._run("-mkdir", "-p", parent)
        temp = destination + "." + uuid.uuid4().hex + ".partial"
        self._run("-put", str(local), temp)
        # Never delete/overwrite a published object. Orphan temporary objects are
        # harmless and deliberately retained if publication fails.
        self._run("-mv", temp, destination)
        self._present.add(name)
        self.write_bytes += Path(local).stat().st_size

    def publish_json(self, name, value):
        data = canonical(value)
        if self.exists(name):
            if canonical(self.read_json(name)) != data:
                raise IntegrityError("Conflicting immutable HDFS manifest")
            return
        with tempfile.NamedTemporaryFile(dir=self.cache) as temp:
            temp.write(data)
            temp.flush()
            self._publish_file(name, temp.name)
        if canonical(self.read_json(name)) != data:
            raise IntegrityError("HDFS manifest readback mismatch")

    def import_file(self, local, ref):
        check_ref(ref)
        if Path(local).stat().st_size != ref["bytes"] or digest_file(local) != ref["sha256"]:
            raise IntegrityError("Imported blob SHA256/size mismatch")
        name = f"blobs/{ref['sha256']}.pt"
        if not self.exists(name):
            self._publish_file(name, local)
        size = int(self._run("-stat", "%b", self._uri(name)))
        if size != ref["bytes"]:
            raise IntegrityError("HDFS blob size mismatch")
        # The imported source was already hash-verified and is durable now.
        # Keep it across tempfile cleanup instead of downloading it again.
        cached = self.cache / (ref["sha256"] + ".pt")
        if Path(local).resolve() != cached:
            temp = cached.with_name(cached.name + "." + uuid.uuid4().hex + ".partial")
            try:
                try:
                    os.link(local, temp)
                except OSError as exc:
                    import errno
                    if exc.errno != errno.EXDEV:
                        raise
                    import shutil
                    shutil.copyfile(local, temp)
                os.replace(temp, cached)
            finally:
                temp.unlink(missing_ok=True)
        return ref

    def put(self, value):
        with tempfile.NamedTemporaryFile(dir=self.cache, suffix=".pt") as temp:
            torch.save(value, temp)
            temp.flush()
            ref = {"sha256": digest_file(temp.name), "bytes": Path(temp.name).stat().st_size}
            self.import_file(temp.name, ref)
        return ref

    def export_file(self, ref):
        check_ref(ref)
        cached = self.cache / (ref["sha256"] + ".pt")
        if cached.exists() and cached.stat().st_size == ref["bytes"] and digest_file(cached) == ref["sha256"]:
            return cached
        temp = cached.with_name(cached.name + "." + uuid.uuid4().hex + ".partial")
        try:
            self._run("-get", self._uri(f"blobs/{ref['sha256']}.pt"), str(temp))
            self.read_bytes += temp.stat().st_size
            if temp.stat().st_size != ref["bytes"] or digest_file(temp) != ref["sha256"]:
                raise IntegrityError("HDFS downloaded blob checksum mismatch")
            os.replace(temp, cached)
        finally:
            temp.unlink(missing_ok=True)
        return cached

    def get(self, ref):
        return torch.load(self.export_file(ref), map_location="cpu", weights_only=True)
