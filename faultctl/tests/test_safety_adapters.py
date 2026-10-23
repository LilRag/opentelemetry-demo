import json
import subprocess
import unittest

from faultctl import safety
from faultctl.adapters import AdapterTimeout, AdapterUnavailable, DockerAdapter, FlagdUiAdapter, HttpClient


class SafetyTests(unittest.TestCase):
    def test_base_url_allows_loopback_only(self):
        self.assertEqual(safety.validate_base_url("http://localhost:8080"), "http://localhost:8080")
        self.assertEqual(safety.validate_base_url("http://127.0.0.1:8080/"), "http://127.0.0.1:8080")
        for bad in (
            "http://10.0.0.1:8080",
            "https://localhost:8080",
            "http://user:pw@localhost:8080",
            "http://localhost.evil.com:8080",
            "http://localhost:8080/feature",
            "file:///etc/passwd",
        ):
            with self.assertRaises(safety.SafetyError, msg=bad):
                safety.validate_base_url(bad)

    def test_container_allowlist(self):
        safety.require_controllable("valkey-cart")
        for bad in ("flagd", "otel-collector", "postgres; rm -rf /", "--all"):
            with self.assertRaises(safety.SafetyError):
                safety.require_controllable(bad)

    def test_docker_host(self):
        safety.check_docker_host({})
        safety.check_docker_host({"DOCKER_HOST": "npipe:////./pipe/docker_engine"})
        with self.assertRaises(safety.SafetyError):
            safety.check_docker_host({"DOCKER_HOST": "ssh://user@remote"})

    def test_confirm(self):
        self.assertTrue(safety.confirm("?", True, lambda _: "n"))
        self.assertTrue(safety.confirm("?", False, lambda _: " Y "))
        self.assertFalse(safety.confirm("?", False, lambda _: ""))

        def eof(_):
            raise EOFError

        self.assertFalse(safety.confirm("?", False, eof))


class RecordingRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        resp = self.responses.get(argv[1], (0, "", ""))
        if isinstance(resp, Exception):
            raise resp
        code, out, err = resp
        return subprocess.CompletedProcess(argv, code, out, err)


class DockerAdapterTests(unittest.TestCase):
    def test_stop_refuses_non_allowlisted_before_exec(self):
        runner = RecordingRunner({})
        with self.assertRaises(safety.SafetyError):
            DockerAdapter(runner).stop("otel-collector")
        self.assertEqual(runner.calls, [])

    def test_stop_argv_is_fixed(self):
        runner = RecordingRunner({})
        DockerAdapter(runner).stop("currency")
        self.assertEqual(runner.calls, [["docker", "stop", "--time", "10", "currency"]])

    def test_inspect_parses_and_handles_missing(self):
        payload = json.dumps([{"State": {"Status": "running", "Health": {"Status": "healthy"}},
                               "Config": {"Labels": {"com.docker.compose.project": "opentelemetry-demo"}}}])
        info = DockerAdapter(RecordingRunner({"inspect": (0, payload, "")})).inspect("astronomy-db")
        self.assertTrue(info.running)
        self.assertEqual(info.health, "healthy")
        missing = DockerAdapter(RecordingRunner({"inspect": (1, "", "Error: No such container: currency")}))
        self.assertIsNone(missing.inspect("currency"))

    def test_docker_not_installed(self):
        with self.assertRaises(AdapterUnavailable):
            DockerAdapter(RecordingRunner({"version": FileNotFoundError()})).ping()

    def test_timeout(self):
        runner = RecordingRunner({"stop": subprocess.TimeoutExpired("docker", 40)})
        with self.assertRaises(AdapterTimeout):
            DockerAdapter(runner).stop("currency")

    def test_remote_context_refused(self):
        runner = RecordingRunner({"version": (0, "29.0", ""), "context": (0, "tcp://1.2.3.4:2376\n", "")})
        with self.assertRaises(safety.SafetyError):
            DockerAdapter(runner).ping()


class FlagdUiAdapterTests(unittest.TestCase):
    def test_rejects_remote_url(self):
        with self.assertRaises(safety.SafetyError):
            FlagdUiAdapter("http://192.168.1.10:8080", HttpClient())

    def test_unreachable_is_unavailable(self):
        # Windows may time out rather than refuse a connection to a closed port.
        adapter = FlagdUiAdapter("http://127.0.0.1:9", HttpClient(), timeout=1)
        with self.assertRaises((AdapterUnavailable, AdapterTimeout)):
            adapter.read_flags()


if __name__ == "__main__":
    unittest.main()
