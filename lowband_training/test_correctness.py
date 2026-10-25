"""CPU regressions for math, durable state, corruption and timeout membership."""
import copy
import tempfile
import unittest
from pathlib import Path

import torch

from .protocol import advance, initialize, latest, nesterov, submit
from .storage import IntegrityError, Store
from .train import TinyLM, load_data, parameters, train_round


def tiny_config():
    return {"model": "tiny", "workers": ["hl", "lf"], "quorum": 2,
            "local_steps": 2, "rounds": 3, "seq_len": 16, "batch_size": 2,
            "round_timeout": 30, "max_seconds": 120, "outer_lr": 0.7,
            "outer_momentum": 0.9, "inner_lr": 0.003, "weight_decay": 0.01,
            "seed": 42}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        torch.manual_seed(42)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "shared", self.root / "cache")
        self.config = tiny_config()
        self.model = TinyLM()
        self.data = load_data(self.config)
        self.base = initialize(self.store, self.config, parameters(self.model))

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, worker="hl", base=None):
        base = self.base if base is None else base
        state = self.store.get(base["state"])
        previous = self.store.get(base["workers"][worker]) if worker in base["workers"] else None
        delta, recovery, count, metrics = train_round(self.model, self.data, self.config, worker, state, previous)
        return submit(self.store, base, worker, delta, recovery, count, metrics)

    def test_nesterov_matches_torch_sgd_multiple_steps(self):
        value = torch.tensor([1.0, -2.0], requires_grad=True)
        opt = torch.optim.SGD([value], lr=0.7, momentum=0.9, nesterov=True)
        state = {"parameters": {"w": value.detach().clone()}, "momentum": {"w": torch.zeros(2)}}
        for grad in (torch.tensor([0.2, 0.4]), torch.tensor([-0.6, 0.3])):
            value.grad = grad.clone()
            opt.step()
            state = nesterov(state["parameters"], state["momentum"], {"w": grad}, 0.7, 0.9)
            torch.testing.assert_close(state["parameters"]["w"], value)

    def test_duplicate_update_is_counted_once(self):
        original = self.publish("hl")
        self.assertEqual(self.publish("hl"), original)
        self.publish("lf")
        commit = advance(self.store, self.base)
        self.assertEqual(commit["effective_tokens"], 128)
        self.assertEqual(len(commit["accepted"]), 2)
        self.assertEqual(advance(self.store, self.base), commit)
        self.assertEqual(latest(self.store)["round"], 1)

    def test_corrupt_blob_rejected_from_cold_cache(self):
        record = self.publish()
        path = self.store.path("blobs/" + record["delta"]["sha256"] + ".pt")
        path.write_bytes(b"corrupted")
        cold = Store(self.store.root, self.root / "cold")
        with self.assertRaises(IntegrityError):
            cold.get(record["delta"])

    def test_stale_base_is_rejected(self):
        self.publish("hl")
        self.publish("lf")
        name = "updates/000000/hl.json"
        record = self.store.read_json(name)
        record["base_sha256"] = "0" * 64
        self.store.path(name).unlink()
        self.store.publish_json(name, record)
        with self.assertRaisesRegex(IntegrityError, "Stale"):
            advance(self.store, self.base)

    def test_changed_config_is_rejected_on_restart(self):
        changed = {**self.config, "local_steps": 9}
        with self.assertRaisesRegex(IntegrityError, "config changed"):
            initialize(self.store, changed, {})

    def test_optimizer_rng_and_cursor_survive_serialization(self):
        self.publish("hl")
        self.publish("lf")
        commit = advance(self.store, self.base)
        state = self.store.get(commit["state"])
        prior = self.store.get(commit["workers"]["hl"])
        first = train_round(self.model, self.data, self.config, "hl", state, prior)
        cold = Store(self.store.root, self.root / "restarted")
        second = train_round(TinyLM(), self.data, self.config, "hl", cold.get(commit["state"]),
                             cold.get(commit["workers"]["hl"]))
        for key in first[0]:
            torch.testing.assert_close(first[0][key], second[0][key], rtol=0, atol=0)
        self.assertEqual(first[1]["cursor"], 8)
        self.assertEqual(first[1]["cursor"], second[1]["cursor"])
        self.assertEqual(first[3]["mean_train_loss"], second[3]["mean_train_loss"])
        torch.testing.assert_close(first[1]["rng"]["cpu"], second[1]["rng"]["cpu"])

    def test_commit_last_recovers_coordinator_crash(self):
        self.publish("hl")
        self.publish("lf")
        original = self.store.publish_json
        def crash(name, value):
            if name == "rounds/000001.json":
                raise RuntimeError("injected crash before manifest")
            original(name, value)
        self.store.publish_json = crash
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            advance(self.store, self.base)
        self.assertEqual(latest(self.store)["round"], 0)
        recovered = Store(self.store.root, self.root / "new-coordinator")
        commit = advance(recovered, latest(recovered))
        self.assertEqual(commit["round"], 1)
        self.assertEqual(commit["effective_tokens"], 128)

    def test_quorum_missing_worker_does_not_advance_its_cursor(self):
        # A new run is required when switching membership policy.
        config = {**self.config, "quorum": 1}
        store = Store(self.root / "quorum-run", self.root / "quorum-cache")
        base = initialize(store, config, parameters(self.model))
        state = store.get(base["state"])
        delta, recovery, tokens, metrics = train_round(self.model, self.data, config, "hl", state)
        submit(store, base, "hl", delta, recovery, tokens, metrics)
        self.assertIsNone(advance(store, base))
        commit = advance(store, base, force_timeout=True)
        self.assertEqual(list(commit["workers"]), ["hl"])
        self.assertEqual(commit["effective_tokens"], 64)
        self.assertNotIn("lf", commit["workers"])
        delta, recovery, tokens, metrics = train_round(self.model, self.data, config, "lf", store.get(commit["state"]))
        self.assertEqual(recovery["cursor"], 4)  # returning worker starts its unconsumed shard

    def test_outer_step_equals_explicit_weighted_average(self):
        a = self.publish("hl")
        b = self.publish("lf")
        base = self.store.get(self.base["state"])
        da, db = self.store.get(a["delta"])["delta"], self.store.get(b["delta"])["delta"]
        result = self.store.get(advance(self.store, self.base)["state"])
        for key in base["parameters"]:
            mean_delta = (da[key] + db[key]) / 2
            expected = base["parameters"][key] + 0.7 * 1.9 * mean_delta
            torch.testing.assert_close(result["parameters"][key], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
