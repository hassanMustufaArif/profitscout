-- ============================================================================
-- TikProfitPro: launch day (2026-11-11)
-- Run in Supabase -> SQL Editor. Idempotent: safe to run twice, safe to run even
-- if 20261106b / 20261107 were already applied (it re-asserts what they add).
-- Run it BEFORE the first FastMoss import and before deploying hark/launch-day.
--
--   1. Full (non-partial) unique indexes so PostgREST upsert on_conflict works
--      for tiktok_products(tiktok_product_id) and creators(tiktok_handle).
--      Duplicates are resolved first WITHOUT deleting rows: the newest row keeps
--      the key, older copies get the key set to NULL (products are also made
--      inactive). Every change is logged in public.launch_day_dedupe_log.
--   2. Columns / tables the backend needs (suppliers.hidden, match_method,
--      similarity nullable, webhook_events, subscriptions, ...).
--   3. auth.users -> public.users trigger + one-time backfill.
--   4. Commented check queries at the end.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. UNIQUE KEYS FOR UPSERTS
-- ---------------------------------------------------------------------------
create table if not exists public.launch_day_dedupe_log (
  id         bigserial primary key,
  table_name text        not null,
  row_ref    text        not null,      -- the row's id (or ctid when there is no id column)
  old_key    text,
  changed_at timestamptz not null default now()
);
alter table public.launch_day_dedupe_log enable row level security;
revoke all on public.launch_day_dedupe_log from anon, authenticated;

-- Dedupe helper: blank keys -> NULL; for each duplicated non-null key keep the newest
-- row (by whichever of data_end_date / scraped_at / updated_at / created_at exist),
-- set the key to NULL on the older rows and log them.
create or replace function public._launch_day_dedupe(p_table text, p_key text)
returns int
language plpgsql set search_path = public as $$
declare
  v_order  text := '';
  v_ref    text;
  v_extra  text := '';
  v_n      int  := 0;
  c        text;
begin
  if to_regclass('public.' || p_table) is null then
    raise notice '%: table missing, skipped', p_table;
    return 0;
  end if;
  if not exists (select 1 from information_schema.columns
                  where table_schema = 'public' and table_name = p_table and column_name = p_key) then
    raise notice '%.%: column missing, skipped', p_table, p_key;
    return 0;
  end if;

  foreach c in array array['data_end_date', 'scraped_at', 'updated_at', 'created_at'] loop
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = p_table and column_name = c) then
      v_order := v_order || format('%I desc nulls last, ', c);
    end if;
  end loop;
  v_order := v_order || 'ctid desc';

  v_ref := case when exists (select 1 from information_schema.columns
                              where table_schema = 'public' and table_name = p_table and column_name = 'id')
                then 'id::text' else 'ctid::text' end;
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = p_table and column_name = 'is_active') then
    v_extra := ', is_active = false';
  end if;

  -- blank keys are not keys
  execute format('update public.%I set %I = null where %I is not null and btrim(%I) = %L',
                 p_table, p_key, p_key, p_key, '');

  execute format($f$
    with ranked as (
      select ctid as rid, %s as ref, %I as k,
             row_number() over (partition by %I order by %s) as rn
        from public.%I
       where %I is not null
    ), logged as (
      insert into public.launch_day_dedupe_log (table_name, row_ref, old_key)
      select %L, ref, k from ranked where rn > 1
      returning 1
    )
    update public.%I t set %I = null %s
      from ranked r
     where t.ctid = r.rid and r.rn > 1
  $f$, v_ref, p_key, p_key, v_order, p_table, p_key, p_table, p_table, p_key, v_extra);
  get diagnostics v_n = row_count;
  raise notice '%.%: % duplicate row(s) un-keyed (see launch_day_dedupe_log)', p_table, p_key, v_n;
  return v_n;
end;
$$;
revoke all on function public._launch_day_dedupe(text, text) from public, anon, authenticated;

