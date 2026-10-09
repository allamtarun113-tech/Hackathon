-- PagerMind core schema.
--
-- Supabase Postgres is the exact source of truth. Hindsight is the learned-memory
-- layer and never replaces these records.
--
-- Conventions
--   * Natural text identifiers (INC-2073, DEP-10644, CONV-0001) are kept as primary keys
--     so imported records stay traceable to the source dataset.
--   * Status columns use CHECK constraints instead of enum types so new states can be
--     added with a single ALTER TABLE.
--   * Every table carries created_at; mutable tables carry updated_at maintained by trigger.
--   * There is no authentication or role model. The FastAPI backend is the only client and
--     connects with server-side credentials; RLS is enabled with no policies so the public
--     anon key cannot read or write anything (see the final section).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------------

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- People and services
-- ---------------------------------------------------------------------------

create table public.teams (
    team_id     text primary key,
    name        text not null unique,
    slug        text not null unique,
    created_at  timestamptz not null default now()
);

create table public.engineers (
    engineer_id   text primary key,
    display_name  text not null,
    title         text,
    team_id       text references public.teams (team_id),
    slack_handle  text,
    preference    text,
    joined_on     date,
    is_active     boolean not null default true,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create table public.team_members (
    team_id      text not null references public.teams (team_id) on delete cascade,
    engineer_id  text not null references public.engineers (engineer_id) on delete cascade,
    joined_at    timestamptz not null default now(),
    primary key (team_id, engineer_id)
);

create table public.services (
    service_id   text primary key,
    team_id      text references public.teams (team_id),
    runtime      text,
    namespace    text,
    description  text,
    depends_on   text[] not null default '{}',
    dashboard_url text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create table public.service_owners (
    service_id         text not null references public.services (service_id) on delete cascade,
    engineer_id        text not null references public.engineers (engineer_id),
    escalation_order   smallint not null default 1 check (escalation_order > 0),
    effective_from     timestamptz not null default now(),
    effective_to       timestamptz,
    primary key (service_id, engineer_id, effective_from)
);

create table public.oncall_shifts (
    shift_id     text primary key,
    engineer_id  text not null references public.engineers (engineer_id),
    shift_role   text not null check (shift_role in ('primary', 'secondary', 'platform', 'incident_commander')),
    starts_at    timestamptz not null,
    ends_at      timestamptz not null,
    service_ids  text[] not null default '{}',
    created_at   timestamptz not null default now(),
    check (ends_at > starts_at)
);

create index oncall_shifts_window_idx on public.oncall_shifts (starts_at, ends_at);

-- ---------------------------------------------------------------------------
-- Incident operations
-- ---------------------------------------------------------------------------

create table public.incidents (
    incident_id          text primary key,
    service_id           text not null references public.services (service_id),
    title                text not null,
    severity             text not null check (severity in ('SEV1', 'SEV2', 'SEV3', 'SEV4')),
    status               text not null default 'open'
                         check (status in ('open', 'investigating', 'mitigated', 'resolved', 'closed')),
    environment          text not null default 'production',
    region               text,
    alert                jsonb not null default '{}'::jsonb,
    customer_impact      text,
    root_cause           text,
    resolution_summary   text,
    knowledge_status     text,
    source               text not null default 'live' check (source in ('historical-seed', 'live')),
    started_at           timestamptz not null,
    resolved_at          timestamptz,
    mttr_minutes         integer check (mttr_minutes is null or mttr_minutes >= 0),
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create index incidents_service_started_idx on public.incidents (service_id, started_at desc);
create index incidents_status_idx on public.incidents (status);

create table public.incident_events (
    event_id      text primary key default ('EVT-' || replace(gen_random_uuid()::text, '-', '')),
    incident_id   text not null references public.incidents (incident_id) on delete cascade,
    occurred_at   timestamptz not null,
    actor_name    text,
    event_type    text not null check (event_type in ('alert', 'note', 'action', 'status_change', 'resolution')),
    summary       text not null,
    created_at    timestamptz not null default now()
);

create index incident_events_incident_idx on public.incident_events (incident_id, occurred_at);

create table public.incident_actions (
    action_id     text primary key default ('ACT-' || replace(gen_random_uuid()::text, '-', '')),
    incident_id   text not null references public.incidents (incident_id) on delete cascade,
    performed_at  timestamptz not null,
    actor_name    text,
    action        text not null,
    command       text,
    outcome       text not null check (outcome in ('worked', 'partial', 'did_not_work', 'made_worse', 'info', 'unknown')),
    result        text,
    created_at    timestamptz not null default now()
);

create index incident_actions_incident_idx on public.incident_actions (incident_id, performed_at);

create table public.deployments (
    deployment_id     text primary key,
    service_id        text not null references public.services (service_id),
    version           text,
    previous_version  text,
    change_type       text not null default 'deploy',
    change_summary    text,
    deployed_by       text,
    environment       text not null default 'production',
    region            text,
    deployed_at       timestamptz not null,
    created_at        timestamptz not null default now()
);

create index deployments_service_time_idx on public.deployments (service_id, deployed_at desc);

create table public.incident_change_links (
    link_id        text primary key,
    incident_id    text not null references public.incidents (incident_id) on delete cascade,
    change_id      text not null references public.deployments (deployment_id),
    relationship   text not null check (relationship in ('causal', 'contributing', 'coincidental', 'ruled_out', 'unknown')),
    confidence     numeric(4, 3) not null check (confidence between 0 and 1),
    evidence       text,
    reviewer_name  text,
    reviewed_at    timestamptz,
    created_at     timestamptz not null default now(),
    unique (incident_id, change_id)
);

create index incident_change_links_change_idx on public.incident_change_links (change_id);

create table public.incident_resolutions (
    resolution_id     text primary key default ('RES-' || replace(gen_random_uuid()::text, '-', '')),
    incident_id       text not null references public.incidents (incident_id) on delete cascade,
    resolved_by       text references public.engineers (engineer_id),
    root_cause        text not null,
    resolution        text not null,
    commands          text[] not null default '{}',
    failed_actions    text[] not null default '{}',
    postmortem_version integer not null default 1 check (postmortem_version > 0),
    resolved_at       timestamptz not null default now(),
    created_at        timestamptz not null default now(),
    unique (incident_id, postmortem_version)
);

create table public.follow_up_items (
    follow_up_id   text primary key default ('FUP-' || replace(gen_random_uuid()::text, '-', '')),
    incident_id    text not null references public.incidents (incident_id) on delete cascade,
    description    text not null,
    owner_name     text,
    status         text not null default 'open' check (status in ('open', 'in_progress', 'done', 'wont_fix')),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Agent state
-- ---------------------------------------------------------------------------

create table public.conversations (
    conversation_id     text primary key,
    engineer_id         text references public.engineers (engineer_id),
    service_id          text references public.services (service_id),
    incident_id         text references public.incidents (incident_id),
    title               text not null,
    status              text not null default 'active' check (status in ('active', 'resolved', 'abandoned')),
    source              text not null default 'live'
                        check (source in ('live', 'historical-seed', 'development-fixture', 'evaluation-fixture')),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index conversations_engineer_idx on public.conversations (engineer_id, created_at desc);

create table public.messages (
    message_id        text primary key,
    conversation_id   text not null references public.conversations (conversation_id) on delete cascade,
    sequence          integer not null check (sequence > 0),
    role              text not null check (role in ('engineer', 'agent', 'system')),
    message_type      text not null check (message_type in (
                          'engineer_input', 'agent_response', 'tool_status', 'system_event', 'resolution_update')),
    engineer_id       text references public.engineers (engineer_id),
    content           text not null,
    payload           jsonb,
    redaction_status  text not null default 'passed' check (redaction_status in ('passed', 'redacted', 'blocked')),
    request_id        text,
    created_at        timestamptz not null default now(),
    unique (conversation_id, sequence)
);

create table public.agent_checkpoints (
    conversation_id   text primary key references public.conversations (conversation_id) on delete cascade,
    incident_id       text references public.incidents (incident_id),
    service_id        text,
    symptoms          text,
    error_signatures  text[] not null default '{}',
    recent_changes    jsonb not null default '[]'::jsonb,
    matched_incidents text[] not null default '{}',
    graph_node        text not null,
    step_count        integer not null default 0,
    recommendation    jsonb,
    last_error        text,
    version           integer not null default 1,
    updated_at        timestamptz not null default now()
);

create table public.agent_runs (
    run_id            text primary key,
    conversation_id   text not null references public.conversations (conversation_id) on delete cascade,
    message_id        text references public.messages (message_id),
    request_id        text not null,
    status            text not null check (status in ('running', 'succeeded', 'failed', 'clarification', 'blocked')),
    outcome           text,
    match_strength    text check (match_strength in ('strong', 'weak', 'none', 'unavailable')),
    memory_available  boolean,
    model             text,
    fallback_used     boolean not null default false,
    input_tokens      integer,
    output_tokens     integer,
    latency_ms        integer,
    error             text,
    started_at        timestamptz not null default now(),
    finished_at       timestamptz
);

create index agent_runs_conversation_idx on public.agent_runs (conversation_id, started_at desc);

create table public.tool_calls (
    tool_call_id   text primary key default ('TC-' || replace(gen_random_uuid()::text, '-', '')),
    run_id         text not null references public.agent_runs (run_id) on delete cascade,
    node           text not null,
    tool           text not null,
    status         text not null check (status in ('ok', 'error', 'timeout', 'skipped')),
    latency_ms     integer,
    input_summary  text,
    output_summary text,
    error          text,
    created_at     timestamptz not null default now()
);

create index tool_calls_run_idx on public.tool_calls (run_id);

-- ---------------------------------------------------------------------------
-- Feedback and memory
-- ---------------------------------------------------------------------------

create table public.feedback (
    feedback_id       text primary key,
    conversation_id   text not null references public.conversations (conversation_id) on delete cascade,
    message_id        text references public.messages (message_id),
    incident_id       text references public.incidents (incident_id),
    step_id           text,
    engineer_id       text references public.engineers (engineer_id),
    service_id        text,
    rating            smallint check (rating in (-1, 1)),
    outcome           text check (outcome in ('worked', 'partial', 'did_not_work', 'made_worse', 'not_tried')),
    note              text,
    validation_state  text not null default 'pending'
                      check (validation_state in ('pending', 'accepted', 'rejected', 'confirmed_by_resolution')),
    retain_status     text not null default 'not_eligible'
                      check (retain_status in ('not_eligible', 'queued', 'retained', 'failed')),
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index feedback_incident_idx on public.feedback (incident_id);
create index feedback_conversation_idx on public.feedback (conversation_id);

create table public.memory_documents (
    document_id       text primary key,
    bank_id           text not null,
    memory_type       text not null check (memory_type in ('incident', 'procedure', 'feedback', 'live_step')),
    source_type       text not null check (source_type in ('incident', 'resolution', 'feedback', 'runbook')),
    source_id         text not null,
    current_checksum  text,
    status            text not null default 'pending' check (status in ('pending', 'retained', 'failed', 'deleted')),
    last_job_id       text,
    retained_at       timestamptz,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create table public.memory_outbox (
    job_id                  text primary key,
    bank_id                 text not null,
    document_id             text not null,
    source_type             text not null check (source_type in ('incident', 'resolution', 'feedback', 'runbook')),
    source_id               text not null,
    update_mode             text not null default 'replace' check (update_mode in ('replace', 'append')),
    payload                 jsonb not null,
    payload_checksum        text not null,
    operation_id            uuid not null unique,
    hindsight_operation_id  text,
    status                  text not null default 'pending'
                            check (status in ('pending', 'validating', 'submitted', 'processing',
                                              'retained', 'failed', 'rejected', 'dead_letter')),
    attempt_count           integer not null default 0,
    max_attempts            integer not null default 6,
    last_error              text,
    rejection_reason        text,
    memory_unit_count       integer,
    next_attempt_at         timestamptz not null default now(),
    locked_by               text,
    locked_at               timestamptz,
    submitted_at            timestamptz,
    completed_at            timestamptz,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    unique (bank_id, document_id, payload_checksum)
);

create index memory_outbox_ready_idx on public.memory_outbox (next_attempt_at)
    where status in ('pending', 'failed');
create index memory_outbox_inflight_idx on public.memory_outbox (submitted_at)
    where status in ('submitted', 'processing');

create table public.memory_operations (
    hindsight_operation_id  text primary key,
    job_id                  text not null references public.memory_outbox (job_id) on delete cascade,
    status                  text not null,
    memory_unit_count       integer,
    error_message           text,
    checked_at              timestamptz not null default now()
);

create table public.memory_webhook_events (
    webhook_event_id        text primary key default ('WHE-' || replace(gen_random_uuid()::text, '-', '')),
    hindsight_operation_id  text,
    event_type              text not null,
    bank_id                 text,
    signature_valid         boolean not null,
    payload                 jsonb not null,
    processing_status       text not null default 'received'
                            check (processing_status in ('received', 'processed', 'duplicate', 'ignored', 'error')),
    error                   text,
    received_at             timestamptz not null default now(),
    processed_at            timestamptz
);

-- At-least-once delivery: one processed row per (operation, event type).
create unique index memory_webhook_events_dedupe_idx
    on public.memory_webhook_events (hindsight_operation_id, event_type)
    where processing_status = 'processed';

-- ---------------------------------------------------------------------------
-- Files
-- ---------------------------------------------------------------------------

create table public.file_manifests (
    file_id            text primary key,
    incident_id        text references public.incidents (incident_id),
    conversation_id    text references public.conversations (conversation_id),
    storage_bucket     text not null,
    storage_path       text not null,
    original_filename  text,
    mime_type          text not null,
    size_bytes         bigint not null check (size_bytes >= 0),
    sha256             char(64) not null,
    source             text not null check (source in ('historical-seed', 'upload', 'runbook', 'evaluation')),
    redaction_state    text not null check (redaction_state in ('pending', 'redacted', 'clean', 'rejected')),
    redaction_count    integer not null default 0,
    uploaded_by        text references public.engineers (engineer_id),
    schema_version     text not null default '1.0',
    uploaded_at        timestamptz not null default now(),
    deleted_at         timestamptz,
    unique (storage_bucket, storage_path)
);

create table public.incident_attachments (
    incident_id  text not null references public.incidents (incident_id) on delete cascade,
    file_id      text not null references public.file_manifests (file_id) on delete cascade,
    purpose      text not null default 'log' check (purpose in ('log', 'postmortem', 'screenshot', 'other')),
    created_at   timestamptz not null default now(),
    primary key (incident_id, file_id)
);

create table public.runbook_versions (
    runbook_id      text not null,
    version         integer not null check (version > 0),
    service_id      text references public.services (service_id),
    title           text not null,
    file_id         text not null references public.file_manifests (file_id),
    sha256          char(64) not null,
    status          text not null default 'approved' check (status in ('draft', 'approved', 'superseded')),
    approved_by     text,
    created_at      timestamptz not null default now(),
    primary key (runbook_id, version)
);

-- ---------------------------------------------------------------------------
-- Evaluation (CI only - never retained into the production bank)
-- ---------------------------------------------------------------------------

create table public.evaluation_cases (
    case_id         text primary key,
    category        text not null,
    service_id      text,
    prompt          jsonb not null,
    expected        jsonb not null,
    created_at      timestamptz not null default now()
);

create table public.evaluation_runs (
    evaluation_run_id  text primary key,
    commit_sha         text,
    bank_id            text not null,
    chat_model         text,
    judge_model        text,
    repetitions        integer not null check (repetitions > 0),
    status             text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
    summary            jsonb,
    started_at         timestamptz not null default now(),
    finished_at        timestamptz
);

create table public.evaluation_results (
    evaluation_result_id  text primary key,
    evaluation_run_id     text not null references public.evaluation_runs (evaluation_run_id) on delete cascade,
    case_id               text not null references public.evaluation_cases (case_id),
    mode                  text not null check (mode in ('without_memory', 'with_memory')),
    repetition            integer not null check (repetition > 0),
    retrieved_incident_ids text[] not null default '{}',
    predicted_strong_match boolean,
    abstained             boolean,
    schema_valid          boolean not null,
    scores                jsonb not null,
    response              jsonb,
    latency_ms            integer,
    input_tokens          integer,
    output_tokens         integer,
    cost_usd              numeric(10, 6),
    error                 text,
    created_at            timestamptz not null default now(),
    unique (evaluation_run_id, case_id, mode, repetition)
);

create table public.guardrail_results (
    guardrail_result_id  text primary key default ('GRR-' || replace(gen_random_uuid()::text, '-', '')),
    evaluation_run_id    text references public.evaluation_runs (evaluation_run_id) on delete cascade,
    request_id           text,
    phase                text not null check (phase in ('input', 'memory', 'output', 'runtime')),
    check_name           text not null,
    decision             text not null,
    reason_code          text,
    details              jsonb,
    created_at           timestamptz not null default now()
);

create index guardrail_results_request_idx on public.guardrail_results (request_id);

-- ---------------------------------------------------------------------------
-- Observability
-- ---------------------------------------------------------------------------

create table public.trace_links (
    request_id        text primary key,
    conversation_id   text references public.conversations (conversation_id) on delete cascade,
    run_id            text references public.agent_runs (run_id) on delete cascade,
    langfuse_trace_id text,
    sentry_event_id   text,
    provenance        text not null default 'live' check (provenance in ('live', 'synthetic_fixture')),
    created_at        timestamptz not null default now()
);

create table public.health_snapshots (
    health_snapshot_id  text primary key default ('HLT-' || replace(gen_random_uuid()::text, '-', '')),
    overall_status      text not null check (overall_status in ('healthy', 'degraded', 'unhealthy')),
    dependencies        jsonb not null,
    pending_memory_jobs integer,
    provenance          text not null default 'live' check (provenance in ('live', 'synthetic_fixture')),
    checked_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Governance
-- ---------------------------------------------------------------------------

create table public.audit_events (
    sequence_no    bigint generated always as identity primary key,
    audit_event_id text not null unique default ('AUD-' || replace(gen_random_uuid()::text, '-', '')),
    event_type     text not null,
    actor_type     text not null check (actor_type in ('engineer', 'system', 'worker', 'webhook', 'ci')),
    actor_id       text,
    resource_type  text not null,
    resource_id    text,
    request_id     text,
    details        jsonb not null default '{}'::jsonb,
    occurred_at    timestamptz not null default now(),
    previous_hash  text not null,
    event_hash     text not null
);

create index audit_events_resource_idx on public.audit_events (resource_type, resource_id);

create table public.data_deletion_log (
    deletion_id    text primary key default ('DEL-' || replace(gen_random_uuid()::text, '-', '')),
    resource_type  text not null,
    resource_id    text not null,
    reason         text not null,
    requested_by   text,
    memory_document_ids text[] not null default '{}',
    deleted_at     timestamptz not null default now()
);

-- Hash chain: each event hashes its predecessor, making silent edits detectable.
-- An advisory lock serialises inserts so the chain never forks.
create or replace function public.audit_events_chain()
returns trigger
language plpgsql
as $$
declare
    last_hash text;
begin
    perform pg_advisory_xact_lock(hashtext('public.audit_events'));

    select event_hash into last_hash
    from public.audit_events
    order by sequence_no desc
    limit 1;

    new.previous_hash := coalesce(last_hash, 'GENESIS');
    new.event_hash := encode(
        digest(
            new.previous_hash
            || '|' || new.audit_event_id
            || '|' || new.event_type
            || '|' || new.actor_type
            || '|' || coalesce(new.actor_id, '')
            || '|' || new.resource_type
            || '|' || coalesce(new.resource_id, '')
            || '|' || coalesce(new.request_id, '')
            || '|' || new.details::text
            || '|' || to_char(new.occurred_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'sha256'
        ),
        'hex'
    );
    return new;
end;
$$;

create or replace function public.audit_events_append_only()
returns trigger
language plpgsql
as $$
begin
    raise exception 'audit_events is append-only';
end;
$$;

create trigger audit_events_chain_trg
    before insert on public.audit_events
    for each row execute function public.audit_events_chain();

create trigger audit_events_no_update_trg
    before update or delete on public.audit_events
    for each row execute function public.audit_events_append_only();

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------

do $$
declare
    t text;
begin
    foreach t in array array[
        'engineers', 'services', 'incidents', 'follow_up_items', 'conversations',
        'feedback', 'memory_documents', 'memory_outbox'
    ]
    loop
        execute format(
            'create trigger %I before update on public.%I for each row execute function public.touch_updated_at()',
            t || '_touch_updated_at', t
        );
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Lock down the PostgREST surface. The backend connects directly to Postgres
-- with server credentials, so no policies are defined: anon/authenticated keys
-- get nothing.
-- ---------------------------------------------------------------------------

do $$
declare
    t record;
begin
    for t in select tablename from pg_tables where schemaname = 'public'
    loop
        execute format('alter table public.%I enable row level security', t.tablename);
    end loop;
end;
$$;
