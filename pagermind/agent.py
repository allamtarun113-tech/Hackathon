"""The 'without Hindsight' baseline: a normal LLM API call (Groq).

It gets only what a person would paste into a chatbot - the alert and its log lines.
No memory, no runbook, no company data. The 'with Hindsight' answer comes from
Hindsight reflect (pagermind/memory.py); both get the same alert and logs, so the
only difference is Hindsight's memory of past incidents.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import groq
from groq import Groq

from pagermind.config import Config

log = logging.getLogger(__name__)

SYSTEM = """You are an assistant helping an on-call engineer. An alert fired. Suggest what to do.
Reply with JSON only:
{"summary": str (max 2 sentences), "likely_cause": str,
 "avoid": [{"action": str, "why": str}],
 "recommended_steps": [{"order": int, "action": str, "why": str, "command": str|null}]}"""


class Agent:
    def __init__(self, cfg: Config):
        self.models = [cfg.groq_model, cfg.groq_fallback_model]
        self.client = Groq(api_key=cfg.groq_api_key, timeout=45, max_retries=0)

    def _complete_json(self, messages: list[dict[str, str]]) -> tuple[dict[str, Any], str]:
        last: Exception | None = None
        for model in self.models:
            extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {"reasoning_effort": "none"}
            for _ in range(2):
                try:
                    r = self.client.chat.completions.create(model=model, messages=messages, max_tokens=3000,
                                                            temperature=0.2, response_format={"type": "json_object"},
                                                            **extra)
                    data = json.loads(r.choices[0].message.content or "")
                    if data.get("summary") and data.get("recommended_steps"):
                        return data, model
                    last = ValueError("missing summary/steps")
                except groq.RateLimitError as e:  # free tier tokens-per-minute: go straight to the next model
                    last = e
                    log.warning("LLM %s rate-limited, trying next model", model)
                    break
                except Exception as e:  # API error, timeout, bad JSON -> retry, then next model
                    last = e
                    log.warning("LLM %s failed: %s", model, str(e)[:200])
        raise RuntimeError(f"LLM unavailable: {last}")

    def suggest_without_memory(self, *, alert: dict[str, Any], log_lines: list[str]) -> dict[str, Any]:
        """A plain LLM call: the alert and its logs, nothing else."""
        user = (f"Alert: {alert['title']} on {alert['service']} ({alert['value']}).\n"
                "Logs:\n" + "\n".join(log_lines))
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        started = time.time()
        try:
            card, model = self._complete_json(messages)
        except RuntimeError as e:
            return {"summary": "The AI model is unavailable right now.", "likely_cause": "unknown", "avoid": [],
                    "recommended_steps": [{"order": 1, "action": "Try again in a minute", "why": str(e)[:120]}],
                    "model": "none", "seconds": 0}
        card["matched_incidents"] = []
        card["model"], card["seconds"] = model, round(time.time() - started, 1)
        return card

    def draft_postmortem(self, *, alert: dict[str, Any], card: dict[str, Any], steps: list[dict[str, str]]) -> str:
        """Short postmortem from what happened in this session - saved to memory when resolved."""
        tried = "\n".join(f"- {s['action']} -> {s['outcome']}" for s in steps) or "- (no steps recorded)"
        prompt = (f"Write a 5-line incident postmortem (Service, Symptoms, Root cause, What did NOT work, What fixed it). "
                  f"Alert: {alert['title']} on {alert['service']} ({alert['value']}).\nLikely cause: {card.get('likely_cause')}\n"
                  f"Steps tried:\n{tried}\nPlain text, no markdown headings.")
        for model in self.models:
            try:
                r = self.client.chat.completions.create(model=model, max_tokens=1200, temperature=0.3,
                                                        messages=[{"role": "user", "content": prompt}])
                return r.choices[0].message.content or ""
            except Exception as e:
                log.warning("postmortem draft with %s failed: %s", model, e)
        return f"Service: {alert['service']}\nSymptoms: {alert['title']}\nRoot cause: {card.get('likely_cause')}\nSteps:\n{tried}"
