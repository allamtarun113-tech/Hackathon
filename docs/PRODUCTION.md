# PagerMind — production guide

```
frontend/   Next.js on Vercel. No service credentials in the bundle.
backend/    FastAPI API, LangGraph agent, memory worker, importers and the evaluation harness (Render)
supabase/   Postgres schema, Storage buckets and views (the exact source of truth)
data/       Seed dataset (500 incidents, 100 held-out scenarios, production fixtures)
```

## Build order → code

| # | Step | Where |
|---|------|-------|
| 1 | Supabase schema and Storage | `supabase/migrations/*.sql` |
| 2 | Hindsight bank configuration | `backend/app/memory/bank.py`, `backend/app/memory/setup.py` (`pagermind-bank apply`) |
| 3 | Data importers | `backend/app/importers/` (`pagermind-import all`) |
| 4 | FastAPI API | `backend/app/main.py`, `backend/app/api/` |
| 5 | LangGraph workflow | `backend/app/agent/` (`graph.py`, `nodes.py`, `service.py`) |
| 6 | Next.js interface | `frontend/src/` |
| 7 | Feedback and memory worker | `backend/app/services/feedback_service.py`, `backend/app/workers/memory_worker.py` |
| 8 | Webhook receiver | `backend/app/api/routes/webhooks.py`, `backend/app/services/webhook_service.py` |
| 9 | Guardrails | `backend/app/guardrails/` (input, file, memory, output, recursive sanitiser) |
| 10 | Observability | `backend/app/observability/`, `backend/app/core/logging.py`, `frontend/src/instrumentation-client.ts` |
| 11 | Real evaluations | `backend/evals/` (`pagermind-eval guardrails`, `pagermind-eval run`) |
| 12 | Deployment and smoke tests | `render.yaml`, `frontend/vercel.json`, `.github/workflows/`, `backend/scripts/smoke_test.py` |

## Architecture

### Layers

Imports only point downward. Nothing in a lower layer knows about a higher one.

```
main.py, container.py        composition root: the only place clients and services are constructed
api/, workers/, importers/   entry points: HTTP routes, the memory worker, CLIs
agent/, services/            use cases: the LangGraph turn, feedback, resolution, files, webhooks
db/, storage/, memory/,      adapters: Postgres repositories (all SQL lives in db/repositories),
llm/, guardrails/,             Supabase Storage, Hindsight, Groq, guardrails, tracing
observability/
domain/                      contracts: API DTOs and the Recommendation model
core/                        config, errors, logging, redaction, hashing, resilience
```

* Routes are thin. They validate input, enforce the operator boundary and call a service on the container.
* Services receive exactly the dependencies they use (`FeedbackService(db, bank_id=…)`) rather than the whole
  container, so they can be tested in isolation.
* `create_app()` is a factory with no import-time side effects. Run it with
  `uvicorn --factory app.main:create_app`. Tests pass fakes for Hindsight, Groq and Storage into
  `Container.build`.

### Capacity and failure isolation

| Concern | Mechanism | Setting |
|---------|-----------|---------|
| Too many concurrent chat turns | Per-instance semaphore; extra requests get 503 instead of exhausting the pool | `MAX_CONCURRENT_AGENT_RUNS` |
| Runaway queries | Server-side `statement_timeout` and `idle_in_transaction_session_timeout` on every connection | `DATABASE_STATEMENT_TIMEOUT_MS` |
| Connection budget | Separate pool sizes per role, tagged `pagermind-api` / `pagermind-worker` in `pg_stat_activity` | `DATABASE_POOL_MAX_SIZE`, `WORKER_DATABASE_POOL_MAX_SIZE` |
| Abuse | Postgres-backed fixed-window limiter shared by all instances | `RATE_LIMIT_PER_MINUTE`, `TRUSTED_PROXY_HOPS` |
| Slow or failing dependency | Per-attempt timeouts, jittered retries, circuit breakers; Hindsight down → disclosed LLM fallback | `HINDSIGHT_TIMEOUT_SECONDS`, `GROQ_TIMEOUT_SECONDS` |
| Unbounded tables | Worker prunes webhook deliveries, live health snapshots and guardrail results | `RETENTION_*` |
| Imperfect model output | Over-long or out-of-range fields are trimmed to the schema instead of discarding the answer | — |

