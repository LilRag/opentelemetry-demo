# Detection to Action API integration

The integrated checkout contains both this detector and `../../action-api`.
The original shop checkout and the published actionapi branch are unchanged by
these local detector integration edits.

## Flow

Watchdog -> telemetry evidence -> bounded RCA/tool loop -> validated contract ->
POST /handovers -> pending human approval.

The detector never approves or executes a proposal. Use the Action API's existing
human decision endpoint and agent execution endpoint afterward. Recovery is not
claimed merely because execution completed. The Action API returns any proposed
VERIFY_RECOVERY checks for the agent; sustained recovery verification and final
incident reporting are still separate work.

## Read-only tools

`tools.py` exposes query_prometheus, get_traces, get_logs and container_health.
They use fixed configured backends and bounded inputs. There are no flag-reading,
shell execution, approval or repair tools in this registry. OpenSearch searching
uses POST, but only to the read-only /_search operation.

The RCA loop can request up to four tools and then return a contract or escalate.
These are Python callable tools, not a newly deployed MCP server. A teammate's
MCP transport can wrap the same registry later. Docker collection calls are reads,
but the OS credentials of this Python process are not a security sandbox: use
restricted infrastructure access in a remote deployment.

Without a configured Gemini key and model, offline reasoning proposes starting
one observed stopped container. Otherwise it escalates rather than guessing or
reporting a fabricated confidence score. A failing trace alone does not prove
that restarting its service is appropriate.

## Run against the local shop

1. Install action-api/requirements.txt, plus this pipeline's required runtime
   libraries: requests, python-dotenv, pydantic and optionally docker.
2. Start the Action API as documented in action-api/README.md, on port 8020.
3. If using the API copied into this checkout against the shop launched from the
   original checkout, set its ACTION_DEMO_ROOT in the API terminal **before**
   starting it:

   ```powershell
   $env:ACTION_DEMO_ROOT = "C:\Users\Chirayu  M\Downloads\bosch\opentelemetry-demo"
   ```

   This makes Compose operations and the flag-file adapter target the same shop.
   Alternatively run the API directly from that original checkout.
4. Copy .env.example to .env in this pipeline directory. Set ACTION_AGENT_TOKEN
   to the same agent token configured on the API. Never give the detector the
   human approval token. Leave ACTION_DRY_RUN=true for initial testing.
5. Endpoints: Prometheus is localhost:9090; Jaeger uses the Envoy v3 trace route
   in the example. OpenSearch discovers its host mapping with Docker, or set
   OPENSEARCH_URL to the actual published port (49242 in the inspected session).
6. To enable Gemini reasoning, explicitly set GEMINI_API_KEY and a supported
   Gemini LLM_MODEL for your account. No model ID is assumed by default. Other
   provider keys are not currently wired to the tool loop.
7. From this directory:

   ```powershell
   python main.py --test checkout
   python main.py --monitor --submit
   ```

`--test` collects evidence; it does not inject faults. `--submit` sends only
validated proposals; omitting it keeps reports local. Uncertain incidents become
ESCALATED and are not sent as executable plans. Submission errors are surfaced;
automatic persistent delivery/retry after an API outage is not implemented.

## Monitoring coverage

`queries.py` uses actual modern HTTP/RPC metrics and legacy RPC milliseconds
metrics, converting latency to seconds. It monitors the maximum error fraction
and p95 across those protocol signals per service. Container memory uses
container_memory_usage_total_bytes / container_memory_usage_limit_bytes. Kafka
uses kafka_consumer_group_lag_sum_ratio grouped by group.

Queries use one-minute windows. Missing/stale series, no traffic, absent metrics
and query failures do not prove health. The watchdog records query warnings and
does not claim incident resolution when no threshold is observed. Full coverage,
CPU signals, deduplication across simultaneous incidents and recovery assessment
need further development. Metrics are deployment-specific; configure/extend
queries if your collector emits different conventions.

The collector no longer reads fault flags. It searches logs only for the service
or correlated trace IDs. LLM output is schema-validated; this validates structure,
not the truth of an LLM's diagnosis. Evidence provenance and PII handling require
care in production. The tracked historical latest_incident.json sample predates
these changes and is not a valid telemetry-only integration fixture.

## Tests and limits

From the checkout root:

```powershell
python -m unittest discover -s action-api -v
```

Tests exercise the real Action API routes with a synthetic stopped database,
human approval separation, dry-run execution, duplicate submissions, unknown
tool rejection, missing-evidence escalation and mocked LLM tool selection.
The existing HTTP server and Action API tests are included. No live repair,
fault injection, active-flag read or paid LLM request is performed by these tests.

Read-only live probes confirmed Prometheus and OpenSearch availability and a
successful Jaeger v3 query. Updated PromQL compiled successfully; memory and queue
queries returned data. Request-rate windows had no recent samples at inspection,
so real detection accuracy and a full injected-fault repair loop are not yet
validated. Verification execution, MCP transport and chatbot approval UI are not
implemented by this integration.
