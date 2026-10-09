"""Import deployments, historical incidents (normalised) and incident/change relationships."""

from __future__ import annotations

from typing import Any

from app.db.pool import Database
from app.db.repositories import deployments, incidents
from app.importers.source import SeedSource, parse_ts

_EVENT_ACTIONS = {"Acknowledged the page": "status_change", "Marked the incident resolved": "resolution"}


async def import_deployments(db: Database, source: SeedSource) -> dict[str, int]:
    async with db.transaction() as conn:
        for deploy in source.deployments:
            await deployments.upsert_deployment(
                conn,
                {
                    "deployment_id": deploy["id"],
                    "service_id": deploy["service"],
                    "version": deploy.get("version"),
                    "previous_version": deploy.get("previous_version"),
                    "change_type": deploy.get("type", "deploy"),
                    "change_summary": deploy.get("change_summary"),
                    "deployed_by": deploy.get("deployed_by"),
                    "environment": deploy.get("environment", "production"),
                    "region": deploy.get("region"),
                    "deployed_at": parse_ts(deploy["at"]),
                },
            )
    return {"deployments": len(source.deployments)}


def _split_timeline(timeline: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for step in timeline:
        at = parse_ts(step["at"])
        if step["action"] in _EVENT_ACTIONS or step["outcome"] == "info":
            events.append(
                {
                    "occurred_at": at,
                    "actor_name": step.get("actor"),
                    "event_type": _EVENT_ACTIONS.get(step["action"], "note"),
                    "summary": f"{step['action']}. {step.get('result', '')}".strip(),
                }
            )
            continue
        actions.append(
            {
                "performed_at": at,
                "actor_name": step.get("actor"),
                "action": step["action"],
                "command": step.get("command"),
                "outcome": step["outcome"],
                "result": step.get("result"),
            }
        )
    return events, actions


async def import_incidents(db: Database, source: SeedSource) -> dict[str, int]:
    action_count = 0
    for incident in source.incidents:
        events, actions = _split_timeline(incident["timeline"])
        started_at = parse_ts(incident["date"])
        resolved_at = parse_ts(incident["timeline"][-1]["at"]) if incident["timeline"] else started_at
        async with db.transaction() as conn:
            await incidents.upsert_historical_incident(
                conn,
                {
                    "incident_id": incident["id"],
                    "service_id": incident["service"],
                    "title": incident["title"],
                    "severity": incident["severity"],
                    "environment": incident.get("environment", "production"),
                    "region": incident.get("region"),
                    "alert": incident["alert"],
                    "customer_impact": incident.get("customer_impact"),
                    "root_cause": incident["root_cause"],
                    "resolution_summary": incident["resolution"],
                    "knowledge_status": incident.get("knowledge_status"),
                    "started_at": started_at,
                    "resolved_at": resolved_at,
                    "mttr_minutes": incident.get("mttr_minutes"),
                },
            )
            await incidents.replace_timeline(conn, incident["id"], events=events, actions=actions)
            await incidents.replace_follow_ups(
                conn, incident["id"], incident.get("action_items", []), owner=incident.get("resolved_by")
            )
            await incidents.upsert_seed_resolution(
                conn,
                incident_id=incident["id"],
                root_cause=incident["root_cause"],
                resolution=incident["resolution"],
                commands=sorted({a["command"] for a in actions if a.get("command")}),
                failed_actions=[attempt["action"] for attempt in incident.get("failed_attempts", [])],
                resolved_at=resolved_at,
            )
        action_count += len(actions)
    return {"incidents": len(source.incidents), "incident_actions": action_count}


async def import_change_links(db: Database, source: SeedSource) -> dict[str, int]:
    links = source.production("incident_change_links")
    known_incidents = {incident["id"] for incident in source.incidents}
    imported = 0
    async with db.transaction() as conn:
        for link in links:
            if link["incident_id"] not in known_incidents:
                continue
            await deployments.upsert_change_link(
                conn,
                {
                    "link_id": link["incident_change_link_id"],
                    "incident_id": link["incident_id"],
                    "change_id": link["change_id"],
                    "relationship": link["relationship"],
                    "confidence": link["confidence"],
                    "evidence": link.get("evidence"),
                    "reviewer_name": link.get("reviewed_by"),
                    "reviewed_at": parse_ts(link.get("reviewed_at")),
                },
            )
            imported += 1
    return {"incident_change_links": imported}
