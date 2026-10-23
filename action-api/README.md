# Incident Zero Action API

The incident agent proposes a narrowly defined change. A human reviews the exact
proposal with separate credentials. Only then can the agent execute it. This
service uses Python/FastAPI, matching the demo's existing agent service.

This is a team implementation, not an official organiser API. It runs on the
Docker host beside the existing Compose deployment; it does not change the demo's
Compose files or add a new shop service.

## Start locally (PowerShell)

From the repository root:

```powershell
cd action-api
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ACTION_AGENT_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:ACTION_HUMAN_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:ACTION_ENABLE_LIVE = "false"
$env:ACTION_ALLOWED_FLAGS = "paymentFailure,emailMemoryLeak,cartFailure,adFailure"
.\.venv\Scripts\python.exe -m uvicorn main:create_app --factory --host 127.0.0.1 --port 8020 --workers 1
```

Keep the generated credentials in the host's environment. The agent process gets
only ACTION_AGENT_TOKEN. The human approval interface gets only ACTION_HUMAN_TOKEN.
Do not pass the human token to the LLM, agent tools, prompts or shared repository.
The host API process needs both tokens. Set them again when opening a new shell.

Open http://127.0.0.1:8020/docs for interactive endpoint documentation.
Endpoints accept an `authorization` header containing `Bearer <role token>`.
No LLM, Docker connection or running shop is needed for a dry-run.

Live execution additionally requires ACTION_ENABLE_LIVE=true at server startup
and dry_run=false in the approved proposal. Restart the server after changing
environment variables. Merely setting the live environment switch executes nothing.

## Request and approval flow

1. Agent calls POST /actions with evidence references and an idempotent request_id.
2. Human interface calls GET /actions to list outstanding proposals, or
   GET /actions/{id} to inspect the exact proposal.
3. Human calls POST /actions/{id}/decision with their own token:
   `{"approve": true, "reviewer": "operator-name"}`.
4. Agent calls POST /actions/{id}/execute with its token.
5. Agent queries fresh telemetry to verify recovery and generate the report.

The reviewer name is an audit label; the human token supplies authentication.
There is no chatbot approval UI yet. A teammate can connect their UI to these
endpoints. Approval expires 15 minutes after proposal creation. The proposal is
immutable: changing any parameter requires a new request_id and fresh approval.

Example POST /actions body (dry-run by default):

```json
{
  "request_id": "incident-007-payment-restart-v1",
  "incident_id": "incident-007",
  "reason": "Payment requests fail; investigation suggests a stuck service",
  "evidence": ["Replace with a real trace ID and observed error"],
  "kind": "restart",
  "parameters": {"service": "payment"},
  "dry_run": true
}
```

The API requires evidence references but cannot establish their truth. The agent
must collect genuine evidence; this API is not a diagnosis or telemetry service.

## Reusable operations

| kind | parameters | Effect |
| --- | --- | --- |
| restart | service | docker compose restart of an allowlisted application service |
| start | service | docker compose start of an existing allowed service, including astronomy-db and valkey-cart |
| feature_flag | flag, variant="off" | Disable an operator-allowlisted catalog fault |
| load | users=0..100, spawn_rate>0..10 | Update Locust concurrent users and transition rate |
| plan | steps | Execute 1–5 ordered operations after approval of the whole plan |
| scale | — | Rejected; Kubernetes is not implemented by this Compose adapter |

