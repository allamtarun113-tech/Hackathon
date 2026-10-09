"""LLM-as-judge scoring against grader-only labels."""

from __future__ import annotations

import json
import random
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.groq_client import GroqClient
from evals.dataset import ScenarioLabels

JUDGE_SYSTEM = """You grade an on-call assistant's answer against a reference.
Score strictly. Reply with JSON only:
{"root_cause": number 0..1, "recommended_action": number 0..1, "rationale": string}
- root_cause: 1 if the answer's likely cause matches the reference cause in substance, 0.5 if partially, 0 if not.
- recommended_action: fraction of the reference "should recommend" items covered by the answer's steps, minus
  0.25 for each reference "should avoid" item the answer recommends (floor at 0)."""

PAIRWISE_SYSTEM = """Two on-call assistant answers (A and B) respond to the same incident. Using the reference,
decide which answer would lead an engineer to the correct fix faster and more safely.
Reply with JSON only: {"winner": "A"|"B"|"tie", "rationale": string}"""


class JudgeScore(BaseModel):
    root_cause: float = Field(ge=0, le=1)
    recommended_action: float = Field(ge=0, le=1)
    rationale: str = ""


class PairwiseVerdict(BaseModel):
    winner: Literal["A", "B", "tie"]
    rationale: str = ""


def _reference(labels: ScenarioLabels) -> str:
    return json.dumps(
        {
            "likely_cause": labels.likely_cause,
            "should_recommend": labels.should_recommend,
            "should_avoid": labels.should_avoid,
            "is_known_problem": labels.strong_match,
        }
    )


class Judge:
    def __init__(self, groq: GroqClient) -> None:
        self.groq = groq

    async def score(self, labels: ScenarioLabels, answer: dict[str, Any]) -> JudgeScore:
        result = await self.groq.complete_json(
            name="eval_judge",
            system=JUDGE_SYSTEM,
            user=f"Reference:\n{_reference(labels)}\n\nAnswer:\n{json.dumps(answer)[:6000]}",
            schema=JudgeScore,
            max_tokens=600,
            temperature=0.0,
        )
        return result.value

    async def compare(
        self,
        labels: ScenarioLabels,
        with_memory: dict[str, Any],
        without_memory: dict[str, Any],
        rng: random.Random,
    ) -> Literal["with_memory", "without_memory", "tie"]:
        """Pairwise preference with randomised position to reduce order bias."""
        swap = rng.random() < 0.5
        first, second = (without_memory, with_memory) if swap else (with_memory, without_memory)
        result = await self.groq.complete_json(
            name="eval_pairwise",
            system=PAIRWISE_SYSTEM,
            user=(
                f"Reference:\n{_reference(labels)}\n\nAnswer A:\n{json.dumps(first)[:5000]}"
                f"\n\nAnswer B:\n{json.dumps(second)[:5000]}"
            ),
            schema=PairwiseVerdict,
            max_tokens=400,
            temperature=0.0,
        )
        if result.value.winner == "tie":
            return "tie"
        a_wins = result.value.winner == "A"
        return "with_memory" if a_wins != swap else "without_memory"
