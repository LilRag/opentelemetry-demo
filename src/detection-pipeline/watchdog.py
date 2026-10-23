# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import subprocess
import time
from typing import Any, Callable, Dict, Optional
import requests

from config import config
from evidence_collector import EvidenceCollector
from queries import error_rate, latency as latency_query, MEMORY, QUEUE

try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


class Watchdog:
    """
    Lightweight 15-second SRE Watchdog.
    Monitors high-level signals across all microservices and classifies
    anomalies into one of the 5 Universal Symptom Families.
    """

    def __init__(self, on_incident_detected: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.collector = EvidenceCollector()
        self.on_incident_detected = on_incident_detected
        self.active_incident: Optional[Dict[str, Any]] = None
        try:
            self.docker_client = docker.from_env() if DOCKER_AVAILABLE else None
        except Exception:
            self.docker_client = None

    def scan_once(self) -> Optional[Dict[str, Any]]:
        """
        Runs one evaluation pass across the 5 Universal Symptoms.
        Returns symptom event dict if an anomaly is detected, else None.
        """
        self.monitoring_warnings = []
        # Symptom 1: High Error Rate (5xx > threshold)
        symptom = self._check_error_rates()
        if symptom:
            return symptom

        # Symptom 2: High Latency (p95 > threshold)
        symptom = self._check_latency()
        if symptom:
            return symptom

        # Symptom 3: Container Down or Unhealthy
        symptom = self._check_container_health()
        if symptom:
            return symptom

        # Symptom 4: Resource Spike (Memory > threshold)
        symptom = self._check_resource_usage()
        if symptom:
            return symptom

        # Symptom 5: Kafka Queue / Pipeline Lag
        symptom = self._check_kafka_lag()
        if symptom:
            return symptom

        return None

    def _query(self, query):
        response = requests.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=3)
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "success" or not result.get("data", {}).get("result"):
            self.monitoring_warnings = getattr(self, "monitoring_warnings", []) + ["Query failed or has no samples: " + query]
        return result

    def monitor_loop(self, poll_interval: Optional[int] = None):
        """
        Continuous monitoring loop. Runs every N seconds.
        """
        interval = poll_interval or config.WATCHDOG_INTERVAL_SECONDS
        print("=" * 60)
        print(f"[*] SRE Watchdog active. Polling system health every {interval}s...")
        print("=" * 60)

        while True:
            try:
                symptom_event = self.scan_once()
                for warning in getattr(self, "monitoring_warnings", []):
                    print("[Telemetry warning] " + warning)

                if symptom_event:
                    service = symptom_event["service"]
                    symptom_name = symptom_event["symptom"]
                    val = symptom_event["value"]

                    # Only trigger if not already handling this exact incident (Deduplication)
                    if not self.active_incident or self.active_incident.get("service") != service:
                        print("\n" + "!" * 60)
                        print(f"[!] ANOMALY DETECTED: [{symptom_name}] on service '{service}' ({val})")
                        print("!" * 60)

                        # Trigger surgical evidence collection
                        evidence_bundle = self.collector.collect_evidence(
                            service=service,
                            symptom=symptom_name
                        )
                        self.active_incident = evidence_bundle

                        # Callback to next layer (LLM RCA Agent)
                        if self.on_incident_detected:
                            self.on_incident_detected(evidence_bundle)
                    else:
                        print(f"[-] Ongoing incident on '{service}' ({symptom_name}) still active. Waiting for resolution...")
                else:
                    if self.active_incident:
                        print("\n[-] No threshold breach detected; recovery still requires explicit verification.")
                    else:
                        print(f"[{time.strftime('%H:%M:%S')}] No configured threshold breach; unavailable telemetry is not proof of health.")

            except Exception as e:
                print(f"[!] Watchdog check error: {e}")

            time.sleep(interval)

    # ------------------------------------------------------------------
    # Symptom 1: Error Rate Evaluation
    # ------------------------------------------------------------------
    def _check_error_rates(self) -> Optional[Dict[str, Any]]:
        query = error_rate()
        try:
            r = self._query(query)
            results = r.get("data", {}).get("result", [])
            for res in results:
                rate = float(res.get("value", [0, 0])[1])
                service = res.get("metric", {}).get("service_name", "unknown")
                if rate > config.ERROR_RATE_THRESHOLD_PERCENT:
                    return {
                        "symptom": "HIGH_ERROR_RATE",
                        "service": service,
                        "value": f"{rate:.1f}% failure rate"
                    }
        except Exception:
            self.monitoring_warnings = getattr(self, "monitoring_warnings", []) + ["Monitoring source unavailable"]
        return None

    # ------------------------------------------------------------------
    # Symptom 2: Latency Evaluation
    # ------------------------------------------------------------------
    def _check_latency(self) -> Optional[Dict[str, Any]]:
        query = latency_query()
        try:
            r = self._query(query)
            results = r.get("data", {}).get("result", [])
            for res in results:
                latency = float(res.get("value", [0, 0])[1])
                service = res.get("metric", {}).get("service_name", "unknown")
                if latency > config.LATENCY_THRESHOLD_SECONDS:
                    return {
                        "symptom": "HIGH_LATENCY",
                        "service": service,
                        "value": f"{latency:.2f}s (p95)"
                    }
        except Exception:
            self.monitoring_warnings = getattr(self, "monitoring_warnings", []) + ["Monitoring source unavailable"]
        return None

    # ------------------------------------------------------------------
    # Symptom 3: Container Down or Restarting
    # ------------------------------------------------------------------
    DEMO_SERVICES = [
        "checkout", "payment", "cart", "ad", "recommendation", "shipping",
        "currency", "email", "quote", "fraud-detection", "accounting",
        "product-catalog", "frontend", "frontend-proxy", "astronomy-db",
        "valkey-cart", "kafka", "image-provider"
    ]

    def _check_container_health(self) -> Optional[Dict[str, Any]]:
        if self.docker_client:
            try:
                for c in self.docker_client.containers.list(all=True):
                    # Strictly match demo microservice names
                    if c.name in self.DEMO_SERVICES:
                        status = c.status
                        health = c.attrs.get("State", {}).get("Health", {}).get("Status")
                        if status != "running" or health == "unhealthy":
                            return {
                                "symptom": "CONTAINER_DOWN",
                                "service": c.name,
                                "value": f"Container status: {status}, health: {health}"
                            }
            except Exception:
                pass

        try:
            cmd = 'docker ps -a --filter "status=exited" --format "{{.Names}}"'
            out = subprocess.check_output(cmd, shell=True, text=True).strip()
            for line in out.splitlines():
                name = line.strip()
                if name in self.DEMO_SERVICES:
                    return {
                        "symptom": "CONTAINER_DOWN",
                        "service": name,
                        "value": "Container exited"
                    }
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Symptom 4: Resource Usage / Memory Leaks
    # ------------------------------------------------------------------
    def _check_resource_usage(self) -> Optional[Dict[str, Any]]:
        query = MEMORY
        try:
            r = self._query(query)
            results = r.get("data", {}).get("result", [])
            for res in results:
                usage_ratio = float(res.get("value", [0, 0])[1])
                container = res.get("metric", {}).get("container_name", "unknown")
                if container in self.DEMO_SERVICES and usage_ratio > config.MEMORY_THRESHOLD_RATIO:
                    return {
                        "symptom": "RESOURCE_SPIKE",
                        "service": container,
                        "value": f"{usage_ratio * 100:.1f}% memory limit"
                    }
        except Exception:
            self.monitoring_warnings = getattr(self, "monitoring_warnings", []) + ["Monitoring source unavailable"]
        return None

    # ------------------------------------------------------------------
    # Symptom 5: Kafka Queue Lag
    # ------------------------------------------------------------------
    def _check_kafka_lag(self) -> Optional[Dict[str, Any]]:
        query = QUEUE
        try:
            r = self._query(query)
            results = r.get("data", {}).get("result", [])
            for res in results:
                lag = float(res.get("value", [0, 0])[1])
                group = res.get("metric", {}).get("group", "unknown")
                if lag > config.KAFKA_LAG_THRESHOLD:
                    return {
                        "symptom": "PIPELINE_QUEUE_LAG",
                        "service": group,
                        "value": f"{int(lag)} unread records"
                    }
        except Exception:
            self.monitoring_warnings = getattr(self, "monitoring_warnings", []) + ["Monitoring source unavailable"]
        return None


if __name__ == "__main__":
    def dummy_callback(bundle):
        print(f"[Callback] Triggered for incident: {bundle['incident_id']}")

    watchdog = Watchdog(on_incident_detected=dummy_callback)
    # Perform a single scan test
    print("[*] Running single health scan...")
    anomaly = watchdog.scan_once()
    if anomaly:
        print(f"[!] Anomaly found: {anomaly}")
    else:
        print("[+] No configured breach detected; missing telemetry is not proof of health.")
