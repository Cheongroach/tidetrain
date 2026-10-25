"""Serve only the non-secret offline package to the internal image builder."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import time
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--port", type=int, default=18327)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    size = package.stat().st_size
    token = args.token_file.read_text().strip()
    if len(token) < 32:
        parser.error("Require a separate build-only token")
    if not package.is_file() or args.deadline <= time.time():
        parser.error("Require a package file and a future deadline")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not secrets.compare_digest(self.path, "/" + token + "/http-worker-v1.tar.gz") or time.time() >= args.deadline:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            try:
                with package.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, 4 << 20)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *values):
            print("artifact_request", self.command, self.client_address[0], flush=True)

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True

    with Server(("::", args.port), Handler) as server:
        server.timeout = 1
        while time.time() < args.deadline:
            server.handle_request()


if __name__ == "__main__":
    main()
