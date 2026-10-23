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
from tools import ReadOnlyTools
from integration import RemediationClient

INCIDENT_FILE = Path(__file__).resolve().parent / "latest_incident.json"


def display_incident_card(contract: dict):
    """Prints a clear, high-impact SRE incident card to the terminal."""
    diag = contract.get("diagnosis", {})
    plan = contract.get("remediation_plan", {})
    ev = diag.get("evidence", {})

    print("\n" + "=" * 70)
    print(f"[!] SRE INCIDENT REPORT: {contract.get('incident_id')}")
    print("=" * 70)
    print(f"  - Target Service : {diag.get('culprit_service', 'unknown').upper()}")
    print(f"  - Root Cause     : {diag.get('root_cause')}")
    print(f"  - Blast Radius   : {diag.get('blast_radius')}")
    print("-" * 70)
    print("[*] FORENSIC EVIDENCE:")
    print(json.dumps(ev, indent=2))
    print("-" * 70)
    print("[+] PROPOSED REMEDIATION PLAN (HANDOVER CONTRACT):")
    print(f"  - Summary: {plan.get('summary')}")
    for act in plan.get("actions", []):
        print(f"    [Step {act.get('step')}] ({act.get('action_type')}) -> {act.get('description')}")
    print("=" * 70)
    print(f"[+] Full JSON contract saved to: {INCIDENT_FILE}")
    print("=" * 70 + "\n")


def handle_incident(evidence_bundle: dict, collector=None, submit=False):
    collector = collector or EvidenceCollector()
    tools = ReadOnlyTools(collector)
    contract = RCAgent().analyze(evidence_bundle, tools=tools)
    with open(INCIDENT_FILE, "w", encoding="utf-8") as handle:
        json.dump(contract, handle, indent=2)
    if contract.get("status") == "ESCALATED":
        print("Escalated to human: " + contract["reason"])
        return contract
    display_incident_card(contract)
    if submit:
        proposal = RemediationClient().submit(contract)
        print(f"Action API proposal {proposal['id']}: {proposal['state']}; waiting for human approval")
    return contract


def main():
    parser = argparse.ArgumentParser(description="Autonomous On-Call Detection & RCA Pipeline")
    parser.add_argument("--monitor", action="store_true", help="Start continuous 15s watchdog monitoring loop")
    parser.add_argument("--test", type=str, metavar="SERVICE", help="Immediately run evidence collection & RCA on a target service (e.g. checkout, payment)")
    parser.add_argument("--submit", action="store_true", help="Submit proposals to Action API; never auto-approve or auto-execute")
    args = parser.parse_args()

    config.display()

    if args.test:
        print(f"[*] Running on-demand investigation test for service: '{args.test}'...")
        collector = EvidenceCollector()
        bundle = collector.collect_evidence(service=args.test, symptom="HIGH_ERROR_RATE")
        handle_incident(bundle, collector, submit=args.submit)
    elif args.monitor:
        watchdog = Watchdog(on_incident_detected=lambda bundle: handle_incident(bundle, submit=args.submit))
        watchdog.monitor_loop()
    else:
        # Default behavior: run single scan and show options
        print("[*] Performing single system health scan...")
        watchdog = Watchdog(on_incident_detected=lambda bundle: handle_incident(bundle, submit=args.submit))
        anomaly = watchdog.scan_once()
        for warning in getattr(watchdog, "monitoring_warnings", []):
            print("[Telemetry warning] " + warning)
        if anomaly:
            print(f"[!] Active anomaly found: {anomaly}")
            collector = EvidenceCollector()
            bundle = collector.collect_evidence(service=anomaly["service"], symptom=anomaly["symptom"])
            handle_incident(bundle, collector, submit=args.submit)
        else:
            print("[+] No configured threshold breach detected; missing telemetry is not proof of health")
            print("\nUsage Tips:")
            print("  python main.py --monitor        (Start continuous 15s SRE monitoring loop)")
            print("  python main.py --test checkout  (Simulate/trigger triage on a service)\n")


if __name__ == "__main__":
    main()
