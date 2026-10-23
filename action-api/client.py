"""HTTP integration for the incident agent. Human approval is intentionally separate."""

import json
from urllib.request import Request, urlopen


class ActionClient:
    def __init__(self, base_url, agent_token):
        self.base_url = base_url.rstrip("/")
        self.token = agent_token

    def _request(self, method, path, body=None):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=120) as response:
            return json.load(response)

    def propose(self, *, request_id, incident_id, reason, evidence, kind, parameters,
                dry_run=True):
        return self._request("POST", "/actions", {
            "request_id": request_id, "incident_id": incident_id, "reason": reason,
            "evidence": evidence, "kind": kind, "parameters": parameters,
            "dry_run": dry_run,
        })

    def restart_service(self, service, **context):
        return self.propose(kind="restart", parameters={"service": service}, **context)

    def start_service(self, service, **context):
        return self.propose(kind="start", parameters={"service": service}, **context)

    def submit_handover(self, contract, *, request_id, dry_run=True):
        return self._request("POST", "/handovers", {
            "request_id": request_id, "dry_run": dry_run, "contract": contract,
        })

    def disable_fault_flag(self, flag, **context):
        return self.propose(kind="feature_flag", parameters={"flag": flag, "variant": "off"},
                            **context)

    def set_load(self, users, spawn_rate, **context):
        return self.propose(kind="load", parameters={"users": users, "spawn_rate": spawn_rate},
                            **context)

    def propose_plan(self, steps, **context):
        return self.propose(kind="plan", parameters={"steps": steps}, **context)

    def status(self, action_id):
        return self._request("GET", f"/actions/{action_id}")

    def execute(self, action_id):
        return self._request("POST", f"/actions/{action_id}/execute")
