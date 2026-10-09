-- Hardening after the production review:
--   * feedback stays provisional until explicitly verified
--   * outbox jobs are serialised per document and may end in needs_review (zero extracted units)
--   * webhook deliveries are claimed atomically
--   * worker heartbeats and a shared rate-limit store

-- ---------------------------------------------------------------------------
-- Feedback verification
-- ---------------------------------------------------------------------------

alter table public.feedback
    add column verified_by  text,
    add column verified_at  timestamptz,
    add column review_note  text;

alter table public.feedback drop constraint feedback_validation_state_check;
alter table public.feedback add constraint feedback_validation_state_check
    check (validation_state in ('pending', 'verified', 'rejected', 'confirmed_by_resolution'));

-- ---------------------------------------------------------------------------
-- Outbox ordering and zero-unit results
-- ---------------------------------------------------------------------------

alter table public.memory_outbox
    add column enqueue_seq bigint generated always as identity;

create index memory_outbox_document_seq_idx on public.memory_outbox (document_id, enqueue_seq);

alter table public.memory_outbox drop constraint memory_outbox_status_check;
alter table public.memory_outbox add constraint memory_outbox_status_check
    check (status in ('pending', 'validating', 'submitted', 'processing', 'retained',
                      'needs_review', 'failed', 'rejected', 'dead_letter'));

alter table public.memory_documents drop constraint memory_documents_status_check;
alter table public.memory_documents add constraint memory_documents_status_check
    check (status in ('pending', 'retained', 'needs_review', 'failed', 'deleted'));

-- ---------------------------------------------------------------------------
-- Atomic webhook claims: the primary key makes "first delivery wins" a single statement.
-- ---------------------------------------------------------------------------

create table public.memory_webhook_claims (
    hindsight_operation_id  text not null,
    event_type              text not null,
    webhook_event_id        text not null references public.memory_webhook_events (webhook_event_id),
    claimed_at              timestamptz not null default now(),
    primary key (hindsight_operation_id, event_type)
);

-- ---------------------------------------------------------------------------
-- Worker heartbeats (health no longer infers liveness from outbox activity)
-- ---------------------------------------------------------------------------

create table public.worker_heartbeats (
    worker_id        text primary key,
    component        text not null,
    version          text,
    started_at       timestamptz not null,
    last_seen_at     timestamptz not null default now(),
    jobs_processed   bigint not null default 0
);

create index worker_heartbeats_component_idx on public.worker_heartbeats (component, last_seen_at desc);

-- ---------------------------------------------------------------------------
-- Shared fixed-window rate limiting (works across API instances)
-- ---------------------------------------------------------------------------

create table public.rate_limit_buckets (
    bucket_key    text not null,
    window_start  timestamptz not null,
    hits          integer not null default 0,
    primary key (bucket_key, window_start)
);

create index rate_limit_buckets_window_idx on public.rate_limit_buckets (window_start);

-- ---------------------------------------------------------------------------
-- Lock down the new tables like the rest of the schema.
-- ---------------------------------------------------------------------------

alter table public.memory_webhook_claims enable row level security;
alter table public.worker_heartbeats enable row level security;
alter table public.rate_limit_buckets enable row level security;
