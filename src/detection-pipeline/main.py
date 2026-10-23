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
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

FAULT_SERVICE_MAP = {
    "payment-failure": "payment",
    "payment-unreachable": "checkout",
    "cart-failure": "cart",
    "product-catalog-failure": "product-catalog",
    "product-catalog-lock-contention": "product-catalog",
    "recommendation-cache-leak": "recommendation",
    "ad-high-cpu": "ad",
    "ad-manual-gc": "ad",
    "ad-failure": "ad",
    "email-memory-leak": "email",
    "homepage-flood": "frontend",
    "image-slow-load": "frontend",
    "intl-shipping-slowdown": "shipping",
    "kafka-queue-problems": "fraud-detection",
    "kafka-consumer-dead": "fraud-detection",
    "cache-unavailable": "cart",
    "database-unavailable": "product-catalog",
    "currency-down": "currency"
}


def run_faultctl(*cmd_args) -> int:
    """Invokes the faultctl CLI submodule."""
    import subprocess
    faultctl_src = PROJECT_ROOT / "faultctl" / "src"
    cmd = [sys.executable, "-m", "faultctl", *cmd_args]
    res = subprocess.run(cmd, cwd=str(faultctl_src))
    return res.returncode


def display_incident_card(contract: dict):
    """Prints a clear, high-impact SRE incident card to the terminal."""
    diag = contract.get("diagnosis", {})
    plan = contract.get("remediation_plan", {})
    ev = diag.get("evidence", {})

    print("\n" + "=" * 70)
    print(f"[!] SRE INCIDENT REPORT: {contract.get('incident_id')}")
    print("=" * 70)
    print(f"  - Target Service : {diag.get('culprit_service', 'unknown').upper()}")
    print(f"  - Status         : {contract.get('status', 'AWAITING_APPROVAL')}")
    print(f"  - Root Cause     : {diag.get('root_cause')}")
    print(f"  - Blast Radius   : {diag.get('blast_radius')}")
    print("-" * 70)
    print("[*] FORENSIC EVIDENCE:")
    print(f"  - Metrics Proof  : {ev.get('metric_proof')}")
    print(f"  - Trace ID       : {ev.get('failing_trace_id')}")
    print(f"  - Failing Span   : {ev.get('failing_span')}")
    print(f"  - Log Excerpt    : {ev.get('log_excerpt')}")
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
    dispatch_to_action_api(contract)


def dispatch_to_action_api(contract: dict):
    """Submits handover contract directly to the action-api gateway if running."""
    import os
    import requests
    api_url = os.getenv("ACTION_API_URL", "http://127.0.0.1:8020")
    agent_token = os.getenv("ACTION_AGENT_TOKEN", "test-agent-token")
    try:
        r = requests.get(f"{api_url}/health", timeout=1)
        if r.status_code == 200:
            sub = {
                "request_id": f"req-{contract.get('incident_id')}-{int(time.time())}",
                "dry_run": False,
                "contract": contract
            }
            headers = {"Authorization": f"Bearer {agent_token}", "Content-Type": "application/json"}
            post_r = requests.post(f"{api_url}/handovers", json=sub, headers=headers, timeout=5)
            if post_r.status_code in [200, 201]:
                print(f"[+] Dispatched handover to Action API gateway (Action ID: {post_r.json().get('id')}).")
            else:
                print(f"[-] Action API returned {post_r.status_code}: {post_r.text}")
    except Exception:
        # Action API not online; file-based handover is primary
        pass


def main():
    parser = argparse.ArgumentParser(description="Autonomous On-Call Detection & RCA Pipeline")
    parser.add_argument("--monitor", action="store_true", help="Start continuous 15s watchdog monitoring loop")
    parser.add_argument("--test", type=str, metavar="SERVICE", help="Immediately run evidence collection & RCA on a target service")
    parser.add_argument("--inject-fault", type=str, metavar="FAULT_ID", help="Inject practice incident via faultctl, then run detection pipeline")
    parser.add_argument("--reset-fault", type=str, metavar="FAULT_ID", help="Reset a specific fault via faultctl")
    parser.add_argument("--reset-all", action="store_true", help="Reset all active faults via faultctl")
    parser.add_argument("--list-faults", action="store_true", help="List all cataloged practice faults in faultctl")
    parser.add_argument("--auto-reset", action="store_true", help="Automatically reset injected fault after detection test finishes")
    parser.add_argument("--bundle", type=str, metavar="BUNDLE", help="Replay and perform RCA on an offline telemetry bundle from bundles/")
    parser.add_argument("--capture-bundle", type=str, metavar="NAME", help="Snapshot active live incident telemetry into bundles/<NAME>")
    args = parser.parse_args()

    config.display()

    if args.capture_bundle:
        import subprocess
        script = PROJECT_ROOT / "scripts" / "capture_bundle.py"
        print(f"[*] Capturing live incident telemetry into: bundles/{args.capture_bundle}...")
        subprocess.run([sys.executable, str(script), "--name", args.capture_bundle], cwd=str(PROJECT_ROOT))
        return

    if args.bundle:
        collector = EvidenceCollector()
        bundle = collector.collect_from_bundle(args.bundle)
        handle_incident(bundle)
        return

    if args.list_faults:
        run_faultctl("list")
        return

    if args.reset_all:
        print("[*] Resetting all active practice incidents...")
        run_faultctl("reset-all", "--yes")
        return

    if args.reset_fault:
        print(f"[*] Resetting incident '{args.reset_fault}'...")
        run_faultctl("reset", args.reset_fault)
        return

    if args.inject_fault:
        fault_id = args.inject_fault
        target_service = FAULT_SERVICE_MAP.get(fault_id, "unknown")
        print(f"[*] Injecting incident '{fault_id}' via faultctl...")
        rc = run_faultctl("inject", fault_id)
        if rc != 0:
            print(f"[-] Fault injection failed (exit code: {rc}). Aborting detection triage.")
            return

        print("\n[*] Waiting 6 seconds for telemetry & error rates to propagate...")
        time.sleep(6)

        print(f"[*] Running autonomous Detection & RCA for service: '{target_service}'...")
        collector = EvidenceCollector()
        bundle = collector.collect_evidence(service=target_service, symptom="HIGH_ERROR_RATE")
        handle_incident(bundle)

        if args.auto_reset:
            print(f"[*] Auto-reset enabled. Resetting incident '{fault_id}'...")
            run_faultctl("reset", fault_id)
        else:
            print(f"[TIP] When finished, reset this fault by running: python main.py --reset-fault {fault_id}")
        return

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
            print("  python main.py --inject-fault payment-failure   (Inject fault with faultctl & trigger triage)")
            print("  python main.py --inject-fault cache-unavailable (Inject cache down & trigger triage)")
            print("  python main.py --reset-all                      (Reset all active faults)")
            print("  python main.py --list-faults                    (Show all available practice faults)")
            print("  python main.py --monitor                        (Start continuous 15s SRE monitoring loop)")
            print("  python main.py --test payment                   (Investigate a specific service)\n")


if __name__ == "__main__":
    main()
