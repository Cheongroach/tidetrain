"""Discover explicit HTTP workers through official Bernard deployment reads."""
import ipaddress
import json
import subprocess
import time
from urllib.error import URLError

from .http_transport import Client, Conflict, RemoteError


class SpotTaskEndpoints:
    def __init__(self, cli, psm, deployments, image_url, token, config_hash,
                 deadline, refresh_seconds=30, min_remaining_seconds=600):
        if deadline <= time.time() or not deployments:
            raise ValueError("Require explicit deployments and a future deadline")
        if not image_url.startswith("hub.byted.org/"):
            raise ValueError("Require the verified China image URL")
        self.cli, self.psm, self.deployments = cli, psm, dict(deployments)
        self.image_url, self.token, self.config_hash = image_url, token, config_hash
        self.deadline, self.refresh_seconds = deadline, refresh_seconds
        self.min_remaining_seconds = min_remaining_seconds
        self.worker_deadlines = {}
        self.selected, self.counts, self.errors = {}, {}, {}
        self.refreshed_at = float("-inf")

    def __call__(self):
        if time.time() >= self.deadline:
            self.selected = {}
            self.counts = dict.fromkeys(self.deployments, 0)
            return {}
        if time.monotonic() - self.refreshed_at < self.refresh_seconds:
            return {worker: endpoint for worker, endpoint in self.selected.items()
                    if self.worker_deadlines.get(worker, 0) > time.time() + self.min_remaining_seconds}
        selected, counts, errors = {}, {}, {}
        worker_deadlines = {}
        for worker, identity in self.deployments.items():
            timeout = min(20, max(.1, self.deadline - time.time()))
            command = [self.cli, "--control-plane", "cn", "service",
                       "deployments", "get", "--json",
                       json.dumps({"deployment_id": str(identity["deployment_id"])})]
            try:
                process = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
                reply = json.loads(process.stdout)
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                counts[worker], errors[worker] = 0, "deployment_query_failed"
                continue
            if process.returncode or "error" in reply:
                counts[worker], errors[worker] = 0, "deployment_query_failed"
                continue
            data = reply["data"]
            config, detail = data["config"], data["detail"]
            if (config["service_id"] != identity["service_id"]
                    or config["service_psm"] != self.psm):
                raise ValueError("Deployment does not match the authorized service")
            if detail.get("infoSpec", {}).get("image") != self.image_url:
                counts[worker], errors[worker] = 0, "image_not_deployed"
                continue
            candidates = []
            candidate_deadlines = {}
            for task in detail.get("tasks", []):
                if (task.get("replicaset") != config.get("latest_replicaset_name")
                        or task.get("version") != "new"
                        or task.get("state") not in ("NotReady", "TASK_RUNNING", "Running")):
                    continue
                if not task.get("containerID") or not task.get("ports"):
                    continue
                address = ipaddress.ip_address(task.get("hostv6") or task["host"])
                port = task["ports"][0]
                if type(port) is not int or not 0 < port < 65536:
                    raise ValueError("Invalid task HTTP port")
                host = f"[{address}]" if address.version == 6 else str(address)
                endpoint = f"http://{host}:{port}"
                try:
                    health = Client(endpoint, self.token, timeout=5, deadline=self.deadline).health()
                    if (health.get("worker") != worker or health.get("device") != "cuda"
                            or health.get("config_hash") != self.config_hash
                            or health.get("api") != 1):
                        errors[worker] = "worker_identity_mismatch"
                        continue
                    if health.get("runtime_ready") is False:
                        errors[worker] = "runtime_files_missing"
                        continue
                    if health.get("cuda_ready") is False:
                        errors[worker] = "cuda_probe_failed"
                        continue
                    if health.get("deadline", 0) <= time.time() + self.min_remaining_seconds:
                        errors[worker] = "worker_lifetime_too_short"
                        continue
                    # Validate the incarnation format before exposing this endpoint.
                    Client(endpoint, self.token, instance=health["instance"])
                    candidates.append(endpoint)
                    candidate_deadlines[endpoint] = health["deadline"]
                except (RemoteError, Conflict, URLError, OSError, TimeoutError, ValueError, KeyError):
                    errors[worker] = "http_worker_unavailable"
            counts[worker] = len(candidates)
            if candidates:
                previous = self.selected.get(worker)
                selected[worker] = previous if previous in candidates else sorted(candidates)[0]
                worker_deadlines[worker] = candidate_deadlines[selected[worker]]
                errors.pop(worker, None)
        self.selected, self.counts, self.errors = selected, counts, errors
        self.worker_deadlines = worker_deadlines
        self.refreshed_at = time.monotonic()
        return dict(selected)
