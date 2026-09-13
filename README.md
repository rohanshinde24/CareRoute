# CareRoute

A referral-coordination system that demonstrates how to put a language model inside an administrative workflow **without letting it become the authority**. Deterministic code owns every decision that has a consequence; the model is confined to interpreting ambiguity and making proposals that must survive validation before anything happens.

## Scope and safety notice

**This project runs entirely on synthetic data and is not a medical device.**

- It does **not** diagnose, prescribe, triage by acuity, or recommend treatment.
- It makes **no claim of HIPAA compliance** and must not be used with real patient data.
- All patients, providers, coverage records, documents, and schedules are generated fixtures.
- Its decisions are administrative only: which specialty a referral was submitted under, whether required paperwork is present, which providers are eligible, and which appointment slots are free.

It is a portfolio and research system for distributed-systems and agent-safety design, not a clinical product.

## The safety boundary

This is the central idea of the project, and everything else is built to enforce it.

Deterministic code owns workflow state, authorization, eligibility, validation, retries, idempotency, and every side effect. A model is invoked only at a bounded ambiguity boundary, and only to *investigate and propose*.

Three investigator loops exist — specialty interpretation, missing-document inference, and provider ranking. Each is hard-capped in turns and evidence calls. Every proposal is schema-constrained and must be **grounded in a candidate set the deterministic layer already produced and observed**. A proposal that is invalid, premature, references anything outside that observed set, or arrives before its evidence does, fails closed.

Concretely, a model in this system **cannot**:

- write workflow state,
- select an appointment slot,
- book, confirm, or cancel anything,
- reach a provider or slot it was not already shown,
- widen its own turn or evidence budget.

A valid provider proposal changes only the **presentation order** of candidates. It does not record a patient's choice. Booking requires an explicit, separately confirmed human command.

This is auditable rather than asserted. Every model turn emits an `agent.turn` span immediately followed by an `agent.policy.validate` span, so the trace itself shows that deterministic validation ran between the model speaking and anything occurring.

## Architecture

**Services and data.** Two domains, two databases, no shared storage.

```mermaid
flowchart LR
    WEB[Next.js<br/>dashboard] --> API
    MCP[MCP tools] --> API[Referral API]
    API --> RDB[(Referral DB<br/>+ outbox)]
    API <--> INN[Inngest<br/>durable steps]
    API -->|typed HTTP gateway| PS[Provider service]
    PS --> BK[[Booking + event<br/>one transaction]]
    BK --> PDB[(Provider DB<br/>+ outbox)]
    RDB -.-> RLY[Relay]
    PDB -.-> RLY
    RLY --> RS[(Redis Streams)]
```

**Agent path.** A model may propose; only deterministic policy may accept.

```mermaid
flowchart LR
    CO[Coordinator] --> IN[Bounded<br/>investigator]
    IN -.runtime flag.-> RT[[loop or<br/>LangGraph]]
    IN --> MD[deterministic /<br/>Ollama / Gemini]
    MD --> PO{Deterministic<br/>policy}
    PO -->|accepted| CO
    PO -->|rejected| HR([NEEDS_HUMAN_REVIEW])
```

**Observability.** Applications know only the Collector.

```mermaid
flowchart LR
    SV[Referral API +<br/>Provider service] -->|OTLP| CL[Collector]
    CL --> TP[(Tempo<br/>14d)]
    CL --> PR[(Prometheus<br/>15d)]
    TP --> GR[Grafana]
    PR --> GR
    PR -.exemplars.-> TP
```

Every arrow into the provider domain is an HTTP request, never a query. The dotted line from the investigators to `AGENT_RUNTIME` is the orchestration flag; both runtimes route through the same deterministic policy box, which is the only thing that can approve a model's proposal.

**The two services own separate databases.** The provider domain owns providers, schedules, slots, appointments, and booking records; the referral domain owns everything else. Neither reads the other's tables. References that cross the boundary are validated UUIDs with no foreign key, because a foreign key cannot be enforced across databases.

The referral API reaches the provider service only through a typed asynchronous gateway that separates transient failures (timeouts, connection errors, 429, selected 5xx) from permanent protocol failures, and fails closed on malformed payloads and correlation mismatches. Retries use exponential backoff with full jitter.

**Booking executes inside the provider service**, where the slot and the appointment live, so the lock, the slot state change, and the appointment write remain a single transaction. It is requested with a caller-supplied idempotency key, and the provider side records its decision — refusals as well as successes — so a caller that loses the response can repeat the request and learn the original outcome rather than act twice.

