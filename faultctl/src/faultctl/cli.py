"""Command-line entry point for faultctl."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from faultctl import __version__, safety
from faultctl.adapters import DockerAdapter, FlagdEvaluator, FlagdUiAdapter, HttpClient
from faultctl.incidents import FLAG, INCIDENTS
from faultctl.operations import EXIT_ABORTED, EXIT_CODES, Controller, Result, Status, exit_code

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_PROJECT = "opentelemetry-demo"

EPILOG = """\
examples:
  faultctl list
  faultctl show payment-failure
  faultctl inject payment-failure --variant 100% --dry-run
  faultctl inject payment-failure --variant 100%
  faultctl inject cart-failure cache-unavailable
  faultctl inject --all --dry-run
  faultctl inject --all --yes
  faultctl status
  faultctl reset payment-failure
  faultctl reset-all --yes

exit codes:
  0 success (verified) or dry run   1 failed            2 invalid usage/incident
  3 refused by safety checks        4 dependency unavailable
  5 outcome unknown (timeout)       6 applied but not verified
  8 partial failure (reset-all)     9 aborted at confirmation

environment:
  FAULTCTL_BASE_URL         frontend proxy URL (default http://localhost:8080; loopback only)
  FAULTCTL_COMPOSE_PROJECT  compose project to operate on (default opentelemetry-demo)
  FAULTCTL_DEMO_DIR         optional; require containers to come from this checkout
"""

ControllerFactory = Callable[[argparse.Namespace], Controller]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faultctl",
        description="Inject and reset practice incidents in the local OpenTelemetry Demo.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"faultctl {__version__}")
    parser.add_argument("--base-url", default=os.environ.get("FAULTCTL_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--project", default=os.environ.get("FAULTCTL_COMPOSE_PROJECT", DEFAULT_PROJECT))
    parser.add_argument("--demo-dir", default=os.environ.get("FAULTCTL_DEMO_DIR"))
    parser.add_argument("--verify-timeout", type=float, default=15.0, help="seconds to wait for flagd to confirm a flag change")
    parser.add_argument("-v", "--verbose", action="store_true", help="log HTTP and docker calls to stderr")

    mutating = argparse.ArgumentParser(add_help=False)
    mutating.add_argument("--dry-run", action="store_true", help="show planned actions without changing anything")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("list", help="list supported incidents")
    p.add_argument("--no-probe", action="store_true", help="do not check Docker/flagd-ui for availability")

    p = sub.add_parser("show", help="show full documentation for one incident")
    p.add_argument("incident")

    p = sub.add_parser("inject", parents=[mutating], help="inject one or more practice incidents")
    p.add_argument("incidents", nargs="*", metavar="INCIDENT")
    p.add_argument("--all", action="store_true", help="inject every incident that is currently available")
    p.add_argument("--variant", help="flag variant to inject; only with a single INCIDENT")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt for --all")

    p = sub.add_parser("status", help="show demo service health")
    p.add_argument("--show-active", action="store_true", help="operator view of active faults (never give this to the agent)")

    p = sub.add_parser("reset", parents=[mutating], help="reset one incident to baseline")
    p.add_argument("incident")

    p = sub.add_parser("reset-all", parents=[mutating], help="reset every faultctl-managed incident")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def default_controller(args: argparse.Namespace) -> Controller:
    http = HttpClient()
    return Controller(
        flagd_ui=FlagdUiAdapter(args.base_url, http),
        evaluator=FlagdEvaluator(args.base_url, http),
        docker=DockerAdapter(),
        http=http,
        project=args.project,
        demo_dir=args.demo_dir,
        flag_verify_timeout=args.verify_timeout,
    )


def _print_result(r: Result, out: TextIO) -> None:
    out.write(f"[{r.status.value.upper()}] {r.action} {r.incident}: {r.message}\n")
    for step in r.steps:
        out.write(f"    {step}\n")


def _cmd_list(args, controller_factory, out) -> int:
    avail: dict[str, str] = {}
    if not args.no_probe:
        avail = controller_factory(args).availability()
    out.write(f"{'INCIDENT':<32} {'MECHANISM':<15} {'TARGET':<32} AVAILABLE\n")
    for inc in INCIDENTS.values():
        out.write(f"{inc.id:<32} {inc.mechanism:<15} {inc.target:<32} {avail.get(inc.id, 'not probed')}\n")
        out.write(f"{'':<32} {inc.summary}\n")
    return 0


def _cmd_show(args, out) -> int:
    inc = INCIDENTS.get(args.incident)
    if inc is None:
        out.write(f"[INVALID] unknown incident {args.incident!r}; run 'faultctl list'\n")
        return EXIT_CODES[Status.INVALID]
    rows = [("id", inc.id), ("summary", inc.summary), ("target", inc.target), ("mechanism", inc.mechanism)]
    if inc.mechanism == FLAG:
        rows += [
            ("flag", inc.flag + (" (targeting rule)" if inc.targeting_rule else "")),
            ("variants", ", ".join(inc.variants) + f" (default {inc.default_variant})"),
            ("inject", "flagd-ui POST /feature/api/write, changing only this flag"),
            ("reset", "set the flag back to 'off' through the same API (idempotent)"),
        ]
    else:
        rows += [
            ("container", inc.container),
            ("inject", f"docker stop {inc.container}"),
            ("reset", f"docker start {inc.container} (idempotent; never removes containers or volumes)"),
        ]
    if inc.requires:
        rows.append(("requires", ", ".join(inc.requires)))
    rows += [("effects", inc.effects), ("verification", inc.verification)]
    if inc.limitations:
        rows.append(("limitations", inc.limitations))
    for k, v in rows:
        out.write(f"{k + ':':<14} {v}\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    controller_factory: ControllerFactory = default_controller,
    out: TextIO | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    out = out or sys.stdout
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        safety.validate_base_url(args.base_url)
        safety.validate_project(args.project)
    except safety.SafetyError as e:
        out.write(f"[REFUSED] {e}\n")
        return EXIT_CODES[Status.REFUSED]

    if args.command == "show":
        return _cmd_show(args, out)
    if args.command == "list":
        return _cmd_list(args, controller_factory, out)

    if args.command == "inject":
        if bool(args.incidents) == bool(args.all):
            out.write("[INVALID] give one or more INCIDENT ids, or --all (not both)\n")
            return EXIT_CODES[Status.INVALID]
        if args.variant and len(args.incidents) != 1:
            out.write("[INVALID] --variant needs exactly one INCIDENT\n")
            return EXIT_CODES[Status.INVALID]

    if args.command == "reset-all" and not args.dry_run:
        prompt = f"Reset ALL {len(INCIDENTS)} faultctl-managed incidents in compose project {args.project!r}?"
        if not safety.confirm(prompt, args.yes, input_fn):
            out.write("[ABORTED] reset-all not confirmed; nothing was changed\n")
            return EXIT_ABORTED

    controller = controller_factory(args)

    if args.command == "status":
        for line in controller.status(show_active=args.show_active):
            out.write(line + "\n")
        return 0

    if args.command == "inject" and args.all:
        avail = controller.availability()
        ids = [i for i, a in avail.items() if a == "yes"]
        for i, a in avail.items():
            if a != "yes":
                out.write(f"[SKIPPED] inject {i}: {a}\n")
        if not ids:
            out.write("[UNAVAILABLE] no incidents are currently available; is the demo running?\n")
            return EXIT_CODES[Status.UNAVAILABLE]
        if not args.dry_run:
            prompt = (
                f"Inject {len(ids)} incidents at once into compose project {args.project!r}? "
                "This loads the machine heavily; undo with 'faultctl reset-all'."
            )
            if not safety.confirm(prompt, args.yes, input_fn):
                out.write("[ABORTED] inject --all not confirmed; nothing was changed\n")
                return EXIT_ABORTED
        results = controller.inject_many(ids, dry_run=args.dry_run)
    elif args.command == "inject" and len(args.incidents) > 1:
        results = controller.inject_many(args.incidents, dry_run=args.dry_run)
    elif args.command == "inject":
        results = [controller.inject(args.incidents[0], args.variant, dry_run=args.dry_run)]
    elif args.command == "reset":
        results = [controller.reset(args.incident, dry_run=args.dry_run)]
    else:
        results = controller.reset_all(dry_run=args.dry_run)

    if getattr(args, "dry_run", False):
        out.write("DRY RUN - planned actions only, no state was changed\n")
    for r in results:
        _print_result(r, out)
    code = exit_code(results)
    if len(results) > 1 and not args.dry_run:
        bad = [r for r in results if EXIT_CODES[r.status] != 0]
        state = "injected" if args.command == "inject" else "at baseline"
        out.write(f"{len(results) - len(bad)}/{len(results)} incidents {state}" + (f"; {len(bad)} need attention\n" if bad else "\n"))
    return code


if __name__ == "__main__":
    sys.exit(main())
