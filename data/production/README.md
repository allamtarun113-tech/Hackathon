# PagerMind production architecture seed data

This directory is the exact-record layer described in the production architecture. The existing files under `data/`
remain the Hindsight memory/evaluation corpus; these files model Supabase rows, observability events, guardrail cases,
file metadata and CI evaluation results.

All records are synthetic, deterministic and carry `data_provenance`. Rows marked as fixtures are not live measurements.

## Core conversation and learning records

- `conversations.json`: 200 conversation headers with expected extraction, tool and behavior labels.
- `messages.json`: 1,200 ordered user/assistant messages (six per conversation).
- `agent_checkpoints.json`: 200 final LangGraph-style checkpoints.
- `feedback.json`: 600 step-level outcomes linked to messages, incidents, users and traces.
- `feedback_retain_jobs.json`: 600 retained/pending/retrying/failed Hindsight outbox jobs.

## Safety and evaluation

- `guardrail_tests.json`: 200 input/output guardrail cases covering synthetic secrets, PII, prompt injection, oversized
  inputs, off-topic prompts, invented citations, false execution claims and destructive commands.
- `evaluation_results.json`: 1,000 explicitly-labelled result fixtures (100 scenarios x with/without memory x five runs).
  Replace these fixture outputs with real CI model calls before publishing performance claims.

## Observability

- `trace_bundles.json`: 200 traces with eight agent spans each (1,600 spans total).
- `sentry_events.json`: 40 handled error fixtures linked to traces and requests.
- `health_snapshots.json`: 120 Groq/Hindsight/Supabase health snapshots.

## Evidence and change correlation

- `file_manifest.json`: checksums and Supabase Storage metadata for all 600 incident/evaluation logs.
- `incident_change_links.json`: 1,500 reviewed relationship fixtures (one causal, one coincidental and one ruled-out
  change per incident).

## Supabase seed records and audit history

- `organizations.json`, `teams.json`, `users.json`, `memberships.json`
- `service_ownership.json`, `oncall_shifts.json`
- `supabase_incidents.json`
- `audit_log.json`: 3,000 append-only, hash-chained audit events.

## Rebuild and validate

```bash
python scripts/generate_architecture_data.py
python scripts/validate_architecture_data.py
```

The generator only writes this directory. It does not modify the existing 500 historical incidents, 100 evaluation
scenarios, deployments, logs, engineers, service catalog or runbooks.
