"""Exercise all six API flows through a real loopback HTTP server."""

import json
import os
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import uvicorn

from main import create_app


class HTTPFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_token = secrets.token_urlsafe(32)
        cls.human_token = secrets.token_urlsafe(32)
        cls.env = patch.dict(os.environ, {
            "ACTION_AGENT_TOKEN": cls.agent_token,
            "ACTION_HUMAN_TOKEN": cls.human_token,
            "ACTION_ENABLE_LIVE": "false",
        })
        cls.env.start()
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp.name) / "http-test.sqlite3"
        cls.calls = []

        def simulated_executor(proposal):
            cls.calls.append(proposal.request_id)
            return {"message": "Simulated operation only; no Docker or flag changes"}

        app = create_app(cls.db_path, executor=simulated_executor)
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.base = f"http://127.0.0.1:{cls.sock.getsockname()[1]}"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        cls.thread = threading.Thread(target=lambda: cls.server.run(sockets=[cls.sock]), daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not cls.server.started:
            cls.tearDownClass()
            raise RuntimeError("Loopback test server failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.sock.close()
        cls.env.stop()
        cls.temp.cleanup()

    def request(self, method, path, body=None, human=False):
        token = self.human_token if human else self.agent_token
        request = Request(self.base + path, method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def proposal(self, dry_run=False):
        request_id = str(uuid.uuid4())
        code, action = self.request("POST", "/actions", {
            "request_id": request_id, "incident_id": "HTTP-TEST",
            "reason": "Synthetic API-flow test, not an actual incident diagnosis",
            "evidence": ["Synthetic test reference"], "kind": "restart",
            "parameters": {"service": "payment"}, "dry_run": dry_run,
        })
        self.assertEqual(code, 200, action)
        return action

    def approve(self, action):
        return self.request("POST", f"/actions/{action['id']}/decision",
                            {"approve": True, "reviewer": "Test human interface"}, human=True)

    def execute(self, action):
        return self.request("POST", f"/actions/{action['id']}/execute")

    def test_1_proposal_is_pending_and_saved(self):
        action = self.proposal()
        self.assertEqual(action["state"], "pending")
        code, saved = self.request("GET", f"/actions/{action['id']}")
        self.assertEqual(code, 200)
        self.assertEqual(saved["payload"]["parameters"], {"service": "payment"})
        self.assertNotIn(action["request_id"], self.calls)

    def test_2_approval_requires_human_credentials(self):
        action = self.proposal()
        self.assertEqual(self.execute(action)[0], 409)
        code, _ = self.request("POST", f"/actions/{action['id']}/decision",
                               {"approve": True, "reviewer": "Agent attempt"})
        self.assertEqual(code, 401)
        code, approved = self.approve(action)
        self.assertEqual(code, 200)
        self.assertEqual(approved["state"], "approved")

    def test_3_execution_dispatches_once_to_simulator(self):
        action = self.proposal()
        self.approve(action)
        code, result = self.execute(action)
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "completed")
        self.execute(action)
        self.assertEqual(self.calls.count(action["request_id"]), 1)

    def test_4_dry_run_never_dispatches(self):
        action = self.proposal(dry_run=True)
        self.approve(action)
        code, result = self.execute(action)
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "dry_run")
        self.assertNotIn(action["request_id"], self.calls)

    def test_5_kill_switch_blocks_approved_action(self):
        action = self.proposal()
        self.approve(action)
        self.assertEqual(self.request("POST", "/kill-switch", {"enabled": True})[0], 401)
        try:
            self.assertEqual(self.request("POST", "/kill-switch", {"enabled": True}, human=True)[0], 200)
            self.assertEqual(self.execute(action)[0], 423)
            self.assertNotIn(action["request_id"], self.calls)
        finally:
            self.request("POST", "/kill-switch", {"enabled": False}, human=True)

    def test_6_logging_records_requests_and_action_lifecycle_on_disk(self):
        action = self.proposal(dry_run=True)
        self.approve(action)
        self.execute(action)
        self.assertEqual(self.request("GET", "/audit")[0], 401)
        code, events = self.request("GET", "/audit", human=True)
        self.assertEqual(code, 200)
        lifecycle = {e["event"] for e in events if e["details"].get("id") == action["id"]}
        self.assertTrue({"proposed", "decision", "execution_started", "execution_finished"} <= lifecycle)
        requests = [e["details"] for e in events if e["event"] == "http_call"]
        self.assertTrue(any(e["status"] == 401 for e in requests))
        self.assertTrue(any(e["path"] == f"/actions/{action['id']}/execute" and e["status"] == 200
                            for e in requests))
        with closing(sqlite3.connect(self.db_path)) as db:
            state = db.execute("SELECT state FROM actions WHERE id=?", (action["id"],)).fetchone()[0]
            self.assertEqual(state, "dry_run")


if __name__ == "__main__":
    unittest.main()
