"""Import teams, engineers, services, owners and on-call shifts."""

from __future__ import annotations

from app.db.pool import Database
from app.db.repositories import reference
from app.importers.source import SeedSource, parse_ts, slugify, team_id_for


async def import_reference(db: Database, source: SeedSource) -> dict[str, int]:
    services = source.catalog["services"]
    team_names = {engineer["team"] for engineer in source.engineers} | {s["team"] for s in services.values()}
    team_names |= {team["name"] for team in source.production("teams")}

    async with db.transaction() as conn:
        for name in sorted(team_names):
            await reference.upsert_team(conn, team_id=team_id_for(name), name=name, slug=slugify(name))

        for engineer in source.engineers:
            await reference.upsert_engineer(
                conn,
                {
                    "engineer_id": engineer["id"],
                    "display_name": engineer["name"],
                    "title": engineer.get("role"),
                    "team_id": team_id_for(engineer["team"]),
                    "slack_handle": engineer.get("slack"),
                    "preference": engineer.get("preference"),
                    "joined_on": engineer.get("joined"),
                },
            )

        for service_id, service in services.items():
            await reference.upsert_service(
                conn,
                {
                    "service_id": service_id,
                    "team_id": team_id_for(service["team"]),
                    "runtime": service.get("runtime"),
                    "namespace": service.get("namespace"),
                    "description": service.get("description"),
                    "depends_on": service.get("depends_on", []),
                    "dashboard_url": service.get("dashboard"),
                },
            )

        owner_count = 0
        for ownership in source.production("service_ownership"):
            for order, user_id in enumerate(ownership["escalation_policy"], start=1):
                engineer_id = source.user_to_engineer.get(user_id)
                if engineer_id:
                    await reference.upsert_service_owner(
                        conn, service_id=ownership["service_id"], engineer_id=engineer_id, order=order
                    )
                    owner_count += 1

        shifts = source.production("oncall_shifts")
        for shift in shifts:
            await reference.upsert_oncall_shift(
                conn,
                {
                    "shift_id": shift["shift_id"],
                    "engineer_id": source.user_to_engineer[shift["user_id"]],
                    "shift_role": shift["role"],
                    "starts_at": parse_ts(shift["starts_at"]),
                    "ends_at": parse_ts(shift["ends_at"]),
                    "service_ids": shift["service_ids"],
                },
            )

    return {
        "teams": len(team_names),
        "engineers": len(source.engineers),
        "services": len(services),
        "service_owners": owner_count,
        "oncall_shifts": len(shifts),
    }
