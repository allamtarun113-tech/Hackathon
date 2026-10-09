-- Circuit breaker state shared by every API instance and worker.
--
-- Each process keeps a fast local breaker and syncs it here, so when Hindsight or Groq is down the
-- whole deployment stops calling it within seconds, instead of every instance discovering it alone.
-- A process that cannot reach this table keeps working with its local breaker.

create table public.circuit_breakers (
    name                  text primary key,
    consecutive_failures  integer not null default 0 check (consecutive_failures >= 0),
    opened_until          timestamptz,
    updated_at            timestamptz not null default now()
);

alter table public.circuit_breakers enable row level security;