Every hot query has a backing index (`supabase/migrations/20261003000000_indexes_and_retention.sql`).

### Dependency management

| Project | Declared in | Locked in | Install |
|---------|-------------|-----------|---------|
| Backend | `backend/pyproject.toml` | `backend/uv.lock` → exported `backend/requirements.txt` / `requirements-dev.txt` (hash-pinned) | `uv sync --frozen` or `pip install --require-hashes -r requirements.txt` |
| Frontend | `frontend/package.json` | `frontend/package-lock.json` | `npm ci` |

`make lock` regenerates every lock and export. CI fails if the exported requirements drift from `uv.lock`.

## Keys to fill in

### Application (root `.env` locally; Render env group and Vercel in production)

| Variable | Where | Purpose |
|----------|-------|---------|
| `DATABASE_URL` | backend, Render | Supabase Postgres (session pooler) |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | backend, Render | Storage (server only) |
| `HINDSIGHT_API_KEY` | backend, Render | Memory |
| `HINDSIGHT_WEBHOOK_SECRET` | backend, Render | Webhook HMAC; any long random string |
| `HINDSIGHT_WEBHOOK_URL` | backend, Render (API) | `https://<api>/webhooks/hindsight`, registered by `pagermind-bank apply` |
| `GROQ_API_KEY` | backend, Render | Understanding, baseline, judge |
| `OPERATOR_TOKEN` | backend, Render | Write boundary (see below); long random string |
| `TRUSTED_PROXY_HOPS` | backend (`0` locally), Render (`1`) | Which X-Forwarded-For entry to trust |
| `CORS_ALLOWED_ORIGINS` | backend, Render (API) | The Vercel URL |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `SENTRY_DSN` | backend, Render | Optional observability |
| `NEXT_PUBLIC_API_BASE_URL` | frontend, Vercel | Backend URL |
| `NEXT_PUBLIC_SENTRY_DSN` | frontend, Vercel | Optional browser errors |

### CI/CD (GitHub repository settings)

| Name | Kind | Used by |
|------|------|---------|
| `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `HINDSIGHT_API_KEY`, `GROQ_API_KEY` | secrets | `evaluate.yml` |
| `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD` | secrets (`production` environment) | `deploy.yml` migrations |
| `RENDER_API_DEPLOY_HOOK`, `RENDER_WORKER_DEPLOY_HOOK` | secrets | `deploy.yml` |
| `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | secrets | `deploy.yml` |
| `SUPABASE_PROJECT_REF`, `PAGERMIND_API_URL`, `PAGERMIND_WEB_URL` | variables | `deploy.yml`, `smoke.yml` |

## Write boundary

PagerMind still has no user accounts. Anything that changes canonical data, promotes feedback into memory or
exposes internal records instead requires the deployment-held `OPERATOR_TOKEN` (`Authorization: Bearer …`):

* `POST /incidents/{id}/resolve`
* `POST /feedback/{id}/review`
* `GET /incidents/{id}` and `GET /conversations/{id}`
* `GET /memory-jobs/{id}`, for the error details. The public view shows progress only.

With no token configured these endpoints return 503 (fail closed). In the UI, the person running the demo types the
key into the resolve form. It is kept in `sessionStorage` for that tab and is never part of the build.

## How a chat turn flows

`validate_input → redact_sensitive_data → understand_incident → (request_clarification | fetch_recent_changes →
hindsight_reflect → validate_evidence → validate_recommendations) → save_message_and_trace → return_response`

* **Attachments.** First-turn uploads are claimed by the new conversation in the same transaction. A claim needs
  the same engineer and must happen within `UPLOAD_CLAIM_TTL_MINUTES`; a file that belongs to another
  conversation is never accepted. Before analysis every attachment is checked against its manifest (owner, size,
  SHA-256). Any failure stops the turn and is shown to the engineer.
* **Answer.** Hindsight `reflect` writes the answer as structured output. Groq only extracts incident details and
  produces the "without memory" baseline, which doubles as the disclosed fallback when Hindsight is down.
* **Evidence.** Citations must exist in Supabase *and* have been retrieved in this request. Otherwise they are
  removed and the match is downgraded.
* **Safety.** Execution claims are stripped, destructive commands are flagged for approval and escalated, and
  contradictions are removed. A final recursive pass then redacts and re-checks citations in every string of the
  response: summary, causes, steps, rationales, evidence facts, baseline, abstention and escalation. The SSE events
  and the stored message payload each pass through redaction again.