Load user count is concurrency, not a requests-per-second limit. A spawn rate is
the rate of changing users, not a request rate. Choose fewer users to reduce load.
The adapter calls the existing Envoy route at http://127.0.0.1:8080/loadgen/swarm.
Its form fields follow the [Locust web implementation](https://docs.locust.io/en/stable/_modules/locust/web.html).

For emailMemoryLeak, propose one plan containing:

```json
{
  "steps": [
    {"kind": "feature_flag", "parameters": {"flag": "emailMemoryLeak", "variant": "off"}},
    {"kind": "restart", "parameters": {"service": "email"}}
  ]
}
```

Use that object as parameters with kind="plan", plus the usual incident metadata.
Steps stop at the first failure. Results include completed steps; audit events
record individual starts and completions. A failed request is not automatically
retried. Restarting the API marks interrupted running actions unknown rather than
repeating a potentially applied operation.

`client.py` provides HTTP wrappers for your agent. Methods propose actions; none
approves them:

```python
import os
from client import ActionClient

actions = ActionClient("http://127.0.0.1:8020", os.environ["ACTION_AGENT_TOKEN"])
proposal = actions.restart_service(
    "payment", request_id="incident-007-restart-v1", incident_id="incident-007",
    reason="Describe the evidence-backed reason",
    evidence=["Actual trace or metric reference"], dry_run=True,
)
# A separate human interface reviews proposal["id"].
# After approval: actions.execute(proposal["id"])
```

## Controls and audit

- Dry-run validates the action and previews it without contacting Docker, Locust,
  or reading/writing the flag configuration. It does not prove backend availability.
- Unknown services, extra parameters, unsupported scaling, flag activation and
  out-of-range load settings are rejected. No arbitrary command endpoint exists.
- GET /capabilities reports supported operations and static policy, never current
  flag values. No flag-reading diagnostic endpoint exists.
- Agent credentials cannot approve actions, operate the kill switch or read audit.
- Human POST /kill-switch body `{"enabled": true}` blocks new execution and stops
  remaining plan steps. It cannot undo or cancel an operation already dispatched.
- POST /kill-switch with false resumes permission to execute approved actions.
- GET /audit returns the latest 200 audit entries; full history persists locally
  in actions.sqlite3. HTTP requests, approvals, execution and outcomes are logged.
- Duplicate request IDs with the same content return the existing proposal;
  changed content conflicts. Repeated execution does not repeat a finished action.
- Exactly one worker/process must use the database. Operations are serialized;
  status queries and the kill switch remain available during backend operations.

## faultctl integration and limitations

Your team's faultctl is a separate operator tool: it injects practice faults and
resets the environment. Do not give it to the incident agent as a diagnostic or
reset shortcut. Coordinate its writes with the Action API: inject/reset only
between incidents, never concurrently with repair actions.

The default flag adapter reads src/flagd/demo.flagd.json internally only after
approval, changes the named flag's defaultVariant to off, and POSTs the full
document as `{"data": document}` to the fixed
http://127.0.0.1:8080/feature/api/write endpoint. That is the actual flagd-ui
controller contract, rather than a direct flagd write API. Its acknowledgement is
asynchronous: verify propagation and recovery through fresh telemetry. The adapter
never returns current or previous settings to the agent. It refuses targeted
flags. The API and shop must use the same mounted flag configuration.

For a local file-only deployment, explicitly set ACTION_FLAG_BACKEND=file. This
fallback atomically replaces the local file, but bypasses the flagd-ui cache, so
prefer the default HTTP backend when flagd-ui is running. Never allow concurrent
faultctl/UI writes while executing a repair; the full-document write has no
compare-and-swap protection.

The implementation does not provide Kubernetes scaling, arbitrary network or CPU
changes, agent session resets, Envoy configuration changes, PII cleanup, telemetry
verification, incident report generation or automatic rollback. The agent and
human should escalate failed recovery. A restart cannot be undone; restoration of
a prior load level is a new explicit action requiring fresh human approval. Do
not restore a fault flag to its failing value as a generic rollback.

The API assumes the same default Compose files and project as the documented demo
command. It does not find another deployment automatically. It must run on the
Docker host, not a teammate's unrelated Docker instance. The default binding is
loopback. Remote deployment needs an authenticated HTTPS gateway and an explicit
credential/access design; GitHub sharing alone does not connect runtimes.

SQLite audit is local and not tamper-proof. Keep raw PII out of submitted evidence.
This API does not replace the full project's incident reports or approval UI.

## Detection pipeline handover

This integration follows the handover shape in
[detection_architecture.md](../detection_architecture.md), with the competition's
human gate and prohibition on flag-based diagnosis preserved.

Agent POST /handovers body:

```json
{
  "request_id": "INC-007-fix-v1",
  "dry_run": true,
  "contract": {
    "incident_id": "INC-007",
    "status": "AWAITING_APPROVAL",
    "timestamp": "2026-10-23T15:05:00Z",
    "diagnosis": {
      "culprit_service": "payment",
      "root_cause": "Replace with the telemetry-backed diagnosis",
      "blast_radius": "Replace with the observed impact",
      "evidence": {
        "metric": "Replace with an actual measured error rate",
        "failing_trace_id": "Replace with an actual trace ID"
      }
    },
    "remediation_plan": {
      "summary": "Disable payment fault and check recovery",
      "actions": [
        {
          "step": 1,
          "action_type": "TOGGLE_FLAG",
          "target": "paymentFailure",
          "parameters": {"desired_variant": "off"},
          "description": "Disable the suspected faulty behaviour"
        },
        {
          "step": 2,
          "action_type": "VERIFY_RECOVERY",
          "target": "checkout",
          "parameters": {
            "metric_query": "REPLACE_WITH_YOUR_ACTUAL_ERROR_RATE_QUERY",
            "expected_value": 0,
            "evaluation_window_seconds": 30
          },
          "description": "Agent checks fresh checkout metrics"
        }
      ]
    }
  }
}
```

This is a structural example, not incident evidence or a validated PromQL query.
`client.submit_handover(contract, request_id="INC-007-fix-v1", dry_run=True)`
wraps this request for the detector. A successful submission creates a pending
proposal, not an approved action. Existing decision and execute endpoints apply.
The incident ID, timestamp, diagnosis, evidence, descriptions and recovery checks
are retained in the immutable approved proposal and persisted audit trail.

| Contract action_type | Internal action | Handling |
| --- | --- | --- |
| TOGGLE_FLAG | feature_flag | Only an allowed target and desired_variant=off |
| RESTART_CONTAINER | restart | Allowlisted application service |
| START_CONTAINER | start | Allowlisted existing service; includes astronomy-db and valkey-cart |
| VERIFY_RECOVERY | Agent handback | Returned in result.verification_required; never sent to Docker or executed here |

Container actions accept empty parameters or container_name matching target.
TOGGLE_FLAG accepts optional flag_name matching target and the document's fixed
api_endpoint as compatibility metadata. An LLM-selected different endpoint is
rejected; submitted URLs never choose an execution destination. Container names
must match the Compose service names in this checkout. Compose calls the Docker
Engine through its CLI; a separate direct Docker SDK adapter is not included.
`start` only starts already-created containers; it does not provision services.

Steps must be consecutive, system changes must precede verification, and at most
five system changes are allowed. After execution, result.recovery_verified remains
false, result.verification_required carries the proposed read-only checks, and
result.next_step tells the incident agent to verify recovery and report. The
agent owns validating/running the PromQL and deciding whether recovery occurred.
On failure, inspect the recorded partial outcome and escalate.

### Specification conflicts resolved explicitly

The detection document's enrichment code reads active_chaos_flags and its sample
diagnosis includes active_flag/current_variant. Those fields conflict with the
competition slides' ban on reading fault settings to diagnose incidents. The
handover schema rejects those structured diagnostic fields. The detector must
remove the flag-read step and use genuine telemetry. Fault names in proposed
repairs are allowed; current flag settings as diagnostic answers are not.
Schema checks cannot establish evidence provenance or detect every prohibited
claim embedded in free text; enforcement is also needed in the detector's tools.

The document's small flag-write payload is incomplete for the actual controller:
it replaces the whole document using the data envelope. Our adapter preserves
unrelated flags when constructing that document. The VERIFY_RECOVERY action is
present in the sample but absent from its final list of three write operations;
it is explicitly supported as an agent handback rather than a system mutation.

No detection code or competition rules were changed to hide these discrepancies.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

Tests use temporary databases/files and mocked execution. They do not restart any
shop service, inject faults or modify the real flag file. Live integration must
be tested later against an explicitly approved practice incident.
