"""Collect authenticated local CUDA diagnostics from authorized deployments."""
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import time

from .http_transport import Client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads(args.deployments.read_text())
    token = (Path(__file__).parent / ".secrets/http-token").read_text().strip()
    reports = {"observed_at": time.time(), "workers": {}}
    for worker, identity in settings["deployments"].items():
        proc = subprocess.run([settings["cli"], "--control-plane", "cn", "service", "deployments", "get",
                               "--json", json.dumps({"deployment_id": str(identity["deployment_id"])})],
                              capture_output=True, text=True, timeout=25)
        data = json.loads(proc.stdout)["data"]
        config, detail = data["config"], data["detail"]
        if config["service_id"] != identity["service_id"] or config["service_psm"] != settings["psm"]:
            raise ValueError("Unauthorized deployment identity")
        report = {"image": detail.get("infoSpec", {}).get("image"), "errors": []}
        for task in detail.get("tasks", []):
            if (task.get("replicaset") != config.get("latest_replicaset_name")
                    or task.get("version") != "new" or not task.get("ports")):
                continue
            try:
                address = ipaddress.ip_address(task.get("hostv6") or task["host"])
                host = f"[{address}]" if address.version == 6 else str(address)
                endpoint = f"http://{host}:{task['ports'][0]}"
                client = Client(endpoint, token, timeout=5)
                health = client.health()
                if health["worker"] != worker:
                    raise ValueError("HTTP worker identity mismatch")
                client = Client(endpoint, token, instance=health["instance"], timeout=5)
                report.update(endpoint=endpoint, health=health,
                              runtime=client.request("GET", "/v1/runtime"))
                break
            except Exception as exc:
                report["errors"].append(type(exc).__name__)
        reports["workers"][worker] = report
        print(json.dumps({"worker": worker, "image": report["image"],
                          "health": report.get("health"), "runtime": report.get("runtime"),
                          "errors": report["errors"]}), flush=True)
    args.output.write_text(json.dumps(reports, indent=2) + "\n")


if __name__ == "__main__":
    main()
