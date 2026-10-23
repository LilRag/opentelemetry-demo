# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import datetime
import json
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
import requests

from config import config
from queries import error_rate, latency

try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


class EvidenceCollector:
    """
    Forensic Evidence Collector for OpenTelemetry Microservices.
    Correlates data across:
      1. Prometheus (Metrics: Error Rate, Latency)
      2. Jaeger v2/v3 (Distributed Traces: Root and child failing spans)
      3. OpenSearch (Logs: Stack traces correlated by trace_id/service)
      5. Docker (Infrastructure: Container states, exit codes, OOM kills)
    """

    def __init__(self):
        self.prom_url = config.PROMETHEUS_URL
        self.jaeger_url = config.JAEGER_URL
        self.opensearch_url = self._resolve_opensearch_url()
        try:
            self.docker_client = docker.from_env() if DOCKER_AVAILABLE else None
        except Exception:
            self.docker_client = None

    def _resolve_opensearch_url(self) -> str:
        """Finds active OpenSearch port (either configured or mapped by Docker)."""
        if config.OPENSEARCH_URL:
            return config.OPENSEARCH_URL
        try:
            # Query docker CLI for dynamically mapped port of opensearch:9200
            out = subprocess.check_output('docker port opensearch 9200', shell=True, text=True).strip()
            # Expected format: '0.0.0.0:59254'
            if ":" in out:
                port = out.split(":")[-1].strip()
                return f"http://localhost:{port}"
        except Exception:
            pass
        return "http://localhost:9200"

    def collect_evidence(self, service: str, symptom: str = "HIGH_ERROR_RATE") -> Dict[str, Any]:
        """
        Executes surgical forensic queries across all backends
        and returns an enriched Evidence Bundle.
        """
        print(f"\n[*] Collecting evidence for service: '{service}' (Symptom: {symptom})...")

        incident_id = "INC-" + uuid.uuid4().hex
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # 1. Gather Metrics Evidence from Prometheus
        metrics_evidence = self._collect_metrics(service)

        # 2. Gather Trace Evidence from Jaeger
        trace_ids, failing_spans = self._collect_traces(service)

        # 3. Gather Log Evidence from OpenSearch
        logs_evidence = self._collect_logs(service, trace_ids)


        # 5. Gather Infrastructure State from Docker
        infra_evidence = self._collect_infra_state(service)

        is_infra = infra_evidence.get("has_infra_anomaly", False)

        bundle = {
            "incident_id": incident_id,
            "timestamp": timestamp,
            "symptom": symptom,
            "target_service": service,
            "classification": "INFRASTRUCTURE" if is_infra else "APPLICATION",
            "evidence": {
                "metrics": metrics_evidence,
                "traces": {
                    "sample_trace_ids": trace_ids,
                    "failing_spans": failing_spans
                },
                "logs": logs_evidence,
                "infrastructure": infra_evidence
            }
        }

        print(f"[+] Evidence bundle assembled successfully (Incident: {incident_id})")
        return bundle

    # ------------------------------------------------------------------
    # 1. Prometheus Metrics Collection
    # ------------------------------------------------------------------
    def _collect_metrics(self, service: str) -> Dict[str, Any]:
        result = {"error_rate": None, "p95_latency": None}
        try:
            q_err = error_rate(service)
            r = requests.get(f"{self.prom_url}/api/v1/query", params={"query": q_err}, timeout=4).json()
            items = r.get("data", {}).get("result", [])
            if items:
                result["error_rate"] = f"{float(items[0]['value'][1]):.1f}%"

            q_lat = latency(service)
            r_lat = requests.get(f"{self.prom_url}/api/v1/query", params={"query": q_lat}, timeout=4).json()
            items_lat = r_lat.get("data", {}).get("result", [])
            if items_lat:
                val = float(items_lat[0]["value"][1])
                result["p95_latency"] = f"{val:.2f}s"
        except Exception as e:
            result["error_query_exception"] = str(e)

        return result

    # ------------------------------------------------------------------
    # 2. Jaeger Traces Collection (Jaeger v3 API)
    # ------------------------------------------------------------------
    def _collect_traces(self, service: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        trace_ids = []
        failing_spans = []

        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            start = now - datetime.timedelta(hours=1)

            params = {
                "query.startTimeMin": start.isoformat(),
                "query.startTimeMax": now.isoformat(),
                "query.serviceName": service,
                "query.limit": 3
            }
            resp = requests.get(self.jaeger_url, params=params, timeout=5).json()
            resource_spans = resp.get("result", {}).get("resourceSpans", [])

            for rs in resource_spans:
                scope_spans = rs.get("scopeSpans", [])
                for ss in scope_spans:
                    for span in ss.get("spans", []):
                        t_id = span.get("traceId")
                        if t_id and t_id not in trace_ids:
                            trace_ids.append(t_id)

                        # Check if span has an error status
                        status = span.get("status", {})
                        if status.get("code") in [2, "STATUS_CODE_ERROR"]:
                            failing_spans.append({
                                "trace_id": t_id,
                                "span_id": span.get("spanId"),
                                "name": span.get("name"),
                                "error_message": status.get("message", "Error status"),
                                "attributes": span.get("attributes", [])
                            })

        except Exception as e:
            failing_spans.append({"query_error": f"Failed to query Jaeger: {e}"})

        return trace_ids, failing_spans[:5]

    # ------------------------------------------------------------------
    # 3. OpenSearch Logs Collection
    # ------------------------------------------------------------------
    def _collect_logs(self, service: str, trace_ids: List[str]) -> List[str]:
        logs = []
        try:
            should_clauses = [{"match": {"resource.service.name": service}},
                              {"match": {"service.name": service}}]
            for tid in trace_ids[:3]:
                should_clauses.extend([{"match": {"traceId": tid}}, {"match": {"trace_id": tid}}])

            query_body = {
                "query": {
                    "bool": {
                        "should": should_clauses,
                        "minimum_should_match": 1,
                        "must": [{"bool": {"should": [{"match": {"severity.text": "ERROR"}},
                                                          {"match": {"severity_text": "ERROR"}}],
                                            "minimum_should_match": 1}}]
                    }
                },
                "size": 3
            }
            response = requests.post(
                f"{self.opensearch_url}/_search",
                json=query_body,
                headers={"Content-Type": "application/json"},
                timeout=4
            )
            response.raise_for_status()
            resp = response.json()

            for hit in resp.get("hits", {}).get("hits", []):
                source = hit.get("_source", {})
                log_line = source.get("body") or source.get("message") or str(source)
                if log_line and log_line not in logs:
                    logs.append(log_line)

        except Exception as e:
            logs.append(f"OpenSearch query error: {e}")

        return logs

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 5. Docker Infrastructure Health Collection
    # ------------------------------------------------------------------
    def _collect_infra_state(self, service: str) -> Dict[str, Any]:
        infra = {
            "has_infra_anomaly": False,
            "containers": []
        }

        if self.docker_client:
            try:
                for c in self.docker_client.containers.list(all=True):
                    if service in c.name or any(k in c.name for k in ["valkey", "astronomy-db", "kafka"]):
                        state = c.attrs.get("State", {})
                        status = state.get("Status", "unknown")
                        exit_code = state.get("ExitCode", 0)
                        oom_killed = state.get("OOMKilled", False)

                        anomaly = (status != "running") or (exit_code != 0) or oom_killed
                        if anomaly:
                            infra["has_infra_anomaly"] = True

                        infra["containers"].append({
                            "name": c.name,
                            "status": status,
                            "exit_code": exit_code,
                            "oom_killed": oom_killed
                        })
                return infra
            except Exception:
                pass

        try:
            cmd = 'docker ps -a --format "{{.Names}}|{{.Status}}"'
            out = subprocess.check_output(cmd, shell=True, text=True)
            for line in out.strip().splitlines():
                if "|" in line:
                    name, status = line.split("|", 1)
                    if service in name or any(k in name for k in ["valkey", "astronomy-db", "kafka"]):
                        is_down = "Exited" in status or "restarting" in status.lower()
                        if is_down:
                            infra["has_infra_anomaly"] = True
                        infra["containers"].append({"name": name, "status": status})
        except Exception as e:
            infra["docker_cli_error"] = str(e)

        return infra


if __name__ == "__main__":
    collector = EvidenceCollector()
    bundle = collector.collect_evidence(service="checkout", symptom="HIGH_ERROR_RATE")
    print("\n" + "=" * 60)
    print("[*] Assembled Evidence Bundle:")
    print("=" * 60)
    print(json.dumps(bundle, indent=2))
