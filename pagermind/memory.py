"""Everything PagerMind does with Hindsight lives in this file.

Memory bank: `pagermind-incidents` - the on-call team's long-term memory.

  RETAIN   every past incident (one document each), every step an engineer tries
           during a live incident (worked / did not work), and every resolved incident.
  REFLECT  when an alert fires, Hindsight reasons over the whole bank and returns the
           suggestion card as structured JSON (response_schema), plus the exact memory
           facts it used (include_facts) - shown in the "Memory used" panel.
  RECALL   used by scripts/check_recall.py to measure retrieval quality: two searches
           (error signatures + symptoms) fused, so look-alike incidents don't get mixed up.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from hindsight_client import Hindsight

from pagermind.config import Config

# --------------------------------------------------------------------------- bank configuration
BANK_NAME = "PagerMind - Kartly on-call memory"
BANK_MISSION = (
    "You are the on-call memory of Kartly's engineering team (Indian online shopping and payments platform). "
    "Remember every production incident: symptoms, error signatures, root cause, every action tried and whether "
    "it worked, did not work or made things worse, the final fix, who fixed it and how long it took."
)
RETAIN_MISSION = (
    "Extract: the affected service, the alert and symptoms, exact error signatures from logs, the root cause, "
    "each action tried with its outcome (worked / did not work / made it worse), the final fix with commands, "
    "who resolved it, time to resolve, and open follow-up tickets."
)
OBSERVATIONS_MISSION = (
    "Form durable lessons about recurring failure patterns: which fixes work for which symptoms and services, "
    "which actions repeatedly do not help, and which follow-ups are still open."
)
REFLECT_MISSION = (
    "Answer as Kartly's most experienced on-call engineer. Ground every claim in specific past incidents and cite "
    "their incident ids and dates. Distinguish look-alike incidents by their error signatures, not just by service "
    "name or time of day. If there is no similar past incident, say so plainly."
)

_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR}
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": _STR,
        "is_strong_match": {"type": "boolean"},
        "matched_incidents": {"type": "array", "items": {"type": "object", "properties": {
            "incident_id": _STR, "why_similar": _STR}, "required": ["incident_id", "why_similar"]}},
        "likely_cause": _STR,
        "avoid": {"type": "array", "items": {"type": "object", "properties": {
            "action": _STR, "why": _STR, "evidence_incident_ids": _IDS}, "required": ["action", "why", "evidence_incident_ids"]}},
        "recommended_steps": {"type": "array", "items": {"type": "object", "properties": {
            "order": {"type": "integer"}, "action": _STR, "why": _STR, "command": _STR, "evidence_incident_ids": _IDS},
            "required": ["order", "action", "why", "evidence_incident_ids"]}},
        "escalate_to": _STR,
    },
    "required": ["summary", "is_strong_match", "matched_incidents", "likely_cause", "avoid",
                 "recommended_steps"],
}

REFLECT_RULES = (
    "Rules: a past incident matches ONLY if it has the same error signature or root cause - the same service or "
    "symptoms is not enough, and an incident on a different service with the same signature DOES match. "
    "If nothing truly matches, set is_strong_match=false and matched_incidents=[], and base the steps "
    "on the current evidence. 'avoid' lists only actions that failed or made things worse in the matched incidents - "
    "never an action that worked in them (if a restart fixed it before, recommend it). Give 2-5 recommended steps, "
    "best first, reusing exact commands from memory adapted to this service, and cite incident ids as evidence."
)
_CONFLICT_WORDS = ("restart", "roll back", "rollback")

IST = timezone(timedelta(hours=5, minutes=30))

OUTCOME_WORDS = {"worked": "WORKED", "partial": "PARTLY HELPED", "did_not_work": "DID NOT WORK",
                 "made_worse": "MADE IT WORSE"}


# --------------------------------------------------------------------------- results
@dataclass
class Fact:
    text: str
    incident_id: str | None
    service: str | None
    title: str | None
    date: str | None


@dataclass
class Match:
    incident_id: str
    service: str | None
    title: str | None
    date: str | None
    score: float = 0.0
    facts: list[str] = field(default_factory=list)


def group_by_incident(facts: list[Fact]) -> list[Match]:
    """Recall returns individual facts; group them into incidents, best first."""
    grouped: OrderedDict[str, Match] = OrderedDict()
    for f in facts:
        if not f.incident_id:
            continue
        m = grouped.setdefault(f.incident_id, Match(f.incident_id, f.service, f.title, f.date))
        m.facts.append(f.text)
    return list(grouped.values())


# --------------------------------------------------------------------------- turning logs into good queries
_TS = [re.compile(p) for p in (r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?Z?\s*", r"^\d+:M \d{2} \w{3} \d{4} [\d:.]+\s*",
                               r"^ts=\S+\s*", r"^\S+ - - \[[^\]]+\]\s*")]
_QUIET = (" INFO ", '"level":"info"', "level=info", "  Normal ", " DEBUG ")
_NOISE = re.compile(
    r'(ORD|SKU)-\d+|traceId=\w+|u_\d+|exec-\d+|consumer-\d+|worker-\d+|latencyMs=\d+|"latency_ms":\d+,?'
    r'|(?<=\s)\d{2,4}ms(?=\s|$)|event_loop_lag_ms=\d+|processed \d+ rows|offset \d+|upstream_response_time=[\d.]+'
    r'|cart=\d+|msg-\d+|"attempt":\d+,?|(?<=pod/)[\w-]+?-[a-z0-9]{10}-[a-z0-9]{5}|"(ts|EdgeStartTimestamp)":"[^"]*",?'
)
_JAVA = re.compile(r"^(ERROR|WARN|DEBUG|INFO)\s+\d+ --- \[[^\]]*\]\s+\S+\s+:\s*")
_NODE = re.compile(r"^(ERROR|WARN|INFO)\s+\[[\w-]+\]\s*")
_JSON_KEEP = re.compile(r'"(msg|error|SecurityAction|SecurityRuleDescription|ClientRequestPath|EdgeResponseStatus)":"?([^",}]*)')


def error_lines(lines: list[str], limit: int = 5) -> list[str]:
    """Distinct warning/error lines without timestamps or per-request ids.

    Healthy lines are skipped on purpose - e.g. 'Pool stats ... waiting=0' would pull
    in connection-pool incidents even though it proves the pool is fine.
    """
    out: list[str] = []
    for line in lines:
        exhausted_pool = "Pool stats" in line and "waiting=0)" not in line
        if ("Pool stats" in line and not exhausted_pool) or (any(q in line for q in _QUIET) and not exhausted_pool):
            continue
        s = line.strip()
        for p in _TS:
            s = p.sub("", s)
        s = re.sub(r"\s{2,}", " ", _NOISE.sub("", s)).strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _message_only(line: str) -> str:
    if line.startswith("{"):
        return " ".join(v if k in ("msg", "error") else f"{k}={v}" for k, v in _JSON_KEEP.findall(line))
    return _NODE.sub("", _JAVA.sub("", line))


def signature_query(log_lines: list[str]) -> str:
    return " | ".join(_message_only(line) for line in error_lines(log_lines))


# --------------------------------------------------------------------------- incident -> memory text
def incident_to_memory(inc: dict[str, Any]) -> dict[str, Any]:
    """One retain item per incident. The hidden `pattern_id` label is never sent."""
    date = datetime.fromisoformat(inc["date"])
    steps = [s for s in inc["timeline"] if s["action"] not in ("Acknowledged the page", "Marked the incident resolved")]
    timeline = "\n".join(
        f"  {datetime.fromisoformat(s['at']):%H:%M} {s['actor']}: {s['action']}"
        + (f" -> {OUTCOME_WORDS[s['outcome']]}" if s["outcome"] in OUTCOME_WORDS else "")
        + f". {s['result']}" + (f" (command: {s['command']})" if s.get("command") else "")
        for s in steps)
    failed = "; ".join(f"{f['action']} ({f['result']})" for f in inc["failed_attempts"]) or "nothing"
    logs = "\n".join(f"  {line}" for line in error_lines(inc["log_excerpt"]))
    content = (
        f"Incident {inc['id']}: {inc['title']}\n"
        f"Date: {date:%Y-%m-%d %H:%M} IST ({date:%A}). Service: {inc['service']}. Severity: {inc['severity']}. "
        f"Time to resolve: {inc['mttr_minutes']} minutes.\n"
        f"Alert: {inc['alert']['title']} ({inc['alert']['value']}).\n"
        f"Key log lines:\n{logs}\n"
        f"Customer impact: {inc['customer_impact']}\n"
        f"Timeline:\n{timeline}\n"
        f"Root cause: {inc['root_cause']}\n"
        f"What did NOT work: {failed}.\n"
        f"What fixed it: {inc['resolution']}\n"
        f"Resolved by {inc['resolved_by']}. Follow-ups: {'; '.join(inc['action_items'])}."
    )
    return {
        "content": content,
        "timestamp": date,
        "context": f"incident postmortem for {inc['service']}",
        "document_id": inc["id"],
        "metadata": {"incident_id": inc["id"], "service": inc["service"], "severity": inc["severity"],
                     "title": inc["title"], "mttr_minutes": str(inc["mttr_minutes"])},
        "tags": [f"service:{inc['service']}", f"severity:{inc['severity']}", "kind:postmortem"],
    }


def _ist_date(value: Any) -> str | None:
    """Hindsight returns UTC timestamps; show the date in IST like the rest of the app."""
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(IST).strftime("%Y-%m-%d") if dt.tzinfo else dt.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- the client
class PagerMindMemory:
    """Thin, synchronous wrapper. A short-lived Hindsight client is used per call so it
    works from any thread (Streamlit runs each interaction on its own thread)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bank = cfg.bank_id

    def _client(self) -> Hindsight:
        return Hindsight(base_url=self.cfg.hindsight_base_url, api_key=self.cfg.hindsight_api_key, timeout=90)

    # ---- setup
    def setup_bank(self, background: str) -> None:
        with self._client() as c:
            c.create_bank(bank_id=self.bank, name=BANK_NAME, mission=BANK_MISSION, background=background,
                          disposition_skepticism=4, disposition_literalism=3, disposition_empathy=2,
                          retain_mission=RETAIN_MISSION, enable_observations=True,
                          observations_mission=OBSERVATIONS_MISSION, reflect_mission=REFLECT_MISSION)

    def stored_incident_ids(self) -> set[str]:
        ids: set[str] = set()
        with self._client() as c:
            loop = asyncio.get_event_loop()
            offset = 0
            while True:
                page = loop.run_until_complete(c.documents.list_documents(self.bank, limit=100, offset=offset))
                ids |= {d.id for d in page.items}
                if len(page.items) < 100:
                    return ids
                offset += 100

    def is_up(self) -> bool:
        try:
            with self._client() as c:
                c.get_version()
            return True
        except Exception:
            return False

    # ---- RETAIN
    def retain_incidents(self, incidents: list[dict[str, Any]]) -> None:
        with self._client() as c:
            c.retain_batch(bank_id=self.bank, items=[incident_to_memory(i) for i in incidents])

    def retain_step(self, *, incident_id: str, service: str, actor: str, action: str, outcome: str,
                    note: str = "") -> None:
        """Remember what an engineer tried during a live incident - including failures."""
        text = (f"During incident {incident_id} on {service}, {actor} tried: {action}. "
                f"Outcome: {OUTCOME_WORDS.get(outcome, outcome)}. {note}").strip()
        with self._client() as c:
            c.retain(bank_id=self.bank, content=text, timestamp=datetime.now().astimezone(),
                     context=f"live incident step on {service}", document_id=f"{incident_id}-steps",
                     update_mode="append", metadata={"incident_id": incident_id, "service": service},
                     tags=[f"service:{service}", "kind:step"])

    def retain_resolution(self, *, incident_id: str, service: str, title: str, summary: str) -> None:
        """A resolved live incident becomes a new past incident that future alerts can recall."""
        with self._client() as c:
            c.retain(bank_id=self.bank, content=f"Incident {incident_id}: {title}\n{summary}",
                     timestamp=datetime.now().astimezone(), context=f"incident postmortem for {service}",
                     document_id=incident_id, update_mode="replace",
                     metadata={"incident_id": incident_id, "service": service, "title": title},
                     tags=[f"service:{service}", "kind:postmortem"])

    # ---- RECALL
    def recall(self, query: str) -> list[Fact]:
        with self._client() as c:
            r = c.recall(bank_id=self.bank, query=query, budget="mid", max_tokens=4096)
        facts = []
        for x in r.results:
            meta = x.metadata or {}
            iid = meta.get("incident_id") or (x.document_id or "").removesuffix("-steps") or None
            facts.append(Fact(x.text, iid, meta.get("service"), meta.get("title"), _ist_date(x.occurred_start)))
        return facts

    def recall_for_alert(self, alert: dict[str, Any], log_lines: list[str], k: int = 10) -> list[Match]:
        """Two recalls fused by reciprocal rank: error signatures (weight 2) + symptoms (weight 1)."""
        searches = [(signature_query(log_lines), 2.0),
                    (f"{alert['title']} on {alert['service']} ({alert['value']})", 1.0)]
        scores: dict[str, float] = {}
        merged: dict[str, Match] = {}
        for query, weight in searches:
            for rank, m in enumerate(group_by_incident(self.recall(query)), start=1):
                scores[m.incident_id] = scores.get(m.incident_id, 0) + weight / (k + rank)
                if m.incident_id in merged:
                    merged[m.incident_id].facts += [f for f in m.facts if f not in merged[m.incident_id].facts]
                else:
                    merged[m.incident_id] = m
        ranked = sorted(merged.values(), key=lambda m: scores[m.incident_id], reverse=True)
        for m in ranked:
            m.score = round(scores[m.incident_id], 3)
        return ranked

    # ---- REFLECT
    def reflect_suggestion(self, alert: dict[str, Any], log_lines: list[str], context: str) -> dict[str, Any]:
        """The 'with Hindsight' answer: Hindsight reasons over the bank and returns the suggestion card.

        reflect's `based_on` facts don't say which incident they came from, so a fast recall runs in
        parallel to fill the "Memory used" panel, and stored document ids validate every citation.
        """
        query = (f"A new alert fired: {alert['title']} on {alert['service']} ({alert['value']}). "
                 f"Error signatures: {signature_query(log_lines)}. Give the on-call engineer a suggestion card: which "
                 f"past incidents match, the likely cause, what to avoid and what to do. {REFLECT_RULES}")

        def reflect():
            with self._client() as c:
                return c.reflect(bank_id=self.bank, query=query, context=context, budget="low",
                                 response_schema=CARD_SCHEMA, include_facts=True)

        started = datetime.now()
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_reflect = pool.submit(reflect)
            f_recall = pool.submit(self.recall_for_alert, alert, log_lines)
            f_ids = pool.submit(self.stored_incident_ids)
            r = f_reflect.result()
            recalled = f_recall.result() if not f_recall.exception() else []
            known = f_ids.result() if not f_ids.exception() else {m.incident_id for m in recalled}
        n_facts = len(r.based_on.memories or []) if r.based_on else 0
        card = dict(r.structured_output or {})
        if not card.get("summary"):  # structured output failed - keep Hindsight's prose answer
            card = {"summary": (r.text or "No answer.")[:500], "is_strong_match": False,
                    "likely_cause": "See the full reasoning below.", "matched_incidents": [], "avoid": [],
                    "recommended_steps": [{"order": 1, "action": "Read Hindsight's full reasoning below", "why": "",
                                           "evidence_incident_ids": []}]}
        return self._clean_card(card, recalled, known, r.text or "", (datetime.now() - started).total_seconds(), n_facts)

    @staticmethod
    def _clean_card(card: dict[str, Any], recalled: list[Match], known: set[str], text: str, seconds: float,
                    n_facts: int) -> dict[str, Any]:
        """Guardrails: only cite incidents that exist in memory; no matches unless it's a strong match;
        never 'avoid' something the card also recommends."""
        if not card.get("is_strong_match"):
            card["matched_incidents"] = []
        card["matched_incidents"] = [m for m in card.get("matched_incidents", []) if m.get("incident_id") in known]
        steps = card.get("recommended_steps", [])
        for item in card.get("avoid", []) + steps:
            item["evidence_incident_ids"] = [i for i in item.get("evidence_incident_ids", []) if i in known]
            if item.get("command") in ("", "N/A", "n/a", None):
                item["command"] = None
        step_text = " ".join(s.get("action", "").lower() for s in steps)
        card["avoid"] = [a for a in card.get("avoid", [])
                         if not any(w in a.get("action", "").lower() and w in step_text for w in _CONFLICT_WORDS)]
        card["recommended_steps"] = sorted(steps, key=lambda s: s.get("order", 0))
        matched = [m["incident_id"] for m in card["matched_incidents"]]
        card["memory"] = sorted(recalled, key=lambda m: (m.incident_id not in matched, matched.index(m.incident_id)
                                                         if m.incident_id in matched else 0))[:6]
        card["facts_used"], card["text"] = n_facts, text
        card["model"], card["seconds"] = "Hindsight reflect", round(seconds, 1)
        return card
