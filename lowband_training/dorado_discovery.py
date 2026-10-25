"""Reuse only LLMClient's public discovery API, not its chat/load-balancer API."""
import time


class BernardEndpoints:
    def __init__(self, psm, services, region="cn", cluster=("default",), env_type=("prod",), refresh_seconds=30):
        self.psm, self.services, self.region = psm, dict(services), region
        self.cluster, self.env_type = list(cluster), list(env_type)
        self.refresh_seconds = refresh_seconds
        self.clients, self.selected, self.counts = {}, {}, {}
        self.refreshed_at = float("-inf")

    def __call__(self):
        if time.monotonic() - self.refreshed_at < self.refresh_seconds:
            return dict(self.selected)
        from bytedmerlin.api.llm_client import LLMClient
        selected = {}
        for worker, service_id in self.services.items():
            try:
                client = self.clients.get(worker)
                if client is None:
                    client = LLMClient(psm=self.psm, bernard_service_id=[service_id], region=self.region,
                                       cluster=self.cluster, env_type=self.env_type, enable_tracer=False,
                                       startup_timeout=0, refresh_timeout=0, min_startup_instances=0,
                                       lb_strategy="sequential")
                    self.clients[worker] = client
                else:
                    client.refresh_endpoints()
                # SDK returns host:port, adding brackets itself for IPv6.
                urls = sorted({"http://" + endpoint for endpoint in client.list_endpoints()})
            except ValueError as exc:
                if "No deployments found" not in str(exc):
                    raise
                urls = []
            self.counts[worker] = len(urls)
            if urls:
                selected[worker] = self.selected.get(worker) if self.selected.get(worker) in urls else urls[0]
        self.selected, self.refreshed_at = selected, time.monotonic()
        return dict(selected)
