"""Target-scope checks, allowlists and confirmations."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from faultctl.incidents import CONTROLLABLE_CONTAINERS, FLAG_CONTROL_PLANE, INCIDENTS

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Compose project names: lowercase letters, digits, dashes and underscores, starting alphanumeric.
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

INSPECTABLE_CONTAINERS = frozenset(
    set(CONTROLLABLE_CONTAINERS)
    | set(FLAG_CONTROL_PLANE)
    | {c for i in INCIDENTS.values() for c in i.requires}
)


class SafetyError(Exception):
    """Raised when an operation would leave the authorized local practice environment."""


def validate_base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise SafetyError(f"base URL must use http:// on a loopback host, got {url!r}")
    if parts.username or parts.password:
        raise SafetyError("base URL must not contain credentials")
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise SafetyError(
            f"refusing non-local target {parts.hostname!r}; only {sorted(LOOPBACK_HOSTS)} are allowed"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise SafetyError("base URL must be just scheme://host:port")
    return f"{parts.scheme}://{parts.netloc}"


def validate_project(project: str) -> str:
    if not _PROJECT_RE.match(project):
        raise SafetyError(f"invalid compose project name {project!r}")
    return project


def check_docker_host(env: Mapping[str, str]) -> None:
    """Refuse to run when the Docker CLI is pointed at a remote daemon."""
    host = env.get("DOCKER_HOST", "")
    if host:
        check_docker_endpoint(host)


def check_docker_endpoint(endpoint: str) -> None:
    if not endpoint.startswith(("npipe://", "unix://")):
        raise SafetyError(f"Docker endpoint {endpoint!r} is not a local daemon socket; refusing")


def require_controllable(name: str) -> str:
    if name not in CONTROLLABLE_CONTAINERS:
        raise SafetyError(f"container {name!r} is not on the faultctl allowlist")
    return name


def require_inspectable(name: str) -> str:
    if name not in INSPECTABLE_CONTAINERS:
        raise SafetyError(f"container {name!r} is not on the faultctl allowlist")
    return name


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def assert_owned(name: str, labels: Mapping[str, str], project: str, demo_dir: str | None) -> None:
    """Prove a container belongs to the configured compose project (and checkout, if given)."""
    actual = labels.get("com.docker.compose.project")
    if actual != project:
        raise SafetyError(
            f"container {name!r} belongs to compose project {actual!r}, not {project!r}; refusing"
        )
    if demo_dir:
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        if _norm_path(working_dir) != _norm_path(demo_dir):
            raise SafetyError(
                f"container {name!r} was started from {working_dir!r}, not the configured demo dir {demo_dir!r}"
            )


def confirm(prompt: str, assume_yes: bool, input_fn: Callable[[str], str] = input) -> bool:
    if assume_yes:
        return True
    try:
        answer = input_fn(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")
