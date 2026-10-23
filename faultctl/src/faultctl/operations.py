"""Inject, reset, status and list logic, independent of argument parsing."""

from __future__ import annotations

import copy
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from faultctl import safety
from faultctl.adapters import (
    AdapterError,
    AdapterTimeout,
    AdapterUnavailable,
    ContainerInfo,
    DockerAdapter,
    FlagdEvaluator,
    FlagdUiAdapter,
    HttpClient,
)
from faultctl.incidents import (
    CONTAINER_STOP,
    FLAG,
    FLAG_CONTROL_PLANE,
    INCIDENTS,
    OFF,
    Incident,
    get_incident,
)


class Status(str, Enum):
    OK = "ok"
    PLANNED = "planned"
    UNVERIFIED = "applied-unverified"
    FAILED = "failed"
    UNKNOWN = "outcome-unknown"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


EXIT_CODES = {
    Status.OK: 0,
    Status.PLANNED: 0,
    Status.FAILED: 1,
    Status.INVALID: 2,
    Status.REFUSED: 3,
    Status.UNAVAILABLE: 4,
    Status.UNKNOWN: 5,
    Status.UNVERIFIED: 6,
}
EXIT_PARTIAL = 8
EXIT_ABORTED = 9


@dataclass
class Result:
    incident: str
    action: str
    status: Status
    message: str
    steps: list[str] = field(default_factory=list)


def exit_code(results: list[Result]) -> int:
    codes = [EXIT_CODES[r.status] for r in results]
    if all(c == 0 for c in codes):
        return 0
    if len(codes) > 1 and any(c == 0 for c in codes):
        return EXIT_PARTIAL
    return next(c for c in codes if c != 0)


def _flag_kind(flagdef: Mapping[str, Any], variant: str) -> str:
    return "boolean" if isinstance(flagdef["variants"][variant], bool) else "number"


def _stored_at(flagdef: Mapping[str, Any], inc: Incident, variant: str) -> bool:
    if inc.targeting_rule:
        rule_ok = flagdef["targeting"]["if"][1] == variant
        return rule_ok and (variant != OFF or flagdef.get("defaultVariant") == OFF)
    return flagdef.get("defaultVariant") == variant


def _set_variant(flagdef: dict[str, Any], inc: Incident, variant: str) -> None:
    if inc.targeting_rule:
        flagdef["targeting"]["if"][1] = variant
        if variant == OFF:
            flagdef["defaultVariant"] = OFF
    else:
        flagdef["defaultVariant"] = variant


def _flag_problem(flags: Mapping[str, Any], inc: Incident, variant: str) -> str | None:
    flagdef = flags.get(inc.flag)
    if not isinstance(flagdef, dict):
        return f"flag {inc.flag!r} is not defined in this demo's flagd configuration"
    if variant not in (flagdef.get("variants") or {}):
        return f"flag {inc.flag!r} has no variant {variant!r} in this demo"
    if inc.targeting_rule:
        rule = (flagdef.get("targeting") or {}).get("if")
        if not (isinstance(rule, list) and len(rule) == 3):
            return f"flag {inc.flag!r} has an unexpected targeting rule; refusing to edit it"
    return None


