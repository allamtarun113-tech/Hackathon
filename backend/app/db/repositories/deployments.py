"""Deployments and incident/change relationships."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.db.pool import Connection


async def recent_changes(
    conn: Connection,
    *,
    service_ids: list[str],
    before: datetime,
    window: timedelta = timedelta(hours=24),
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Changes to the affected service (and its dependencies) shortly before the incident."""
    cursor = await conn.execute(
        """
        select deployment_id, service_id, version, previous_version, change_type, change_summary,
               deployed_by, deployed_at, incident_links
        from recent_service_changes
        where service_id = any(%s)
          and deployed_at between %s and %s
        order by deployed_at desc
        limit %s
        """,
        (service_ids, before - window, before, limit),
    )
    return await cursor.fetchall()


async def upsert_deployment(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into deployments (deployment_id, service_id, version, previous_version, change_type,
                                 change_summary, deployed_by, environment, region, deployed_at)
        values (%(deployment_id)s, %(service_id)s, %(version)s, %(previous_version)s, %(change_type)s,
                %(change_summary)s, %(deployed_by)s, %(environment)s, %(region)s, %(deployed_at)s)
        on conflict (deployment_id) do update set
            version = excluded.version,
            previous_version = excluded.previous_version,
            change_type = excluded.change_type,
            change_summary = excluded.change_summary,
            deployed_by = excluded.deployed_by,
            deployed_at = excluded.deployed_at
        """,
        row,
    )


async def upsert_change_link(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into incident_change_links (link_id, incident_id, change_id, relationship, confidence,
                                           evidence, reviewer_name, reviewed_at)
        values (%(link_id)s, %(incident_id)s, %(change_id)s, %(relationship)s, %(confidence)s,
                %(evidence)s, %(reviewer_name)s, %(reviewed_at)s)
        on conflict (incident_id, change_id) do update set
            relationship = excluded.relationship,
            confidence = excluded.confidence,
            evidence = excluded.evidence,
            reviewer_name = excluded.reviewer_name,
            reviewed_at = excluded.reviewed_at
        """,
        row,
    )


async def reviewed_links_for_memory(conn: Connection, incident_id: str) -> list[dict[str, Any]]:
    """Only reviewed causal / contributing / ruled-out links are worth remembering."""
    cursor = await conn.execute(
        """
        select l.change_id, l.relationship, l.confidence, l.evidence, d.service_id, d.version, d.change_summary
        from incident_change_links l
        join deployments d on d.deployment_id = l.change_id
        where l.incident_id = %s
          and l.reviewed_at is not null
          and l.relationship in ('causal', 'contributing', 'ruled_out')
        order by l.confidence desc
        """,
        (incident_id,),
    )
    return await cursor.fetchall()
