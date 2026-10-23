# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import sys
import time
from pathlib import Path

from config import config
from evidence_collector import EvidenceCollector
from watchdog import Watchdog
from rca_agent import RCAgent

INCIDENT_FILE = Path(__file__).resolve().parent / "latest_incident.json"


def display_incident_card(contract: dict):
    """Prints a clear, high-impact SRE incident card to the terminal."""
    diag = contract.get("diagnosis", {})
    plan = contract.get("remediation_plan", {})
    ev = diag.get("evidence_summary", {})

    print("\n" + "=" * 70)
    print(f"[!] SRE INCIDENT REPORT: {contract.get('incident_id')}")
    print("=" * 70)
    print(f"  - Target Service : {diag.get('culprit_service', 'unknown').upper()}")
    print(f"  - Fault Type     : {diag.get('fault_type', 'UNKNOWN')}")
    print(f"  - Confidence     : {float(diag.get('confidence_score', 0)) * 100:.0f}%")
    print(f"  - Root Cause     : {diag.get('root_cause')}")
    print(f"  - Blast Radius   : {diag.get('blast_radius')}")
    print("-" * 70)
    print("[*] FORENSIC EVIDENCE:")
    print(f"  - Metrics Proof  : {ev.get('metric_proof')}")
    print(f"  - Trace ID       : {ev.get('failing_trace_id')}")
    print(f"  - Failing Span   : {ev.get('failing_span')}")
    print(f"  - Log Excerpt    : {ev.get('log_excerpt')}")
    print(f"  - Flag / State   : {ev.get('flag_or_infra_state')}")
    print("-" * 70)
    print("[+] PROPOSED REMEDIATION PLAN (HANDOVER CONTRACT):")
    print(f"  - Summary: {plan.get('summary')}")
    for act in plan.get("actions", []):
        print(f"    [Step {act.get('step')}] ({act.get('action_type')}) -> {act.get('description')}")
    print("=" * 70)
    print(f"[+] Full JSON contract saved to: {INCIDENT_FILE}")
    print("=" * 70 + "\n")


def handle_incident(evidence_bundle: dict):
    """Orchestrates RCA inference and contract saving when an incident is detected."""
    agent = RCAgent()
    contract = agent.analyze(evidence_bundle)

    # Save to JSON file for teammate's remediation pipeline
    with open(INCIDENT_FILE, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2)

    display_incident_card(contract)


def main():
    parser = argparse.ArgumentParser(description="Autonomous On-Call Detection & RCA Pipeline")
    parser.add_argument("--monitor", action="store_true", help="Start continuous 15s watchdog monitoring loop")
    parser.add_argument("--test", type=str, metavar="SERVICE", help="Immediately run evidence collection & RCA on a target service (e.g. checkout, payment)")
    args = parser.parse_args()

    config.display()

    if args.test:
        print(f"[*] Running on-demand investigation test for service: '{args.test}'...")
        collector = EvidenceCollector()
        bundle = collector.collect_evidence(service=args.test, symptom="HIGH_ERROR_RATE")
        handle_incident(bundle)
    elif args.monitor:
        watchdog = Watchdog(on_incident_detected=handle_incident)
        watchdog.monitor_loop()
    else:
        # Default behavior: run single scan and show options
        print("[*] Performing single system health scan...")
        watchdog = Watchdog(on_incident_detected=handle_incident)
        anomaly = watchdog.scan_once()
        if anomaly:
            print(f"[!] Active anomaly found: {anomaly}")
            collector = EvidenceCollector()
            bundle = collector.collect_evidence(service=anomaly["service"], symptom=anomaly["symptom"])
            handle_incident(bundle)
        else:
            print("[+] All services are currently healthy! (0% errors, normal latency)")
            print("\nUsage Tips:")
            print("  python main.py --monitor        (Start continuous 15s SRE monitoring loop)")
            print("  python main.py --test checkout  (Simulate/trigger triage on a service)\n")


if __name__ == "__main__":
    main()
