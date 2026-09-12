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

```text
Browser / Next.js
       |
       v
Referral API (FastAPI) ----------------> PostgreSQL
       |
       +---- local Ollama or Gemini (bounded proposals only)
       |
       | typed HTTP gateway: timeouts, bounded retry/backoff,
       | response validation, correlation IDs, trace propagation
       v
Provider service (FastAPI)

Referral API + Provider service
       |
       | OTLP/HTTP + W3C Trace Context
       v
OpenTelemetry Collector --> Grafana Tempo --> Grafana

Referral API <--> Inngest (durable workflow steps, waits, retries)
```

The provider service independently owns specialty discovery, eligible-provider search, and free-slot reads. The referral API reaches it only through a typed asynchronous gateway that separates transient failures (timeouts, connection errors, 429, selected 5xx) from permanent protocol failures, and fails closed on malformed payloads and correlation mismatches.

The referral API owns referral coordination, model adapters, patient and coverage tools, MCP, FHIR translation, commands, and booking.

### Workflow states

`RECEIVED`, `PARSING`, `VALIDATING`, `WAITING_FOR_DOCUMENTS`, `CHECKING_COVERAGE`, `MATCHING_PROVIDER`, `WAITING_FOR_SLOT_SELECTION`, `BOOKING`, `CONFIRMED`, `NEEDS_HUMAN_REVIEW`, `PROVIDER_UNAVAILABLE`, `COVERAGE_UNVERIFIED`, `BOOKING_FAILED`, `CANCELLED`.

Transitions are deterministic. Ambiguity routes to `NEEDS_HUMAN_REVIEW` rather than to a guess.

## Reliability

- **Durable workflows** via Inngest: retryable and memoized steps, waits, timeouts, and cancellation.
- **Idempotent commands**: document arrival, slot selection, confirmation, and cancellation are deduplicated, so a retried or duplicated request cannot double-apply.
- **Effectively-once booking**: confirmation-gated and row-locked, so concurrent confirmations cannot produce two appointments for one slot.
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

- Both services currently share one PostgreSQL database. The service boundary is real at the API layer; data ownership is not yet distributed.
- Booking's guarantee holds because referral, slot, and appointment records share one transaction. It is not a distributed guarantee and is not claimed as one.
- Provider matching uses case-insensitive location substring comparison — not geocoding, distance, travel time, payer network, language, or subspecialty.
- The Inngest dev server keeps run history in memory; domain state lives in PostgreSQL.
- No end-user authentication or authorization, binary document storage, payer integration, centralized logging, or hosted deployment.
- Local Grafana runs with anonymous access for demo convenience. **Do not expose it outside localhost as configured.**
- Model latency depends entirely on local hardware. Unit tests use SQLite and do not replace PostgreSQL integration coverage.

## License

MIT — see [LICENSE](LICENSE).

The license disclaims warranty and liability. That disclaimer is load-bearing here: this is a synthetic-data demonstration system, not clinical software, and it must not be used with real patient data or in any care-delivery setting.
