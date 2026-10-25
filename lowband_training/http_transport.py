"""Bounded HTTP blob transport; all connections are initiated by the controller.

No torch, HDFS, ByteNAS, service-discovery SDK or external packages on this path.
An instance UUID pins every stateful request to one server incarnation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

CHUNK = 4 << 20
MAX_BLOB = 16 << 30


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def reference(path):
    return {"sha256": digest_file(path), "bytes": Path(path).stat().st_size}


def check_ref(ref):
    if (not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", "")))
            or type(ref.get("bytes")) is not int or not 0 < ref["bytes"] <= MAX_BLOB):
        raise ValueError("Invalid blob reference")
    return ref


class Conflict(RuntimeError):
    pass


class RemoteError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class Depot:
    """Ephemeral, content-addressed local files with resumable uploads."""
    def __init__(self, root, quota=24 << 30):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = quota
        self.lock = threading.RLock()

    def path(self, digest, suffix=".pt"):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid SHA256")
        return self.root / (digest + suffix)

    def status(self, ref):
        check_ref(ref)
        with self.lock:
            path = self.path(ref["sha256"])
            if path.exists():
                if path.stat().st_size != ref["bytes"]:
                    raise Conflict("Blob size conflict")
                return {"offset": ref["bytes"], "complete": True}
            partial = self.path(ref["sha256"], ".partial")
            return {"offset": partial.stat().st_size if partial.exists() else 0, "complete": False}

    def put_chunk(self, ref, offset, block, digest):
        check_ref(ref)
        if not block or len(block) > CHUNK or hashlib.sha256(block).hexdigest() != digest:
            raise ValueError("Invalid chunk or checksum")
        if offset < 0 or offset + len(block) > ref["bytes"]:
            raise ValueError("Chunk outside blob")
        with self.lock:
            status = self.status(ref)
            if status["complete"]:
                return status
            path = self.path(ref["sha256"], ".partial")
            if offset < status["offset"]:  # a response was lost after the server wrote the chunk
                with path.open("rb") as src:
                    src.seek(offset)
                    if src.read(len(block)) == block:
                        return status
                raise Conflict("Conflicting upload retry")
            if offset != status["offset"]:
                raise Conflict("Upload offset changed; query status")
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
            if used + len(block) > self.quota or shutil.disk_usage(self.root).free < len(block) + (64 << 20):
                raise OSError("Local blob quota/free space exhausted")
            with path.open("ab") as dst:
                dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            if path.stat().st_size == ref["bytes"]:
                if digest_file(path) != ref["sha256"]:
                    path.unlink()
                    raise ValueError("Full blob checksum mismatch")
                os.replace(path, self.path(ref["sha256"]))
            return self.status(ref)

    def verified(self, ref):
        check_ref(ref)
        path = self.path(ref["sha256"])
        if not path.exists() or path.stat().st_size != ref["bytes"] or digest_file(path) != ref["sha256"]:
            raise ValueError("Blob missing or checksum mismatch")
        return path

    def add(self, path):
        ref = reference(path)
        check_ref(ref)
        os.replace(path, self.path(ref["sha256"]))
        return ref

    def clear(self):
        with self.lock:
            for path in self.root.iterdir():
                if path.is_file():
                    path.unlink()


class Client:
    def __init__(self, endpoint, token, instance=None, timeout=30, deadline=None, chunk_size=CHUNK):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Expected an explicit HTTP(S) endpoint")
        if not 0 < chunk_size <= CHUNK:
            raise ValueError("Invalid chunk size")
        if instance is not None and not re.fullmatch(r"[0-9a-f]{32}", instance):
            raise ValueError("Invalid instance UUID")
        self.endpoint = endpoint.rstrip("/")
        self.token, self.instance = token, instance
        self.timeout, self.deadline, self.chunk_size = timeout, deadline, chunk_size
        # Internal endpoint traffic must not inherit an unrelated internet proxy.
        self.opener = build_opener(ProxyHandler({}))
        self.sent_bytes = self.received_bytes = 0

    def request(self, method, path, body=None, headers=None, binary=False):
        if self.deadline and time.time() >= self.deadline:
            raise TimeoutError("Run deadline reached")
        head = {"Authorization": "Bearer " + self.token}
        if self.instance:
            head["X-Lowband-Instance"] = self.instance
        head.update(headers or {})
        if isinstance(body, dict):
            body = canonical(body)
            head["Content-Type"] = "application/json"
        timeout = min(self.timeout, max(.1, self.deadline - time.time())) if self.deadline else self.timeout
        try:
            with self.opener.open(Request(self.endpoint + path, data=body, headers=head, method=method), timeout=timeout) as response:
                if self.instance and response.headers.get("X-Lowband-Instance") != self.instance:
                    raise Conflict("Endpoint incarnation changed")
                limit = CHUNK if binary else 1 << 20
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Oversized response")
                self.sent_bytes += len(body or b"")
                self.received_bytes += len(data)
                if binary:
                    if hashlib.sha256(data).hexdigest() != response.headers.get("X-Chunk-SHA256"):
                        raise ValueError("Response chunk checksum mismatch")
                    return data
                return json.loads(data)
        except HTTPError as exc:
            raise RemoteError(exc.code, exc.read(4096).decode(errors="replace")) from exc

    def health(self):
        return self.request("GET", "/health")

    def upload(self, path, ref=None):
        ref = check_ref(ref or reference(path))
        if reference(path) != ref:
            raise ValueError("Upload source checksum mismatch")
        route = "/v1/blobs/" + ref["sha256"]
        status = self.request("GET", route + "/status?" + urlencode({"size": ref["bytes"]}))
        offset = status["offset"]
        if not 0 <= offset <= ref["bytes"]:
            raise ValueError("Invalid server offset")
        with Path(path).open("rb") as src:
            while offset < ref["bytes"]:
                src.seek(offset)
                data = src.read(min(self.chunk_size, ref["bytes"] - offset))
                result = self.request("PUT", route + "?" + urlencode({"offset": offset, "size": ref["bytes"]}),
                                      data, {"X-Chunk-SHA256": hashlib.sha256(data).hexdigest()})
                if not offset < result["offset"] <= ref["bytes"]:
                    raise ValueError("Upload made no progress")
                offset = result["offset"]
        if not self.request("GET", route + "/status?" + urlencode({"size": ref["bytes"]}))["complete"]:
            raise ValueError("Upload not committed")
        return ref

    def download(self, ref, directory):
        check_ref(ref)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / (ref["sha256"] + ".pt")
        if final.exists() and reference(final) == ref:
            return final
        # Partial downloads are tied to an incarnation, so IP reuse cannot mix files.
        part = directory / (ref["sha256"] + "." + (self.instance or "unknown") + ".partial")
        offset = part.stat().st_size if part.exists() else 0
        if offset > ref["bytes"]:
            part.unlink()
            offset = 0
        with part.open("ab") as dst:
            while offset < ref["bytes"]:
                count = min(self.chunk_size, ref["bytes"] - offset)
                data = self.request("GET", "/v1/blobs/" + ref["sha256"] + "?" +
                                    urlencode({"offset": offset, "count": count}), binary=True)
                if len(data) != count:
                    raise ValueError("Truncated response")
                dst.write(data)
                dst.flush()
                os.fsync(dst.fileno())
                offset += len(data)
        if reference(part) != ref:
            part.unlink()
            raise ValueError("Full download checksum mismatch")
        os.replace(part, final)
        return final


def token_matches(header, token):
    return secrets.compare_digest(header or "", "Bearer " + token)
