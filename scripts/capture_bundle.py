#!/usr/bin/env python3
"""
capture_bundle.py — Snapshot telemetry from a live incident into a bundle.

Run this WHILE a fault is active (after 2-3 minutes of fault injection).
The bundle can then be used to test the agent offline without Docker running.

Usage:
    # After injecting a fault and waiting 2-3 mins:
    python scripts/capture_bundle.py --name incident_01_payment_failure

    # Capture all known faults one by one (inject each manually first):
    python scripts/capture_bundle.py --name incident_02_cart_failure

Endpoints (override with env vars if needed):
    PROMETHEUS_URL  default: http://localhost:9090
    JAEGER_URL      default: http://localhost:16686
    OPENSEARCH_URL  default: http://localhost:9200
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import subprocess

def _resolve_opensearch():
    if os.getenv("OPENSEARCH_URL"):
        return os.getenv("OPENSEARCH_URL")
    try:
        out = subprocess.check_output("docker port opensearch 9200", shell=True, text=True).strip()
        if ":" in out:
            return f"http://localhost:{out.split(':')[-1].strip()}"
    except Exception:
        pass
    return "http://localhost:9200"

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
OPENSEARCH_URL = _resolve_opensearch()
JAEGER_V3_URL = os.getenv("JAEGER_V3_URL", "http://localhost:8080/jaeger/ui/api/v3/traces")

BUNDLES_DIR = Path(__file__).parent.parent / "bundles"

# ---------------------------------------------------------------------------
# All Prometheus queries we want to snapshot
# ---------------------------------------------------------------------------
PROMETHEUS_QUERIES = {
    # Error rates — most important for diagnosis
    "error_rate_grpc": (
        'rate(rpc_server_duration_count{status_code="STATUS_CODE_ERROR"}[5m])'
    ),
    "error_rate_http": (
        'rate(http_server_request_duration_count'
        '{http_response_status_code=~"5.."}[5m])'
    ),
    # Latency
    "latency_p99": (
        "histogram_quantile(0.99, "
        "rate(rpc_server_duration_bucket[5m]))"
    ),
    "latency_p95": (
        "histogram_quantile(0.95, "
        "rate(rpc_server_duration_bucket[5m]))"
    ),
    # Resource usage
    "memory_usage": "container_memory_usage_bytes",
    "cpu_usage": "rate(container_cpu_usage_seconds_total[5m])",
    # Request volume
    "request_rate": "rate(rpc_server_duration_count[5m])",
    # Kafka consumer lag (relevant for kafkaQueueProblems / kafkaConsumerDead)
    "kafka_consumer_lag": "kafka_consumer_group_lag",
    # Container last seen (for crash detection)
    "container_last_seen": 'container_last_seen{name!=""}',
}

# Services to fetch traces for
TRACE_SERVICES = [
    "checkout",
    "payment",
    "cart",
    "frontend",
    "recommendation",
    "productcatalog",
    "ad",
    "shipping",
    "email",
    "currency",
]


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------

def capture_prometheus(bundle_path: Path) -> bool:
    print("\n  [*] Capturing Prometheus metrics...")
    results = {}
    failed = []

    for name, query in PROMETHEUS_QUERIES.items():
        try:
            r = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            r.raise_for_status()
            results[name] = {
                "query": query,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "response": r.json(),
            }
            count = len(r.json().get("data", {}).get("result", []))
            print(f"    [+] {name:<35} ({count} series)")
        except requests.exceptions.ConnectionError:
            failed.append(name)
            print(f"    [-] {name:<35} (connection refused -- is Prometheus running?)")
        except Exception as e:
            failed.append(name)
            print(f"    [-] {name:<35} ({e})")

    out_file = bundle_path / "metrics.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  -> Saved to {out_file}")

    if failed:
        print(f"  [!] {len(failed)} queries failed: {failed}")
    return len(failed) < len(PROMETHEUS_QUERIES)


def capture_jaeger(bundle_path: Path) -> bool:
    print("\n  [*] Capturing Jaeger traces...")
    results = {}
    failed = []

    use_v3_proxy = False
    try:
        svc_resp = requests.get(f"{JAEGER_URL}/api/services", timeout=2)
        jaeger_services = svc_resp.json().get("data", [])
    except Exception:
        use_v3_proxy = True
        jaeger_services = TRACE_SERVICES
        print("    [!] Jaeger 16686 not exposed directly; routing via Envoy proxy (port 8080)")

    now = datetime.now(timezone.utc)
    start = now - datetime.timedelta(hours=1) if hasattr(datetime, "timedelta") else now

    # Query traces for each service
    for svc in TRACE_SERVICES:
        try:
            if not use_v3_proxy:
                r = requests.get(
                    f"{JAEGER_URL}/api/traces",
                    params={"service": svc, "limit": 20, "lookback": "1h"},
                    timeout=3,
                )
                r.raise_for_status()
                data = r.json()
            else:
                r = requests.get(
                    JAEGER_V3_URL,
                    params={
                        "query.serviceName": svc,
                        "query.limit": 20,
                        "query.startTimeMin": (now.replace(hour=max(0, now.hour-1))).isoformat(),
                        "query.startTimeMax": now.isoformat(),
                    },
                    timeout=3,
                )
                data = r.json() if r.status_code == 200 else {"result": {"resourceSpans": []}}

            trace_count = len(data.get("data", [])) or len(data.get("result", {}).get("resourceSpans", []))
            results[svc] = {
                "service": svc,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "response": data,
            }
            print(f"    [+] {svc:<20} {trace_count} traces captured")
        except requests.exceptions.ConnectionError:
            failed.append(svc)
            print(f"    [-] {svc:<20} (connection refused)")
        except Exception as e:
            failed.append(svc)
            print(f"    [-] {svc:<20} ({e})")

    out_file = bundle_path / "traces.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  -> Saved to {out_file}")
    return len(failed) < len(TRACE_SERVICES)


def capture_logs(bundle_path: Path) -> bool:
    print("\n  [*] Capturing OpenSearch logs...")

    # Capture ERROR logs from all services in the last 15 minutes
    query = {
        "query": {
            "bool": {
                "must": [
                    {"range": {"@timestamp": {"gte": "now-15m"}}},
                    {"terms": {"severity_text": ["ERROR", "WARN", "error", "warn"]}},
                ]
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 200,
        "_source": [
            "@timestamp",
            "severity_text",
            "body",
            "service.name",
            "trace_id",
            "span_id",
        ],
    }

    try:
        r = requests.post(
            f"{OPENSEARCH_URL}/otel-logs*/_search",
            json=query,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        result = r.json()
        hit_count = result.get("hits", {}).get("total", {}).get("value", 0)
        print(f"    [+] Found {hit_count} error/warn log entries")

        out_file = bundle_path / "logs.json"
        with open(out_file, "w") as f:
            json.dump(
                {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "response": result,
                },
                f,
                indent=2,
            )
        print(f"  -> Saved to {out_file}")
        return True

    except requests.exceptions.ConnectionError:
        print(f"    [-] Cannot connect to OpenSearch at {OPENSEARCH_URL}")
        # Save empty placeholder so bundle is still usable
        out_file = bundle_path / "logs.json"
        with open(out_file, "w") as f:
            json.dump({"error": "OpenSearch unavailable", "logs": []}, f, indent=2)
        return False
    except Exception as e:
        print(f"    [-] Log capture failed: {e}")
        return False


def write_metadata(bundle_path: Path, bundle_name: str):
    """Write a metadata template — user fills in the fault details."""
    # Try to detect which fault is currently active from flagd
    flagd_file = Path(__file__).parent.parent / "src" / "flagd" / "demo.flagd.json"
    active_faults = {}
    try:
        with open(flagd_file) as f:
            flagd = json.load(f)
        for name, config in flagd["flags"].items():
            if config["defaultVariant"] != "off":
                active_faults[name] = config["defaultVariant"]
    except Exception:
        pass

    metadata = {
        "incident_id": bundle_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "active_faults_at_capture": active_faults,
        # --- Fill these in after capturing ---
        "fault_name": list(active_faults.keys())[0] if active_faults else "FILL_IN",
        "fault_variant": list(active_faults.values())[0] if active_faults else "FILL_IN",
        "affected_service": "FILL_IN",
        "fault_type": "application",  # or "infrastructure"
        "correct_diagnosis": "FILL_IN — describe what is failing and why",
        "correct_fix": {
            "action": "FILL_IN",  # toggle-flag | restart-service | throttle-load
            "parameters": {}
        },
        "notes": "FILL_IN — any observations about this incident",
        "endpoints": {
            "prometheus": PROMETHEUS_URL,
            "jaeger": JAEGER_URL,
            "opensearch": OPENSEARCH_URL,
        },
    }

    out_file = bundle_path / "metadata.json"
    with open(out_file, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"  -> Saved to {out_file}")
    return active_faults


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Capture a telemetry bundle snapshot from a live incident"
    )
    parser.add_argument(
        "--name", "-n",
        required=True,
        help="Bundle name, e.g. 'incident_01_payment_failure'"
    )
    parser.add_argument(
        "--skip-prometheus", action="store_true",
        help="Skip Prometheus capture"
    )
    parser.add_argument(
        "--skip-jaeger", action="store_true",
        help="Skip Jaeger capture"
    )
    parser.add_argument(
        "--skip-logs", action="store_true",
        help="Skip OpenSearch logs capture"
    )
    args = parser.parse_args()

    bundle_path = BUNDLES_DIR / args.name
    bundle_path.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Capturing bundle: {args.name}")
    print(f"  Output dir:       {bundle_path}")
    print(f"{'='*60}")

    # Check connectivity first
    print("\n  Checking connectivity...")
    for name, url in [("Prometheus", PROMETHEUS_URL), ("Jaeger", JAEGER_URL), ("OpenSearch", OPENSEARCH_URL)]:
        try:
            requests.get(url, timeout=3)
            print(f"    [+] {name} reachable at {url}")
        except Exception:
            print(f"    [-] {name} NOT reachable at {url}")

    # Capture each telemetry source
    if not args.skip_prometheus:
        capture_prometheus(bundle_path)

    if not args.skip_jaeger:
        capture_jaeger(bundle_path)

    if not args.skip_logs:
        capture_logs(bundle_path)

    # Write metadata
    print("\n  [*] Writing metadata...")
    active_faults = write_metadata(bundle_path, args.name)

    print(f"\n{'='*60}")
    print(f"  [+] Bundle captured: bundles/{args.name}/")
    print(f"     metrics.json  -- Prometheus snapshots")
    print(f"     traces.json   -- Jaeger traces")
    print(f"     logs.json     -- OpenSearch error logs")
    print(f"     metadata.json -- Ground truth (edit this!)")

    if active_faults:
        print(f"\n  Active faults detected at capture time:")
        for name, variant in active_faults.items():
            print(f"    {name} = {variant}")
        print(f"\n  [*] Open bundles/{args.name}/metadata.json")
        print(f"      Fill in: affected_service, correct_diagnosis, correct_fix")
    else:
        print(f"\n  [!] No active faults detected in flagd.")
        print(f"      Make sure you inject a fault BEFORE capturing.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
