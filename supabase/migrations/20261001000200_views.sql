-- Read models used by the API and the health endpoint.

create or replace view public.memory_outbox_summary
with (security_invoker = true) as
select
    status,
    count(*)                                   as job_count,
    min(created_at)                            as oldest_created_at,
    max(attempt_count)                         as max_attempts_seen
from public.memory_outbox
group by status;

-- Recent changes for a service, newest first, with any reviewed incident links.
create or replace view public.recent_service_changes
with (security_invoker = true) as
select
    d.deployment_id,
    d.service_id,
    d.version,
    d.previous_version,
    d.change_type,
    d.change_summary,
    d.deployed_by,
    d.deployed_at,
    coalesce(
        jsonb_agg(
            jsonb_build_object(
                'incident_id', l.incident_id,
                'relationship', l.relationship,
                'confidence', l.confidence
            )
        ) filter (where l.link_id is not null),
        '[]'::jsonb
    ) as incident_links
from public.deployments d
left join public.incident_change_links l
    on l.change_id = d.deployment_id
   and l.reviewed_at is not null
group by d.deployment_id;
