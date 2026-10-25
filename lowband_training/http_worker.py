"""HTTP training worker. Only local files and inbound HTTP are used.

The supervisor does not import torch; training runs in one child process so
health/status remain responsive and expired leases can terminate CUDA work.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .http_transport import CHUNK, Conflict, Depot, canonical, token_matches


def local_config(config, assets):
    localized = dict(config)
    if config["model"] != "tiny":
        localized["model_path"] = str(Path(assets) / "model")
        localized["data_manifest"] = str(Path(assets) / "data/manifest.json")
    return localized


class Jobs:
    def __init__(self, root, config, worker, assets, device, max_seconds=10800, lease_seconds=120):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config, self.worker, self.assets, self.device = config, worker, assets, device
        self.fingerprint = hashlib.sha256(canonical(config)).hexdigest()
        self.instance = uuid.uuid4().hex
        self.depot = Depot(self.root / self.instance / "blobs")
        self.jobs = {}
        self.active = None
        self.lock = threading.RLock()
        self.deadline = time.time() + max_seconds
        self.lease_seconds = lease_seconds
        report = os.environ.get("LOWBAND_CUDA_REPORT")
        self.cuda_report = json.loads(Path(report).read_text()) if report else None
        self.stop = threading.Event()
        threading.Thread(target=self.watch, daemon=True).start()

    def runtime_integrity(self):
        import importlib.util
        checks = {"python": Path(sys.executable).is_file()}
        if self.config["model"] != "tiny":
            spec = importlib.util.find_spec("transformers")
            checks["transformers"] = bool(spec and spec.origin and Path(spec.origin).is_file())
            checks["model"] = (Path(self.assets) / "model/config.json").is_file()
            checks["data"] = (Path(self.assets) / "data/manifest.json").is_file()
        return checks

    def watch(self):
        while not self.stop.wait(.2):
            with self.lock:
                self.reap()

    def reap(self):
        if self.active is None:
            return
        entry = self.jobs[self.active]
        proc = entry["process"]
        if proc.poll() is None and (time.monotonic() > entry["lease_until"] or
                                    time.time() >= min(self.deadline, entry["request"]["deadline"])):
            self.kill(proc)
            entry["error"] = "Lease expired or run deadline reached"
        if proc.poll() is not None:
            result = entry["directory"] / "result.json"
            entry["status"] = "succeeded" if proc.returncode == 0 and result.exists() else "failed"
            if entry["status"] == "succeeded":
                entry["result"] = json.loads(result.read_text())
            else:
                entry.setdefault("error", f"Training process exited {proc.returncode}; see local job log")
                with (entry["directory"] / "worker.log").open("rb") as source:
                    source.seek(0, os.SEEK_END)
                    source.seek(max(0, source.tell() - 16384))
                    entry["log_tail"] = source.read().decode(errors="replace")
                print(canonical({"event": "training_failed", "job_id": entry["request"]["job_id"],
                                 "error": entry["error"], "log_tail": entry["log_tail"]}).decode(), flush=True)
            entry["log"].close()
            self.active = None

    @staticmethod
    def kill(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)

    def close(self):
        self.stop.set()
        with self.lock:
            if self.active:
                self.kill(self.jobs[self.active]["process"])
                self.reap()

    def start(self, request):
        job_id = request.get("job_id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", job_id):
            raise ValueError("Invalid job ID")
        if request.get("worker") != self.worker or request.get("config_hash") != self.fingerprint:
            raise Conflict("Worker or config mismatch")
        if request.get("instance") != self.instance:
            raise Conflict("Worker incarnation changed")
        if not time.time() < request["deadline"] <= self.deadline + 1:
            raise ValueError("Job deadline outside service lifetime")
        with self.lock:
            self.reap()
            if job_id in self.jobs:
                if canonical(self.jobs[job_id]["request"]) != canonical(request):
                    raise Conflict("Job ID reused with different inputs")
                return self.status(job_id)
            if self.active:
                raise Conflict("One training task per GPU; worker is busy")
            self.depot.verified(request["input_state"])
            if request.get("previous"):
                self.depot.verified(request["previous"])
            directory = self.root / self.instance / job_id
            directory.mkdir()
            (directory / "job.json").write_bytes(canonical({"request": request, "config": self.config,
                    "assets": str(self.assets), "device": self.device, "blobs": str(self.depot.root)}))
            log = (directory / "worker.log").open("wb")
            proc = subprocess.Popen([sys.executable, "-m", "lowband_training.http_worker", "execute",
                                     str(directory / "job.json")], stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            self.jobs[job_id] = {"request": request, "process": proc, "log": log, "directory": directory,
                                 "status": "running", "lease_until": time.monotonic() + self.lease_seconds}
            self.active = job_id
            return self.status(job_id)

    def status(self, job_id, renew=False):
        with self.lock:
            self.reap()
            entry = self.jobs[job_id]
            if renew and entry["status"] == "running":
                entry["lease_until"] = time.monotonic() + self.lease_seconds
            result = entry.get("result")
            artifacts = ({key: self.depot.status(result[key]) for key in ("delta", "recovery")
                          if key in result} if result else {})
            return {"job_id": job_id, "instance": self.instance, "status": entry["status"],
                    "result": result, "artifacts": artifacts, "error": entry.get("error"),
                    "log_tail": entry.get("log_tail")}

    def release(self, job_id):
        with self.lock:
            self.reap()
            if self.active:
                raise Conflict("Cannot release local blobs during training")
            entry = self.jobs[job_id]
            if entry["status"] == "released":
                return {"released": job_id}
            # Keep the idempotency tombstone, but reclaim large ephemeral files.
            references = [entry["request"]["input_state"], entry["request"].get("previous")]
            result = entry.get("result") or {}
            references += [result.get("delta"), result.get("recovery")]
            protected = set()
            for other_id, other in self.jobs.items():
                if other_id == job_id or other["status"] == "released":
                    continue
                refs = [other["request"]["input_state"], other["request"].get("previous")]
                value = other.get("result") or {}
                refs += [value.get("delta"), value.get("recovery")]
                protected.update(ref["sha256"] for ref in refs if ref)
            for ref in references:
                if ref and ref["sha256"] not in protected:
                    self.depot.path(ref["sha256"]).unlink(missing_ok=True)
            entry["status"] = "released"
            entry.pop("result", None)
            return {"released": job_id}


def handler_for(jobs, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # do not log bearer tokens or request payloads

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def reply(self, status, payload, binary=False):
            data = payload if binary else canonical(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream" if binary else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Lowband-Instance", jobs.instance)
            if binary:
                self.send_header("X-Chunk-SHA256", hashlib.sha256(data).hexdigest())
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self):
            path = urlsplit(self.path)
            pieces = path.path.strip("/").split("/")
            query = parse_qs(path.query)
            if self.command == "GET" and path.path == "/health":
                self.reply(200, {"api": 1, "instance": jobs.instance, "worker": jobs.worker,
                                 "config_hash": jobs.fingerprint, "device": jobs.device,
                                 "deadline": jobs.deadline, "lease_seconds": jobs.lease_seconds,
                                 "cuda_ready": jobs.cuda_report.get("ready") if jobs.cuda_report else None,
                                 "runtime_ready": all(jobs.runtime_integrity().values()),
                                 "training_verified": False})
                return
            if not token_matches(self.headers.get("Authorization"), token):
                self.reply(401, {"error": "Unauthorized"})
                return
            if self.headers.get("X-Lowband-Instance") != jobs.instance:
                self.reply(409, {"error": "Worker incarnation changed"})
                return
            if time.time() >= jobs.deadline:
                self.reply(410, {"error": "Service lifetime expired"})
                return
            if pieces == ["v1", "runtime"] and self.command == "GET":
                files = sorted(jobs.depot.root.iterdir())
                self.reply(200, {"cuda": jobs.cuda_report, "runtime_integrity": jobs.runtime_integrity(),
                                "storage": {"root": str(jobs.depot.root),
                                            "files": [{"name": p.name, "bytes": p.stat().st_size}
                                                      for p in files[:32] if p.is_file()]}})
            elif pieces[:2] == ["v1", "blobs"] and len(pieces) in (3, 4):
                digest = pieces[2]
                if self.command == "GET" and len(pieces) == 4 and pieces[3] == "status":
                    self.reply(200, jobs.depot.status({"sha256": digest, "bytes": int(query["size"][0])}))
                elif self.command == "PUT" and len(pieces) == 3:
                    size = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get("Transfer-Encoding") or not 0 < size <= CHUNK:
                        raise ValueError("Bounded Content-Length required")
                    block = self.rfile.read(size)
                    if len(block) != size:
                        raise ValueError("Truncated request")
                    self.reply(200, jobs.depot.put_chunk({"sha256": digest, "bytes": int(query["size"][0])},
                               int(query["offset"][0]), block, self.headers.get("X-Chunk-SHA256")))
                elif self.command == "GET" and len(pieces) == 3:
                    offset, count = int(query["offset"][0]), int(query["count"][0])
                    blob = jobs.depot.path(digest)
                    if not 0 <= offset < blob.stat().st_size or not 0 < count <= CHUNK:
                        raise ValueError("Invalid range")
                    with blob.open("rb") as src:
                        src.seek(offset)
                        self.reply(200, src.read(count), binary=True)
                else:
                    raise KeyError("Unknown route")
            elif pieces == ["v1", "jobs"] and self.command == "POST":
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid JSON body length")
                self.reply(202, jobs.start(json.loads(self.rfile.read(size))))
            elif pieces[:2] == ["v1", "jobs"] and len(pieces) in (3, 4):
                if self.command == "GET" and len(pieces) == 3:
                    self.reply(200, jobs.status(pieces[2]))
                elif self.command == "POST" and len(pieces) == 4 and pieces[3] == "renew":
                    self.reply(200, jobs.status(pieces[2], renew=True))
                elif self.command == "DELETE" and len(pieces) == 3:
                    self.reply(200, jobs.release(pieces[2]))
                else:
                    raise KeyError("Unknown route")
            else:
                raise KeyError("Unknown route")

        def handle_request(self):
            try:
                self.dispatch()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
            except Conflict as exc:
                self.reply(409, {"error": str(exc)})
            except (KeyError, FileNotFoundError):
                self.reply(404, {"error": "Not found"})
            except (ValueError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except OSError:
                self.reply(507, {"error": "Local storage/process failure"})

        do_GET = do_POST = do_PUT = do_DELETE = handle_request
    return Handler


def execute(path):
    import torch
    from .train import create_model, load_data, train_round
    torch.set_num_threads(int(os.environ.get("LOWBAND_CPU_THREADS", "2")))
    job = json.loads(Path(path).read_text())
    req = job["request"]
    config = local_config(job["config"], job["assets"])
    depot = Depot(job["blobs"])
    print(json.dumps({"event": "training_runtime", "torch": torch.__version__,
                      "cuda_runtime": torch.version.cuda, "device": job["device"],
                      "cuda_available": torch.cuda.is_available(),
                      "device_count": torch.cuda.device_count(),
                      "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}),
          flush=True)
    if req.get("operation", "train") not in ("train", "evaluate"):
        raise ValueError("Unknown job operation")
    if job["device"] == "cuda" and not torch.cuda.is_bf16_supported():
        raise RuntimeError("CUDA BF16 support required")
    state = torch.load(depot.verified(req["input_state"]), map_location="cpu", weights_only=True)
    previous = (torch.load(depot.verified(req["previous"]), map_location="cpu", weights_only=True)
                if req.get("previous") else None)
    model = create_model(config, job["device"])
    data = load_data(config)
    if req.get("operation") == "evaluate":
        from .evaluate import evaluate_model
        from .train import load_parameters
        load_parameters(model, state["parameters"])
        metrics = evaluate_model(model, data["validation"], config, job["device"],
                                 req.get("max_batches", 0), req.get("batch_size", 2),
                                 req.get("bf16", False))
        result = {"job_id": req["job_id"], "instance": req["instance"], "worker": req["worker"],
                  "operation": "evaluate", "base_round": req["base_round"],
                  "base_sha256": req["base_sha256"], "config_hash": req["config_hash"],
                  "input_state": req["input_state"], "metrics": metrics}
        temp = Path(path).parent / "result.partial"
        temp.write_bytes(canonical(result))
        os.replace(temp, temp.with_name("result.json"))
        return
    delta, recovery, count, metrics = train_round(model, data, config, req["worker"], state, previous,
                                                  job["device"], req["deadline"])
    refs = {}
    for name, value in (("delta", {"delta": delta}), ("recovery", recovery)):
        temp = Path(path).parent / (name + ".partial")
        with temp.open("wb") as dst:
            torch.save(value, dst)
            dst.flush()
            os.fsync(dst.fileno())
        refs[name] = depot.add(temp)
    result = {"job_id": req["job_id"], "instance": req["instance"], "worker": req["worker"],
              "base_round": req["base_round"], "base_sha256": req["base_sha256"],
              "config_hash": req["config_hash"], "tokens": count, "metrics": metrics, **refs}
    temp = Path(path).parent / "result.partial"
    temp.write_bytes(canonical(result))
    os.replace(temp, temp.with_name("result.json"))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    child = sub.add_parser("execute")
    child.add_argument("job")
    serve = sub.add_parser("serve")
    serve.add_argument("--config", required=True)
    serve.add_argument("--worker", required=True)
    serve.add_argument("--assets", required=True)
    serve.add_argument("--root", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    serve.add_argument("--max-seconds", type=int, default=10800)
    serve.add_argument("--lease-seconds", type=float, default=120)
    args = parser.parse_args()
    if args.action == "execute":
        execute(args.job)
        return
    token = os.environ.get("LOWBAND_HTTP_TOKEN", "")
    if len(token) < 24:
        parser.error("Set LOWBAND_HTTP_TOKEN to a per-run secret of at least 24 characters")
    if not 0 < args.max_seconds <= 10800 or args.lease_seconds <= 0:
        parser.error("Positive lifetime <=10800 seconds and positive lease required")
    jobs = Jobs(args.root, json.loads(Path(args.config).read_text()), args.worker, args.assets, args.device,
                args.max_seconds, args.lease_seconds)
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 16
    if ":" in args.host:
        Server.address_family = socket.AF_INET6
    server = Server((args.host, args.port), handler_for(jobs, token))
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    timer = threading.Timer(args.max_seconds, shutdown)
    timer.daemon = True
    timer.start()
    print(json.dumps({"event": "http_worker_ready", "instance": jobs.instance, "worker": args.worker,
                      "port": server.server_port, "device": args.device}), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        timer.cancel()
        jobs.close()
        server.server_close()


if __name__ == "__main__":
    main()
