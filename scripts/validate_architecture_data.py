"""Validate production architecture seed data and every cross-record link."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROD = DATA / "production"


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def prod(name: str) -> list[dict[str, Any]]:
    return load(PROD / name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    incidents = load(DATA / "incidents_history.json")
    scenarios = load(DATA / "demo_scenarios.json")
    deploys = load(DATA / "deploys.json")
    engineers = load(DATA / "engineers.json")
    catalog = yaml.safe_load((DATA / "service_catalog.yaml").read_text())

    organizations = prod("organizations.json")
    teams = prod("teams.json")
    users = prod("users.json")
    memberships = prod("memberships.json")
    ownership = prod("service_ownership.json")
    shifts = prod("oncall_shifts.json")
    conversations = prod("conversations.json")
    messages = prod("messages.json")
    checkpoints = prod("agent_checkpoints.json")
    feedback = prod("feedback.json")
    retain_jobs = prod("feedback_retain_jobs.json")
    guardrail = prod("guardrail_tests.json")
    eval_results = prod("evaluation_results.json")
    traces = prod("trace_bundles.json")
    sentry = prod("sentry_events.json")
    health = prod("health_snapshots.json")
    files = prod("file_manifest.json")
    change_links = prod("incident_change_links.json")
    supabase_incidents = prod("supabase_incidents.json")
    audit = prod("audit_log.json")

    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    expected_counts = {
        "organizations": (organizations, 1),
        "users": (users, 17),
        "memberships": (memberships, 17),
        "service_ownership": (ownership, 15),
        "oncall_shifts": (shifts, 270),
        "conversations": (conversations, 200),
        "messages": (messages, 1200),
        "agent_checkpoints": (checkpoints, 200),
        "feedback": (feedback, 600),
        "feedback_retain_jobs": (retain_jobs, 600),
        "guardrail_tests": (guardrail, 200),
        "evaluation_results": (eval_results, 1000),
        "trace_bundles": (traces, 200),
        "sentry_events": (sentry, 40),
        "health_snapshots": (health, 120),
        "file_manifest": (files, 600),
        "incident_change_links": (change_links, 1500),
        "supabase_incidents": (supabase_incidents, 500),
        "audit_log": (audit, 3000),
    }
    for label, (rows, expected) in expected_counts.items():
        require(len(rows) == expected, f"{label}: expected {expected}, found {len(rows)}")

    id_fields = {
        "organizations": (organizations, "org_id"), "teams": (teams, "team_id"),
        "users": (users, "user_id"), "memberships": (memberships, "membership_id"),
        "ownership": (ownership, "ownership_id"), "shifts": (shifts, "shift_id"),
        "conversations": (conversations, "conversation_id"), "messages": (messages, "message_id"),
        "checkpoints": (checkpoints, "checkpoint_id"), "feedback": (feedback, "feedback_id"),
        "retain_jobs": (retain_jobs, "job_id"), "guardrail": (guardrail, "guardrail_test_id"),
        "eval_results": (eval_results, "evaluation_result_id"), "traces": (traces, "trace_id"),
        "sentry": (sentry, "event_id"), "health": (health, "health_snapshot_id"),
        "files": (files, "file_id"), "change_links": (change_links, "incident_change_link_id"),
        "supabase_incidents": (supabase_incidents, "incident_id"), "audit": (audit, "audit_event_id"),
    }
    for label, (rows, key) in id_fields.items():
        values = [row[key] for row in rows]
        require(len(values) == len(set(values)), f"{label}: duplicate {key}")

    org_ids = {r["org_id"] for r in organizations}
    team_ids = {r["team_id"] for r in teams}
    user_ids = {r["user_id"] for r in users}
    service_ids = set(catalog["services"])
    incident_ids = {r["id"] for r in incidents}
    scenario_ids = {r["id"] for r in scenarios}
    deploy_ids = {r["id"] for r in deploys}
    conversation_ids = {r["conversation_id"] for r in conversations}
    message_ids = {r["message_id"] for r in messages}
    trace_ids = {r["trace_id"] for r in traces}
    file_ids = {r["file_id"] for r in files}
    feedback_ids = {r["feedback_id"] for r in feedback}

    require(len(teams) == len({e["team"] for e in engineers}), "teams do not cover every engineer team")
    for user in users:
        require(user["org_id"] in org_ids, f"{user['user_id']}: unknown org")
    for membership in memberships:
        require(membership["user_id"] in user_ids, f"{membership['membership_id']}: unknown user")
        require(membership["team_id"] in team_ids, f"{membership['membership_id']}: unknown team")
    for row in ownership:
        require(row["service_id"] in service_ids, f"{row['ownership_id']}: unknown service")
        require(row["team_id"] in team_ids, f"{row['ownership_id']}: unknown team")
        require(row["primary_owner_user_id"] in user_ids, f"{row['ownership_id']}: unknown owner")
    for shift in shifts:
        require(shift["user_id"] in user_ids, f"{shift['shift_id']}: unknown user")
        require(set(shift["service_ids"]) <= service_ids, f"{shift['shift_id']}: unknown service scope")

    messages_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        require(message["conversation_id"] in conversation_ids, f"{message['message_id']}: unknown conversation")
        if message["user_id"]:
            require(message["user_id"] in user_ids, f"{message['message_id']}: unknown user")
        messages_by_conversation[message["conversation_id"]].append(message)
    for conversation in conversations:
        cid = conversation["conversation_id"]
        require(conversation["user_id"] in user_ids, f"{cid}: unknown user")
        require(conversation["service_id"] in service_ids, f"{cid}: unknown service")
        require(conversation["trace_id"] in trace_ids, f"{cid}: unknown trace")
        require(set(conversation["attachment_file_ids"]) <= file_ids, f"{cid}: unknown attachment")
        require(len(messages_by_conversation[cid]) == 6, f"{cid}: expected 6 messages")
        require(sorted(m["sequence"] for m in messages_by_conversation[cid]) == list(range(1, 7)),
                f"{cid}: invalid message sequence")
        if conversation["source_kind"] == "held-out-evaluation":
            require(conversation["source_id"] in scenario_ids, f"{cid}: unknown scenario")
        else:
            require(conversation["source_id"] in incident_ids, f"{cid}: unknown incident fixture")

    checkpoint_conversations = Counter(row["conversation_id"] for row in checkpoints)
    require(set(checkpoint_conversations) == conversation_ids, "checkpoint coverage does not match conversations")
    require(all(count == 1 for count in checkpoint_conversations.values()), "conversation has multiple checkpoints")

    outcome_counts = Counter(row["outcome"] for row in feedback)
    require(outcome_counts == Counter({
        "worked": 250, "did_not_work": 200, "partial": 75, "made_worse": 50, "confirmed_fix": 25,
    }), f"feedback outcome distribution is wrong: {dict(outcome_counts)}")
    status_counts = Counter(row["hindsight_retain_status"] for row in feedback)
    require(status_counts == Counter({"retained": 540, "pending": 30, "retrying": 20, "failed": 10}),
            f"feedback retention distribution is wrong: {dict(status_counts)}")
    feedback_by_conversation = Counter(row["conversation_id"] for row in feedback)
    require(all(feedback_by_conversation[cid] == 3 for cid in conversation_ids), "each conversation needs 3 feedback rows")
    for row in feedback:
        require(row["conversation_id"] in conversation_ids, f"{row['feedback_id']}: unknown conversation")
        require(row["message_id"] in message_ids, f"{row['feedback_id']}: unknown message")
        require(row["trace_id"] in trace_ids, f"{row['feedback_id']}: unknown trace")
        require(row["user_id"] in user_ids, f"{row['feedback_id']}: unknown user")
        if row["incident_id"]:
            require(row["incident_id"] in incident_ids, f"{row['feedback_id']}: unknown incident")
    require({r["feedback_id"] for r in retain_jobs} == feedback_ids, "retain-job coverage does not match feedback")

    guardrail_counts = Counter(row["category"] for row in guardrail)
    require(guardrail_counts == Counter({
        "synthetic_secret": 40, "contact_pii": 25, "log_prompt_injection": 40,
        "stacktrace_instruction": 20, "oversized_input": 20, "off_topic": 20,
        "invented_incident_id": 15, "false_execution_claim": 10, "destructive_command": 10,
    }), f"guardrail distribution is wrong: {dict(guardrail_counts)}")

    eval_by_scenario_config: Counter[tuple[str, str]] = Counter()
    for row in eval_results:
        require(row["scenario_id"] in scenario_ids, f"{row['evaluation_result_id']}: unknown scenario")
        require(row["execution_mode"] == "synthetic_fixture_not_live_measurement",
                f"{row['evaluation_result_id']}: fixture is not labelled honestly")
        require(set(row["retrieved_incident_ids"]) <= incident_ids,
                f"{row['evaluation_result_id']}: invented incident citation")
        eval_by_scenario_config[(row["scenario_id"], row["configuration"])] += 1
    for scenario_id in scenario_ids:
        require(eval_by_scenario_config[(scenario_id, "with_memory")] == 5,
                f"{scenario_id}: expected five with-memory runs")
        require(eval_by_scenario_config[(scenario_id, "without_memory")] == 5,
                f"{scenario_id}: expected five without-memory runs")

    trace_conversations = Counter(row["conversation_id"] for row in traces)
    require(set(trace_conversations) == conversation_ids, "trace coverage does not match conversations")
    for trace in traces:
        require(len(trace["spans"]) == 8, f"{trace['trace_id']}: expected 8 spans")
        require([s["name"] for s in trace["spans"]] == [
            "validate_input", "redact_sensitive_data", "understand_incident", "fetch_recent_changes",
            "hindsight_reflect", "validate_evidence", "validate_recommendations", "save_message_and_trace",
        ], f"{trace['trace_id']}: unexpected graph span sequence")
        require(not any(s.get("tool") == "hindsight.recall" for s in trace["spans"]),
                f"{trace['trace_id']}: live PagerMind is reflect-only")
    for event in sentry:
        require(event["trace_id"] in trace_ids, f"{event['event_id']}: unknown trace")

    source_paths = [row["source_path"] for row in files]
    require(len(source_paths) == len(set(source_paths)), "file manifest contains duplicate source paths")
    expected_paths = {i["log_file"] for i in incidents} | {s["log_file"] for s in scenarios}
    require(set(source_paths) == expected_paths, "file manifest does not exactly cover incident and evaluation logs")
    for row in files:
        path = DATA / row["source_path"]
        require(path.exists(), f"{row['file_id']}: source file missing")
        if path.exists():
            require(row["sha256"] == sha256_file(path), f"{row['file_id']}: checksum mismatch")
            require(row["size_bytes"] == path.stat().st_size, f"{row['file_id']}: size mismatch")

    links_by_incident = Counter(row["incident_id"] for row in change_links)
    relationships = Counter(row["relationship"] for row in change_links)
    require(set(links_by_incident) == incident_ids, "change links do not cover every incident")
    require(all(count == 3 for count in links_by_incident.values()), "each incident must have exactly 3 change links")
    require(relationships == Counter({"causal": 500, "coincidental": 500, "ruled_out": 500}),
            f"change relationship distribution is wrong: {dict(relationships)}")
    for row in change_links:
        require(row["change_id"] in deploy_ids, f"{row['incident_change_link_id']}: unknown change")

    require({row["incident_id"] for row in supabase_incidents} == incident_ids,
            "Supabase incident seed does not cover the memory corpus")

    previous_hash = "GENESIS"
    audit_by_conversation: Counter[str] = Counter()
    for row in audit:
        require(row["conversation_id"] in conversation_ids, f"{row['audit_event_id']}: unknown conversation")
        require(row["trace_id"] in trace_ids, f"{row['audit_event_id']}: unknown trace")
        require(row["previous_hash"] == previous_hash, f"{row['audit_event_id']}: broken previous-hash link")
        core = {k: v for k, v in row.items() if k not in {"event_hash", "data_provenance"}}
        canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256((previous_hash + canonical).encode()).hexdigest()
        require(row["event_hash"] == expected_hash, f"{row['audit_event_id']}: invalid event hash")
        previous_hash = row["event_hash"]
        audit_by_conversation[row["conversation_id"]] += 1
    require(all(audit_by_conversation[cid] == 15 for cid in conversation_ids),
            "each conversation must have exactly 15 audit events")

    if errors:
        print(f"ARCHITECTURE DATA INVALID: {len(errors)} error(s)")
        for error in errors[:120]:
            print(f"- {error}")
        raise SystemExit(1)

    print("ARCHITECTURE DATA VALID")
    print("- 200 conversations / 1,200 messages / 200 checkpoints")
    print("- 600 feedback rows / 600 Hindsight retain jobs")
    print("- 200 guardrail tests")
    print("- 1,000 labelled evaluation-result fixtures")
    print("- 200 trace bundles / 1,600 agent spans / 40 Sentry events / 120 health snapshots")
    print("- 600 checksummed file-manifest rows")
    print("- 1,500 incident/change links with balanced causal negatives")
    print("- 3,000 hash-chained audit events plus Supabase identity, ownership, on-call and incident seeds")


if __name__ == "__main__":
    main()
