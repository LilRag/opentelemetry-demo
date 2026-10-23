# Integration checks without faultctl

## Automated result

`python -m unittest discover -s action-api -q`: **35 tests passed**.

Covered Action API validation, separate human credentials, unapproved execution
rejection, approval expiry, dry-run, mocked execution, idempotency, kill switch,
partial-plan stopping, audit persistence, detector contract submission, escalation,
read-only tool dispatch, bounded mocked LLM tool selection and all five symptom
families using simulated metric/container observations. Six API flows also run
through a real loopback HTTP server with simulated infrastructure execution.

## Read-only live checks

| Component | Observed result |
| --- | --- |
| Prometheus tool | Query succeeded and returned a series |
| Jaeger tool | Retrieved 57 trace IDs and four error spans in the inspected window |
| OpenSearch | Reachable; corrected checkout ERROR search succeeded with zero hits |
| Docker health tool | Read four relevant containers without an error; no anomaly in that set |
| Watchdog metric checks | All four query paths completed after fixing latency evaluation |
| Updated PromQL | HTTP 200/success; memory and queue queries returned data |

Recent request-rate/latency samples were missing in some windows. Missing samples
are not proof of healthy traffic. A memory threshold breach was observed during
one scan; no repair was attempted. Error traces may be historical and are not,
by themselves, proof of a currently active incident.

Docker reads initially failed inside the restricted execution environment. The
same read-only container tool succeeded when given access to Docker Desktop.

## Fixes made during testing

- Renamed the imported latency-query function to prevent a local-variable naming
  collision that crashed latency detection.
- Updated OpenSearch field matching to include resource.service.name,
  severity.text and traceId, while requiring service/trace correlation.
- Restricted memory-trigger targets to known demo services.
- Fixed the test loader's collision with the unrelated installed watchdog package.

## Not established by these tests

- No live fault was injected, no flags were inspected or changed, no container was
  stopped/restarted, and no load-generator settings were changed.
- Live Docker repair, live flag-write and traffic-control effects remain untested.
- No paid/live LLM call was made. LLM tool-selection behavior was simulated.
- Zero OpenSearch hits confirms the request path, not retrieval of a real matching
  error log; that still needs a suitable incident or existing matching data.
- Sustained post-repair verification, incident-report generation, MCP transport,
  chatbot approval UI and end-to-end fault-repair accuracy remain outside the
  implemented proposal integration.

The integration stops at a pending Action API proposal. Actual execution still
requires human approval and explicit execution. Integration edits remain local.
