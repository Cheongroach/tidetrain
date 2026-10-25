"""Slot-preserving discovery behavior; never allocates or invokes real GPUs."""
import unittest
from unittest.mock import patch

from .dorado_discovery import BernardEndpoints


class DiscoveryTests(unittest.TestCase):
    def test_empty_pool_recovery_and_sticky_selection(self):
        pools = {"hl-service": [], "lf-service": ["[::1]:10002"]}
        class FakeClient:
            def __init__(self, **kwargs):
                self.service = kwargs["bernard_service_id"][0]
                if not pools[self.service]:
                    raise ValueError("No deployments found for test")
            def refresh_endpoints(self):
                pass
            def list_endpoints(self):
                return pools[self.service]
        with patch("bytedmerlin.api.llm_client.LLMClient", FakeClient):
            provider = BernardEndpoints("test.psm", {"hl": "hl-service", "lf": "lf-service"}, refresh_seconds=0)
            self.assertEqual(provider(), {"lf": "http://[::1]:10002"})
            pools["hl-service"] = ["127.0.0.1:10004", "127.0.0.1:10003"]
            self.assertEqual(provider()["hl"], "http://127.0.0.1:10003")
            pools["hl-service"].append("127.0.0.1:10001")
            self.assertEqual(provider()["hl"], "http://127.0.0.1:10003")
            pools["hl-service"].remove("127.0.0.1:10003")
            self.assertEqual(provider()["hl"], "http://127.0.0.1:10001")
            pools["lf-service"] = []
            self.assertNotIn("lf", provider())
            self.assertEqual(provider.counts, {"hl": 2, "lf": 0})

    def test_configuration_error_is_not_zero_capacity(self):
        with patch("bytedmerlin.api.llm_client.LLMClient", side_effect=ValueError("Bad region")):
            provider = BernardEndpoints("test.psm", {"hl": "hl-service"}, refresh_seconds=0)
            with self.assertRaisesRegex(ValueError, "Bad region"):
                provider()