**Domain events go through a transactional outbox.** Each database has its own outbox table, written in the *same transaction* as the state change it describes — a commit followed by a separate publish is not atomic, and would either announce something that rolled back or silently lose something that happened. A relay claims rows with `SELECT ... FOR UPDATE SKIP LOCKED`, publishes to Redis Streams, then marks them dispatched. It publishes *before* marking on purpose: a crash between the two produces a duplicate, which consumers absorb, rather than a silence, which nothing downstream can repair.

The relay runs outside the request path, so a broker outage delays delivery instead of failing a user-facing operation. Redis uses AOF with `appendfsync everysec`; the resulting one-second window of acknowledged-but-unwritten events is survivable because the outbox holds the authoritative record.

The referral API owns referral coordination, the workflow state machine, confirmation gating, model adapters, patient and coverage tools, MCP, and FHIR translation.

### Workflow states

Every transition below is enforced by an explicit allow-list in `workflow.py`. A move that is not drawn here raises `InvalidTransition` and is rejected — including any a model might propose. Ambiguity routes to `NEEDS_HUMAN_REVIEW` rather than to a guess.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> RECEIVED
    RECEIVED --> PARSING
    PARSING --> VALIDATING
    VALIDATING --> CHECKING_COVERAGE
    CHECKING_COVERAGE --> MATCHING_PROVIDER
    MATCHING_PROVIDER --> WAITING_FOR_SLOT_SELECTION
    WAITING_FOR_SLOT_SELECTION --> BOOKING
    BOOKING --> CONFIRMED
    CONFIRMED --> [*]
```

Every way of pausing or failing returns to `PARSING` rather than skipping ahead, so a resumed referral is re-evaluated from the start instead of continuing on stale conclusions:

```mermaid
stateDiagram-v2
    direction LR
    VALIDATING --> WAITING_FOR_DOCUMENTS
    CHECKING_COVERAGE --> COVERAGE_UNVERIFIED
    MATCHING_PROVIDER --> PROVIDER_UNAVAILABLE
    PARSING --> NEEDS_HUMAN_REVIEW
    VALIDATING --> NEEDS_HUMAN_REVIEW
    MATCHING_PROVIDER --> NEEDS_HUMAN_REVIEW
    BOOKING --> BOOKING_FAILED
    BOOKING_FAILED --> WAITING_FOR_SLOT_SELECTION
    WAITING_FOR_DOCUMENTS --> PARSING
    COVERAGE_UNVERIFIED --> PARSING
    PROVIDER_UNAVAILABLE --> PARSING
    NEEDS_HUMAN_REVIEW --> PARSING
    VALIDATING --> PARSING
    CHECKING_COVERAGE --> PARSING
    MATCHING_PROVIDER --> PARSING
    CANCELLED --> [*]
```

`BOOKING` is the sole route into `CONFIRMED`, and it is gated on a recorded confirmation command. Cancellation is reachable from every non-terminal state and is omitted from both drawings, because thirteen identical edges would bury the shape of everything else.

This diagram is verified by `tests/test_readme_diagram.py`, which parses it and compares it against the transition table in the code. If they ever disagree, the test fails rather than the README quietly misleading a reader.

### The provider investigation, as a graph

The provider-ranking investigator ships in two interchangeable orchestrations, selected by `AGENT_RUNTIME`: `legacy`, a handwritten loop, and `langgraph`, a bounded state graph. They share one deterministic policy — the graph imports the loop's available-action and validation functions rather than restating them, so the two cannot drift apart.

```mermaid
flowchart LR
    S([start]) --> D[decide]
    D --> V{validate}
    V -->|rejected| X([fail closed])
    V -->|get slots| O[observe]
    O --> D
    V -->|propose or<br/>escalate| F([finish])
