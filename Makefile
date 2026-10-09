.PHONY: help lock install workflow-eval workflow-eval-live workflow-dataset migrate bank bank-verify bank-audit import api worker web lint test test-integration guardrails validate-data eval eval-quick eval-gate probe memory-review e2e smoke

PG_TEST_URL ?= postgresql://postgres:postgres@localhost:5432/postgres
API_PORT ?= 8000

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-17s %s\n", $$1, $$2}'

lock: ## Re-lock dependencies and regenerate every requirements file
	cd backend && uv lock && \
		uv export --frozen --no-dev --no-emit-project --format requirements-txt -q -o requirements.txt && \
		uv export --frozen --extra dev --no-emit-project --format requirements-txt -q -o requirements-dev.txt
	cd frontend && npm install --package-lock-only

install: ## Install locked backend and frontend dependencies
	cd backend && uv sync --frozen --extra dev
	cd frontend && npm ci

migrate: ## 1. Apply Supabase migrations (requires `supabase link`)
	supabase db push

bank: ## 2. Configure the Hindsight bank and webhook, then verify (locally without a public URL: make bank WEBHOOK=0)
	cd backend && uv run --frozen pagermind-bank apply $(if $(filter 0,$(WEBHOOK)),--no-webhook,)

bank-verify: ## Read back the live bank config (Memory Defense, observations, verbose extraction); fails if missing
	cd backend && uv run --frozen pagermind-bank verify

bank-audit: ## Read-only: compare the bank's documents, tags, directives and webhook with expectations
	cd backend && uv run --frozen pagermind-bank audit

import: ## 3. Import the seed dataset and queue memory documents
	cd backend && uv run --frozen pagermind-import all

api: ## 4/5. Run the FastAPI API with the LangGraph agent
	cd backend && uv run --frozen uvicorn --factory app.main:create_app --reload --port $(API_PORT)

web: ## 6. Run the Next.js interface
	cd frontend && npm run dev

worker: ## 7. Run the memory worker (also writes heartbeats and health snapshots)
	cd backend && uv run --frozen python -m app.workers.memory_worker

lint: ## Lint and type-check everything
	cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy app evals
	cd frontend && npm run lint && npm run typecheck

test: ## Unit and contract tests (no external services)
	cd backend && uv run --frozen pytest -m "not integration"

test-integration: ## Postgres integration tests (fresh database migrated from empty): make test-integration PG_TEST_URL=...
	cd backend && TEST_DATABASE_URL=$(PG_TEST_URL) uv run --frozen pytest -m integration

guardrails: ## 9. Run the 200-case guardrail suite
	cd backend && uv run --frozen pagermind-eval guardrails

validate-data: ## Validate the seed dataset
	uv run --frozen --project backend python scripts/validate_dataset.py
	uv run --frozen --project backend python scripts/validate_architecture_data.py

workflow-dataset: ## Regenerate the workflow evaluation dataset (data/evaluation/workflow_cases.jsonl)
	cd backend && uv run --frozen pagermind-eval workflow generate

workflow-eval: ## Workflow evaluation, offline (365 cases, real app + Postgres): make workflow-eval PG_TEST_URL=...
	cd backend && WORKFLOW_EVAL_DATABASE_URL=$(PG_TEST_URL) uv run --frozen pagermind-eval workflow run

workflow-eval-live: ## Workflow evaluation with real Groq + Hindsight: make workflow-eval-live BANK=pagermind-eval-...
	cd backend && WORKFLOW_EVAL_DATABASE_URL=$(PG_TEST_URL) uv run --frozen pagermind-eval workflow run --mode live --bank-id $(BANK)

eval-quick: ## 11. Quick live evaluation (5 scenarios, 1 run)
	cd backend && uv run --frozen pagermind-eval run --limit 5 --repetitions 1

probe: ## Live Hindsight check on a small disposable bank: config, entities, observations, reflect latency
	cd backend && uv run --frozen pagermind-eval probe

eval-gate: ## Live with/without-memory eval on a disposable bank; exits non-zero if any target is missed: make eval-gate REPS=1
	cd backend && uv run --frozen pagermind-eval run --repetitions $(or $(REPS),1) --enforce-targets

memory-review: ## List retain jobs that produced no searchable memory: make memory-review API=https://... TOKEN=...
	curl -fsS -H "Authorization: Bearer $(TOKEN)" "$(or $(API),http://localhost:$(API_PORT))/memory-jobs" | python3 -m json.tool

eval: ## 11. Full evaluation: 100 scenarios x 2 modes x 5 runs
	cd backend && uv run --frozen pagermind-eval run --repetitions 5

e2e: ## Browser journey against a running stack: make e2e WEB=https://... TOKEN=...
	cd frontend && E2E_BASE_URL=$(WEB) E2E_OPERATOR_TOKEN=$(TOKEN) npx playwright test

smoke: ## 12. Smoke test a deployment: make smoke API=https://... WEB=https://...
	python backend/scripts/smoke_test.py --api $(API) $(if $(WEB),--web $(WEB),)
