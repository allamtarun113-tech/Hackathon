"""Engineers, teams and services."""

from __future__ import annotations

from typing import Any

from app.db.pool import Connection


async def list_engineers(conn: Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select e.engineer_id, e.display_name, e.title, t.name as team_name, e.preference
        from engineers e
        left join teams t using (team_id)
        where e.is_active
        order by e.display_name
        """
    )
    return await cursor.fetchall()


async def engineer_exists(conn: Connection, engineer_id: str) -> bool:
    cursor = await conn.execute(
        "select 1 from engineers where engineer_id = %s and is_active", (engineer_id,)
    )
    return await cursor.fetchone() is not None


async def list_services(conn: Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select s.service_id, s.description, s.runtime, s.depends_on, t.name as team_name
        from services s
        left join teams t using (team_id)
        order by s.service_id
        """
    )
    return await cursor.fetchall()


async def service_ids(conn: Connection) -> set[str]:
    cursor = await conn.execute("select service_id from services")
    return {row["service_id"] for row in await cursor.fetchall()}


async def escalation_team(conn: Connection, service_id: str) -> str | None:
    cursor = await conn.execute(
        "select t.name from services s join teams t using (team_id) where s.service_id = %s",
        (service_id,),
    )
    row = await cursor.fetchone()
    return row["name"] if row else None


async def upsert_team(conn: Connection, *, team_id: str, name: str, slug: str) -> None:
    await conn.execute(
        """
        insert into teams (team_id, name, slug) values (%s, %s, %s)
        on conflict (team_id) do update set name = excluded.name, slug = excluded.slug
        """,
        (team_id, name, slug),
    )


async def upsert_engineer(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into engineers (engineer_id, display_name, title, team_id, slack_handle, preference, joined_on)
        values (%(engineer_id)s, %(display_name)s, %(title)s, %(team_id)s, %(slack_handle)s,
                %(preference)s, %(joined_on)s)
        on conflict (engineer_id) do update set
            display_name = excluded.display_name,
            title = excluded.title,
            team_id = excluded.team_id,
            slack_handle = excluded.slack_handle,
            preference = excluded.preference,
            joined_on = excluded.joined_on
        """,
        row,
    )
    await conn.execute(
        "insert into team_members (team_id, engineer_id) values (%(team_id)s, %(engineer_id)s) on conflict do nothing",
        row,
    )


async def upsert_service(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into services (service_id, team_id, runtime, namespace, description, depends_on, dashboard_url)
        values (%(service_id)s, %(team_id)s, %(runtime)s, %(namespace)s, %(description)s,
                %(depends_on)s, %(dashboard_url)s)
        on conflict (service_id) do update set
            team_id = excluded.team_id,
            runtime = excluded.runtime,
            namespace = excluded.namespace,
            description = excluded.description,
            depends_on = excluded.depends_on,
            dashboard_url = excluded.dashboard_url
        """,
        row,
    )


async def upsert_service_owner(conn: Connection, *, service_id: str, engineer_id: str, order: int) -> None:
    await conn.execute(
        """
        insert into service_owners (service_id, engineer_id, escalation_order, effective_from)
        values (%s, %s, %s, '2026-01-01T00:00:00Z')
        on conflict do nothing
        """,
        (service_id, engineer_id, order),
    )


async def upsert_oncall_shift(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into oncall_shifts (shift_id, engineer_id, shift_role, starts_at, ends_at, service_ids)
        values (%(shift_id)s, %(engineer_id)s, %(shift_role)s, %(starts_at)s, %(ends_at)s, %(service_ids)s)
        on conflict (shift_id) do nothing
        """,
        row,
    )


async def engineer_name(conn: Connection, engineer_id: str | None) -> str | None:
    if not engineer_id:
        return None
    cursor = await conn.execute("select display_name from engineers where engineer_id = %s", (engineer_id,))
    row = await cursor.fetchone()
    return row["display_name"] if row else None


async def service_dependencies(conn: Connection, service_id: str) -> list[str]:
    cursor = await conn.execute("select depends_on from services where service_id = %s", (service_id,))
    row = await cursor.fetchone()
    return list(row["depends_on"]) if row else []