do $$
begin
  perform public._launch_day_dedupe('tiktok_products', 'tiktok_product_id');
  perform public._launch_day_dedupe('creators', 'tiktok_handle');
end $$;

-- Full unique indexes (NULLs stay distinct, so rows without a key are allowed).
-- The older partial indexes (tiktok_products_tt_id_uniq, creators_handle_uniq) are
-- left in place: harmless, and PostgREST now infers these full ones.
do $$
begin
  if to_regclass('public.tiktok_products') is not null then
    execute 'create unique index if not exists tiktok_products_tt_id_full on public.tiktok_products (tiktok_product_id)';
  end if;
  if to_regclass('public.creators') is not null then
    execute 'create unique index if not exists creators_handle_full on public.creators (tiktok_handle)';
  end if;
end $$;

-- FastMoss rows have no creator count: store NULL ("unknown"), not 0.
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'tiktok_products'
                and column_name = 'creator_count' and is_nullable = 'NO') then
    execute 'alter table public.tiktok_products alter column creator_count drop not null';
    raise notice 'tiktok_products.creator_count is now nullable';
  end if;
end $$;

-- Columns the importer writes (re-asserted from 20261027; no-ops when present).
alter table if exists tiktok_products add column if not exists tiktok_product_id      text;
alter table if exists tiktok_products add column if not exists data_start_date        date;
alter table if exists tiktok_products add column if not exists data_end_date          date;
alter table if exists tiktok_products add column if not exists window_days            int;
alter table if exists tiktok_products add column if not exists revenue_window         numeric;
alter table if exists tiktok_products add column if not exists units_window           int;
alter table if exists tiktok_products add column if not exists price_min              numeric;
alter table if exists tiktok_products add column if not exists price_max              numeric;
alter table if exists tiktok_products add column if not exists rating                 numeric;
alter table if exists tiktok_products add column if not exists commission_rate        numeric;
alter table if exists tiktok_products add column if not exists launch_date            date;
alter table if exists tiktok_products add column if not exists creator_conversion_pct numeric;
alter table if exists tiktok_products add column if not exists growth_source          text;
alter table if exists tiktok_products add column if not exists image_storage_url      text;


-- ---------------------------------------------------------------------------
-- 2. FROM 20261106 / 20261106b / 20261107 (all idempotent, re-asserted here)
-- ---------------------------------------------------------------------------

-- 2a. Webhook idempotency table (20261106)
create table if not exists public.webhook_events (
  id          text primary key,
  event_name  text,
  received_at timestamptz not null default now()
);
alter table public.webhook_events enable row level security;
revoke all on public.webhook_events from anon;

-- 2b. Supplier columns. Without suppliers.hidden every supplier lookup 500s.
alter table if exists suppliers add column if not exists similarity             numeric;
alter table if exists suppliers add column if not exists match_method           text;     -- auto | photo | text | manual
alter table if exists suppliers add column if not exists hidden                 boolean not null default false;
alter table if exists suppliers add column if not exists reviewed_by            text;
alter table if exists suppliers add column if not exists reviewed_at            timestamptz;
alter table if exists suppliers add column if not exists product_title          text;
alter table if exists suppliers add column if not exists image_url              text;
alter table if exists suppliers add column if not exists external_product_id    text;
alter table if exists suppliers add column if not exists orders                 int;
alter table if exists suppliers add column if not exists rating                 numeric;
alter table if exists suppliers add column if not exists years_on_platform      int;
alter table if exists suppliers add column if not exists supplier_trust_score   numeric;
alter table if exists suppliers add column if not exists verified_supplier      boolean;
alter table if exists suppliers add column if not exists shipping_estimate_days int;
alter table if exists suppliers add column if not exists shipping_cost_estimate numeric;
alter table if exists suppliers add column if not exists whitelabel_available   boolean;
alter table if exists suppliers add column if not exists sourced_at             timestamptz;

