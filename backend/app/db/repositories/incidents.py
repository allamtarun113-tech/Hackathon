"""Incidents, their timelines, resolutions and follow-ups."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.db.json import jsonb
from app.db.pool import Connection


async def get_incident(conn: Connection, incident_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute("select * from incidents where incident_id = %s", (incident_id,))
    incident = await cursor.fetchone()
    if incident is None:
        return None

    actions = await conn.execute(
        "select * from incident_actions where incident_id = %s order by performed_at", (incident_id,)
    )
    resolutions = await conn.execute(
        "select * from incident_resolutions where incident_id = %s order by postmortem_version desc",
        (incident_id,),
    )
    follow_ups = await conn.execute(
        "select * from follow_up_items where incident_id = %s order by created_at", (incident_id,)
    )
    incident["actions"] = await actions.fetchall()
    incident["resolutions"] = await resolutions.fetchall()
    incident["follow_ups"] = await follow_ups.fetchall()
    return incident


async def existing_incident_ids(conn: Connection, candidate_ids: Iterable[str]) -> set[str]:
    ids = list(set(candidate_ids))
    if not ids:
        return set()
    cursor = await conn.execute("select incident_id from incidents where incident_id = any(%s)", (ids,))
    return {row["incident_id"] for row in await cursor.fetchall()}


async def create_live_incident(
    conn: Connection,
    *,
    incident_id: str,
    service_id: str,
    title: str,
    severity: str,
    alert: dict[str, Any],
    started_at: datetime,
) -> None:
    await conn.execute(
        """
        insert into incidents (incident_id, service_id, title, severity, status, alert, source, started_at)
        values (%s, %s, %s, %s, 'investigating', %s, 'live', %s)
        """,
        (incident_id, service_id, title, severity, jsonb(alert), started_at),
    )


async def upsert_historical_incident(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into incidents (
            incident_id, service_id, title, severity, status, environment, region, alert, customer_impact,
            root_cause, resolution_summary, knowledge_status, source, started_at, resolved_at, mttr_minutes
        ) values (
            %(incident_id)s, %(service_id)s, %(title)s, %(severity)s, 'resolved', %(environment)s, %(region)s,
            %(alert)s, %(customer_impact)s, %(root_cause)s, %(resolution_summary)s, %(knowledge_status)s,
            'historical-seed', %(started_at)s, %(resolved_at)s, %(mttr_minutes)s
        )
        on conflict (incident_id) do update set
            title = excluded.title,
            severity = excluded.severity,
            alert = excluded.alert,
            customer_impact = excluded.customer_impact,
            root_cause = excluded.root_cause,
            resolution_summary = excluded.resolution_summary,
            knowledge_status = excluded.knowledge_status,
            resolved_at = excluded.resolved_at,
            mttr_minutes = excluded.mttr_minutes
        """,
        {**row, "alert": jsonb(row["alert"])},
    )


async def replace_timeline(
    conn: Connection,
    incident_id: str,
    *,
    events: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> None:
    """Imports are re-runnable: the timeline for an incident is replaced as a whole."""
    await conn.execute("delete from incident_events where incident_id = %s", (incident_id,))
    await conn.execute("delete from incident_actions where incident_id = %s", (incident_id,))
    async with conn.cursor() as cursor:
        await cursor.executemany(
            """
            insert into incident_events (incident_id, occurred_at, actor_name, event_type, summary)
            values (%(incident_id)s, %(occurred_at)s, %(actor_name)s, %(event_type)s, %(summary)s)
            """,
            [{**event, "incident_id": incident_id} for event in events],
        )
        await cursor.executemany(
            """
            insert into incident_actions (incident_id, performed_at, actor_name, action, command, outcome, result)
            values (%(incident_id)s, %(performed_at)s, %(actor_name)s, %(action)s, %(command)s,
                    %(outcome)s, %(result)s)
            """,
            [{**action, "incident_id": incident_id} for action in actions],
        )


async def replace_follow_ups(conn: Connection, incident_id: str, items: list[str], owner: str | None) -> None:
    await conn.execute("delete from follow_up_items where incident_id = %s", (incident_id,))
    async with conn.cursor() as cursor:
        await cursor.executemany(
            "insert into follow_up_items (incident_id, description, owner_name) values (%s, %s, %s)",
            [(incident_id, item, owner) for item in items],
        )


async def add_action(
    conn: Connection,
    *,
    incident_id: str,
    actor_name: str,
    action: str,
    command: str | None,
    outcome: str,
    result: str | None,
    performed_at: datetime,
) -> None:
    await conn.execute(
        """
        insert into incident_actions (incident_id, performed_at, actor_name, action, command, outcome, result)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (incident_id, performed_at, actor_name, action, command, outcome, result),
    )


async def record_resolution(
    conn: Connection,
    *,
    incident_id: str,
    resolved_by: str,
    root_cause: str,
    resolution: str,
    commands: list[str],
    failed_actions: list[str],
    follow_ups: list[str],
    resolved_at: datetime,
) -> int:
    """Store a (possibly revised) postmortem and close the incident. Returns the postmortem version."""
    cursor = await conn.execute(
        "select coalesce(max(postmortem_version), 0) + 1 as next from incident_resolutions where incident_id = %s",
        (incident_id,),
    )
    version: int = (await cursor.fetchone() or {"next": 1})["next"]
    await conn.execute(
        """
        insert into incident_resolutions (
            incident_id, resolved_by, root_cause, resolution, commands, failed_actions, postmortem_version, resolved_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (incident_id, resolved_by, root_cause, resolution, commands, failed_actions, version, resolved_at),
    )
    await conn.execute(
        """
        update incidents
        set status = 'resolved',
            root_cause = %s,
            resolution_summary = %s,
            resolved_at = %s,
            mttr_minutes = greatest(0, extract(epoch from (%s - started_at))::int / 60)
        where incident_id = %s
        """,
        (root_cause, resolution, resolved_at, resolved_at, incident_id),
    )
    await replace_follow_ups(conn, incident_id, follow_ups, owner=resolved_by)
    return version


async def upsert_seed_resolution(
    conn: Connection,
    *,
    incident_id: str,
    root_cause: str,
    resolution: str,
    commands: list[str],
    failed_actions: list[str],
    resolved_at: datetime | None,
) -> None:
    """The historical postmortem is always version 1; re-imports overwrite it in place."""
    await conn.execute(
        """
        insert into incident_resolutions (incident_id, root_cause, resolution, commands, failed_actions,
                                          postmortem_version, resolved_at)
        values (%s, %s, %s, %s, %s, 1, coalesce(%s, now()))
        on conflict (incident_id, postmortem_version) do update set
            root_cause = excluded.root_cause,
            resolution = excluded.resolution,
            commands = excluded.commands,
            failed_actions = excluded.failed_actions
        """,
        (incident_id, root_cause, resolution, commands, failed_actions, resolved_at),
    )
