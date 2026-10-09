-- Indexes for the hot query paths, plus support for time-based retention.
--
-- Every index below backs a specific repository query:
--   memory_outbox(hindsight_operation_id)   memory.job_by_operation (webhook + reconciler)
--   feedback(message_id)                    feedback lookups per agent message
--   messages(request_id)                    trace/feedback correlation
--   trace_links(conversation_id)            conversations.trace_ids_for_incident
--   agent_runs(request_id)                  run lookup by request
--   memory_documents(source_id)             memory.documents_for_source
--   file_manifests(conversation_id)         attachment ownership checks
--   *(received_at / checked_at / created_at) retention pruning by the memory worker

create unique index if not exists memory_outbox_hindsight_operation_idx
    on public.memory_outbox (hindsight_operation_id)
    where hindsight_operation_id is not null;

create index if not exists feedback_message_idx on public.feedback (message_id);
create index if not exists messages_request_idx on public.messages (request_id) where request_id is not null;
create index if not exists trace_links_conversation_idx on public.trace_links (conversation_id);
create index if not exists agent_runs_request_idx on public.agent_runs (request_id);
create index if not exists memory_documents_source_idx on public.memory_documents (source_id);
create index if not exists file_manifests_conversation_idx on public.file_manifests (conversation_id)
    where conversation_id is not null;

create index if not exists memory_webhook_events_received_idx on public.memory_webhook_events (received_at);
create index if not exists health_snapshots_live_idx on public.health_snapshots (provenance, checked_at desc);
create index if not exists guardrail_results_created_idx on public.guardrail_results (created_at);
