"""The schema builds from an empty database and enforces its own invariants."""

from __future__ import annotations

import psycopg
import pytest

EXPECTED_TABLES = {
    "engineers", "teams", "team_members", "services", "service_owners", "oncall_shifts",
    "incidents", "incident_events", "incident_actions", "deployments", "incident_change_links",
    "incident_resolutions", "follow_up_items",
    "conversations", "messages", "agent_checkpoints", "agent_runs", "tool_calls",
    "feedback", "memory_outbox", "memory_documents", "memory_operations", "memory_webhook_events",
    "memory_webhook_claims", "worker_heartbeats", "rate_limit_buckets",
    "file_manifests", "incident_attachments", "runbook_versions",
    "evaluation_cases", "evaluation_runs", "evaluation_results", "guardrail_results",
    "trace_links", "health_snapshots", "audit_events", "data_deletion_log",
}  # fmt: skip


def test_every_table_exists_with_row_level_security(clean_database: str) -> None:
    with psycopg.connect(clean_database) as conn:
        rows = conn.execute(
            "select tablename, rowsecurity from pg_tables where schemaname = 'public'"
        ).fetchall()
    tables = {name: rls for name, rls in rows}
    assert set(tables) >= EXPECTED_TABLES
    assert all(tables.values()), [name for name, rls in tables.items() if not rls]


def test_audit_log_is_hash_chained_and_append_only(clean_database: str) -> None:
    with psycopg.connect(clean_database, autocommit=True) as conn:
        for index in range(3):
            conn.execute(
                """
                insert into audit_events (event_type, actor_type, resource_type, resource_id,
                                          previous_hash, event_hash)
                values ('test.event', 'system', 'test', %s, '', '')
                """,
                (str(index),),
            )
        chain = conn.execute(
            "select previous_hash, event_hash from audit_events order by sequence_no"
        ).fetchall()
        assert chain[0][0] == "GENESIS"
        assert all(chain[i][0] == chain[i - 1][1] for i in range(1, len(chain)))
        assert len({row[1] for row in chain}) == 3

        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("update audit_events set details = '{}'::jsonb")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("delete from audit_events")


def test_status_constraints_reject_unknown_states(clean_database: str) -> None:
    with psycopg.connect(clean_database) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            insert into feedback (feedback_id, conversation_id, validation_state)
            values ('F1', 'missing', 'accepted')
            """
        )
