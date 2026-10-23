import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient

from main import create_app, disable_fault_flag, restart_service, start_service, set_load


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ACTION_AGENT_TOKEN": "test-agent", "ACTION_HUMAN_TOKEN": "test-human",
            "ACTION_ALLOWED_FLAGS": "paymentFailure,emailMemoryLeak",
            "ACTION_ENABLE_LIVE": "false",
            "ACTION_FLAG_BACKEND": "file",
        })
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.executor = lambda p: self.calls.append(p.kind) or {"message": "simulated"}
        self.client = TestClient(create_app(Path(self.temp.name) / "test.sqlite3", self.executor))
        self.client.__enter__()
        self.agent = {"Authorization": "Bearer test-agent"}
        self.human = {"Authorization": "Bearer test-human"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()
        self.env.stop()

    def propose(self, **changes):
        payload = dict(request_id="request-1", incident_id="incident-1", reason="Investigated failure",
                       evidence=["trace:real-reference"], kind="restart",
                       parameters={"service": "payment"}, dry_run=False)
        payload.update(changes)
        return self.client.post("/actions", json=payload, headers=self.agent)

    def approve(self, action_id):
        return self.client.post(f"/actions/{action_id}/decision", headers=self.human,
                                json={"approve": True, "reviewer": "Human operator"})

    def execute(self, action_id):
        return self.client.post(f"/actions/{action_id}/execute", headers=self.agent)

    def test_no_self_approval_or_unapproved_execution(self):
        action_id = self.propose().json()["id"]
        self.assertEqual(self.execute(action_id).status_code, 409)
        response = self.client.post(f"/actions/{action_id}/decision", headers=self.agent,
                                    json={"approve": True, "reviewer": "agent"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.calls, [])

    def test_approved_execution_is_idempotent(self):
        first = self.propose().json()
        self.assertEqual(self.propose().json()["id"], first["id"])
        self.approve(first["id"])
        self.assertEqual(self.execute(first["id"]).json()["state"], "completed")
        self.execute(first["id"])
        self.assertEqual(self.calls, ["restart"])
        self.assertEqual(self.propose(reason="Changed plan").status_code, 409)

    def test_dry_run_never_calls_adapter(self):
        action_id = self.propose(dry_run=True).json()["id"]
        self.approve(action_id)
        self.assertEqual(self.execute(action_id).json()["state"], "dry_run")
        self.assertEqual(self.calls, [])

    def test_kill_switch(self):
        action_id = self.propose().json()["id"]
        self.approve(action_id)
        self.client.post("/kill-switch", json={"enabled": True}, headers=self.human)
        self.assertEqual(self.execute(action_id).status_code, 423)
        self.assertEqual(self.calls, [])

    def test_validation_and_flag_privacy(self):
        for parameters in ({"service": "payment; whoami"}, {"service": []}):
            self.assertEqual(self.propose(parameters=parameters).status_code, 422)
        for kind, params in (("feature_flag", {"flag": "paymentFailure", "variant": "on"}),
                             ("feature_flag", {"flag": "notAllowed", "variant": "off"}),
                             ("load", {"users": 1000, "spawn_rate": 1}),
                             ("load", {"users": True, "spawn_rate": 1}),
                             ("scale", {"service": "payment", "replicas": 2})):
            self.assertEqual(self.propose(kind=kind, parameters=params).status_code, 422)
        self.assertEqual(self.client.get("/flags", headers=self.agent).status_code, 404)
        self.assertEqual(self.client.get("/audit", headers=self.agent).status_code, 401)

    def test_ordered_plan_stops_after_failure(self):
        steps = [{"kind": "feature_flag", "parameters": {"flag": "emailMemoryLeak", "variant": "off"}},
                 {"kind": "restart", "parameters": {"service": "email"}},
                 {"kind": "restart", "parameters": {"service": "payment"}}]
        def operation(p):
            self.calls.append(p.kind)
            if p.kind == "restart":
                raise RuntimeError("Docker unavailable")
            return {"message": "done"}
        with TestClient(create_app(Path(self.temp.name) / "plan.sqlite3", operation)) as client:
                old, self.client = self.client, client
                action_id = self.propose(kind="plan", parameters={"steps": steps}).json()["id"]
                self.approve(action_id)
                result = self.execute(action_id).json()
                self.client = old
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(result["result"]["completed_steps"]), 1)
        self.assertEqual(self.calls, ["feature_flag", "restart"])

    def test_approval_expires(self):
        action_id = self.propose().json()["id"]
        self.approve(action_id)
        with patch("main.time.time", return_value=9999999999):
            self.assertEqual(self.execute(action_id).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_kill_switch_stops_remaining_plan_steps(self):
        def operation(p):
            self.calls.append(p.kind)
            self.client.post("/kill-switch", json={"enabled": True}, headers=self.human)
            return {"message": "done"}
        with TestClient(create_app(Path(self.temp.name) / "kill-plan.sqlite3", operation)) as client:
            old, self.client = self.client, client
            try:
                steps = [{"kind": "restart", "parameters": {"service": name}}
                         for name in ("payment", "email")]
                action_id = self.propose(kind="plan", parameters={"steps": steps}).json()["id"]
                self.approve(action_id)
                result = self.execute(action_id).json()
                self.assertEqual(result["state"], "failed")
                self.assertEqual(self.calls, ["restart"])
            finally:
                self.client = old

    def test_switch_responds_during_running_action(self):
        started, finish = threading.Event(), threading.Event()
        def slow(p):
            started.set()
            if not finish.wait(5):
                raise RuntimeError("test timed out")
            return {"message": "done"}
        with TestClient(create_app(Path(self.temp.name) / "concurrent.sqlite3", slow)) as client:
            old, self.client = self.client, client
            action_id = self.propose().json()["id"]
            self.approve(action_id)
            worker = threading.Thread(target=lambda: self.execute(action_id))
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                response = client.post("/kill-switch", json={"enabled": True}, headers=self.human)
                self.assertEqual(response.status_code, 200)
            finally:
                finish.set()
                worker.join(5)
                self.client = old

    def test_live_disabled_by_default(self):
        with TestClient(create_app(Path(self.temp.name) / "live.sqlite3")) as client:
            old, self.client = self.client, client
            action_id = self.propose().json()["id"]
            self.approve(action_id)
            with patch("main.perform") as adapter:
                self.assertEqual(self.execute(action_id).json()["state"], "failed")
                adapter.assert_not_called()
            self.client = old

    def test_flag_adapter_only_changes_requested_flag(self):
        root = Path(self.temp.name)
        folder = root / "src" / "flagd"
        folder.mkdir(parents=True)
        path = folder / "demo.flagd.json"
        original = {"flags": {"paymentFailure": {"defaultVariant": "on", "variants": {"on": True, "off": False}},
                              "other": {"defaultVariant": "on"}}}
        path.write_text(json.dumps(original))
        with patch("main.ROOT", root):
            result = disable_fault_flag("paymentFailure")
        updated = json.loads(path.read_text())
        self.assertEqual(updated["flags"]["paymentFailure"]["defaultVariant"], "off")
        self.assertEqual(updated["flags"]["other"], original["flags"]["other"])
        self.assertNotIn("defaultVariant", json.dumps(result))

    def test_docker_uses_argument_array_and_locust_errors_propagate(self):
        with patch("main.subprocess.run", return_value=Mock(stdout="done")) as run:
            restart_service("payment")
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertEqual(run.call_args.args[0][-2:], ["restart", "payment"])
            start_service("astronomy-db")
            self.assertEqual(run.call_args.args[0][-2:], ["start", "astronomy-db"])
        with patch("main.urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"success":false}'
            with self.assertRaises(RuntimeError):
                set_load(2, 1)

    def handover(self):
        return {
            "request_id": "handover-1", "dry_run": False,
            "contract": {
                "incident_id": "INC-1", "status": "AWAITING_APPROVAL",
                "timestamp": "2026-10-23T15:05:00Z",
                "diagnosis": {
                    "culprit_service": "payment", "root_cause": "Payment charge calls fail",
                    "blast_radius": "Checkout users cannot pay",
                    "evidence": {"metric": "Checkout error rate 96.4%", "failing_trace_id": "trace-1"},
                },
                "remediation_plan": {
                    "summary": "Disable payment fault, then verify recovery",
                    "actions": [
                        {"step": 1, "action_type": "TOGGLE_FLAG", "target": "paymentFailure",
                         "parameters": {"desired_variant": "off", "api_endpoint": "http://localhost:8080/feature/api/write"},
                         "description": "Disable the payment fault"},
                        {"step": 2, "action_type": "VERIFY_RECOVERY", "target": "checkout",
                         "parameters": {"metric_query": "checkout_error_rate", "expected_value": 0,
                                        "evaluation_window_seconds": 30},
                         "description": "Check fresh checkout telemetry"},
                    ],
                },
            },
        }

    def test_handover_preserves_context_and_returns_verification_to_agent(self):
        payload = self.handover()
        response = self.client.post("/handovers", json=payload, headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        action = response.json()
        self.assertEqual(action["state"], "pending")
        self.assertEqual(action["payload"]["handover"]["diagnosis"], payload["contract"]["diagnosis"])
        self.assertEqual(self.execute(action["id"]).status_code, 409)
        self.approve(action["id"])
        result = self.execute(action["id"]).json()["result"]
        self.assertEqual(self.calls, ["feature_flag"])
        self.assertFalse(result["recovery_verified"])
        self.assertEqual(result["verification_required"][0]["target"], "checkout")
        duplicate = self.client.post("/handovers", json=payload, headers=self.agent).json()
        self.assertEqual(duplicate["id"], action["id"])

    def test_handover_cannot_import_approval_or_flag_answers(self):
        payload = self.handover()
        payload["contract"]["status"] = "APPROVED"
        self.assertEqual(self.client.post("/handovers", json=payload, headers=self.agent).status_code, 422)
        payload = self.handover()
        payload["contract"]["diagnosis"]["evidence"]["active_flag"] = {"current_variant": "100%"}
        self.assertEqual(self.client.post("/handovers", json=payload, headers=self.agent).status_code, 422)

    def test_handover_rejects_arbitrary_urls_and_misordered_steps(self):
        payload = self.handover()
        payload["contract"]["remediation_plan"]["actions"][0]["parameters"]["api_endpoint"] = "http://example.com/execute"
        self.assertEqual(self.client.post("/handovers", json=payload, headers=self.agent).status_code, 422)
        payload = self.handover()
        payload["contract"]["remediation_plan"]["actions"][0]["step"] = 7
        self.assertEqual(self.client.post("/handovers", json=payload, headers=self.agent).status_code, 422)

    def test_handover_start_dependency_and_reject_unknown_container(self):
        payload = self.handover()
        step = payload["contract"]["remediation_plan"]["actions"][0]
        step.update(action_type="START_CONTAINER", target="valkey-cart", parameters={})
        response = self.client.post("/handovers", json=payload, headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        self.approve(response.json()["id"])
        self.execute(response.json()["id"])
        self.assertEqual(self.calls, ["start"])
        payload["request_id"] = "handover-2"
        step["target"] = "unrelated-container"
        self.assertEqual(self.client.post("/handovers", json=payload, headers=self.agent).status_code, 422)

    def test_http_flag_adapter_uses_actual_controller_payload(self):
        root = Path(self.temp.name)
        folder = root / "src" / "flagd"
        folder.mkdir(parents=True)
        path = folder / "demo.flagd.json"
        path.write_text(json.dumps({"flags": {
            "paymentFailure": {"defaultVariant": "on", "variants": {"off": False, "on": True}},
            "other": {"defaultVariant": "on"},
        }}))
        with patch("main.ROOT", root), patch.dict(os.environ, {"ACTION_FLAG_BACKEND": "http"}), patch("main.urlopen") as call:
            call.return_value.__enter__.return_value.read.return_value = b'{}'
            disable_fault_flag("paymentFailure")
            request = call.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8080/feature/api/write")
            data = json.loads(request.data)["data"]
            self.assertEqual(data["flags"]["paymentFailure"]["defaultVariant"], "off")
            self.assertEqual(data["flags"]["other"]["defaultVariant"], "on")
        self.assertEqual(json.loads(path.read_text())["flags"]["paymentFailure"]["defaultVariant"], "on")

    def test_every_handled_http_call_is_logged(self):
        self.client.get("/health")
        self.client.get("/capabilities")
        self.client.post("/handovers", headers=self.agent, json={})
        events = self.client.get("/audit", headers=self.human).json()
        calls = [e["details"] for e in events if e["event"] == "http_call"]
        self.assertTrue(any(c["path"] == "/health" and c["status"] == 200 for c in calls))
        self.assertTrue(any(c["status"] == 401 for c in calls))
        self.assertTrue(any(c["path"] == "/handovers" and c["status"] == 422 for c in calls))


if __name__ == "__main__":
    unittest.main()
