"""Validate PagerMind dataset shape, integrity, split isolation and coverage."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_json(name: str) -> list[dict[str, Any]]:
    return json.loads((DATA / name).read_text())


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> None:
    incidents = load_json("incidents_history.json")
    scenarios = load_json("demo_scenarios.json")
    deploys = load_json("deploys.json")
    engineers = load_json("engineers.json")
    catalog = yaml.safe_load((DATA / "service_catalog.yaml").read_text())
    errors: list[str] = []

    require(len(incidents) == 500, f"expected 500 incidents, found {len(incidents)}", errors)
    require(len(scenarios) == 100, f"expected 100 scenarios, found {len(scenarios)}", errors)
    require(len(deploys) == 800, f"expected 800 deploys, found {len(deploys)}", errors)
    require(len(catalog["services"]) == 15, f"expected 15 services, found {len(catalog['services'])}", errors)

    for label, rows in (("incident", incidents), ("scenario", scenarios), ("deploy", deploys), ("engineer", engineers)):
        ids = [row["id"] for row in rows]
        require(len(ids) == len(set(ids)), f"duplicate {label} IDs", errors)

    services = set(catalog["services"])
    engineer_names = {e["name"] for e in engineers}
    deploy_ids = {d["id"] for d in deploys}
    incident_ids = {i["id"] for i in incidents}
    incident_by_id = {i["id"]: i for i in incidents}
    service_counts = Counter(i["service"] for i in incidents)
    pattern_counts = Counter(i["pattern_id"] for i in incidents if i.get("pattern_id"))

    require(set(service_counts) == services, "not every catalog service has an incident", errors)
    for service, count in service_counts.items():
        require(count >= 20, f"{service} has only {count} incidents; minimum is 20", errors)
    require(len(pattern_counts) >= 25, f"expected at least 25 patterns, found {len(pattern_counts)}", errors)
    for pattern_id, count in pattern_counts.items():
        require(count >= 6, f"{pattern_id} has only {count} examples; minimum is 6", errors)

    all_postmortems: list[str] = []
    history_evidence_text: list[str] = []
    for inc in incidents:
        iid = inc["id"]
        required = {
            "id", "title", "date", "service", "severity", "alert", "log_excerpt", "log_file",
            "timeline", "failed_attempts", "root_cause", "resolution", "action_items", "resolved_by",
            "responders", "mttr_minutes", "customer_impact", "postmortem_summary", "pattern_id",
        }
        require(required <= set(inc), f"{iid}: missing incident keys {sorted(required - set(inc))}", errors)
        require(inc["service"] in services, f"{iid}: unknown service {inc['service']}", errors)
        require(inc["alert"]["service"] == inc["service"], f"{iid}: alert service mismatch", errors)
        require(inc["resolved_by"] in engineer_names, f"{iid}: unknown resolver {inc['resolved_by']}", errors)
        for responder in inc["responders"]:
            require(responder in engineer_names, f"{iid}: unknown responder {responder}", errors)
        fire = datetime.fromisoformat(inc["alert"]["fired_at"])
        end = datetime.fromisoformat(inc["timeline"][-1]["at"])
        require(inc["date"] == inc["alert"]["fired_at"], f"{iid}: date does not equal fired_at", errors)
        require(round((end - fire).total_seconds() / 60) == inc["mttr_minutes"], f"{iid}: MTTR mismatch", errors)
        times = [datetime.fromisoformat(step["at"]) for step in inc["timeline"]]
        require(times == sorted(times), f"{iid}: timeline not sorted", errors)
        log_path = DATA / inc["log_file"]
        require(log_path.exists(), f"{iid}: log file missing: {inc['log_file']}", errors)
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            require(all(line in text for line in inc["log_excerpt"]), f"{iid}: excerpt not contained in log", errors)
            history_evidence_text.append("\n".join(inc["log_excerpt"]))
        for deploy_id in inc.get("related_deploy_ids", []):
            require(deploy_id in deploy_ids, f"{iid}: missing deploy {deploy_id}", errors)
        all_postmortems.append(inc["postmortem_summary"])
    require(len(all_postmortems) == len(set(all_postmortems)), "duplicate postmortem summaries", errors)
    memory_text = "\n".join(history_evidence_text)

    featured = 0
    case_counts: Counter[str] = Counter()
    train_log_paths = {i["log_file"] for i in incidents}
    test_log_paths: set[str] = set()
    for sc in scenarios:
        sid = sc["id"]
        required = {"id", "name", "purpose", "service", "severity", "alert", "log_excerpt", "log_file", "expected"}
        require(required <= set(sc), f"{sid}: missing scenario keys {sorted(required - set(sc))}", errors)
        require(sc["service"] in services, f"{sid}: unknown service", errors)
        require(sc["alert"]["service"] == sc["service"], f"{sid}: alert service mismatch", errors)
        featured += bool(sc.get("featured", True))
        case_counts[sc.get("case_type", "curated-demo")] += 1
        test_log_paths.add(sc["log_file"])
        expected = sc["expected"]
        expected_ids = expected["expected_incident_ids"]
        require(all(iid in incident_ids for iid in expected_ids), f"{sid}: unknown expected incident ID", errors)
        if expected["strong_match"]:
            require(bool(expected_ids), f"{sid}: strong match has no expected incidents", errors)
            for iid in expected_ids:
                require(incident_by_id[iid].get("pattern_id") == expected["pattern_id"],
                        f"{sid}: expected incident {iid} has wrong pattern", errors)
        else:
            require(not expected_ids, f"{sid}: novel case contains expected IDs", errors)
        if sc.get("data_provenance", {}).get("split") == "evaluation":
            evidence_terms = expected.get("required_evidence_terms", [])
            require(bool(evidence_terms), f"{sid}: generated test has no required evidence term", errors)
            for term in evidence_terms:
                if expected["strong_match"]:
                    require(term in memory_text, f"{sid}: strong-match evidence is absent from memory", errors)
                else:
                    require(term not in memory_text, f"{sid}: novel evidence leaks into memory", errors)
            require(expected["likely_cause"] not in "\n".join(sc["log_excerpt"]),
                    f"{sid}: labelled root cause is copied directly into test logs", errors)
        log_path = DATA / sc["log_file"]
        require(log_path.exists(), f"{sid}: missing log file", errors)
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            require(all(line in text for line in sc["log_excerpt"]), f"{sid}: excerpt not contained in log", errors)

    require(featured == 8, f"expected 8 featured demos, found {featured}", errors)
    require(not train_log_paths & test_log_paths, "history and evaluation share log files", errors)
    require(sum(1 for s in scenarios if not s["expected"]["strong_match"]) >= 15,
            "evaluation needs at least 15 novel/abstention cases", errors)
    require(sum(1 for s in scenarios if s.get("case_type") == "cross-service") >= 14,
            "evaluation needs at least 14 cross-service cases", errors)

    generated_history_ids = {i["id"] for i in incidents if i.get("data_provenance", {}).get("split") == "memory"}
    generated_test_ids = {s["id"] for s in scenarios if s.get("data_provenance", {}).get("split") == "evaluation"}
    require(not generated_history_ids & generated_test_ids, "memory/evaluation ID overlap", errors)

    if errors:
        print(f"DATASET INVALID: {len(errors)} error(s)")
        for error in errors[:100]:
            print(f"- {error}")
        raise SystemExit(1)

    print("DATASET VALID")
    print(f"- historical incidents: {len(incidents)}")
    print(f"- held-out scenarios: {len(scenarios)} ({featured} featured demos)")
    print(f"- services: {len(services)}; minimum incidents/service: {min(service_counts.values())}")
    print(f"- recurring patterns: {len(pattern_counts)}; minimum examples/pattern: {min(pattern_counts.values())}")
    print(f"- deploy/change records: {len(deploys)}")
    print(f"- scenario classes: {dict(sorted(case_counts.items()))}")


if __name__ == "__main__":
    main()
