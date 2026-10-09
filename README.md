# PagerMind

**An on-call assistant that remembers every outage.**

When an alert fires, PagerMind answers it twice, side by side:

| Without memory | With Hindsight |
|---|---|
| A plain LLM answer built only from the alert and its logs | An answer from [Hindsight](https://hindsight.vectorize.io/) `reflect` over your team's incident history, citing the past incidents it relies on |
| Generic advice ("check the logs, restart the pods") | What matched before, what failed, what fixed it, and what changed recently |

Every incident it helps resolve becomes memory, so the **next** incident with the same error signature is answered
better. That learning loop, **cite → learn → retain → re-cite**, is the product.

---

## Contents

1. [The problem](#1-the-problem)
2. [How PagerMind works](#2-how-pagermind-works)
3. [How Hindsight is used](#3-how-hindsight-is-used)
4. [Repository tour](#4-repository-tour)
5. [Accounts you need](#5-accounts-you-need)
6. [Run it locally](#6-run-it-locally)
7. [Deploy to production](#7-deploy-to-production)
8. [Environment variables](#8-environment-variables)
9. [Command reference](#9-command-reference)
10. [Testing and evaluation](#10-testing-and-evaluation)
11. [Operating PagerMind](#11-operating-pagermind)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. The problem

At 3 AM the payment page stops working. The engineer on call has never seen this error. They search old chat
threads, wake up a senior colleague, and spend 40 minutes on a problem the team has already solved three times.
Team knowledge lives in people's heads and in postmortems nobody reads, and it leaves when they do.

PagerMind puts that knowledge in front of the on-call engineer, with evidence, in seconds.

The repository ships with a realistic synthetic company, **Kartly** (an Indian shopping and payments platform):
15 services, **500 historical incidents**, 800 deployments, 16 runbooks, engineers and teams, and **100 held-out
alert scenarios** used only for evaluation.

---

## 2. How PagerMind works

### 2.1 The big picture

```
                       ┌──────────────────────────────────────────────────────────┐
 Engineer's browser    │ Next.js app (Vercel)                                     │
 ────────────────────► │  chat · side-by-side answers · evidence · learning loop  │
                       └───────────────┬──────────────────────────────────────────┘
                                       │ HTTPS + Server-Sent Events (streamed progress)
                       ┌───────────────▼──────────────────────────────────────────┐
                       │ FastAPI API (Render)                                     │
                       │  LangGraph agent: one deterministic graph per chat turn  │
                       └──┬──────────────┬──────────────┬─────────────────────────┘
                          │              │              │
              ┌───────────▼───┐  ┌───────▼──────┐  ┌────▼───────────────────────┐
              │ Hindsight     │  │ Groq         │  │ Supabase                   │
              │ reflect =     │  │ understand + │  │ Postgres: incidents,       │
              │ grounded      │  │ no-memory    │  │ conversations, feedback,   │
              │ answer        │  │ baseline     │  │ outbox · Storage: logs     │
              └───────▲───────┘  └──────────────┘  └────┬───────────────────────┘
                      │ retain (async)                   │ memory_outbox
              ┌───────┴──────────────────────────────────▼───────────────────────┐
              │ Memory worker (Render background worker)                         │
              │  drains the outbox into Hindsight, reconciles, writes heartbeats │
              └──────────────────────────────────────────────────────────────────┘
                      ▲ signed webhook: retain.completed / memory_defense.triggered
```

* **Frontend** (`frontend/`): Next.js 15 / React, deployed on Vercel. It holds no service credentials.
* **API** (`backend/app/`): FastAPI. Every chat turn runs a LangGraph state machine and streams progress to the
  browser.
* **Hindsight**: the memory. It writes the grounded answer (`reflect`) and stores every incident, runbook and
  verified outcome (`retain`).
* **Groq**: a fast LLM, used only to extract the incident details and to write the "without memory" answer. It
  never writes the grounded answer.
* **Supabase**: Postgres is the system of record; Storage keeps uploaded logs and runbooks.
* **Memory worker**: the only process that writes to Hindsight, through a transactional outbox.

### 2.2 What happens on one chat turn

The agent is a fixed graph. The application, not the LLM, decides when memory is read and written.

```
validate_input ─► redact_sensitive_data ─► understand_incident ─┬─► request_clarification ─────────────┐
                                                                │                                      │
                                                                └─► fetch_recent_changes               │
                                                                      ─► hindsight_reflect (+ baseline) │
                                                                      ─► validate_evidence              │
                                                                      ─► validate_recommendations       │
                                                                                  │                     │
                                              save_message_and_trace ◄────────────┴─────────────────────┘
                                                        ─► return_response
```

| Node | What it does | Code |
|---|---|---|
| `validate_input` | Length limits; verifies attachments (owner, size, SHA-256); rejects off-topic messages | `backend/app/agent/nodes.py` |
| `redact_sensitive_data` | Removes secrets and personal data; neutralises prompt-injection text in logs | `backend/app/guardrails/` |
| `understand_incident` | Groq extracts service, severity and symptoms; error signatures come from the logs deterministically, plus the model | `backend/app/agent/understanding.py` |
| `request_clarification` | Asks one focused question if the service or symptom is unknown | `nodes.py` |
| `fetch_recent_changes` | Deployments to the service and its dependencies in the last 24 h | `backend/app/db/repositories/deployments.py` |
| `hindsight_reflect` | Hindsight `reflect` and the Groq baseline run **concurrently** | `backend/app/agent/grounding.py` |
| `validate_evidence` | Citations must exist in Supabase **and** have been retrieved in this request; decides strong / weak / novel | `grounding.py`, `backend/app/guardrails/output_guard.py` |
| `validate_recommendations` | Flags destructive commands for approval, strips execution claims, redacts everything again | `output_guard.py`, `sanitize.py` |
| `save_message_and_trace` | Persists the message, tool calls, guardrail results and audit events; opens a live incident | `nodes.py` |

If Hindsight is slow or down, the turn **degrades to the baseline answer and says so**. It never fails silently.

### 2.3 The learning loop (the demo)

The workspace opens with a four-step panel that reads live API state:

1. **Alert A.** Hindsight cites past incidents. The card says *"This answer used N memories from M incidents"*,
   with one sentence on why the match is strong, weak or novel, tied to the error signatures.
2. **The engineer marks a step** **✓ Worked** / **✗ Didn't work**. This is stored as *provisional*.
3. **Resolve.** The postmortem and the verified step outcomes go through the outbox. You watch the retain job move
   `queued → submitted → retained`. A retain that extracts zero facts shows as **not retained**, never as success.
4. **Replay alert A as a new incident.** The panel checks that alert B now cites the incident you just resolved.

### 2.4 How memory is written (safely)

```
feedback ──► provisional ──(resolver verifies)──► incident action + memory document
                                                          │ same DB transaction
                                                          ▼
                                                   memory_outbox (pending)
                                                          │ memory worker
                       checksum ok? source exists? no secrets? no evaluation data?
                                                          │
                                                          ▼
                                    Hindsight retain (async, stable operation ID)
                                                          │
                     signed webhook / reconciler confirms via the operations API
                                                          ▼
                          retained   or   needs_review (0 facts)   or   rejected
```

* Feedback never reaches memory until a person verifies it.
* A document and its source record are written in **one** Postgres transaction (transactional outbox).
* Jobs for one document run strictly in order; resubmitting is idempotent.
* A webhook is trusted only after it's confirmed with Hindsight's operations API. If a webhook is lost, the
  reconciler completes the job anyway.
* Memory Defense blocks are rejected immediately (no retry loop) and appear in the operator review queue.

---

## 3. How Hindsight is used

Hindsight is the core of PagerMind, not an add-on. Every Hindsight call goes through one gateway,
`backend/app/memory/client.py`, which handles timeouts, retries and the circuit breaker in one place.

### 3.1 The bank (`backend/app/memory/bank.py`)

One shared bank, `pagermind-incidents`, configured as code and applied with `make bank`:

| Setting | Value |
|---|---|
| Background | Kartly's business context (peak hours, "Midnight Deals") |
| Retain mission | Extract service, exact error signatures, actions and their outcomes, root cause, fix, commands |
| Observations | Enabled. The mission tracks recurring signatures, fixes that work and actions that never work |
| Reflect mission | Act as a senior on-call engineer; match on error signatures and confirmed causes; abstain when weak |
| Directives (8) | Never invent incident IDs, never claim execution, correlation ≠ causation, no secrets, warn on destructive commands, prefer the newest fix, abstain when weak, never use evaluation data |
| Entity labels | Controlled vocabularies for service, team, severity, outcome…; `error_signature` is free text, several per fact |
| Extraction | `verbose` |
| Memory Defense | `sensitive_data` → redact, `prompt_injection` → block (each applied only if your Hindsight plan includes it) |

### 3.2 What is retained (`backend/app/memory/documents.py`)

| Memory type | Document ID | Update mode | When |
|---|---|---|---|
| Incident postmortem | `incident:<ID>` | replace | Seed import; every live-incident resolution (v1, v2, …) |
| Verified step outcome | `feedback:<incident ID>` | append | When a resolver or operator verifies feedback |
| Approved runbook | `runbook:RB-<service>` | replace | Seed import |

Every document carries:

* **The same tag** live reflect filters on: `environment:production`. A test fails if any memory type doesn't
  carry it.
* **Explicit entities**: the incident ID, the service, and **each error signature verbatim**, so the
  signatures Hindsight matches on are first-class entities, not prose the extractor might miss.
* **Metadata**: the incident ID, the Supabase record ID and the review status. This is how a fact is traced back
  to an incident.

### 3.3 How it is read (`backend/app/agent/grounding.py`)

* **Query**: only the search signal: error signatures, service, symptoms.
* **Context**: the answer rules, recent deployments and the redacted log excerpt.
* **Tags**: `environment:production`, never filtered by service, so cross-service signature matches still work.
* **Budget**: `high` for SEV1 (25 s limit), `mid` otherwise (12 s limit). Reflect is never retried.
* **Structured output**: the `Recommendation` schema (`backend/app/domain/recommendation.py`).
* **Evidence**: facts, observations and mental models. Observations and mental models are knowledge Hindsight
  consolidated across incidents; the incidents they name count as evidence.

The **live graph and the evaluation call the exact same functions**, so measured quality is the quality engineers
get.

---

## 4. Repository tour

```
.
├── backend/                     Python 3.12, FastAPI, LangGraph (deployed to Render)
│   ├── app/
│   │   ├── main.py, container.py   App factory and composition root (the only place clients are built)
│   │   ├── api/routes/             HTTP endpoints: chat (SSE), feedback, incidents, files, memory-jobs, webhooks, health
│   │   ├── agent/                  The chat turn: graph.py, nodes.py, understanding.py, grounding.py, prompts.py
│   │   ├── memory/                 Hindsight: client.py (gateway), bank.py (config), documents.py, setup.py (CLI), audit.py
│   │   ├── services/               Use cases: feedback, resolution, outbox, webhooks, health
│   │   ├── workers/                memory_worker.py: outbox → Hindsight, reconciler, heartbeats
│   │   ├── guardrails/             Input, file, memory and output guards; recursive sanitiser
│   │   ├── importers/              Seed data → Supabase + memory outbox (pagermind-import)
│   │   ├── db/                     Connection pool and all SQL (db/repositories/)
│   │   ├── llm/                    Groq client with model fallback
│   │   ├── domain/                 API contracts and the Recommendation model
│   │   ├── core/                   Config, errors, logging, redaction, retries + circuit breakers
│   │   └── observability/          Langfuse tracing, Sentry
│   ├── evals/                   Evaluation harness (pagermind-eval): guardrails, workflow, live with/without memory, probe
│   ├── tests/                   Unit, contract and Postgres integration tests
│   └── Dockerfile               Used by both the API and the worker on Render
├── frontend/                    Next.js (deployed to Vercel)
│   └── src/
│       ├── app/page.tsx            The workspace
│       ├── components/             LearningLoop, RecommendationCard, EvidencePanel, StepFeedback, ResolveIncidentForm, …
│       ├── hooks/                  useIncidentChat (SSE stream), useMemoryJob (retain progress)
│       └── lib/                    API client and types
├── supabase/migrations/         Postgres schema, Storage buckets, views, indexes (source of truth)
├── data/                        Seed dataset: incidents, deploys, engineers, runbooks, logs, held-out scenarios
├── scripts/                     Dataset generators and validators
├── docs/PRODUCTION.md           Deeper notes on architecture, capacity and the write boundary
├── .github/workflows/           ci.yml, deploy.yml, evaluate.yml, smoke.yml
├── render.yaml                  Render Blueprint: API + memory worker
├── Makefile                     Every command you need (run `make help`)
└── .env.example                 Every setting, documented
```

---

## 5. Accounts you need

All have free tiers that are enough for a demo.

| Service | What for | What you copy out of it |
|---|---|---|
| [Supabase](https://supabase.com) | Postgres + file storage | Session pooler connection string, project URL, `service_role` key, project ref, DB password |
| [Hindsight](https://hindsight.vectorize.io) | Memory | API key |
| [Groq](https://console.groq.com) | Fast LLM | API key |
| [Render](https://render.com) | Hosts the API and the memory worker | Deploy hook URLs (only for CI/CD) |
| [Vercel](https://vercel.com) | Hosts the frontend | Token, org ID, project ID (only for CI/CD) |
| GitHub | Code + CI/CD | — |
| [Langfuse](https://langfuse.com), [Sentry](https://sentry.io) | Optional: traces and errors | Keys / DSN |

Tools on your machine: **Python 3.12**, **[uv](https://docs.astral.sh/uv/)**, **Node 22** (see `frontend/.nvmrc`),
**[Supabase CLI](https://supabase.com/docs/guides/cli)**, `make`, `git`. Docker is optional.

---

## 6. Run it locally

```bash
git clone https://github.com/allamtarun113-tech/Hackathon.git pagermind
cd pagermind
make install                      # backend (uv, locked) + frontend (npm ci)
cp .env.example .env              # ONE .env at the repo root, shared by backend and frontend
```

Fill in `.env`. The minimum for a local run:

```dotenv
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role key>
HINDSIGHT_API_KEY=<hindsight key>
GROQ_API_KEY=<groq key>
OPERATOR_TOKEN=<any long random string, e.g. `openssl rand -hex 32`>
HINDSIGHT_WEBHOOK_SECRET=<any long random string>
```

> Use the Supabase **Session pooler** (port **5432**), not the Transaction pooler (6543). PagerMind sets
> per-connection timeouts, which transaction mode rejects.

Then, in order:

```bash
supabase link --project-ref <ref>
make migrate                     # 1. create the schema and Storage buckets
make bank WEBHOOK=0              # 2. configure the Hindsight bank (no public webhook URL locally; see below)
make import                      # 3. load the seed data and queue 516 memory documents in the outbox
make worker                      # 4. terminal 1: drains the outbox into Hindsight (leave it running)
make api                         # 5. terminal 2: http://localhost:8000/docs
make web                         # 6. terminal 3: http://localhost:3000
```

Locally there's no public URL for Hindsight's webhook, so `make bank WEBHOOK=0` skips registering it. The worker's
reconciler polls Hindsight for completed retains instead, which is slower but complete. The first import retains
516 documents; let the worker finish (`make bank-audit` shows progress) before judging answers.

**Try it:** pick an engineer, then paste:

```
payments-api p99 latency 10s and 5xx at 13%.
WARN HikariPool-1 - Connection is not available, request timed out after 30000ms.
```

---

## 7. Deploy to production

The production layout is **Supabase** (database) + **Render** (API + worker) + **Vercel** (frontend) +
**Hindsight** (memory) + **Groq** (LLM). Follow the steps in order; each one depends on the previous one.

### Step 1: Supabase

1. Create a project. Note the **project ref** (in the URL) and the **database password**.
2. *Project Settings → Database → Connection string → Session pooler*: copy it as `DATABASE_URL`.
3. *Project Settings → API*: copy the **Project URL** (`SUPABASE_URL`) and the **service_role** key
   (`SUPABASE_SERVICE_ROLE_KEY`). Never put the service_role key in the frontend.
4. From your machine:
   ```bash
   supabase link --project-ref <ref>
   make migrate
   ```

### Step 2: Hindsight and Groq keys

* Hindsight: create an API key → `HINDSIGHT_API_KEY`.
* Groq: create an API key → `GROQ_API_KEY`.
* Generate two secrets: `openssl rand -hex 32` → `OPERATOR_TOKEN`, and again → `HINDSIGHT_WEBHOOK_SECRET`.

### Step 3: Render (API + memory worker)

1. Push this repository to GitHub.
2. Render → **New → Blueprint** → select the repo. Render reads `render.yaml` and creates:
   * `pagermind-api`: a web service (Docker, health check `/health/ready`)
   * `pagermind-memory-worker`: a background worker (same image, runs `python -m app.workers.memory_worker`)
   * the env group `pagermind-backend`
3. Fill in the secrets Render asks for (`sync: false` in `render.yaml`):

   | Variable | Value |
   |---|---|
   | `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | from Step 1 |
   | `HINDSIGHT_API_KEY`, `GROQ_API_KEY` | from Step 2 |
   | `OPERATOR_TOKEN`, `HINDSIGHT_WEBHOOK_SECRET` | from Step 2 |
   | `CORS_ALLOWED_ORIGINS` (API only) | your Vercel URL, e.g. `https://pagermind.vercel.app` (fill in after Step 4) |
   | `HINDSIGHT_WEBHOOK_URL` (API only) | `https://<your-api>.onrender.com/webhooks/hindsight` |
   | `LANGFUSE_*`, `SENTRY_DSN` | optional; leave empty to disable |

4. Deploy both services. Check `https://<your-api>.onrender.com/health/ready` returns `{"status":"ready"}`.

> `render.yaml` has `autoDeploy: false` on purpose: the GitHub `deploy.yml` workflow deploys only after CI passes
> (Step 7). For a one-off deploy, click **Manual Deploy** in Render.

### Step 4: Vercel (frontend)

1. Vercel → **Add New → Project** → import the repo.
2. Set **Root Directory** to `frontend`. Vercel detects Next.js.
3. Environment variables:

   | Variable | Value |
   |---|---|
   | `NEXT_PUBLIC_API_BASE_URL` | `https://<your-api>.onrender.com` (no trailing slash) |
   | `NEXT_PUBLIC_APP_ENV` | `production` |
   | `NEXT_PUBLIC_SENTRY_DSN` | optional |

4. Deploy. Put the resulting URL in Render's `CORS_ALLOWED_ORIGINS` and redeploy the API.

### Step 5: Configure the Hindsight bank (with the webhook)

On your machine, set the **production** values in `.env` (including `HINDSIGHT_WEBHOOK_URL` from Step 3), then:

```bash
make bank          # creates/converges the bank, syncs the 8 directives, registers the webhook, then verifies
```

`make bank` reads the configuration back from Hindsight and exits non-zero if anything is missing. If your plan
lacks a Memory Defense detector, it says which one and continues: PagerMind's own guardrails still screen that
content.

### Step 6: Load the data into production

Still with the production `.env`:

```bash
make import        # seeds Supabase, uploads logs and runbooks to Storage, queues 516 memory documents
```

The **Render memory worker** picks up the queued documents and retains them in Hindsight. Watch progress:

```bash
make bank-audit    # read-only: documents, tag scope, directives, webhook, config; exits 0 when all is well
```

### Step 7: Automated deploys (GitHub Actions, optional but recommended)

Once configured, every push to `main` runs CI, and if CI passes, `deploy.yml` will:
migrate Supabase → deploy Render (pinned to the commit) → wait for readiness → build a Vercel preview → smoke-test it →
promote it to production → smoke-test production.

In GitHub → *Settings → Secrets and variables → Actions*, add:

| Name | Kind | Where to find it |
|---|---|---|
| `SUPABASE_ACCESS_TOKEN` | secret | Supabase → Account → Access Tokens |
| `SUPABASE_DB_PASSWORD` | secret | Step 1 |
| `RENDER_API_DEPLOY_HOOK`, `RENDER_WORKER_DEPLOY_HOOK` | secret | Render → each service → Settings → Deploy Hook |
| `VERCEL_TOKEN` | secret | Vercel → Account Settings → Tokens |
| `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | secret | `frontend/.vercel/project.json` after running `npx vercel link` in `frontend/` |
| `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `HINDSIGHT_API_KEY`, `GROQ_API_KEY` | secret | for the scheduled live evaluation (`evaluate.yml`) |
| `SUPABASE_PROJECT_REF` | variable | Step 1 |
| `PAGERMIND_API_URL` | variable | `https://<your-api>.onrender.com` |
| `PAGERMIND_WEB_URL` | variable | your Vercel production URL |

Also create a GitHub **environment** named `production` (the deploy jobs use it, so you can add required reviewers).

### Step 8: Verify the deployment

```bash
make smoke API=https://<your-api>.onrender.com WEB=https://<your-app>.vercel.app
make probe          # live Hindsight check on a small disposable bank: config, entities, observations, latency
make e2e WEB=https://<your-app>.vercel.app TOKEN=<OPERATOR_TOKEN>   # browser journey through the full learning loop
```

---

## 8. Environment variables

All variables live in one `.env` at the repo root locally, and in Render (backend) / Vercel (frontend) in
production. `.env.example` documents every one. `.env` is git-ignored; never commit it.

| Variable | Required | Used by | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | backend | Supabase Postgres, Session pooler (port 5432) |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | yes | backend | Storage for logs and runbooks (server only) |
| `HINDSIGHT_API_KEY` | yes | backend | Memory |
| `HINDSIGHT_BANK_ID` | no (`pagermind-incidents`) | backend | Bank name |
| `HINDSIGHT_WEBHOOK_URL` | production | API | Public `…/webhooks/hindsight` URL, registered by `make bank` |
| `HINDSIGHT_WEBHOOK_SECRET` | production | backend | HMAC secret for webhook signatures |
| `HINDSIGHT_REFLECT_TIMEOUT_SECONDS` | no (12) | backend | Reflect limit at `mid` budget |
| `HINDSIGHT_REFLECT_HIGH_TIMEOUT_SECONDS` | no (25) | backend | Reflect limit for SEV1 (`high` budget) |
| `GROQ_API_KEY` | yes | backend | Understanding, baseline, evaluation judge |
| `GROQ_PRIMARY_MODEL`, `GROQ_FALLBACK_MODEL` | no | backend | Models (fallback is used when the primary fails) |
| `OPERATOR_TOKEN` | yes | backend | Protects resolve / review / internal reads. Empty = those endpoints are disabled |
| `CORS_ALLOWED_ORIGINS` | production | API | Your frontend URL(s), comma-separated |
| `TRUSTED_PROXY_HOPS` | no | API | `0` locally, `1` on Render |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | no | backend | Tracing |
| `SENTRY_DSN` | no | backend | Error reporting |
| `NEXT_PUBLIC_API_BASE_URL` | yes | frontend | The API URL (public) |
| `NEXT_PUBLIC_SENTRY_DSN`, `NEXT_PUBLIC_APP_ENV` | no | frontend | Browser error reporting |
| `DATABASE_POOL_MAX_SIZE`, `MAX_CONCURRENT_AGENT_RUNS`, `RATE_LIMIT_PER_MINUTE`, `RETENTION_*` | no | backend | Capacity and retention (defaults in `.env.example`) |

---

## 9. Command reference

Run `make help` for the full list. The important ones:

| Command | What it does |
|---|---|
| `make install` | Install locked backend and frontend dependencies |
| `make migrate` | Apply Supabase migrations (`supabase link` first) |
| `make bank` | Create / converge the Hindsight bank, directives and webhook, then verify |
| `make bank-verify` | Read back the bank config and fail if anything required is missing |
| `make bank-audit` | Read-only: compare the bank's documents, tags, directives and webhook with expectations |
| `make import` | Seed Supabase and queue memory documents |
| `make api` / `make worker` / `make web` | Run the API, the memory worker, the frontend |
| `make lint` / `make test` | Lint + types; unit and contract tests |
| `make test-integration PG_TEST_URL=…` | Postgres integration tests (builds a fresh database) |
| `make guardrails` | 200 guardrail cases |
| `make workflow-eval PG_TEST_URL=…` | 365 workflow cases through the real graph |
| `make probe` | Live Hindsight check on a small disposable bank |
| `make eval-gate REPS=1` | Live with-vs-without-memory evaluation on a disposable bank; fails below target |
| `make memory-review TOKEN=…` | Retains that produced no searchable memory |
| `make smoke API=… WEB=…` | Post-deploy checks |

Backend CLIs (from `backend/`): `uv run pagermind-bank --help`, `uv run pagermind-import --help`,
`uv run pagermind-eval --help`.

---

## 10. Testing and evaluation

| Suite | What it proves | Run |
|---|---|---|
| Unit + contract | Guardrails, Hindsight/Groq response handling, grounding rules, circuit breakers | `make test` |
| Integration | API, chat journey, outbox ordering and concurrency, webhooks, migrations from empty | `make test-integration PG_TEST_URL=…` |
| Guardrail suite | 200 cases: every input and output guardrail decision | `make guardrails` |
| Workflow evaluation | 365 cases: routing, attachments, evidence, safety, resilience, end to end | `make workflow-eval PG_TEST_URL=…` |
| Live probe | Real Hindsight accepts the config and entities; observations form; reflect latency vs limits | `make probe` |
| Answer quality | 100 held-out scenarios, with vs without memory, judged by a second LLM | `make eval-gate REPS=1` |

**Answer-quality targets** (enforced by `eval-gate`):

| Metric | Target |
|---|---|
| Recall@3 (the right past incident is in the top 3) | ≥ 0.90 |
| Novel-problem abstention (says "this looks new" when it is) | ≥ 0.90 |
| Citation validity (never cites an incident that wasn't retrieved) | = 1.0 |
| With-memory win rate vs the plain LLM | ≥ 0.70 |
| Unsafe, unflagged commands | = 0 |
| p95 latency | ≤ 15 s |
| Memory failure rate (reflect timed out / errored) | ≤ 0.05 |

The evaluation runs the **same** understanding and grounding code as production, against a disposable bank loaded
with production's documents minus the held-out cases, and waits for Hindsight to finish building observations
before scoring.

---

## 11. Operating PagerMind

* **Health**: `GET /health/live` (process up), `GET /health/ready` (database reachable), `GET /health` (latest
  snapshot: Hindsight bank reachable, worker heartbeat, outbox counts; failed observation consolidation = degraded).
* **Memory review queue**: in the UI (bottom of the page, needs the operator key), or `make memory-review`. Lists
  retains that ended with nothing searchable: zero facts extracted, guardrail rejection, Memory Defense block,
  dead letter.
* **Bank audit**: `make bank-audit` (read-only). Flags legacy documents, documents outside reflect's tag scope,
  empty documents, missing documents, missing directives and an unregistered webhook.
* **Legacy cleanup**: `cd backend && uv run pagermind-bank prune-legacy` lists documents PagerMind never writes (for
  example from an old seed script); add `--apply` to delete them.
* **Circuit breakers**: shared across every API instance and the worker through Postgres. When Hindsight or Groq
  is down, the whole deployment stops calling it within seconds and answers with the disclosed fallback.

**Access model.** PagerMind has no sign-in. Engineers pick their name for attribution. Anything that changes
canonical data or promotes feedback into memory (resolve, review, internal reads) requires the deployment's
`OPERATOR_TOKEN`, typed into the UI by the person running the demo. It's kept only in that browser tab's
`sessionStorage`, never in the build. With no token configured, those endpoints are disabled.

---

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Every answer says "Hindsight memory was unavailable" | Wrong `HINDSIGHT_API_KEY`, the bank doesn't exist (run `make bank`), or reflect is slower than the limit: check `/health`, run `make probe`, and raise `HINDSIGHT_REFLECT_TIMEOUT_SECONDS` if needed |
| Answers never cite past incidents | Memory not loaded yet: is the worker running? `make bank-audit` shows missing documents |
| Retain jobs stuck in `submitted` | Webhook not registered or not reachable: set `HINDSIGHT_WEBHOOK_URL` and re-run `make bank`. The reconciler finishes them anyway, more slowly |
| Jobs in `needs_review` | Hindsight extracted 0 facts from that document: open the memory review queue |
| `make bank` fails with 422 | The bank config sent to Hindsight is malformed; the error body names the field |
| Database errors mentioning prepared statements or startup parameters | You used the Transaction pooler (6543); switch `DATABASE_URL` to the Session pooler (5432) |
| Browser shows CORS errors | `CORS_ALLOWED_ORIGINS` on the API must contain the exact frontend URL |
| Resolve says "operator endpoints are disabled" | `OPERATOR_TOKEN` is not set on the API |
| Replay (learning-loop step 4) doesn't cite the resolved incident | The postmortem must reach `retained` first (step 3). If it's `needs_review`, the resolution text was too thin to extract facts from |
