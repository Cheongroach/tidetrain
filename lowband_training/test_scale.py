"""Failure tests for the new durable and bounded execution path."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

import torch

from .ledger import Ledger, Rejected
from .protocol import nesterov
from .shards import Shards, reduce_nesterov, references
from .storage import IntegrityError
from .http_transport import Client, RemoteError
from .scale_worker import CapacityClient


def acquire_child(path, owner, output):
    try:
        output.put(("accepted", Ledger(path).acquire("coordinator", owner, 60)["fence"]))
    except Rejected:
        output.put(("rejected", owner))


def crash_transaction(path):
    ledger = Ledger(path)
    with ledger.transaction() as db:
        ledger.event(db, "must_not_survive", {"tokens": 999999})
        os._exit(19)


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shards = Shards(self.root / "blobs", 256)
        self.config = {"workers": ["a", "b"], "quorum": 2, "local_steps": 2,
                       "batch_size": 2, "seq_len": 8, "round_timeout": 1}
        self.ledger = Ledger(self.root / "ledger.db")
        self.state = {"parameters": self.shards.tree({"w": torch.arange(129).float()}),
                      "momentum": self.shards.tree({"w": torch.zeros(129)})}
        self.ledger.initialize(self.config, self.state)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, worker, lease, base):
        recovery = {"cursor": 4, "rng": {"cpu": torch.get_rng_state(), "python": random.getstate()},
                    "optimizer": {0: {"exp_avg": torch.ones(129), "step": torch.tensor(2.)}}}
        return {"worker": worker, "fence": lease["fence"], "base_round": base["round"],
                "epoch": base["epoch"], "base_sha256": Ledger.state_hash(base["state"]),
                "tokens": 32, "cursor": 4, "delta": self.shards.tree({"w": torch.ones(129)}),
                "recovery": self.shards.tree(recovery)}

    def test_chunked_outer_matches_baseline_bitwise(self):
        torch.manual_seed(44)
        parameters = {"w": torch.randn(73, 7), "scalar": torch.randn(())}
        velocity = {name: torch.randn_like(value) for name, value in parameters.items()}
        deltas = [{name: torch.randn_like(value) for name, value in parameters.items()} for _ in range(3)]
        weights = [7, 13, 19]
        base = {"parameters": self.shards.tree(parameters), "momentum": self.shards.tree(velocity)}
        gradient = {name: torch.zeros_like(value) for name, value in parameters.items()}
        for delta, weight in zip(deltas, weights):
            for name in gradient:
                gradient[name].add_(delta[name], alpha=-weight / sum(weights))
        expected = nesterov(parameters, velocity, gradient, .7, .8)
        actual = reduce_nesterov(self.shards, base,
            [{"tokens": weight, "delta": self.shards.tree(delta)} for delta, weight in zip(deltas, weights)], .7, .8)
        for kind in expected:
            for name, value in self.shards.restore(actual[kind]).items():
                self.assertTrue(torch.equal(value, expected[kind][name]), (kind, name))
        self.assertLessEqual(self.shards.peak_chunk_bytes, 256)

    def test_complete_recovery_types_and_corruption(self):
        value = {0: {"m": torch.randn(101), "step": torch.tensor(4.)},
                 "rng": random.getstate(), "cursor": 7, "empty": torch.empty(0)}
        tree = self.shards.tree(value)
        recovered = self.shards.restore(tree)
        self.assertEqual(recovered["rng"], value["rng"])
        self.assertEqual(recovered["cursor"], 7)
        self.assertTrue(torch.equal(recovered[0]["m"], value[0]["m"]))
        path = self.shards.path(next(references(tree)))
        path.write_bytes(b"corrupt")
        with self.assertRaises(IntegrityError):
            self.shards.restore(tree)

    def test_fencing_and_duplicate_tokens(self):
        base = self.ledger.snapshot()
        old = self.ledger.acquire("worker:a", "old", .03)
        record = self.record("a", old, base)
        time.sleep(.05)
        current = self.ledger.acquire("worker:a", "new", 60)
        self.assertGreater(current["fence"], old["fence"])
        with self.assertRaises(Rejected):
            self.ledger.submit(old, record)
        record = self.record("a", current, base)
        self.assertTrue(self.ledger.submit(current, record))
        self.assertFalse(self.ledger.submit(current, record))
        with self.assertRaises(Rejected):
            self.ledger.submit(current, {**record, "tokens": 64})
        b = self.ledger.acquire("worker:b", "b", 60)
        second = self.record("b", b, base)
        self.ledger.submit(b, second)
        coordinator = self.ledger.acquire("coordinator", "c", 60)
        commit = self.ledger.commit(coordinator, base, [record, second], self.state)
        self.assertEqual(commit["effective_tokens"], 64)
        with self.assertRaises(Rejected):
            self.ledger.commit(coordinator, base, [record, second], self.state)
        with self.assertRaises(Rejected):
            self.ledger.submit(current, record)
        replay = Ledger(self.root / "ledger.db").snapshot()
        self.assertEqual(replay, commit)
        self.assertEqual(len([e for e in self.ledger.events() if e["kind"] == "round_committed"]), 1)

    def test_parallel_authorities_and_crash_rollback(self):
        ctx = mp.get_context("fork")
        output = ctx.Queue()
        children = [ctx.Process(target=acquire_child, args=(self.ledger.path, str(i), output)) for i in range(4)]
        for child in children:
            child.start()
        results = [output.get(timeout=10) for _ in children]
        for child in children:
            child.join(timeout=10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(sum(kind == "accepted" for kind, _ in results), 1)
        crashed = ctx.Process(target=crash_transaction, args=(self.ledger.path,))
        crashed.start()
        crashed.join(timeout=10)
        self.assertEqual(crashed.exitcode, 19)
        self.assertNotIn("must_not_survive", [e["kind"] for e in Ledger(self.ledger.path).events()])

    def test_bad_epoch_ancestry_and_recovery_cursor(self):
        lease = self.ledger.acquire("worker:a", "a")
        record = self.record("a", lease, self.ledger.snapshot())
        for altered in ({**record, "epoch": 2}, {**record, "base_sha256": "0" * 64},
                        {**record, "cursor": 8}):
            with self.assertRaises(Rejected):
                self.ledger.submit(lease, altered)

    def test_compact_round_hydrates_only_requested_worker(self):
        base = self.ledger.snapshot()
        records = [self.record(worker, self.ledger.acquire("worker:" + worker, worker), base)
                   for worker in self.config["workers"]]
        for record in records:
            lease = self.ledger.acquire("worker:" + record["worker"], record["worker"])
            self.ledger.submit(lease, record)
        self.ledger.commit(self.ledger.acquire("coordinator", "c"), base, records, self.state)
        compact = Ledger(self.ledger.path).snapshot()
        self.assertTrue(all("recovery" not in row for row in compact["workers"].values()))
        hydrated = Ledger(self.ledger.path).worker_snapshot("a")
        self.assertEqual(set(hydrated["workers"]), {"a"})
        self.assertEqual(hydrated["workers"]["a"]["recovery"], records[0]["recovery"])
        self.assertNotIn("recovery_sha256", hydrated["workers"]["a"])
        self.assertEqual(compact, Ledger(self.ledger.path).snapshot())

    def test_overload_retry_does_not_retry_stale_conflict(self):
        client = CapacityClient("http://localhost:12345", "test")
        with patch.object(Client, "request", side_effect=[URLError("reset"), {"ok": True}]), \
                patch("lowband_training.scale_worker.time.sleep"):
            self.assertEqual(client.request("GET", "/snapshot"), {"ok": True})
        self.assertEqual(client.transient_retries, 1)
        with patch.object(Client, "request", side_effect=RemoteError(409, "stale")) as call:
            with self.assertRaises(RemoteError):
                client.request("POST", "/submit", {})
            self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