```

The graph owns which node runs next and the turn and tool-call ceilings. It does not own the candidate set, slot selection, referral state, booking, or the right to skip validation — a rejected proposal ends the investigation rather than being retried.

**What the policy checks, and why it can.** A proposal is refused until every matching candidate's availability has been observed, and the accepted answer is the earliest observed free slot, ties broken by ID. That criterion was chosen because it is objective and totally ordered, so the validator does not assess whether a proposal is *reasonable* — it recomputes the answer and checks equality. A validator that had to judge quality would be a second model with the same failure modes.

The honest consequence: **the model cannot improve on the deterministic answer here — it can only match it or fail.** This slice is a testbed for the propose-and-verify boundary, not a place where a language model adds decision value.

**Measured against real models.** The deepest legal case — three candidates, spending the full 4-turn and 3-tool budget — was run 6 times per runtime against two local models:

| Model | Completed the ranking | Notes |
|---|---|---|
| `gemma3:4b` | **0 / 12** | fails the final judgment after all three lookups |
| `qwen2.5:7b` | **1 / 12** | ~3× slower, no more reliable |

All 23 failures were **caught**: the policy recomputed the correct provider, saw a mismatch, and routed to `NEEDS_HUMAN_REVIEW`. No incorrect proposal reached a patient-visible outcome. Both models reliably perform the three lookups and then fail the ranking itself, so this is a documented capability boundary rather than a flaky run.

**Measured, not assumed.** Both runtimes pass the 19-case benchmark. Beyond that, `careroute-compare-runtimes` drives both across the full legal range of the ranking path — the number of candidates matching the location preference, which is what actually determines how many turns and tool calls an investigation spends. Fifteen repetitions per cell:

| Scenario | Turns | Tool calls | Outcome | `legacy` p50 | `langgraph` p50 |
|---|---|---|---|---|---|
| 1 matching candidate | 2 | 1 | `provider_proposed` | 89.9 ms | 85.8 ms |
| 2 matching candidates | 3 | 2 | `provider_proposed` | 120.3 ms | 125.7 ms |
| 3 matching candidates | 4 | 3 | `provider_proposed` | 149.2 ms | 156.5 ms |
| above candidate ceiling | 0 | 0 | `candidate_limit` | 113.2 ms | 107.4 ms |
| no location match | 0 | 0 | `no_location_match` | 59.6 ms | 60.9 ms |

Every scenario agreed on state, outcome, turns, and tool calls. Latency differences ranged from −5.8 ms to +7.3 ms — the graph is sometimes faster and sometimes slower, so at this scale its orchestration cost is **not distinguishable from noise**; total latency is dominated by database work.

Three matching candidates is the deepest legal investigation: it spends the full tool budget and hits the four-turn ceiling exactly. A larger matching set short-circuits to `candidate_limit` before any model turn.

`legacy` remains the default. The graph is here because an explicit state machine is easier to reason about and test than a loop — not because it made the agent smarter, which it did not.

### Booking across the domain boundary

Booking is the one operation that spans both services. The referral domain decides *whether* to ask; the provider domain decides *what happens*.

```mermaid
sequenceDiagram
    autonumber
    participant R as Referral API
    participant P as Provider service
    R->>R: record confirmation, state BOOKING
    R->>P: book slot (idempotency key)
    Note over P: one transaction:<br/>lock slot, verify provider,<br/>mark busy, insert appointment
    P-->>R: outcome + appointment id
    R->>R: state CONFIRMED
```


## Reliability

- **Durable workflows** via Inngest: retryable and memoized steps, waits, timeouts, and cancellation.
- **Idempotent commands**: document arrival, slot selection, confirmation, and cancellation are deduplicated, so a retried or duplicated request cannot double-apply.
- **Effectively-once booking**: confirmation-gated and row-locked inside the provider database, so concurrent confirmations cannot produce two appointments for one slot. Proven by tests that race several bookings at a single slot.
- **Recovery**: lost responses and stale slot selections are handled explicitly rather than assumed away.

## Observability

Applications export OTLP to an OpenTelemetry Collector and know nothing about the storage or visualization backends. Traces cross both services in one trace via W3C Trace Context, and cover workflow coordination, model interpretation, investigator turns, gateway retry attempts, deterministic validation, and completion.

**Privacy is enforced at export time.** A filtering span exporter rebuilds every span immediately before it leaves the process, permitting only an explicit attribute allowlist and dropping event and link payloads. Referral reasons, prompts, raw model output, patient and provider identifiers, member IDs, names, document data, SQL text, URLs, and status descriptions cannot reach the exporter — including attributes created by automatic instrumentation before any application hook runs.

A workflow-run UUID is carried as a separate domain correlation identifier and is deliberately **not** the OpenTelemetry trace ID.

Metrics follow the same path: applications export OTLP to the Collector, which exposes a Prometheus scrape endpoint, and Grafana links a metric back to a representative trace through exemplars. Domain instruments cover deterministic validation outcomes, workflow terminal states, investigator turn counts, model latency and confidence, gateway retries, and booking results, with alert rules for the fail-closed rate, retry exhaustion, and any duplicate booking.

Metric labels pass through their own allowlist, because on the metrics side privacy and cardinality are the same constraint: an identifier used as a label value is both a data leak and an unbounded time series.

## Quickstart

Requires Docker and Docker Compose. For live model inference, also [Ollama](https://ollama.com).

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api python -m app.seed
```

