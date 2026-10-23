"""Read-only tools with fixed backends. No shell, flag-read or execution tools."""
import requests
from config import config

SERVICES = {"checkout", "payment", "cart", "ad", "recommendation", "shipping", "currency",
            "email", "quote", "fraud-detection", "accounting", "product-catalog", "frontend",
            "frontend-proxy", "astronomy-db", "valkey-cart", "kafka", "image-provider"}


class ReadOnlyTools:
    def __init__(self, collector):
        self.collector = collector
        self.history = []

    def definitions(self):
        return {"query_prometheus": {"query": "bounded PromQL string"},
                "get_traces": {"service": "service name"}, "get_logs": {"service": "service name"},
                "container_health": {"service": "service name"}}

    def call(self, name, arguments):
        if not isinstance(name, str) or name not in self.definitions() or not isinstance(arguments, dict):
            raise ValueError("Unknown read-only tool")
        if name == "query_prometheus":
            if set(arguments) != {"query"} or not isinstance(arguments["query"], str) or not 1 <= len(arguments["query"]) <= 2000:
                raise ValueError("Invalid query")
            response = requests.get(config.PROMETHEUS_URL + "/api/v1/query", params=arguments, timeout=5)
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "success":
                raise ValueError("Metrics query failed")
            result["data"]["result"] = result.get("data", {}).get("result", [])[:20]
        else:
            if set(arguments) != {"service"} or not isinstance(arguments["service"], str) or arguments["service"] not in SERVICES:
                raise ValueError("Unknown service")
            service = arguments["service"]
            if name == "get_traces":
                ids, spans = self.collector._collect_traces(service)
                result = {"trace_ids": ids, "failing_spans": spans}
            elif name == "get_logs":
                result = self.collector._collect_logs(service, [])
            else:
                result = self.collector._collect_infra_state(service)
        self.history.append({"tool": name, "arguments": arguments})
        return result
