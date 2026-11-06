-- ============================================================================
-- TikProfitPro: security fixes (audit 2026-11-07)
-- Run once in Supabase -> SQL Editor, AFTER all earlier migrations
-- (20261027_*, 20261027b_*, 20261106_*, 20261106b_*). Safe to re-run.
-- Run it BEFORE deploying the security-fixes backend: the webhook calls
-- upsert_subscription() (it falls back to the old per-event logic if missing).
-- The backend uses the service_role key, which bypasses RLS.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. users.plan: only free / pro / ultimate (audit L3)
--    NULL or unknown values become 'free'; 'Pro' becomes 'pro'.
-- ---------------------------------------------------------------------------
update public.users set plan = lower(btrim(plan))
 where plan is not null and plan <> lower(btrim(plan));

update public.users set plan = 'free'
 where plan is null or plan not in ('free', 'pro', 'ultimate');

alter table public.users alter column plan set default 'free';
-- (No NOT NULL: whatever creates users rows might insert NULL explicitly. The CHECK below
--  allows NULL and the backend treats NULL as 'free'.)

alter table public.users drop constraint if exists users_plan_check;
alter table public.users add constraint users_plan_check
  check (plan in ('free', 'pro', 'ultimate'));

-- The backend now matches users by exact lowercased email (never ILIKE, audit C2).
-- Lowercase stored emails, but only where that can't collide with another row.
do $$
declare r record;
begin
  for r in select ctid, user_email from public.users
            where user_email is not null and user_email <> lower(btrim(user_email)) loop
    begin
      if not exists (select 1 from public.users o where o.user_email = lower(btrim(r.user_email))) then
        update public.users set user_email = lower(btrim(user_email)) where ctid = r.ctid;
      end if;
    exception when unique_violation then
      raise notice 'kept mixed-case email (a lowercase twin exists); fix by hand';
    end;
  end loop;
end $$;
-- Rows left with upper-case letters are duplicates of another row: review them by hand:
--   select user_email from public.users where user_email <> lower(user_email);

-- ---------------------------------------------------------------------------
-- 2. subscriptions: one row per Lemon Squeezy subscription (audit C2 / M4)
-- ---------------------------------------------------------------------------
create table if not exists public.subscriptions (
  ls_subscription_id text primary key,
  user_email         text,
  user_id            uuid null,
  variant_id         text,
  status             text,
  updated_at         timestamptz
);
create index if not exists subscriptions_email_idx on public.subscriptions (user_email);
create index if not exists subscriptions_user_idx  on public.subscriptions (user_id);

alter table public.subscriptions enable row level security;
revoke all on public.subscriptions from anon, authenticated;
-- No policies on purpose: only the backend (service_role) reads or writes it.

-- Upsert unless we already hold a NEWER state for this subscription (LS can deliver
-- webhooks out of order). Returns true when applied, false when the event was stale.
create or replace function public.upsert_subscription(
  p_id text, p_user_email text, p_user_id uuid, p_variant_id text, p_status text, p_updated_at timestamptz)
returns boolean
language plpgsql security definer set search_path = public as $$
declare v_id text;
begin
  insert into subscriptions as s (ls_subscription_id, user_email, user_id, variant_id, status, updated_at)
  values (p_id, lower(p_user_email), p_user_id, p_variant_id, lower(p_status), coalesce(p_updated_at, now()))
  on conflict (ls_subscription_id) do update
     set user_email = excluded.user_email,
         user_id    = coalesce(excluded.user_id, s.user_id),
         variant_id = excluded.variant_id,
         status     = excluded.status,
         updated_at = excluded.updated_at
   where s.updated_at is null or excluded.updated_at >= s.updated_at
  returning ls_subscription_id into v_id;
  return v_id is not null;
end;
$$;
revoke all on function public.upsert_subscription(text, text, uuid, text, text, timestamptz)
  from public, anon, authenticated;

-- claim_sourcing_job is SECURITY DEFINER without a search_path (audit L10).
do $$
begin
  if to_regprocedure('public.claim_sourcing_job()') is not null then
    execute 'alter function public.claim_sourcing_job() set search_path = public';
  end if;
exception when others then
  raise notice 'claim_sourcing_job search_path not changed: %', sqlerrm;
end $$;

-- ---------------------------------------------------------------------------
-- 3. saved_products: RLS, own rows only, nothing for anon (frontend audit #7)
--    The app does not use this table today. Policies match on user_id when that
--    column exists, else on user_email.
-- ---------------------------------------------------------------------------
do $$
declare
  has_uid   boolean;
  has_email boolean;
  cond      text;
begin
  if to_regclass('public.saved_products') is null then
    raise notice 'saved_products does not exist; skipped';
    return;
  end if;

  execute 'alter table public.saved_products enable row level security';
  execute 'revoke all on public.saved_products from anon';

  select exists (select 1 from information_schema.columns
                  where table_schema = 'public' and table_name = 'saved_products' and column_name = 'user_id'),
         exists (select 1 from information_schema.columns
                  where table_schema = 'public' and table_name = 'saved_products' and column_name = 'user_email')
    into has_uid, has_email;

  if has_uid then
    cond := 'user_id::text = auth.uid()::text';
  elsif has_email then
    cond := 'lower(user_email) = lower(auth.jwt() ->> ''email'')';
  else
    raise notice 'saved_products has no user_id / user_email column: RLS on, no policies (authenticated sees nothing)';
    return;
  end if;

  execute 'drop policy if exists "saved_products select own" on public.saved_products';
  execute 'drop policy if exists "saved_products insert own" on public.saved_products';
  execute 'drop policy if exists "saved_products delete own" on public.saved_products';
  execute format('create policy "saved_products select own" on public.saved_products for select to authenticated using (%s)', cond);
  execute format('create policy "saved_products insert own" on public.saved_products for insert to authenticated with check (%s)', cond);
  execute format('create policy "saved_products delete own" on public.saved_products for delete to authenticated using (%s)', cond);
end $$;

-- ---------------------------------------------------------------------------
-- 4. Storage: bucket product-images must not be LISTABLE with the anon key (audit L8)
--    The bucket stays public, so its public URLs (/storage/v1/object/public/...) keep
--    working: those don't go through RLS. Listing does, via SELECT policies on
--    storage.objects. None of our migrations created such a policy (it was made in the
--    dashboard), so this drops every SELECT policy on storage.objects that mentions
--    product-images and applies to anon / public. The backend uploads with service_role
--    (bypasses RLS), so imports are unaffected.
--    If this errors with "must be owner", do it in Dashboard -> Storage -> Policies.
-- ---------------------------------------------------------------------------
do $$
declare pol record;
begin
  for pol in
    select policyname
      from pg_policies
     where schemaname = 'storage' and tablename = 'objects'
       and cmd in ('SELECT', 'ALL')
       and (roles && array['anon', 'public']::name[])
       and coalesce(qual, '') like '%product-images%'
  loop
    execute format('drop policy if exists %I on storage.objects', pol.policyname);
    raise notice 'dropped storage policy %', pol.policyname;
  end loop;
exception when insufficient_privilege then
  raise notice 'Not allowed to change storage policies from SQL; remove the anon SELECT policy for product-images in Dashboard -> Storage -> Policies';
end $$;
