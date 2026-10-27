-- Photo sourcing worker: job queue + richer supplier rows. Run after 20261027_credits_v2_and_snapshots.sql

create table if not exists sourcing_jobs (
  id           bigserial primary key,
  product_id   text        not null,
  platform     text        not null,          -- alibaba | aliexpress
  user_email   text,
  charged      boolean     not null default false,
  status       text        not null default 'queued',  -- queued | running | done | no_match | failed
  attempts     int         not null default 0,
  error        text,
  result_count int         default 0,
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz
);
create index if not exists sourcing_jobs_status_idx on sourcing_jobs (status, created_at);
create index if not exists sourcing_jobs_product_idx on sourcing_jobs (product_id, platform);

-- Atomically hand the oldest queued job to one worker (safe with many workers).
-- Also re-queues jobs stuck in 'running' for more than 10 minutes (worker crash).
create or replace function claim_sourcing_job() returns setof sourcing_jobs
language plpgsql security definer as $$
begin
  update sourcing_jobs set status = 'queued'
   where status = 'running' and started_at < now() - interval '10 minutes' and attempts < 3;
  return query
  update sourcing_jobs j
     set status = 'running', started_at = now(), attempts = j.attempts + 1
   where j.id = (
     select id from sourcing_jobs where status = 'queued'
      order by charged desc, created_at          -- paying users' clicks before batch jobs
      limit 1 for update skip locked)
  returning j.*;
end;
$$;
revoke all on function claim_sourcing_job() from public, anon, authenticated;

alter table if exists suppliers add column if not exists similarity          numeric;
alter table if exists suppliers add column if not exists match_method        text;
alter table if exists suppliers add column if not exists external_product_id text;
alter table if exists suppliers add column if not exists orders              int;
alter table if exists suppliers add column if not exists rating              numeric;
alter table if exists suppliers add column if not exists verified_supplier   boolean;
alter table if exists suppliers add column if not exists sourced_at          timestamptz;