-- Matching off saves 'auto' rows with similarity NULL: the column must allow NULL.
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'suppliers'
                and column_name = 'similarity' and is_nullable = 'NO') then
    execute 'alter table public.suppliers alter column similarity drop not null';
  end if;
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'suppliers'
                and column_name = 'hidden' and column_default is null) then
    execute 'alter table public.suppliers alter column hidden set default false';
  end if;
end $$;
update public.suppliers set hidden = false where hidden is null;

-- 2c. Review queue (20261106b)
create table if not exists supplier_candidates (
  id                  bigserial primary key,
  tiktok_product_id   text        not null,
  platform            text        not null,
  supplier_name       text,
  product_title       text,
  unit_cost_min       numeric,
  unit_cost_max       numeric,
  moq                 int,
  supplier_url        text,
  image_url           text,
  similarity          numeric,
  match_method        text,
  external_product_id text,
  orders              int,
  rating              numeric,
  years_on_platform   int,
  verified_supplier   boolean,
  status              text        not null default 'pending',
  reviewed_by         text,
  reviewed_at         timestamptz,
  supplier_id         bigint,
  created_at          timestamptz not null default now()
);
create index if not exists supplier_candidates_queue_idx on supplier_candidates (status, tiktok_product_id, platform);
alter table supplier_candidates enable row level security;
revoke all on supplier_candidates from anon;

-- 2d. users.plan: only free / pro / ultimate, default free (20261107)
update public.users set plan = lower(btrim(plan))
 where plan is not null and plan <> lower(btrim(plan));
update public.users set plan = 'free'
 where plan is null or plan not in ('free', 'pro', 'ultimate');
alter table public.users alter column plan set default 'free';
alter table public.users drop constraint if exists users_plan_check;
alter table public.users add constraint users_plan_check
  check (plan in ('free', 'pro', 'ultimate'));

-- 2e. subscriptions + upsert_subscription (20261107)
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
-- (20261107's saved_products RLS and storage-policy sections are one-off and were not
--  needed by the backend; re-run 20261107 itself if check 6 below shows they're missing.)


-- ---------------------------------------------------------------------------
-- 3. A public.users ROW FOR EVERY SIGNUP
-- The backend only UPDATEs users.plan, so a missing row = a paying customer stays Free.
-- The insert adapts to the columns public.users actually has:
--   user_email (required), plan = 'free', id / user_id = auth uid when such a uuid
--   column exists, credits = 5 (the Free allowance) when that column exists and has
--   no default. It never blocks a signup: any error is turned into a warning.
-- The trigger is named zz_* so it fires after any existing auth.users trigger
-- (Postgres fires same-event triggers alphabetically) and skips rows that exist.
-- ---------------------------------------------------------------------------
do $$
declare
  v_cols  text := 'user_email, plan';
  v_vals  text := 'lower(btrim(p_email)), ''free''';
  v_body  text;
begin
  if exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'users'
              and column_name = 'id' and data_type = 'uuid') then
    v_cols := v_cols || ', id';       v_vals := v_vals || ', p_id';
  end if;
  if exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'users'
              and column_name = 'user_id' and data_type = 'uuid') then
    v_cols := v_cols || ', user_id';  v_vals := v_vals || ', p_id';
  end if;
  if exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'users'
              and column_name = 'credits' and column_default is null
              and data_type in ('integer', 'bigint', 'smallint', 'numeric')) then
    v_cols := v_cols || ', credits';  v_vals := v_vals || ', 5';
  end if;

  v_body := format($f$
    create or replace function public.tpp_ensure_user_row(p_id uuid, p_email text)
    returns void
    language plpgsql security definer set search_path = public as $b$
    begin
      if p_email is null or btrim(p_email) = '' then
        return;
      end if;
      if exists (select 1 from public.users u where lower(u.user_email) = lower(btrim(p_email))) then
        return;
      end if;
      insert into public.users (%s) values (%s);
    exception when unique_violation then
      null;                              -- created concurrently: fine
    end;
    $b$;
  $f$, v_cols, v_vals);
  execute v_body;
