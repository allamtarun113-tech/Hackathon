"""Drive the real application through every case and observe what it does.

The full stack runs unchanged: FastAPI routes and middleware, the LangGraph workflow,
guardrails, repositories and Postgres. Only the external services are swapped:

* offline: scripted Groq + Hindsight (deterministic, no keys, CI-safe)
* live:    real Groq + a Hindsight evaluation bank (model-agnostic expectations only)

Storage is always in memory, because file storage is not what is being evaluated.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal, cast

import httpx

from app.container import Container
from app.core.config import Settings
from app.core.context import new_id
from app.db.pool import Database
from app.db.repositories import conversations, incidents
from app.llm.groq_client import GroqClient
from app.main import create_app
from app.memory.client import HindsightGateway
from app.storage.supabase_storage import SupabaseStorage
from evals.workflow.cases import Expectation, Turn, WorkflowCase
from evals.workflow.checks import (
    CheckResult,
    TurnObservation,
    check_expectations,
    check_invariants,
    stored_text,
)
from evals.workflow.database import seed_deployments
from evals.workflow.simulator import MemoryStorage, ScriptedGroq, ScriptedHindsight

Mode = Literal["offline", "live"]


@dataclass
class TurnResult:
    index: int
    expected_outcome: list[str]
    outcome: str
    latency_ms: int
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


@dataclass
class CaseResult:
    case_id: str
    category: str
    description: str
    turns: list[TurnResult] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(turn.passed for turn in self.turns)


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


def derive_outcome(events: list[tuple[str, dict[str, Any]]]) -> tuple[str, str | None]:
    names = {name: data for name, data in events}
    if "error" in names:
        return "error", names["error"].get("code")
    if "rejected" in names:
        return "rejected", None
    if "clarification" in names:
        return "clarification", None
    if "recommendation" in names:
        unavailable = names["recommendation"].get("match_strength") == "unavailable"
        return ("memory_unavailable" if unavailable else "answered"), None
    return "incomplete", None


class WorkflowRunner:
    def __init__(self, settings: Settings, *, mode: Mode, hindsight: Any, groq: Any) -> None:
        self.settings = settings
        self.mode = mode
        self.storage = MemoryStorage()
        self.hindsight = hindsight
        self.groq = groq
        self.db = Database(settings, role="cli")

    @classmethod
    def offline(cls, settings: Settings) -> WorkflowRunner:
        return cls(settings, mode="offline", hindsight=ScriptedHindsight(), groq=ScriptedGroq())

    @classmethod
    def live(cls, settings: Settings, *, bank_id: str) -> WorkflowRunner:
        return cls(
            settings,
            mode="live",
            hindsight=HindsightGateway(settings, bank_id=bank_id),
            groq=GroqClient(settings),
        )

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        async def factory(cfg: Settings) -> Container:
            database = Database(cfg, role="api")
            await database.open()
            # The scripted/in-memory stand-ins implement the same async surface as the real clients.
            return Container.build(
                cfg,
                db=database,
                storage=cast(SupabaseStorage, self.storage),
                hindsight=cast(HindsightGateway, self.hindsight),
                groq=cast(GroqClient, self.groq),
            )

        app = create_app(self.settings, container_factory=factory)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://workflow-eval", timeout=120
            ) as client,
        ):
            yield client

    async def run(
        self, cases: list[WorkflowCase], *, on_case: Callable[[CaseResult], None] | None = None
    ) -> list[CaseResult]:
        await self.db.open()
        results = []
        try:
            async with self._client() as client:
                for case in cases:
                    result = await self._run_case(client, case)
                    results.append(result)
                    if on_case:
                        on_case(result)
        finally:
            await self.db.close()
        return results

    # ------------------------------------------------------------------ one case
    async def _run_case(self, client: httpx.AsyncClient, case: WorkflowCase) -> CaseResult:
        result = CaseResult(case.case_id, case.category, case.description)
        try:
            await seed_deployments(self.db, case.deployments)
            conversation_id: str | None = None
            for index, turn in enumerate(case.turns):
                expect = turn.expect if self.mode == "offline" else turn.expect_live
                assert expect is not None, "live mode only runs cases with live expectations"
                observation, conversation_id = await self._run_turn(client, case, turn, conversation_id)
                checks = check_expectations(expect, observation)
                checks += check_invariants(observation, chat_sent=turn.message is not None)
                result.turns.append(
                    TurnResult(
                        index,
                        [str(o) for o in _expected(expect)],
                        observation.outcome,
                        observation.latency_ms,
                        checks,
                    )
                )
        except Exception as error:  # a crash is a failed case, never a crashed evaluation
            result.error = f"{type(error).__name__}: {error}"
        return result

    async def _run_turn(
        self, client: httpx.AsyncClient, case: WorkflowCase, turn: Turn, conversation_id: str | None
    ) -> tuple[TurnObservation, str | None]:
        obs = TurnObservation()
        file_ids = []
        for attachment in turn.attachments:
            engineer, target_conversation = case.engineer_id, conversation_id
            if attachment.owner == "other_engineer":
                engineer = "arjun" if case.engineer_id != "arjun" else "priya"
                target_conversation = None
            elif attachment.owner == "other_conversation":
                target_conversation = await self._foreign_conversation(case.engineer_id)
            data = {
                "engineer_id": engineer,
                **({"conversation_id": target_conversation} if target_conversation else {}),
            }
            response = await client.post(
                "/files",
                data=data,
                files={"file": (attachment.name, attachment.content.encode(), "text/plain")},
            )
            body = response.json()
            obs.uploads.append((response.status_code, body))
            if response.status_code == 201:
                file_ids.append(body["file_id"])
                if attachment.tamper:
                    self.storage.tamper(f"{body['file_id']}{PurePath(attachment.name).suffix}")

        if turn.message is None:
            return obs, conversation_id

        if self.mode == "offline":
            self.groq.simulation = turn.simulation
            self.hindsight.simulation = turn.simulation

        started = time.perf_counter()
        response = await client.post(
            "/chat",
            json={
                "engineer_id": case.engineer_id,
                "message": turn.message,
                "conversation_id": conversation_id,
                "file_ids": file_ids,
            },
        )
        obs.latency_ms = int((time.perf_counter() - started) * 1000)
        obs.raw_stream = response.text
        obs.events = (
            parse_sse(response.text)
            if response.status_code == 200
            else [("error", response.json().get("error", {}))]
        )
        obs.outcome, obs.error_code = derive_outcome(obs.events)

        started_event = obs.event("conversation") or {}
        conversation_id = started_event.get("conversation_id", conversation_id)
        request_id = started_event.get("request_id") or (obs.event("error") or {}).get("request_id")
        done = obs.event("done") or {}
        await self._collect(obs, request_id=request_id, incident_id=done.get("incident_id"))
        return obs, conversation_id

    async def _collect(
        self, obs: TurnObservation, *, request_id: str | None, incident_id: str | None
    ) -> None:
        async with self.db.transaction() as conn:
            if request_id:
                cursor = await conn.execute(
                    "select reason_code from guardrail_results where request_id = %s", (request_id,)
                )
                obs.guardrail_reasons = {
                    row["reason_code"] for row in await cursor.fetchall() if row["reason_code"]
                }
                cursor = await conn.execute(
                    "select content, payload from messages where request_id = %s", (request_id,)
                )
                obs.stored_text = stored_text(await cursor.fetchall())
            if incident_id:
                incident = await incidents.get_incident(conn, incident_id)
                obs.incident_id = incident_id
                obs.incident_is_live = bool(incident and incident["source"] == "live")
            rec = obs.recommendation or {}
            cited = {m["incident_id"] for m in rec.get("matched_incidents", [])}
            for item in [*rec.get("recommended_steps", []), *rec.get("actions_to_avoid", [])]:
                cited.update(item.get("evidence_ids", []))
            obs.cited_existing = await incidents.existing_incident_ids(conn, cited)

    async def _foreign_conversation(self, engineer_id: str) -> str:
        conversation_id = new_id("CONV")
        async with self.db.transaction() as conn:
            await conversations.create_conversation(
                conn,
                conversation_id=conversation_id,
                engineer_id=engineer_id,
                title="Another engineer's incident",
            )
        return conversation_id

    async def close(self) -> None:
        await self.groq.close()
        await self.hindsight.close()


def _expected(expect: Expectation) -> list[Any]:
    if expect.outcome is None:
        return []
    return expect.outcome if isinstance(expect.outcome, list) else [expect.outcome]
