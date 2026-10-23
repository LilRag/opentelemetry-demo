"""Local incident action gateway. Run one worker on the Docker host."""

import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from handover import HandoverContract, HandoverSubmission, to_proposal

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"ad", "cart", "checkout", "currency", "email", "payment",
            "product-catalog", "recommendation", "shipping", "quote",
            "frontend", "accounting", "fraud-detection"}
SERVICES.update({"image-provider", "agent"})
START_SERVICES = SERVICES | {"astronomy-db", "valkey-cart"}
FLAGS = {
    "paymentFailure", "paymentUnreachable", "cartFailure", "productCatalogFailure",
    "adFailure", "recommendationCacheFailure", "failedReadinessProbe",
    "emailMemoryLeak", "adHighCpu", "adManualGc", "productCatalogLockContention",
    "loadGeneratorFloodHomepage", "intlShippingSlowdown", "imageSlowLoad",
    "kafkaConsumerDead", "kafkaQueueProblems", "aiRunawayAgent", "aiSlowResponse",
    "cartMetricCardinalityOverflow", "emitRawPii",
}


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    incident_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=20)
    kind: Literal["start", "restart", "feature_flag", "load", "scale", "plan"]
    parameters: dict
    dry_run: bool = True
    handover: HandoverContract | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approve: bool
    reviewer: str = Field(min_length=1, max_length=100)


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def create_app(db_path=None, executor=None):
    LOCK = threading.RLock()
    execution_lock = threading.Lock()
    agent_token = os.environ.get("ACTION_AGENT_TOKEN", "")
    human_token = os.environ.get("ACTION_HUMAN_TOKEN", "")
    if not agent_token or not human_token or agent_token == human_token:
        raise RuntimeError("Set distinct ACTION_AGENT_TOKEN and ACTION_HUMAN_TOKEN")
    db = sqlite3.connect(str(db_path or Path(__file__).with_name("actions.sqlite3")),
                         check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS actions (
          id TEXT PRIMARY KEY, request_id TEXT UNIQUE, payload TEXT,
          state TEXT, created REAL, result TEXT);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, timestamp REAL, event TEXT, details TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT OR IGNORE INTO settings VALUES ('killed', 'false');
    """)
    db.execute("UPDATE actions SET state='unknown', result=? WHERE state='running'",
               (json.dumps({"message": "Process interrupted; inspect system before retrying"}),))
    db.commit()
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            db.close()

    app = FastAPI(title="Incident Action API", version="0.1.0", lifespan=lifespan)

    def audit(event, details):
        db.execute("INSERT INTO audit(timestamp,event,details) VALUES(?,?,?)",
                   (time.time(), event, json.dumps(details)))
        db.commit()

    @app.middleware("http")
    async def log_call(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            with LOCK:
                audit("http_call", {"method": request.method, "path": request.url.path, "status": 500})
            raise
        with LOCK:
            audit("http_call", {"method": request.method, "path": request.url.path,
                                "status": response.status_code})
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=422, content={"detail": "Invalid request schema; see /docs"})

    def auth(role):
        def check(authorization: str = Header(default="")):
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            expected = human_token if role == "human" else agent_token
            if not secrets.compare_digest(token, expected):
                with LOCK:
                    audit("unauthorized", {"role": role})
                raise HTTPException(401, "Invalid credentials")
        return check

    agent = Depends(auth("agent"))
    human = Depends(auth("human"))

    def get_action(action_id):
        row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown action")
        return {**dict(row), "payload": json.loads(row["payload"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    def validate(p):
        params = p.parameters
        if p.kind in {"restart", "start"}:
            allowed_services = START_SERVICES if p.kind == "start" else SERVICES
            if set(params) != {"service"} or not isinstance(params["service"], str) or params["service"] not in allowed_services:
                raise ValueError("Choose an allowlisted service for this operation")
        elif p.kind == "feature_flag":
            allowed = set(os.environ.get("ACTION_ALLOWED_FLAGS", "").split(",")) & FLAGS
            if set(params) != {"flag", "variant"} or not isinstance(params["flag"], str) or params["flag"] not in allowed:
                raise ValueError("Flag is not explicitly allowlisted by the operator")
            if params["variant"] != "off":
                raise ValueError("Only the remediation variant 'off' is allowed")
        elif p.kind == "load":
            if set(params) != {"users", "spawn_rate"}:
                raise ValueError("load requires users and spawn_rate")
            if type(params["users"]) is not int or not 0 <= params["users"] <= 100:
                raise ValueError("users must be an integer from 0 to 100")
            if type(params["spawn_rate"]) not in (float, int) or not 0 < params["spawn_rate"] <= 10:
                raise ValueError("spawn_rate must be greater than 0 and at most 10")
        elif p.kind == "plan":
            steps = params.get("steps")
            if set(params) != {"steps"} or not isinstance(steps, list) or not 1 <= len(steps) <= 5:
                raise ValueError("A plan requires 1 to 5 ordered steps")
            for step in steps:
                if not isinstance(step, dict) or set(step) != {"kind", "parameters"} or not isinstance(step["kind"], str) or step["kind"] not in {"start", "restart", "feature_flag", "load"} or not isinstance(step["parameters"], dict):
                    raise ValueError("Invalid plan step")
                validate(p.model_copy(update=step))
        else:
            raise ValueError("Kubernetes scaling is unsupported by this Compose adapter")

    @app.get("/health")
    def health():
        return {"status": "ok", "backend": "docker-compose"}

    @app.get("/capabilities", dependencies=[agent])
    def capabilities():
        return {"actions": ["start", "restart", "feature_flag", "load", "plan"],
                "services": sorted(SERVICES), "scale": "unsupported",
                "start_services": sorted(START_SERVICES),
                "handover_endpoint": "/handovers", "verification_owner": "incident-agent",
                "default_dry_run": True, "approval_required": True,
                "feature_flags": sorted(set(os.environ.get("ACTION_ALLOWED_FLAGS", "").split(",")) & FLAGS),
                "flag_variants": ["off"], "load_limits": {"users": [0, 100], "spawn_rate": [0.001, 10]}}

    @app.post("/handovers", dependencies=[agent])
    def submit_handover(submission: HandoverSubmission):
        return propose(Proposal(**to_proposal(submission)))

    @app.post("/actions", dependencies=[agent])
    def propose(p: Proposal):
        with LOCK:
            payload = p.model_dump_json()
            previous = db.execute("SELECT id,payload FROM actions WHERE request_id=?",
                                  (p.request_id,)).fetchone()
            if previous:
                previous_payload = json.loads(previous["payload"])
                previous_payload.setdefault("handover", None)
                if previous_payload != json.loads(payload):
                    audit("request_id_conflict", {"request_id": p.request_id})
                    raise HTTPException(409, "request_id already used for another proposal")
                audit("duplicate", {"id": previous["id"]})
                return get_action(previous["id"])
            try:
                validate(p)
            except (ValueError, TypeError) as exc:
                audit("rejected", {"proposal": p.model_dump(mode="json"), "error": str(exc)})
                raise HTTPException(422, str(exc)) from exc
            action_id = str(uuid.uuid4())
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,NULL)",
                       (action_id, p.request_id, payload, "pending", time.time()))
            audit("proposed", {"id": action_id, "proposal": p.model_dump(mode="json")})
            return get_action(action_id)

    @app.get("/actions", dependencies=[human])
    def pending_actions():
        with LOCK:
            ids = db.execute("SELECT id FROM actions WHERE state IN ('pending','approved','running') ORDER BY created DESC LIMIT 100").fetchall()
            return [get_action(row["id"]) for row in ids]

    @app.get("/actions/{action_id}")
    def status(action_id: str, authorization: str = Header(default="")):
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not any(secrets.compare_digest(token, expected) for expected in (agent_token, human_token)):
            raise HTTPException(401, "Invalid credentials")
        with LOCK:
            return get_action(action_id)

    @app.post("/actions/{action_id}/decision", dependencies=[human])
    def decide(action_id: str, decision: Decision):
        with LOCK:
            action = get_action(action_id)
            if action["state"] != "pending":
                raise HTTPException(409, "Only pending actions can be reviewed")
            if time.time() - action["created"] > 900:
                raise HTTPException(409, "Proposal expired; submit a fresh proposal")
            state = "approved" if decision.approve else "rejected"
            db.execute("UPDATE actions SET state=? WHERE id=?", (state, action_id))
            audit("decision", {"id": action_id, **decision.model_dump()})
            return get_action(action_id)

    @app.post("/actions/{action_id}/execute", dependencies=[agent])
    def execute(action_id: str):
        with LOCK:
            action = get_action(action_id)
            if db.execute("SELECT value FROM settings WHERE key='killed'").fetchone()[0] == "true":
                audit("blocked", {"id": action_id, "reason": "kill switch"})
                raise HTTPException(423, "Kill switch is enabled")
            if action["state"] in {"completed", "dry_run", "failed", "unknown"}:
                return action
            if action["state"] != "approved":
                audit("blocked", {"id": action_id, "reason": "no human approval"})
                raise HTTPException(409, "Explicit human approval is required")
            if time.time() - action["created"] > 900:
                raise HTTPException(409, "Proposal expired")
            p = Proposal(**action["payload"])
            if not execution_lock.acquire(blocking=False):
                raise HTTPException(409, "Another action is executing; retry after it finishes")
            try:
                validate(p)
            except (ValueError, TypeError) as exc:
                execution_lock.release()
                raise HTTPException(422, str(exc)) from exc
            db.execute("UPDATE actions SET state='running' WHERE id=?", (action_id,))
            audit("execution_started", {"id": action_id})
        try:
            if p.dry_run:
                result = {"message": "Validated preview only; no system changes",
                          "kind": p.kind, "parameters": p.parameters}
                state = "dry_run"
            else:
                if os.environ.get("ACTION_ENABLE_LIVE", "false").lower() != "true" and executor is None:
                    raise RuntimeError("Live execution disabled; operator must set ACTION_ENABLE_LIVE=true")
                steps = p.parameters["steps"] if p.kind == "plan" else [{"kind": p.kind, "parameters": p.parameters}]
                results = []
                for index, step in enumerate(steps):
                    with LOCK:
                        if db.execute("SELECT value FROM settings WHERE key='killed'").fetchone()[0] == "true":
                            raise RuntimeError("Kill switch enabled; remaining steps stopped")
                        audit("step_started", {"id": action_id, "index": index})
                    outcome = (executor or perform)(p.model_copy(update=step))
                    results.append(outcome)
                    with LOCK:
                        audit("step_finished", {"id": action_id, "index": index, "result": outcome})
                verification = [step.model_dump() for step in p.handover.remediation_plan.actions
                                if step.action_type == "VERIFY_RECOVERY"] if p.handover else []
                result = {"steps": results, "recovery_verified": False,
                          "verification_required": verification, "next_step": "verify_recovery_and_report"}
                state = "completed"
        except Exception as exc:
            result = {"message": str(exc), "recovery_verified": False,
                      "completed_steps": locals().get("results", []),
                      "warning": "Action may have partially applied; inspect before retrying. Escalate if recovery fails."}
            state = "failed"
        with LOCK:
            db.execute("UPDATE actions SET state=?,result=? WHERE id=?",
                       (state, json.dumps(result), action_id))
            audit("execution_finished", {"id": action_id, "state": state, "result": result})
            execution_lock.release()
            return get_action(action_id)

    @app.post("/kill-switch", dependencies=[human])
    def kill_switch(switch: Switch):
        with LOCK:
            db.execute("UPDATE settings SET value=? WHERE key='killed'",
                       (json.dumps(switch.enabled),))
            audit("kill_switch", switch.model_dump())
            return switch.model_dump()

    @app.get("/audit", dependencies=[human])
    def history():
        with LOCK:
            return [{**dict(r), "details": json.loads(r["details"])} for r in
                    db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")]

    return app


def perform(p):
    if p.kind == "start":
        return start_service(p.parameters["service"])
    if p.kind == "restart":
        return restart_service(p.parameters["service"])
    if p.kind == "feature_flag":
        return disable_fault_flag(p.parameters["flag"])
    if p.kind == "load":
        return set_load(p.parameters["users"], p.parameters["spawn_rate"])
    raise ValueError("Unsupported operation")


def restart_service(service):
    if service not in SERVICES:
        raise ValueError("Service is not allowlisted")
    return compose_service_action("restart", service)


def start_service(service):
    if service not in START_SERVICES:
        raise ValueError("Service is not allowlisted for start")
    return compose_service_action("start", service)


def compose_service_action(operation, service):
    command = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.full.yaml",
               "-f", "compose.observability.yaml", operation, service]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               timeout=90, check=True, shell=False)
    return {"message": completed.stdout[-2000:], "recovery_verified": False}


def disable_fault_flag(name):
    if name not in FLAGS or name not in os.environ.get("ACTION_ALLOWED_FLAGS", "").split(","):
        raise ValueError("Flag is not allowlisted")
    path = ROOT / "src" / "flagd" / "demo.flagd.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    flag = data["flags"].get(name)
    if not flag or "off" not in flag["variants"]:
        raise ValueError("Unknown flag or variant")
    if flag.get("targeting"):
        raise ValueError("Targeted flags require an organiser-approved adapter")
    flag["defaultVariant"] = "off"
    backend = os.environ.get("ACTION_FLAG_BACKEND", "http")
    if backend == "http":
        request = Request(
            "http://127.0.0.1:8080/feature/api/write",
            data=json.dumps({"data": data}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=15) as response:
            json.load(response)
    elif backend == "file":
        temporary = path.with_suffix(".action-api.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    else:
        raise ValueError("ACTION_FLAG_BACKEND must be http or file")
    return {"message": "Flag update submitted; verify propagation through telemetry",
            "recovery_verified": False}


def set_load(users, spawn_rate):
    if type(users) is not int or not 0 <= users <= 100:
        raise ValueError("users must be an integer from 0 to 100")
    if type(spawn_rate) not in (int, float) or not 0 < spawn_rate <= 10:
        raise ValueError("spawn_rate must be greater than 0 and at most 10")
    request = Request("http://127.0.0.1:8080/loadgen/swarm",
                      data=urlencode({"user_count": users,
                                      "spawn_rate": spawn_rate}).encode(),
                      method="POST")
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get("success") is not True:
        raise RuntimeError("Locust rejected the load update")
    return {"message": "Locust accepted the user count update", "recovery_verified": False}
