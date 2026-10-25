"""Bound a submitted capacity run and retain safe status/Pod evidence."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from .storage import canonical


TERMINAL = {"success", "failed", "failed_to_launch", "stopped", "cancelled"}


def call(cli, control_plane, group, action, body):
    process = subprocess.run([cli, "--control-plane", control_plane, "job-v2",
                              group, action, "--json", json.dumps(body)],
                             capture_output=True, text=True, timeout=40)
    if process.returncode:
        # CLI diagnostics may echo request contexts; do not store raw output.
        raise RuntimeError(f"{group}/{action} returned {process.returncode}")
    value = json.loads(process.stdout)
    if value.get("ResponseBase", {}).get("code", 0):
        raise RuntimeError(f"{group}/{action} rejected")
    return value


def collect_pods(cli, control_plane, trial):
    result, page = [], "1"
    while page:
        value = call(cli, control_plane, "run-trial-pods", "list",
                     {"filter": {"trial_sid_any": [trial]}, "page_size": 200, "page_token": page})
        result.extend({key: row.get(key) for key in (
            "sid", "status", "exit_status", "gpuv", "schedule_at", "start_at", "stop_at"
        )} | {"host_assigned": bool(row.get("host"))} for row in value.get("data", []))
        page = value.get("pagination", {}).get("next_page_token")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--control-plane", default="cn")
    parser.add_argument("--sid", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--deadline-epoch", type=float, required=True)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("Polling interval must be 1–60 seconds")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    last_status = None
    stop_requested = False
    while True:
        try:
            value = call(args.cli, args.control_plane, "runs", "get", {"sid": args.sid})["data"]
            status = value["status"]
            trial = value.get("runtime_context", {}).get("trial_id")
            record = {"time": time.time(), "sid": args.sid, "control_plane": args.control_plane,
                      "status": status, "trial": trial,
                      "trial_status": value.get("runtime_context", {}).get("trial_status"),
                      "deadline_epoch": args.deadline_epoch, "stop_requested": stop_requested}
            with (output / "status.jsonl").open("ab") as stream:
                stream.write(canonical(record) + b"\n")
            (output / "latest.json").write_bytes(canonical(record))
            if status != last_status or status in TERMINAL:
                print(canonical(record).decode(), flush=True)
                if trial:
                    (output / "pods.json").write_bytes(canonical(
                        collect_pods(args.cli, args.control_plane, trial)))
                last_status = status
            if status in TERMINAL:
                return
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyError) as exc:
            print(canonical({"time": time.time(), "event": "monitor_query_failed",
                             "type": type(exc).__name__}).decode(), flush=True)
        if time.time() >= args.deadline_epoch:
            try:
                call(args.cli, args.control_plane, "runs", "stop",
                     {"sid": args.sid, "stop_reason": "Capacity experiment absolute deadline"})
                stop_requested = True
                print(canonical({"time": time.time(), "event": "deadline_stop_requested",
                                 "sid": args.sid}).decode(), flush=True)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                print(canonical({"event": "deadline_stop_failed", "type": type(exc).__name__}).decode(),
                      flush=True)
            # Continue until the platform confirms terminal state; retry failed
            # deadline stops rather than silently abandoning a queued resource.
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