class Controller:
    def __init__(
        self,
        flagd_ui: FlagdUiAdapter,
        evaluator: FlagdEvaluator,
        docker: DockerAdapter,
        http: HttpClient,
        project: str,
        demo_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        flag_verify_timeout: float = 15.0,
        container_verify_timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> None:
        self.flagd_ui = flagd_ui
        self.evaluator = evaluator
        self.docker = docker
        self.http = http
        self.project = safety.validate_project(project)
        self.demo_dir = demo_dir
        self.env = os.environ if env is None else env
        self.sleep = sleep
        self.clock = clock
        self.flag_verify_timeout = flag_verify_timeout
        self.container_verify_timeout = container_verify_timeout
        self.poll_interval = poll_interval

    # ---- public operations -------------------------------------------------

    def inject(self, incident_id: str, variant: str | None = None, dry_run: bool = False) -> Result:
        inc = get_incident(incident_id)
        if inc is None:
            return _unknown_incident(incident_id, "inject")
        if inc.mechanism == CONTAINER_STOP and variant:
            return Result(inc.id, "inject", Status.INVALID, f"{inc.id} has no variants")
        return self._guard(inc.id, "inject", lambda: self._inject(inc, variant, dry_run))

    def inject_many(self, incident_ids: Iterable[str], dry_run: bool = False) -> list[Result]:
        """Inject several incidents at their default variants; flag changes go in one write."""
        ids = list(dict.fromkeys(incident_ids))
        results: dict[str, Result] = {}
        flag_incs: list[Incident] = []
        for iid in ids:
            inc = get_incident(iid)
            if inc is None:
                results[iid] = _unknown_incident(iid, "inject")
            elif inc.mechanism == FLAG:
                flag_incs.append(inc)

        if flag_incs:
            try:
                self._preflight(FLAG_CONTROL_PLANE)
                ready = []
                for inc in flag_incs:
                    try:
                        self._preflight(inc.requires)
                    except Exception as e:  # noqa: BLE001 - converted to a per-incident result
                        status, msg = _classify(e)
                        results[inc.id] = Result(inc.id, "inject", status, msg)
                    else:
                        ready.append(inc)
                for r in self._apply_flags([(i, i.default_variant) for i in ready], "inject", dry_run):
                    results[r.incident] = r
            except Exception as e:  # noqa: BLE001 - converted to per-incident results
                status, msg = _classify(e)
                for inc in flag_incs:
                    results.setdefault(inc.id, Result(inc.id, "inject", status, msg))

        for iid in ids:
            inc = get_incident(iid)
            if inc is not None and inc.mechanism == CONTAINER_STOP:
                results[iid] = self._guard(iid, "inject", lambda inc=inc: self._inject_container(inc, dry_run))
        return [results[i] for i in ids]

    def reset(self, incident_id: str, dry_run: bool = False) -> Result:
        inc = get_incident(incident_id)
        if inc is None:
            return _unknown_incident(incident_id, "reset")
        if inc.mechanism == FLAG:
            return self._guard(inc.id, "reset", lambda: self._reset_flags([inc], dry_run)[0])
        return self._guard(inc.id, "reset", lambda: self._reset_container(inc, dry_run))

    def reset_all(self, dry_run: bool = False) -> list[Result]:
        flag_incs = [i for i in INCIDENTS.values() if i.mechanism == FLAG]
        try:
            results = self._reset_flags(flag_incs, dry_run)
        except Exception as e:  # noqa: BLE001 - converted to per-incident results
            status, msg = _classify(e)
            results = [Result(i.id, "reset", status, msg) for i in flag_incs]
        for inc in INCIDENTS.values():
            if inc.mechanism == CONTAINER_STOP:
                results.append(self._guard(inc.id, "reset", lambda inc=inc: self._reset_container(inc, dry_run)))
        return results

    def status(self, show_active: bool = False) -> list[str]:
        lines: list[str] = []
        try:
            safety.check_docker_host(self.env)
            self.docker.ping()
            rows = self.docker.list_project(self.project)
            lines.append(f"Docker: reachable; compose project {self.project!r} has {len(rows)} containers")
            for row in sorted(rows, key=lambda r: r.get("Names", "")):
                lines.append(f"  {row.get('Names', '?'):<22} {row.get('State', '?'):<10} {row.get('Status', '')}")
            if not rows:
                lines.append("  (no containers - is the demo running? see README)")
        except safety.SafetyError as e:
            lines.append(f"Docker: REFUSED - {e}")
        except AdapterError as e:
            lines.append(f"Docker: unavailable - {e}")

        for label, url in (
            ("Frontend (via proxy)", f"{self.flagd_ui.base_url}/"),
            ("flagd-ui API", f"{self.flagd_ui.base_url}/feature/api/read"),
        ):
            try:
                lines.append(f"{label}: HTTP {self.http.probe(url, timeout=5)} from {url}")
            except AdapterError as e:
                lines.append(f"{label}: unreachable - {e}")

        if not show_active:
            lines.append(
                "Active faults: hidden. Use --show-active for the operator view "
                "(never feed that output to the diagnosis agent)."
            )
            return lines

        lines.append("Active faults (OPERATOR VIEW - do not give this to the agent):")
        try:
            flags = self.flagd_ui.read_flags()
            for inc in INCIDENTS.values():
                if inc.mechanism != FLAG or _flag_problem(flags, inc, OFF):
                    continue
                if not _stored_at(flags[inc.flag], inc, OFF):
                    lines.append(f"  {inc.id}: {inc.flag} is not at 'off'")
        except AdapterError as e:
            lines.append(f"  flag state unavailable - {e}")
        for inc in INCIDENTS.values():
            if inc.mechanism != CONTAINER_STOP:
                continue
            try:
                info = self.docker.inspect(inc.container)
            except AdapterError as e:
                lines.append(f"  {inc.id}: container state unavailable - {e}")
                continue
            if info is not None and not info.running:
                lines.append(f"  {inc.id}: {inc.container} is {info.status}")
        return lines

    def availability(self) -> dict[str, str]:
        """Probe which incidents can currently be injected. Read-only."""
        result: dict[str, str] = {}
        try:
            safety.check_docker_host(self.env)
            self.docker.ping()
            states = {r.get("Names"): r.get("State") for r in self.docker.list_project(self.project)}
            docker_err = None
        except (safety.SafetyError, AdapterError) as e:
            states, docker_err = {}, str(e)
        try:
            flags = self.flagd_ui.read_flags()
            flags_err = None
        except AdapterError as e:
            flags, flags_err = {}, str(e)

        for inc in INCIDENTS.values():
            if docker_err:
                result[inc.id] = "unknown (docker unavailable)"
                continue
            needed = list(inc.requires) + ([inc.container] if inc.container else list(FLAG_CONTROL_PLANE))
            missing = [n for n in needed if n not in states]
            if missing:
                result[inc.id] = f"no ({', '.join(missing)} not in project)"
            elif inc.mechanism == FLAG:
                down = [n for n in needed if states.get(n) != "running"]
                if down:
                    result[inc.id] = f"no ({', '.join(down)} not running)"
                elif flags_err:
                    result[inc.id] = "unknown (flagd-ui unreachable)"
                else:
                    problem = _flag_problem(flags, inc, inc.default_variant or "")
                    result[inc.id] = f"no ({problem})" if problem else "yes"
            else:
                result[inc.id] = "yes"
        return result

    # ---- internals ------------------------------------------------------------

    def _guard(self, incident_id: str, action: str, fn: Callable[[], Result]) -> Result:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - every failure becomes an explicit result
            status, msg = _classify(e)
            return Result(incident_id, action, status, msg)

    def _preflight(self, names: Iterable[str], must_run: bool = True) -> dict[str, ContainerInfo]:
        safety.check_docker_host(self.env)
        self.docker.ping()
        infos: dict[str, ContainerInfo] = {}
        for name in names:
            info = self.docker.inspect(name)
            if info is None:
                raise AdapterUnavailable(f"container {name!r} not found - is the demo running with the right compose files?")
            safety.assert_owned(name, info.labels, self.project, self.demo_dir)
            if must_run and not info.running:
                raise AdapterUnavailable(f"container {name!r} is {info.status}, expected running")
            infos[name] = info
        return infos

    def _inject(self, inc: Incident, variant: str | None, dry_run: bool) -> Result:
        if inc.mechanism == FLAG:
            variant = variant or inc.default_variant
            if variant not in inc.variants:
                return Result(
                    inc.id, "inject", Status.INVALID,
                    f"variant {variant!r} is not allowed for {inc.id}; allowed: {', '.join(inc.variants)}",
                )
            self._preflight((*FLAG_CONTROL_PLANE, *inc.requires))
            return self._apply_flags([(inc, variant)], "inject", dry_run)[0]
        return self._inject_container(inc, dry_run)

    def _reset_flags(self, incs: list[Incident], dry_run: bool) -> list[Result]:
        self._preflight(FLAG_CONTROL_PLANE)
        return self._apply_flags([(i, OFF) for i in incs], "reset", dry_run)

    def _apply_flags(self, changes: list[tuple[Incident, str]], action: str, dry_run: bool) -> list[Result]:
        flags = self.flagd_ui.read_flags()
        results: dict[str, Result] = {}
        valid: list[tuple[Incident, str]] = []
        for inc, variant in changes:
            problem = _flag_problem(flags, inc, variant)
            if problem:
                results[inc.id] = Result(inc.id, action, Status.UNAVAILABLE, problem)
            else:
                valid.append((inc, variant))

        pending = [(i, v) for i, v in valid if not _stored_at(flags[i.flag], i, v)]
        pending_ids = {i.id for i, _ in pending}

        if dry_run:
            for inc, variant in valid:
                steps = [
                    f"PLAN: set flag {inc.flag} to variant {variant!r} via POST /feature/api/write"
                    + (" (targeting rule)" if inc.targeting_rule else "")
                    if inc.id in pending_ids
                    else f"PLAN: no write needed, {inc.flag} is already at {variant!r}",
                    f"PLAN: verify flagd evaluates {inc.flag} to {variant!r}",
                ]
                results[inc.id] = Result(inc.id, action, Status.PLANNED, "dry run - nothing was changed", steps)
            return [results[i.id] for i, _ in changes]

        if pending:
            new_flags = copy.deepcopy(flags)
            for inc, variant in pending:
                _set_variant(new_flags[inc.flag], inc, variant)
            try:
                self.flagd_ui.write_flags(new_flags)
            except AdapterTimeout as e:
                for inc, _ in pending:
                    results[inc.id] = Result(inc.id, action, Status.UNKNOWN, f"write timed out; flag state unknown ({e})")
            except AdapterError as e:
                for inc, _ in pending:
                    results[inc.id] = Result(inc.id, action, Status.FAILED, f"flagd-ui rejected the write: {e}")
            else:
                for inc, variant in self._unconfirmed_writes(pending):
                    results[inc.id] = Result(
                        inc.id, action, Status.UNKNOWN,
                        f"write accepted but flagd-ui does not show {inc.flag}={variant!r}",
                    )

        to_eval = [(i, v) for i, v in valid if i.id not in results]
        for inc, variant, status, msg in self._verify_eval(to_eval, flags):
            verb = "already" if inc.id not in pending_ids else ("injected" if action == "inject" else "reset")
            if status is Status.OK:
                if verb == "already":
                    msg = f"{inc.flag} was already at {variant!r} (no change made); {msg}"
                else:
                    msg = f"{verb}: {inc.flag}={variant!r}; {msg}"
            results[inc.id] = Result(inc.id, action, status, msg)
        return [results[i.id] for i, _ in changes]

    def _unconfirmed_writes(self, pending: list[tuple[Incident, str]]) -> list[tuple[Incident, str]]:
        remaining = list(pending)
        for _ in range(5):
            try:
                flags = self.flagd_ui.read_flags()
            except AdapterError:
                flags = None
            if flags is not None:
                remaining = [(i, v) for i, v in remaining if not _stored_at(flags[i.flag], i, v)]
            if not remaining:
                return []
            self.sleep(0.3)
        return remaining

    def _verify_eval(self, items: list[tuple[Incident, str]], flags: Mapping[str, Any]):
        deadline = self.clock() + self.flag_verify_timeout
        remaining = list(items)
        last_seen: dict[str, Any] = {}
        while remaining:
            still = []
            for inc, variant in remaining:
                try:
                    seen = self.evaluator.resolve_variant(inc.flag, _flag_kind(flags[inc.flag], variant), inc.context)
                except AdapterError as e:
                    yield inc, variant, Status.UNVERIFIED, f"stored in flagd-ui, but flagd evaluation could not be checked: {e}"
                    continue
                if seen == variant:
                    yield inc, variant, Status.OK, f"flagd confirms variant {variant!r}"
                else:
                    last_seen[inc.id] = seen
                    still.append((inc, variant))
            remaining = still
            if remaining and self.clock() < deadline:
                self.sleep(self.poll_interval)
            elif remaining:
                break
        for inc, variant in remaining:
            yield inc, variant, Status.UNVERIFIED, (
                f"stored in flagd-ui, but after {self.flag_verify_timeout:.0f}s flagd still evaluates "
                f"{inc.flag} to {last_seen.get(inc.id)!r}"
            )

    def _inject_container(self, inc: Incident, dry_run: bool) -> Result:
        name = inc.container
        info = self._preflight([name], must_run=False)[name]
        if dry_run:
            steps = (
                [f"PLAN: docker stop --time 10 {name}", f"PLAN: verify {name} is no longer running"]
                if info.running
                else [f"PLAN: no action, {name} is already {info.status}"]
            )
            return Result(inc.id, "inject", Status.PLANNED, "dry run - nothing was changed", steps)
        if not info.running:
            return Result(inc.id, "inject", Status.OK, f"{name} was already {info.status} (no change made)")
        try:
            self.docker.stop(name)
        except AdapterTimeout as e:
            return Result(inc.id, "inject", Status.UNKNOWN, f"docker stop timed out; state unknown ({e})")
        except AdapterError as e:
            return Result(inc.id, "inject", Status.FAILED, f"docker stop failed: {e}")
        try:
            after = self.docker.inspect(name)
        except AdapterError as e:
            return Result(inc.id, "inject", Status.UNVERIFIED, f"docker stop succeeded but state could not be checked: {e}")
        if after is not None and not after.running:
            return Result(inc.id, "inject", Status.OK, f"injected: {name} is now {after.status}")
        return Result(inc.id, "inject", Status.FAILED, f"{name} is still running after docker stop")

    def _reset_container(self, inc: Incident, dry_run: bool) -> Result:
        name = inc.container
        info = self._preflight([name], must_run=False)[name]
        healthy = info.running and info.health in (None, "healthy")
        if dry_run:
            steps = (
                [f"PLAN: no action, {name} is already running"]
                if info.running
                else [f"PLAN: docker start {name}", f"PLAN: verify {name} is running and healthy"]
            )
            return Result(inc.id, "reset", Status.PLANNED, "dry run - nothing was changed", steps)
        if healthy:
            return Result(inc.id, "reset", Status.OK, f"{name} was already running (no change made)")
        if not info.running:
            try:
                self.docker.start(name)
            except AdapterTimeout as e:
                return Result(inc.id, "reset", Status.UNKNOWN, f"docker start timed out; state unknown ({e})")
            except AdapterError as e:
                return Result(inc.id, "reset", Status.FAILED, f"docker start failed: {e}")
        deadline = self.clock() + self.container_verify_timeout
        while True:
            try:
                now = self.docker.inspect(name)
            except AdapterError as e:
                return Result(inc.id, "reset", Status.UNVERIFIED, f"docker start issued but state could not be checked: {e}")
            if now is not None and now.running and now.health in (None, "healthy"):
                return Result(inc.id, "reset", Status.OK, f"reset: {name} is running" + (" and healthy" if now.health else ""))
            if self.clock() >= deadline:
                break
            self.sleep(self.poll_interval)
        if now is not None and now.running:
            return Result(inc.id, "reset", Status.UNVERIFIED, f"{name} is running but health is {now.health!r}")
        return Result(inc.id, "reset", Status.FAILED, f"{name} is {now.status if now else 'missing'} after docker start")


def _unknown_incident(incident_id: str, action: str) -> Result:
    return Result(
        incident_id, action, Status.INVALID,
        f"unknown incident {incident_id!r}; run 'faultctl list' to see supported incidents",
    )


def _classify(e: Exception) -> tuple[Status, str]:
    if isinstance(e, safety.SafetyError):
        return Status.REFUSED, f"refused: {e}"
    if isinstance(e, AdapterTimeout):
        return Status.UNAVAILABLE, f"timed out before any change was made: {e}"
    if isinstance(e, AdapterUnavailable):
        return Status.UNAVAILABLE, str(e)
    if isinstance(e, AdapterError):
        return Status.FAILED, str(e)
    return Status.FAILED, f"unexpected error: {type(e).__name__}: {e}"
