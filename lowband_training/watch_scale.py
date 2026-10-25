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
    parser.add_argument("--terminal-grace-seconds", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("Polling interval must be 1–60 seconds")
    if not 0 <= args.terminal_grace_seconds <= 3600:
        parser.error("Terminal grace must be 0–3600 seconds")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    last_phase, last_pod_poll = None, 0.
    stop_requested = False
    terminal_since = None
    while True:
        confirmed_terminal = False
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
            phase = (status, record["trial_status"], trial)
            if phase != last_phase or status in TERMINAL:
                print(canonical(record).decode(), flush=True)
            if trial and (phase != last_phase or status in TERMINAL or
                          time.monotonic() - last_pod_poll >= 60):
                pods = collect_pods(args.cli, args.control_plane, trial)
                (output / "pods.json").write_bytes(canonical(pods))
                (output / "pod-counts.json").write_bytes(canonical({
                    "time": time.time(), "total": len(pods),
                    "hosted": sum(row["host_assigned"] for row in pods),
                    "started": sum(bool(row["start_at"]) for row in pods)}))
                last_pod_poll = time.monotonic()
            if status in TERMINAL:
                confirmed_terminal = True
                if time.time() < args.deadline_epoch:
                    return
                if terminal_since is None or phase != last_phase:
                    terminal_since = time.monotonic()
                # The platform may schedule an automatic retry shortly after
                # reporting stopped. Observe a stable terminal window first.
                if time.monotonic() - terminal_since >= args.terminal_grace_seconds:
                    return
            else:
                terminal_since = None
            last_phase = phase
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyError) as exc:
            terminal_since = None
            print(canonical({"time": time.time(), "event": "monitor_query_failed",
                             "type": type(exc).__name__}).decode(), flush=True)
        if time.time() >= args.deadline_epoch and not confirmed_terminal:
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
