"""Durable HTTP authority for the sharded synchronous capacity experiment."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .ledger import Ledger, Rejected
from .shards import Shards, reduce_nesterov
from .storage import canonical, digest_file


class Authority:
    def __init__(self, root, config, state):
        self.root = Path(root)
        self.shards = Shards(self.root / "shards", config["chunk_bytes"])
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.ledger.initialize(config, state)
        self.config = config
        self.owner = uuid.uuid4().hex
        self.lease = self.ledger.acquire("coordinator", self.owner, 600)
        self.stop = threading.Event()
        self.io_slots = threading.BoundedSemaphore(config.get("io_concurrency", 16))
        self.metrics = {"uploaded_bytes": 0, "downloaded_bytes": 0, "rounds": []}
        self.metrics_lock = threading.Lock()
        self.thread = threading.Thread(target=self.advance, daemon=True)
        self.thread.start()

    def advance(self):
        while not self.stop.wait(.25):
            try:
                self.lease = self.ledger.renew(self.lease, 600)
                base = self.ledger.snapshot()
                if base["round"] >= self.config["rounds"]:
                    continue
                records = self.ledger.pending(base)
                if len(records) < len(self.config["workers"]) and (
                        len(records) < self.config["quorum"] or time.time() <
                        base["created_at"] + self.config["round_timeout"]):
                    continue
                started = time.monotonic()
                state = reduce_nesterov(self.shards, base["state"], records,
                                        self.config["outer_lr"], self.config["outer_momentum"])
                commit = self.ledger.commit(self.lease, base, records, state)
                result = {"round": commit["round"], "accepted": len(records),
                          "effective_tokens": commit["effective_tokens"],
                          "reduce_seconds": time.monotonic() - started,
                          "peak_chunk_bytes": self.shards.peak_chunk_bytes}
                self.metrics["rounds"].append(result)
                print(canonical({"event": "scale_round_committed", **result}).decode(), flush=True)
                self.save_metrics()
            except Rejected as exc:
                print(canonical({"event": "coordinator_rejected", "error": str(exc)}).decode(), flush=True)
                self.stop.set()
            except Exception as exc:
                # Never commit after a reduction/verification failure.
                print(canonical({"event": "coordinator_failed", "error": str(exc)}).decode(), flush=True)
                self.stop.set()

    def save_metrics(self):
        with self.metrics_lock:
            value = {**self.metrics, "events": self.ledger.events(),
                     "state": self.ledger.snapshot()}
            (self.root / "metrics.json").write_bytes(canonical(value))


class HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    request_queue_size = 128

    def process_request(self, request, client_address):
        # Bound active threads as well as aggregate request bodies.
        if not self.authority.io_slots.acquire(timeout=5):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.authority.io_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.authority.io_slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, value, binary=False):
        body = value if binary else canonical(value)
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream" if binary else "application/json")
        self.send_header("Connection", "close")
        if binary:
            import hashlib
            self.send_header("X-Chunk-SHA256", hashlib.sha256(body).hexdigest())
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def dispatch(self):
        authority = self.server.authority
        if not hmac.compare_digest(self.headers.get("Authorization", ""),
                                   "Bearer " + self.server.token):
            self.reply(401, {"error": "Unauthorized"})
            return
        self.connection.settimeout(30)
        route = urlsplit(self.path).path
        limit = authority.config["chunk_bytes"] + 4096 if route == "/chunk" else 1 << 20
        count = int(self.headers.get("Content-Length", 0))
        if not 0 <= count <= limit:
            raise ValueError("Body exceeds byte admission bound")
        raw = self.rfile.read(count)
        if len(raw) != count:
            raise ValueError("Incomplete body")
        body = json.loads(raw) if raw and route != "/chunk" else None
        if self.command == "GET" and route == "/health":
            self.reply(200, {"ready": not authority.stop.is_set(),
                             "round": authority.ledger.snapshot()["round"]})
        elif self.command == "GET" and route == "/snapshot":
            selected = parse_qs(urlsplit(self.path).query).get("worker", [])
            if selected:
                if len(selected) != 1 or selected[0] not in authority.config["workers"]:
                    raise Rejected("Unknown snapshot worker")
                base = authority.ledger.worker_snapshot(selected[0])
            else:
                base = authority.ledger.snapshot()
                base["workers"] = {}
            self.reply(200, {"base": base, "config": authority.config})
        elif self.command == "POST" and route == "/lease":
            self.reply(200, authority.ledger.acquire(**body))
        elif self.command == "POST" and route == "/renew":
            self.reply(200, authority.ledger.renew(body["lease"], 600))
        elif self.command == "POST" and route == "/ready":
            self.reply(200, authority.ledger.ready(body["lease"], body["hardware"]))
        elif self.command == "POST" and route == "/submit":
            authority.shards.verify_tree(body["record"]["delta"])
            authority.shards.verify_tree(body["record"]["recovery"])
            recovery = body["record"]["recovery"]
            # Cursor is scalar metadata, checked against the embedded recovery.
            cursors = [value["value"] for key, value in recovery["items"]
                       if key == {"kind": "scalar", "value": "cursor"} and value["kind"] == "scalar"]
            if cursors != [body["record"]["cursor"]]:
                raise Rejected("Recovery cursor does not match update")
            self.reply(200, {"inserted": authority.ledger.submit(body["lease"], body["record"])})
        elif self.command == "POST" and route == "/chunk":
            ref = {"sha256": self.headers["X-Blob-SHA256"], "bytes": count}
            final = authority.shards.path(ref)
            fd, temporary = tempfile.mkstemp(dir=authority.shards.root, suffix=".partial")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(raw)
                    out.flush()
                    os.fsync(out.fileno())
                if digest_file(temporary) != ref["sha256"]:
                    raise ValueError("Upload SHA256 mismatch")
                if final.exists():
                    authority.shards.verify(ref)
                else:
                    os.replace(temporary, final)
                    authority.shards.sync_directory()
                with authority.metrics_lock:
                    authority.metrics["uploaded_bytes"] += count
                self.reply(200, ref)
            finally:
                Path(temporary).unlink(missing_ok=True)
        elif self.command == "POST" and route == "/fetch":
            raw = authority.shards.verify(body).read_bytes()
            with authority.metrics_lock:
                authority.metrics["downloaded_bytes"] += len(raw)
            self.reply(200, raw, binary=True)
        elif self.command == "POST" and route == "/worker-report":
            worker = body["worker"]
            if worker not in authority.config["workers"]:
                raise Rejected("Unknown report worker")
            reports = authority.root / "reports"
            reports.mkdir(exist_ok=True)
            # Configured worker names are validated at startup.
            path = reports / (worker + ".json")
            if path.exists() and path.read_bytes() != canonical(body):
                raise Rejected("Conflicting worker report")
            path.write_bytes(canonical(body))
            authority.save_metrics()
            self.reply(200, {"stored": True})
        else:
            self.reply(404, {"error": "Unknown route"})

    def handle_request(self):
        try:
            self.dispatch()
        except (Rejected, ValueError, KeyError, FileNotFoundError) as exc:
            self.reply(409, {"error": str(exc)})
        except Exception as exc:
            self.reply(500, {"error": type(exc).__name__})

    do_GET = do_POST = handle_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="::")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    import re
    import torch
    from .train import create_model
    config = json.loads(Path(args.config).read_text())
    if any(not re.fullmatch("[a-zA-Z0-9_-]+", w) for w in config["workers"]):
        raise ValueError("Invalid worker name")
    root = Path(args.root)
    shards = Shards(root / "shards", config["chunk_bytes"])
    state_path = root / "initial.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        model = create_model(config, torch.device("cpu"))
        state = {"parameters": shards.tree({n: p.detach() for n, p in model.named_parameters()}),
                 "momentum": shards.tree({n: torch.zeros_like(p) for n, p in model.named_parameters()})}
        state_path.write_bytes(canonical(state))
        del model
    authority = Authority(root, config, state)
    server = HTTPServer((args.host, args.port), Handler)
    server.authority, server.token = authority, Path(args.token_file).read_text().strip()
    print(canonical({"event": "scale_gateway_ready", "port": args.port,
                     "members": len(config["workers"])}).decode(), flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        authority.stop.set()
        authority.thread.join(timeout=30)
        authority.save_metrics()
        server.server_close()


if __name__ == "__main__":
    main()