## How memory is written

1. Feedback is **provisional** when submitted and never creates a memory job.
2. It becomes eligible only when an operator verifies it (`/feedback/{id}/review`), or when the resolver lists it in
   `verified_feedback_ids`. A rating without an outcome never qualifies.
3. Verified feedback is added to the incident's actions, so the postmortem document includes it. The postmortem
   replaces the incident's memory document under the same ID.
4. Outbox → worker: the checksum is validated, the source must exist, there must be no evaluation data and no
   secrets. The worker then submits an async retain with a stable operation ID. Jobs for one document run strictly
   in order.
5. The webhook accepts only the timestamped signature, which defeats replays. The delivery is claimed atomically
   (exactly once), then confirmed with the operations API. **Zero extracted units → `needs_review`, never
   `retained`.** The reconciler completes jobs whose webhook was lost.

## Health

* `GET /health/live`: the process is up. It touches nothing.
* `GET /health/ready`: Supabase is reachable. Read-only. Used as the Render health check.
* `GET /health`: the latest snapshot written by the worker every `HEALTH_SNAPSHOT_INTERVAL_SECONDS`, plus worker
  liveness from the `worker_heartbeats` table. Public requests never write anything.

## Testing

```bash
make test                       # unit + contract tests + 200 guardrail cases (no services needed)
make test-integration PG_TEST_URL=postgresql://postgres:postgres@localhost:5432/postgres
make validate-data              # both dataset validators
make workflow-eval PG_TEST_URL=postgresql://postgres:postgres@localhost:5432/postgres   # 365 workflow cases
make e2e WEB=https://… TOKEN=…  # Playwright browser journey against a running stack
make smoke API=https://… WEB=https://…
```

**Workflow evaluation.** `data/evaluation/workflow_cases.jsonl` is a separate test dataset of 365 cases for the
LangGraph workflow itself. It covers routing, guardrails, attachments, evidence validation, safety, resilience
and recent-change context. `pagermind-eval workflow run` drives the real app against a scratch Postgres with
scripted Groq and Hindsight, and must pass 100% in CI. `--mode live` re-runs the model-agnostic subset against
real services. See `data/evaluation/README.md`.

The integration suite creates a brand-new database, applies every migration from empty, and runs against fake
Hindsight, Groq and Storage. It covers:

* the API, errors, the operator boundary, the shared rate limit and spoofed forwarding headers
* the full demo journey: upload → cited recommendation → baseline → provisional feedback → verified resolution →
  outbox → worker → signed webhook → retained → a later incident citing it
* attachment isolation and checksum tampering, memory outage fallback, clarification, off-topic input and client
  disconnects
* outbox ordering, dedupe, backoff/dead-letter and concurrent multi-worker claims
* webhook replays and concurrent duplicate deliveries

## Deployment

Production promotion runs only from `.github/workflows/deploy.yml`, after **CI** succeeds on `main`. CI covers
lint, types, unit, integration and migration-from-empty tests, guardrails, dataset validation, frontend build,
dependency audits and the Docker build. The deploy workflow then:

1. applies Supabase migrations
2. triggers the Render API and worker deploy hooks pinned to the commit, and waits for `/health/ready`
3. builds a Vercel preview of the same commit
4. smoke-tests the API and the preview
5. promotes the preview to production and smoke-tests production

Render `autoDeploy` and Vercel Git deployments are disabled. Builds are reproducible from `backend/uv.lock`
(`uv sync --frozen`) and `frontend/package-lock.json` (`npm ci`), with Docker base images pinned by digest.
Dependabot proposes updates for all four ecosystems.

## Known limits

* The circuit breakers and the concurrent-turn limit are per process. Rate limiting is shared through Postgres, and
  falls back to per-process limits only if the database is unreachable.
* Engineer selection is attribution only. The operator token is a single shared secret, not per-user identity.
* The Memory Defense rule format in `app/memory/setup.py` follows Hindsight's documented detector/action model.
  Confirm it in the Hindsight console; the setup tolerates rejection, and the app-side guardrails always apply.
* The deploy workflow and the Docker build were written against the platform documentation but have not run yet.
  They need the repository secrets above.
