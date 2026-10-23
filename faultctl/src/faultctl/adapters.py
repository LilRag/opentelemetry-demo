"""Integrations with the demo's documented control interfaces.

- flagd-ui REST API (/feature/api/read, /feature/api/write), documented in
  opentelemetry-demo/src/flagd-ui/README.md.
- flagd evaluation API (flagd.evaluation.v1.Service, Connect JSON), exposed by the
  frontend proxy under /flagservice/. Used only to verify injections and resets.
- The Docker CLI, restricted to inspect/ps/stop/start on allowlisted containers.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from faultctl import safety

log = logging.getLogger(__name__)

FLAGD_SCHEMA = "https://flagd.dev/schema/v0/flags.json"


class AdapterError(Exception):
    """The operation failed."""


class AdapterTimeout(AdapterError):
    """The operation did not complete in time; its outcome is unknown."""


class AdapterUnavailable(AdapterError):
    """A required dependency (Docker, flagd-ui, flagd) could not be reached."""


class HttpClient:
    def __init__(self) -> None:
        # Never route loopback control traffic through an environment-configured proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request_json(self, method: str, url: str, body: Any, timeout: float) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        log.debug("HTTP %s %s", method, url)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            raise AdapterError(f"HTTP {e.code} from {url}: {detail}") from e
        except (socket.timeout, TimeoutError) as e:
            raise AdapterTimeout(f"timed out after {timeout}s: {method} {url}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise AdapterTimeout(f"timed out after {timeout}s: {method} {url}") from e
            raise AdapterUnavailable(f"cannot reach {url}: {e.reason}") from e
        except ConnectionError as e:
            raise AdapterUnavailable(f"cannot reach {url}: {e}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as e:
            raise AdapterError(f"non-JSON response from {url}") from e

    def probe(self, url: str, timeout: float) -> int:
        """Return the HTTP status of a GET, raising AdapterUnavailable if unreachable."""
        try:
            with self._opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except (socket.timeout, TimeoutError) as e:
            raise AdapterTimeout(f"timed out after {timeout}s: GET {url}") from e
        except (urllib.error.URLError, ConnectionError) as e:
            raise AdapterUnavailable(f"cannot reach {url}: {getattr(e, 'reason', e)}") from e


class FlagdUiAdapter:
    def __init__(self, base_url: str, http: HttpClient, timeout: float = 5.0) -> None:
        self.base_url = safety.validate_base_url(base_url)
        self.http = http
        self.timeout = timeout

    def read_flags(self) -> dict[str, Any]:
        body = self.http.request_json("GET", f"{self.base_url}/feature/api/read", None, self.timeout)
        if not isinstance(body, dict) or not isinstance(body.get("flags"), dict):
            raise AdapterError("flagd-ui /api/read returned an unexpected document")
        return body["flags"]

    def write_flags(self, flags: dict[str, Any]) -> None:
        # The write endpoint replaces the whole flag file, so callers must pass the
        # complete, freshly read flag set with only their intended change applied.
        payload = {"data": {"$schema": FLAGD_SCHEMA, "flags": flags}}
        self.http.request_json("POST", f"{self.base_url}/feature/api/write", payload, self.timeout)


class FlagdEvaluator:
    def __init__(self, base_url: str, http: HttpClient, timeout: float = 5.0) -> None:
        self.base_url = safety.validate_base_url(base_url)
        self.http = http
        self.timeout = timeout

    def resolve_variant(self, flag: str, kind: str, context: dict[str, str]) -> str | None:
        method = "ResolveBoolean" if kind == "boolean" else "ResolveFloat"
        url = f"{self.base_url}/flagservice/flagd.evaluation.v1.Service/{method}"
        body = self.http.request_json("POST", url, {"flagKey": flag, "context": context}, self.timeout)
        if not isinstance(body, dict):
            raise AdapterError(f"unexpected flagd evaluation response for {flag}")
        return body.get("variant")


@dataclass
class ContainerInfo:
    name: str
    status: str
    health: str | None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.status == "running"


Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]


def _subprocess_runner(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)


class DockerAdapter:
    def __init__(self, runner: Runner = _subprocess_runner, timeout: float = 30.0) -> None:
        self._runner = runner
        self.timeout = timeout

    def _run(self, args: Sequence[str], timeout: float | None = None) -> str:
        argv = ["docker", *args]
        log.debug("exec %s", argv)
        try:
            proc = self._runner(argv, timeout or self.timeout)
        except FileNotFoundError as e:
            raise AdapterUnavailable("docker CLI not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise AdapterTimeout(f"timed out: {' '.join(argv)}") from e
        if proc.returncode != 0:
            raise AdapterError((proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}")
        return proc.stdout

    def ping(self) -> None:
        try:
            self._run(["version", "--format", "{{.Server.Version}}"], timeout=15)
        except AdapterTimeout:
            raise
        except AdapterError as e:
            raise AdapterUnavailable(f"Docker daemon is not reachable: {e}") from e
        endpoint = self._run(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], timeout=15)
        safety.check_docker_endpoint(endpoint.strip())

    def inspect(self, name: str) -> ContainerInfo | None:
        safety.require_inspectable(name)
        try:
            out = self._run(["inspect", "--type", "container", name])
        except AdapterTimeout:
            raise
        except AdapterError as e:
            if "no such" in str(e).lower():
                return None
            raise
        try:
            data = json.loads(out)[0]
            state = data["State"]
            return ContainerInfo(
                name=name,
                status=state["Status"],
                health=(state.get("Health") or {}).get("Status"),
                labels=data["Config"].get("Labels") or {},
            )
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise AdapterError(f"unexpected docker inspect output for {name}") from e

    def list_project(self, project: str) -> list[dict[str, str]]:
        safety.validate_project(project)
        out = self._run(
            ["ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{json .}}"]
        )
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError as e:
                    raise AdapterError("unexpected docker ps output") from e
        return rows

    def stop(self, name: str, grace_seconds: int = 10) -> None:
        safety.require_controllable(name)
        self._run(["stop", "--time", str(grace_seconds), name], timeout=grace_seconds + 30)

    def start(self, name: str) -> None:
        safety.require_controllable(name)
        self._run(["start", name])
