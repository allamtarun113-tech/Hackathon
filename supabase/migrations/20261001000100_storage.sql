-- PagerMind Storage buckets.
--
-- Both buckets are private. Objects are written and read only by the FastAPI
-- backend using the service-role key; the browser never receives a storage URL
-- that is not short-lived and signed.
--
-- Guarded so the migration also applies to a plain Postgres (CI) where the
-- Supabase Storage schema does not exist.

do $$
begin
    if to_regclass('storage.buckets') is null then
        raise notice 'storage.buckets not found; skipping bucket creation';
        return;
    end if;

    insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
    values
        (
            'incident-evidence',
            'incident-evidence',
            false,
            5 * 1024 * 1024,
            array['text/plain', 'application/json', 'application/x-ndjson', 'text/x-log']
        ),
        (
            'runbooks',
            'runbooks',
            false,
            1024 * 1024,
            array['text/markdown', 'text/plain']
        )
    on conflict (id) do update
    set public             = excluded.public,
        file_size_limit    = excluded.file_size_limit,
        allowed_mime_types = excluded.allowed_mime_types;
end;
$$;
