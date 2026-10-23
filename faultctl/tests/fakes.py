"""In-memory fakes for the external interfaces. Nothing here touches Docker or the network."""

from __future__ import annotations

import copy

from faultctl import safety
from faultctl.adapters import AdapterError, AdapterTimeout, AdapterUnavailable, ContainerInfo
from faultctl.incidents import FLAG_CONTROL_PLANE, INCIDENTS
from faultctl.operations import Controller

PROJECT = "opentelemetry-demo"


def demo_flags() -> dict:
    flags = {}
    for inc in INCIDENTS.values():
        if not inc.flag:
            continue
        variants = {v: (True if v == "on" else 1.0) for v in inc.variants}
        variants["off"] = False if "on" in inc.variants else 0
        flag = {"defaultVariant": "off", "description": inc.summary, "state": "ENABLED", "variants": variants}
        if inc.targeting_rule:
            flag["targeting"] = {"if": [{"==": [{"var": "product_id"}, "OLJCESPC7Z"]}, "off", "off"]}
        flags[inc.flag] = flag
    return flags


class FakeFlagdUi:
    base_url = "http://localhost:8080"

    def __init__(self, flags=None):
        self.flags = demo_flags() if flags is None else flags
        self.writes = 0
        self.read_exc = None
        self.write_exc = None
        self.drop_writes = False

    def read_flags(self):
        if self.read_exc:
            raise self.read_exc
        return copy.deepcopy(self.flags)

    def write_flags(self, flags):
        self.writes += 1
        if self.write_exc:
            raise self.write_exc
        if not self.drop_writes:
            self.flags = copy.deepcopy(flags)


class FakeEvaluator:
    """Evaluates like flagd would, from the fake flagd-ui state."""

    def __init__(self, ui: FakeFlagdUi):
        self.ui = ui
        self.exc = None
        self.stale = False

    def resolve_variant(self, flag, kind, context):
        if self.exc:
            raise self.exc
        f = self.ui.flags[flag]
        if self.stale:
            return "off"
        if "targeting" in f and context.get("product_id") == "OLJCESPC7Z":
            return f["targeting"]["if"][1]
        return f["defaultVariant"]


class FakeDocker:
    def __init__(self, project=PROJECT, names=None):
        self.ping_exc = None
        self.stop_exc = None
        self.start_exc = None
        self.calls: list[tuple[str, str]] = []
        names = names if names is not None else (
            set(FLAG_CONTROL_PLANE) | {i.container for i in INCIDENTS.values() if i.container} | {"load-generator"}
        )
        self.containers = {
            n: ContainerInfo(n, "running", None, {"com.docker.compose.project": project}) for n in names
        }

    def ping(self):
        if self.ping_exc:
            raise self.ping_exc

    def inspect(self, name):
        safety.require_inspectable(name)
        info = self.containers.get(name)
        return copy.deepcopy(info) if info else None

    def list_project(self, project):
        return [
            {"Names": n, "State": c.status, "Status": c.status}
            for n, c in self.containers.items()
            if c.labels.get("com.docker.compose.project") == project
        ]

    def stop(self, name, grace_seconds=10):
        safety.require_controllable(name)
        self.calls.append(("stop", name))
        if self.stop_exc:
            raise self.stop_exc
        self.containers[name].status = "exited"

    def start(self, name):
        safety.require_controllable(name)
        self.calls.append(("start", name))
        if self.start_exc:
            raise self.start_exc
        self.containers[name].status = "running"


class FakeHttp:
    def __init__(self, code=200):
        self.code = code

    def probe(self, url, timeout):
        return self.code


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_controller(ui=None, docker=None, evaluator=None, env=None, demo_dir=None):
    ui = ui or FakeFlagdUi()
    docker = docker or FakeDocker()
    clock = FakeClock()
    return Controller(
        flagd_ui=ui,
        evaluator=evaluator or FakeEvaluator(ui),
        docker=docker,
        http=FakeHttp(),
        project=PROJECT,
        demo_dir=demo_dir,
        env=env or {},
        sleep=clock.sleep,
        clock=clock,
        flag_verify_timeout=5,
        container_verify_timeout=5,
        poll_interval=1,
    )


__all__ = [
    "AdapterError",
    "AdapterTimeout",
    "AdapterUnavailable",
    "FakeDocker",
    "FakeEvaluator",
    "FakeFlagdUi",
    "make_controller",
]
