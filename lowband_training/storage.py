"""Hash-verified blobs and commit-last manifests on a mounted shared filesystem.

Requires atomic same-directory rename and one active publisher per manifest.
This is NOT a distributed lock / consensus implementation. Validate mounts and
cross-host visibility before deployment. Never run two coordinators for a run.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

import torch


class IntegrityError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(4 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class Store:
    def __init__(self, root, cache, bandwidth_mib=0, deadline=None):
        self.root, self.cache = Path(root).resolve(), Path(cache).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.bandwidth = bandwidth_mib * (1 << 20)
        self.deadline = deadline
        self.read_bytes = self.write_bytes = 0
        self.transfer_seconds = 0.0

    def check_deadline(self):
        if self.deadline and time.time() >= self.deadline:
            raise TimeoutError("Persisted run deadline reached")

    def _pace(self, size, start):
        # Logical per-process blob-transfer cap, NOT a physical link shaper.
        target = start + size / self.bandwidth if self.bandwidth else start
        while time.monotonic() < target:
            self.check_deadline()
            time.sleep(min(1, target - time.monotonic()))
        self.transfer_seconds += time.monotonic() - start

    def path(self, name):
        path = self.root / name
        if path.is_absolute() and not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Object path outside run root")
        return path

    def exists(self, name):
        return self.path(name).exists()

    def import_file(self, local, reference):
        """Import a serialized HTTP result without materializing its tensors."""
        import shutil
        self.check_deadline()
        digest, size = reference["sha256"], reference["bytes"]
        if not re.fullmatch("[0-9a-f]{64}", digest) or not isinstance(size, int) or size <= 0:
            raise IntegrityError("Malformed blob reference")
        if Path(local).stat().st_size != size or digest_file(local) != digest:
            raise IntegrityError("Imported blob SHA256/size mismatch")
        destination = self.path(f"blobs/{digest}.pt")
        if destination.exists():
            if destination.stat().st_size != size or digest_file(destination) != digest:
                raise IntegrityError("Existing shared blob is corrupt")
            return reference
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
        try:
            with Path(local).open("rb") as src, temporary.open("xb") as dst:
                shutil.copyfileobj(src, dst, 4 << 20)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temporary, destination)
            self.write_bytes += size
        finally:
            temporary.unlink(missing_ok=True)
        return reference

    def export_file(self, reference):
        path = self.path(f"blobs/{reference['sha256']}.pt")
        if path.stat().st_size != reference["bytes"] or digest_file(path) != reference["sha256"]:
            raise IntegrityError("Exported blob SHA256/size mismatch")
        return path

    def publish_json(self, name, value):
        self.check_deadline()
        path = self.path(name)
        data = canonical(value)
        if path.exists():
            if path.read_bytes() != data:
                raise IntegrityError(f"Conflicting immutable manifest: {name}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
        try:
            with temp.open("xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def read_json(self, name):
        return json.loads(self.path(name).read_bytes())

    def put(self, value):
        self.check_deadline()
        with tempfile.NamedTemporaryFile(dir=self.cache, suffix=".pt", delete=False) as out:
            local = Path(out.name)
        try:
            with local.open("wb") as stream:
                torch.save(value, stream)
            digest, size = digest_file(local), local.stat().st_size
            destination = self.path(f"blobs/{digest}.pt")
            if not destination.exists():
                destination.parent.mkdir(exist_ok=True)
                temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
                started = time.monotonic()
                try:
                    with local.open("rb") as src, temporary.open("xb") as dst:
                        while chunk := src.read(4 << 20):
                            self.check_deadline()
                            dst.write(chunk)
                        dst.flush()
                        os.fsync(dst.fileno())
                    self.write_bytes += size
                    self._pace(size, started)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                if digest_file(destination) != digest:
                    raise IntegrityError("Existing shared blob is corrupt")
            cached = self.cache / (digest + ".pt")
            os.replace(local, cached)
            return {"sha256": digest, "bytes": size}
        finally:
            local.unlink(missing_ok=True)

    def get(self, reference):
        self.check_deadline()
        digest, size = reference["sha256"], reference["bytes"]
        if not re.fullmatch("[0-9a-f]{64}", digest) or not isinstance(size, int) or size <= 0:
            raise IntegrityError("Malformed blob reference")
        cached = self.cache / (digest + ".pt")
        if cached.exists() and (cached.stat().st_size != size or digest_file(cached) != digest):
            cached.unlink()
        if not cached.exists():
            source = self.path(f"blobs/{digest}.pt")
            temporary = cached.with_name(cached.name + "." + uuid.uuid4().hex + ".partial")
            started = time.monotonic()
            try:
                with source.open("rb") as src, temporary.open("xb") as dst:
                    while chunk := src.read(4 << 20):
                        self.check_deadline()
                        dst.write(chunk)
                self.read_bytes += temporary.stat().st_size
                self._pace(temporary.stat().st_size, started)
                if temporary.stat().st_size != size or digest_file(temporary) != digest:
                    raise IntegrityError("Downloaded blob SHA256/size mismatch")
                os.replace(temporary, cached)
            finally:
                temporary.unlink(missing_ok=True)
        return torch.load(cached, map_location="cpu", weights_only=True)
