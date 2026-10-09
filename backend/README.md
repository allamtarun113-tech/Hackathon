# PagerMind backend

FastAPI API, LangGraph agent, Hindsight memory worker, seed importers and the offline evaluation harness.

```bash
uv sync --frozen --extra dev   # locked dependencies
cp ../.env.example ../.env     # one root .env shared with the frontend; fill in the blanks

uv run pagermind-bank apply                      # 2. configure the Hindsight bank (before loading data)
uv run pagermind-import all                      # 3. import the seed dataset and queue memory documents
uv run uvicorn --factory app.main:create_app --reload   # 4. API on http://localhost:8000 (docs at /docs)
uv run python -m app.workers.memory_worker       # 7. memory worker (heartbeats + health snapshots)
uv run pytest -m "not integration"               # unit, contract and guardrail tests
TEST_DATABASE_URL=postgresql://... uv run pytest -m integration   # real Postgres, migrated from empty
uv run pagermind-eval run --limit 5 --repetitions 1               # 11. quick live evaluation
```

See `docs/PRODUCTION.md` for the architecture, write boundary, memory lifecycle and deployment pipeline.