| Service | URL |
|---|---|
| Dashboard | http://localhost:3001 |
| API docs | http://localhost:8000/docs |
| Provider service docs | http://localhost:8010/docs |
| Inngest dev server | http://localhost:8288 |
| Grafana | http://localhost:3002 |
| Prometheus | http://localhost:9090 |
| Redis (domain events) | localhost:6379 |

The system runs in `deterministic` mode by default and needs no network or API key. To use a local model:

```bash
ollama pull gemma3:4b
ollama serve
```

Then set `MODEL_PROVIDER=ollama` in `.env` and restart. `gemini` is also implemented and requires `GEMINI_API_KEY`.

Trace storage is intentionally not published on a host port; view traces through Grafana's Explore view with the pre-provisioned Tempo data source.

## Verification

```bash
cd backend && .venv/bin/python -m pytest -q
```

```bash
cd frontend && npm run lint && npm run build
```

```bash
docker compose exec api careroute-benchmark
```

The benchmark is a deterministic 19-case suite with hidden ground truth and **no LLM judge**. It measures routing correctness, duplicate bookings, forbidden-action rate, and resume-recovery rate. Automated tests never call an external model.

Evaluation fixtures are tagged and server-side isolated: ordinary requests cannot opt into them, and they are excluded from product lists and normal candidate discovery.

### Concurrency tests require PostgreSQL

Most of the suite runs on SQLite and needs no network. The booking concurrency tests cannot: SQLite has no `SELECT ... FOR UPDATE`, so the row-locking that makes booking effectively-once is impossible to exercise there. Those tests run against real PostgreSQL and **skip silently when none is reachable** — a green run with skips has not tested the locking.

With the stack up, PostgreSQL is published on `localhost:55432` and they run as part of the normal suite:

```bash
cd backend && .venv/bin/python -m pytest tests/test_booking_concurrency.py -q
```

Point them elsewhere with `CAREROUTE_TEST_DATABASE_URL`. Expect 5 skips rather than 5 passes if the database is unreachable.

These tests race several confirmations at a single appointment slot and assert that exactly one appointment results. One of them documents a deliberate property of the current design: slot *selection* is not a reservation, so several referrals may hold the same selection and booking is the arbiter.

## API surface

Referrals (`GET`/`POST /api/referrals`, detail, `POST .../process`, `POST .../process/durable`), commands (`documents`, `slot-selection`, `confirmation`, `cancel`), reads (`patients`, `patients/{id}/fhir`, `providers`, `slots`, `appointments`), and workflow inspection (`workflows/{id}`, `workflows/{id}/events`). Full schema at `/docs`.

### MCP

Nine administrative tools are exposed over the official Python MCP SDK via stdio:

`getReferral`, `getPatient`, `getCoverage`, `getReferralDocuments`, `getReferralHistory`, `getRecentProcedures`, `requestMissingDocument`, `findProviders`, `getAvailableSlots`.

Eight are annotated read-only; only `requestMissingDocument` writes, and it performs an administrative request rather than a clinical action.

```bash
cd backend && python -m app.mcp_server
```

## Stack

Python 3.12+, FastAPI, SQLAlchemy, Alembic, PostgreSQL 16, Inngest, OpenTelemetry, Grafana Tempo, Grafana, Next.js 15, React 19, Docker Compose.

## Limitations

Stated plainly, because a system like this is only as trustworthy as its honesty about what it isn't:

- Booking's guarantee holds because the slot and appointment share one transaction in the provider database. It is not a distributed guarantee and is not claimed as one.
- The referral state change that follows a booking is a second transaction in the other database. The appointment is authoritative if one is interrupted, and repair is forward-only. There is no automatic reconciler yet.
- The outbox and relay publish domain events, but nothing consumes them yet.
- The API does not currently recover from saturation: under sustained overload every pooled connection ends up `idle in transaction` and the service stops serving until the process is restarted. This is a known open defect, not a tuning parameter.
- Provider matching uses case-insensitive location substring comparison — not geocoding, distance, travel time, payer network, language, or subspecialty.
- The Inngest dev server keeps run history in memory; domain state lives in PostgreSQL.
- No end-user authentication or authorization, binary document storage, payer integration, centralized logging, or hosted deployment.
- Local Grafana runs with anonymous access for demo convenience. **Do not expose it outside localhost as configured.**
- Model latency depends entirely on local hardware. Unit tests use SQLite and do not replace PostgreSQL integration coverage.

## License

MIT — see [LICENSE](LICENSE).

The license disclaims warranty and liability. That disclaimer is load-bearing here: this is a synthetic-data demonstration system, not clinical software, and it must not be used with real patient data or in any care-delivery setting.