end $$;
revoke all on function public.tpp_ensure_user_row(uuid, text) from public, anon, authenticated;

create or replace function public.tpp_handle_new_auth_user()
returns trigger
language plpgsql security definer set search_path = public as $$
begin
  begin
    perform public.tpp_ensure_user_row(new.id, new.email);
  exception when others then
    raise warning 'tpp_handle_new_auth_user: users row not created: %', sqlerrm;   -- never block a signup
  end;
  return new;
end;
$$;
revoke all on function public.tpp_handle_new_auth_user() from public, anon, authenticated;

drop trigger if exists zz_tpp_create_public_user on auth.users;
create trigger zz_tpp_create_public_user
  after insert on auth.users
  for each row execute function public.tpp_handle_new_auth_user();

-- Email added/changed later (e.g. phone signup, then email): create the row then too.
drop trigger if exists zz_tpp_create_public_user_on_email on auth.users;
create trigger zz_tpp_create_public_user_on_email
  after update of email on auth.users
  for each row when (new.email is distinct from old.email)
  execute function public.tpp_handle_new_auth_user();

-- One-time backfill (re-running only fills rows that are still missing).
do $$
declare r record; n int := 0;
begin
  for r in select a.id, a.email
             from auth.users a
            where a.email is not null and btrim(a.email) <> ''
              and not exists (select 1 from public.users u where lower(u.user_email) = lower(btrim(a.email)))
  loop
    perform public.tpp_ensure_user_row(r.id, r.email);
    n := n + 1;
  end loop;
  raise notice 'backfilled % public.users row(s)', n;
end $$;


-- ---------------------------------------------------------------------------
-- 4. CHECKS (run by hand after the script; copy one block at a time)
-- ---------------------------------------------------------------------------
-- 1) Full unique indexes exist (expect tiktok_products_tt_id_full and creators_handle_full):
-- select tablename, indexname, indexdef from pg_indexes
--  where schemaname = 'public' and tablename in ('tiktok_products', 'creators') order by 1, 2;
--
-- 2) No duplicate keys left (expect 0 rows each):
-- select tiktok_product_id, count(*) from public.tiktok_products where tiktok_product_id is not null group by 1 having count(*) > 1;
-- select tiktok_handle, count(*) from public.creators where tiktok_handle is not null group by 1 having count(*) > 1;
-- select table_name, count(*) from public.launch_day_dedupe_log group by 1;   -- what was un-keyed
--
-- 3) Every auth user has a users row (expect 0):
-- select count(*) from auth.users a
--   left join public.users u on lower(u.user_email) = lower(a.email)
--  where a.email is not null and u.user_email is null;
--
-- 4) Trigger present (expect zz_tpp_create_public_user, zz_tpp_create_public_user_on_email):
-- select tgname from pg_trigger where tgrelid = 'auth.users'::regclass and not tgisinternal;
--
-- 5) Supplier columns (expect 8 rows; similarity is_nullable = YES):
-- select column_name, is_nullable, column_default from information_schema.columns
--  where table_schema = 'public' and table_name = 'suppliers'
--    and column_name in ('hidden','similarity','match_method','years_on_platform','supplier_trust_score',
--                        'shipping_estimate_days','shipping_cost_estimate','whitelabel_available');
--
-- 6) Tables (expect no NULLs):
-- select to_regclass('public.subscriptions'), to_regclass('public.webhook_events'),
--        to_regclass('public.supplier_candidates'), to_regprocedure('public.upsert_subscription(text,text,uuid,text,text,timestamptz)');
--
-- 7) creator_count nullable (expect YES):
-- select is_nullable from information_schema.columns
--  where table_schema = 'public' and table_name = 'tiktok_products' and column_name = 'creator_count';
--
-- 8) Plans (expect only free / pro / ultimate):
-- select plan, count(*) from public.users group by plan order by 2 desc;
