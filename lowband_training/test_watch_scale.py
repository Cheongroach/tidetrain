"""Monitor tests for phase transitions, deadline stops and pagination."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .watch_scale import collect_pods, main


def status(phase, state="started"):
    return {"data": {"status": state, "runtime_context": {
        "trial_id": "trial", "trial_status": phase}}}


class WatchScaleTest(unittest.TestCase):
    def test_phase_changes_collect_pods(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = ["watch", "--cli", "cli", "--sid", "run", "--output", directory,
                    "--deadline-epoch", "99999999999", "--interval", "1"]
            with patch("sys.argv", argv), patch("lowband_training.watch_scale.time.sleep"), \
                    patch("lowband_training.watch_scale.call", side_effect=[
                        status("queued"), status("staging"), status("done", "success")]), \
                    patch("lowband_training.watch_scale.collect_pods", return_value=[]) as pods:
                main()
            self.assertEqual(pods.call_count, 3)
            self.assertEqual(json.loads(Path(directory, "latest.json").read_text())["status"], "success")

    def test_deadline_stop_waits_for_terminal_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = ["watch", "--cli", "cli", "--sid", "run", "--output", directory,
                    "--deadline-epoch", "0", "--interval", "1", "--terminal-grace-seconds", "0"]
            with patch("sys.argv", argv), patch("lowband_training.watch_scale.time.sleep"), \
                    patch("lowband_training.watch_scale.call", side_effect=[
                        status("queued"), {}, status("stopped", "stopped")]) as call, \
                    patch("lowband_training.watch_scale.collect_pods", return_value=[]):
                main()
            self.assertEqual(call.call_args_list[1].args[3], "stop")
            self.assertTrue(json.loads(Path(directory, "latest.json").read_text())["stop_requested"])

    def test_automatic_retry_after_stop_is_stopped_again(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [0]

            def advance(_):
                clock[0] += 1

            argv = ["watch", "--cli", "cli", "--sid", "run", "--output", directory,
                    "--deadline-epoch", "0", "--interval", "1", "--terminal-grace-seconds", "2"]
            retried = status("queued")
            retried["data"]["runtime_context"]["trial_id"] = "new-trial"
            terminal = status("killed", "stopped")
            with patch("sys.argv", argv), \
                    patch("lowband_training.watch_scale.time.sleep", side_effect=advance), \
                    patch("lowband_training.watch_scale.time.monotonic", side_effect=lambda: clock[0]), \
                    patch("lowband_training.watch_scale.call", side_effect=[
                        terminal, retried, {}, terminal, terminal, terminal]) as call, \
                    patch("lowband_training.watch_scale.collect_pods", return_value=[]):
                main()
            self.assertEqual(sum(row.args[3] == "stop" for row in call.call_args_list), 1)
            self.assertEqual(call.call_count, 6)
            self.assertTrue(json.loads(Path(directory, "latest.json").read_text())["stop_requested"])

    def test_pod_pagination_covers_more_than_200_nodes(self):
        with patch("lowband_training.watch_scale.call", side_effect=[
                {"data": [{"sid": "first", "host": ""}], "pagination": {"next_page_token": "2"}},
                {"data": [{"sid": "last", "host": "assigned"}], "pagination": {}}]) as call:
            rows = collect_pods("cli", "cn", "trial")
        self.assertEqual([row["host_assigned"] for row in rows], [False, True])
        self.assertEqual(call.call_args_list[1].args[4]["page_token"], "2")


if __name__ == "__main__":
    unittest.main()
