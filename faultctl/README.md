# faultctl

`faultctl` injects and resets **practice incidents** in a local
[OpenTelemetry Demo](https://github.com/LilRag/opentelemetry-demo) (Astronomy Shop) for the
Bosch *Incident Zero* hackathon. It is the operator's tool for creating incidents. It is **not**
part of the AI on-call agent, and it does no remediation.

## How it works

| Mechanism | Used for | Inject | Reset | Verification |
|---|---|---|---|---|
| `flag` | the demo's built-in failure flags | Reads all flags from flagd-ui `GET /feature/api/read`, changes only the target flag, and writes them back with `POST /feature/api/write` (documented in `opentelemetry-demo/src/flagd-ui/README.md`) | Sets the flag back to `off` through the same API | flagd-ui must show the new value, **and** flagd must evaluate the flag to the new variant (`/flagservice/flagd.evaluation.v1.Service/Resolve*` through the frontend proxy) |
| `container-stop` | infrastructure faults | `docker stop <allowlisted container>` | `docker start <container>` | `docker inspect` state (and health, when the container has a healthcheck) |

Every flag and container in the catalog was checked against the demo source. The flag must be
read by a service, and the container must be defined in `compose.yaml`.
`tests/test_incidents.py` re-checks the catalog against the demo checkout.

## Supported incidents

Run `faultctl list` to see which incidents can be injected right now, and `faultctl show <id>` for
the full details of one incident (effects, verification, limitations).

| ID | Mechanism | Target | Variants (default) |
|---|---|---|---|
| payment-failure | flag `paymentFailure` | payment | 10%…100% (50%) |
| payment-unreachable | flag `paymentUnreachable` | checkout → payment | on |
| cart-failure | flag `cartFailure` | cart | 10%…100% (50%) |
| product-catalog-failure | flag `productCatalogFailure` (targeting rule, product OLJCESPC7Z) | product-catalog | on |
| product-catalog-lock-contention | flag `productCatalogLockContention` | product-catalog / astronomy-db | on |
| recommendation-cache-leak | flag `recommendationCacheFailure` | recommendation | on |
| ad-high-cpu | flag `adHighCpu` | ad | on |
| ad-manual-gc | flag `adManualGc` | ad | on |
| ad-failure | flag `adFailure` | ad | on |
| email-memory-leak | flag `emailMemoryLeak` | email | 1x…10000x (100x) |
| homepage-flood | flag `loadGeneratorFloodHomepage` | load-generator → frontend | on |
| image-slow-load | flag `imageSlowLoad` | frontend (browser side) | 5sec, 10sec (5sec) |
| intl-shipping-slowdown | flag `intlShippingSlowdown` | shipping | 5sec, 10sec (5sec) |
| kafka-queue-problems | flag `kafkaQueueProblems` (needs `compose.full.yaml`) | fraud-detection / kafka | 1sec, 3sec, 5sec (1sec) |
| kafka-consumer-dead | flag `kafkaConsumerDead` (needs `compose.full.yaml`) | fraud-detection / kafka | on |
| cache-unavailable | stop `valkey-cart` | cart cache | – |
| database-unavailable | stop `astronomy-db` | product database | – |
| currency-down | stop `currency` | currency service | – |

These are not supported (left out on purpose):
- Network latency or packet loss between services: the containers don't have the `NET_ADMIN`
  capability, and adding `tc` or sidecars would change the demo.
- CPU or memory starvation through `docker update`: the original limits can't be restored
  reliably.
- Restart loops, `failedReadinessProbe` (Kubernetes only), the `ai*` flags (need the agent layer),
  and `emitRawPii` (not a failure).

## Prerequisites

- Windows, macOS or Linux, with Python **3.10+**. It is tested on 3.10.11. The brief asks for
  3.11, but there are no 3.11-only features and no third-party dependencies.
- Docker Desktop running locally. `faultctl` refuses a remote `DOCKER_HOST` or Docker context.
- The demo running from its checkout, for example:

```powershell
cd <your checkout>\opentelemetry-demo
# full stack, the same files `make start` uses: Kafka, plus Jaeger/Grafana/Prometheus/OpenSearch
docker compose -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f compose.extras.yaml up --force-recreate --remove-orphans --detach
# minimal stack (no Kafka incidents, no Jaeger/Grafana UIs):
docker compose up --force-recreate --remove-orphans --detach
```

## Installation (Windows PowerShell)

```powershell
cd <your checkout>\opentelemetry-demo\faultctl
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
faultctl --help
```

Without installing: `$env:PYTHONPATH="src"; python -m faultctl --help`.

## Usage

Global options go **before** the command (`faultctl --verbose inject ...`).

```powershell
faultctl list                                   # catalog plus live availability
faultctl list --no-probe                        # catalog only, no Docker/HTTP calls
faultctl show payment-failure                   # full documentation for one incident
faultctl inject payment-failure --dry-run       # print the plan, change nothing
faultctl inject payment-failure --variant 100%
faultctl inject cache-unavailable
faultctl inject cart-failure ad-high-cpu currency-down   # several at once (default variants)
faultctl inject --all --dry-run                 # plan every currently available incident
faultctl inject --all                           # asks for confirmation; --yes to skip
faultctl status                                 # service health only
faultctl status --show-active                   # operator view of active faults
faultctl reset payment-failure                  # idempotent
faultctl reset-all                              # asks for confirmation
faultctl reset-all --yes
faultctl --verbose reset cache-unavailable      # log every HTTP/docker call to stderr
```

Every result line is tagged `[OK]`, `[PLANNED]`, `[FAILED]`, `[OUTCOME-UNKNOWN]`,
`[APPLIED-UNVERIFIED]`, `[UNAVAILABLE]`, `[REFUSED]` or `[INVALID]`. `[OK]` is printed only after
the verification above has passed.

| Exit code | Meaning |
|---|---|
| 0 | Success, verified (or a dry run) |
| 1 | Operation failed |
| 2 | Invalid usage or unknown incident |
| 3 | Refused by a safety check |
| 4 | Dependency unavailable (Docker, demo container, flagd-ui) |
| 5 | Outcome unknown (a mutation timed out; it is not retried) |
| 6 | Applied, but verification did not confirm it |
| 8 | Partial failure (`reset-all`) |
| 9 | Aborted at the confirmation prompt |

Configuration through environment variables (or the matching flags):

| Variable | Default | Notes |
|---|---|---|
| `FAULTCTL_BASE_URL` | `http://localhost:8080` | Frontend proxy. Only `localhost`, `127.0.0.1` and `::1` are accepted. |
| `FAULTCTL_COMPOSE_PROJECT` | `opentelemetry-demo` | Every container touched must carry this compose project label. |
| `FAULTCTL_DEMO_DIR` | unset | If set, containers must also have been started from this directory. |

## Injection and reset behaviour

- **Inject** validates the incident ID and variant against the allowlist, then checks the
  environment. Docker must be local. `frontend-proxy`, `flagd` and `flagd-ui` (for flags) or the
  target container must exist and belong to the configured compose project. Any containers the
  incident needs (such as kafka) must be running. Then it performs the change and verifies it.
  If the incident is already active, nothing is written.
- **Inject several / `--all`**: each incident uses its default variant (`--variant` works only
  with a single ID). All flag changes go in one flagd-ui write, and containers are then stopped
  one by one. `--all` asks for confirmation, then injects every incident that `list` shows as
  available. It prints the others as `[SKIPPED]` (for example the Kafka incidents without
  `compose.full.yaml`). Each incident gets its own result line. An unknown ID mixed with valid
  ones gives exit 8 (partial). Running everything at once loads a laptop heavily, so undo it with
  `faultctl reset-all --yes`.
- **Reset** returns the incident to baseline (flag `off`, container running). It is idempotent:
  if the incident is already at baseline, nothing is written.
- **reset-all** asks for confirmation, then resets every flag incident in **one** read-modify-write
  and starts any of the three allowlisted containers that are stopped. It reports each incident
  separately and exits with 8 on partial failure. If flagd-ui is down, it still resets the
  containers.
- **Dry run** (`--dry-run` on inject, reset and reset-all) may make read-only checks, prints
  `PLAN:` steps, and never changes state.

## Safety guarantees

- Only loopback HTTP targets and a local Docker daemon socket (`npipe://`, `unix://`).
- Fixed Docker argument lists, run without a shell. Containers can only be `stop`ped or `start`ed
  if they are in the hard-coded allowlist: `valkey-cart`, `astronomy-db`, `currency`.
- The tool never removes containers, volumes, images or data. It never runs `docker compose down`
  and never edits demo source code.
- Flag writes change only the target flag in a freshly read copy of the full flag set.
- No automatic remediation, and no use of the Action API. A timed-out mutation is reported as
  outcome-unknown and is not retried.
- `status` hides active-fault state unless `--show-active` is passed. **Never pipe
  `--show-active`, `show` or flag files into the diagnosis agent.** The agent must reason from
  telemetry.

## Tests

```powershell
$env:PYTHONPATH="src;tests"
python -m unittest discover -s tests -t tests -v
```

The unit tests use in-memory fakes and never touch Docker or the network, except for one test that
connects to the closed loopback port 9. Integration tests run against the **local** demo only:
they inject and reset `payment-failure`, `product-catalog-failure` and `cache-unavailable`.

```powershell
$env:FAULTCTL_INTEGRATION="1"; python -m unittest tests.test_integration -v
```

## Troubleshooting

- `[UNAVAILABLE] container 'frontend-proxy' not found`: the demo isn't running, or was started
  under a different compose project. Run `docker compose ls`, then pass `--project`.
- `[REFUSED] ... belongs to compose project ...`: a container with that name belongs to another
  stack. `faultctl` won't touch it.
- `[APPLIED-UNVERIFIED] ... flagd still evaluates ...`: flagd-ui stored the change, but flagd
  didn't reload it in time. This can happen with Docker Desktop bind-mount file watching. Raise
  `--verify-timeout`, or check `docker logs flagd`.
- Unexpected flag changes: the flagd-ui **scheduler** (on `/feature`) can flip flags by itself.
  Stop it during practice.

## Limitations

- No organizer starter kit, Action API contract, faultctl spec or incident bundles were present
  when this was written. If one is published, its interface takes precedence. The incident IDs
  here are this tool's own.
- The flagd-ui write replaces the whole flag file. A change someone else makes between faultctl's
  read and write is lost.
- Flag writes also change `src/flagd/demo.flagd.json` in the demo checkout (the bind mount).
  `reset`/`reset-all` return the flags to `off`. `git diff` may still show whitespace or `$schema`
  differences.
- Verification confirms that the *mechanism* is in effect. It does not check that the telemetry
  symptom appeared. Memory already leaked (recommendation, email) is not reclaimed by a reset.
- `valkey-cart` keeps carts in memory, so stopping it loses the existing carts.
- Kubernetes deployments are not supported.
