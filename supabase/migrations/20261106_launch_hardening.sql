-- ============================================================================
-- TikProfitPro: launch hardening (credits fixes, webhook idempotency, RLS)
-- Run once in Supabase -> SQL Editor, AFTER 20261027_credits_v2_and_snapshots.sql
-- and 20261027b_sourcing_jobs.sql. Safe to re-run.
-- The backend uses the service_role key, which bypasses RLS, so nothing below
-- affects the API. It only closes the tables to the public anon key.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. CREDITS: race-free consume, honest refund, period on payment
-- ---------------------------------------------------------------------------

-- Spend 1 credit. The unlock row is claimed FIRST, so two clicks at the same
-- moment can never charge twice for the same product+platform.
create or replace function consume_credit(p_email text, p_product_id text, p_platform text, p_mode text default 'text')
returns json
language plpgsql security definer set search_path = public as $$
declare
  v_bal      json;
  v_unlock   bigint;
  v_rows     int;
begin
  v_bal := get_credit_balance(p_email);           -- creates / rolls over the row

  insert into credit_unlocks (user_email, product_id, platform, mode)
  values (p_email, p_product_id, p_platform, p_mode)
  on conflict (user_email, product_id, platform) do nothing
  returning id into v_unlock;

  if v_unlock is null then                        -- already unlocked: free
    return (json_build_object('allowed', true, 'charged', false)::jsonb || v_bal::jsonb)::json;
  end if;

  update user_credits
     set credits_used = credits_used + 1
   where user_email = p_email and credits_used < credits_total;
  get diagnostics v_rows = row_count;

  if v_rows = 0 then                              -- out of credits: give the unlock back
    delete from credit_unlocks where id = v_unlock;
    return (json_build_object('allowed', false, 'charged', false)::jsonb || get_credit_balance(p_email)::jsonb)::json;
  end if;

  begin
    insert into credit_logs (user_email, product_id, platform, credits_deducted)
    values (p_email, p_product_id, p_platform, 1);
  exception when others then null;
  end;

  return (json_build_object('allowed', true, 'charged', true)::jsonb || get_credit_balance(p_email)::jsonb)::json;
end;
$$;

-- Refund only if there really was an unlock to undo (no double refunds, no
-- refunding a credit that was never charged).
create or replace function refund_credit(p_email text, p_product_id text, p_platform text)
returns void
language plpgsql security definer set search_path = public as $$
declare v_rows int;
begin
  delete from credit_unlocks where user_email = p_email and product_id = p_product_id and platform = p_platform;
  get diagnostics v_rows = row_count;
  if v_rows = 0 then
    return;
  end if;
  update user_credits set credits_used = greatest(credits_used - 1, 0) where user_email = p_email;
  begin
    insert into credit_logs (user_email, product_id, platform, credits_deducted)
    values (p_email, p_product_id, p_platform, -1);
  exception when others then null;
  end;
end;
$$;

-- New 30-day period at the plan allowance. Called on subscription_created and
-- on every subscription_payment_success, so credits line up with billing.
create or replace function start_credit_period(p_email text)
returns json
language plpgsql security definer set search_path = public as $$
declare v_plan text; v_allow int;
begin
  perform get_credit_balance(p_email);            -- make sure the row exists
  select coalesce(lower(plan), 'free') into v_plan from users where lower(user_email) = lower(p_email);
  v_allow := plan_allowance(coalesce(v_plan, 'free'));
  update user_credits
     set plan = coalesce(v_plan, 'free'), credits_total = v_allow, credits_used = 0,
         period_start = now(), period_end = now() + interval '30 days'
   where user_email = p_email;
  return get_credit_balance(p_email);
end;
$$;

alter function get_credit_balance(text) set search_path = public;
alter function plan_allowance(text)     set search_path = public;

revoke all on function consume_credit(text, text, text, text) from public, anon, authenticated;
revoke all on function refund_credit(text, text, text)        from public, anon, authenticated;
revoke all on function start_credit_period(text)              from public, anon, authenticated;
revoke all on function get_credit_balance(text)               from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. WEBHOOK IDEMPOTENCY
-- ---------------------------------------------------------------------------
create table if not exists webhook_events (
  id          text primary key,
  event_name  text,
  received_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 3. ROW LEVEL SECURITY
-- The frontend reads only users.plan directly (usePlan hook), for the signed-in
-- user. Everything else goes through the API (service role).
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array[
    'users', 'user_credits', 'credit_unlocks', 'credit_logs', 'suppliers', 'sourcing_jobs',
    'tiktok_products', 'product_snapshots', 'product_creators', 'creators', 'shops', 'webhook_events'
  ] loop
    if to_regclass('public.' || t) is not null then
      execute format('alter table public.%I enable row level security', t);
      execute format('revoke all on public.%I from anon', t);
    end if;
  end loop;
end $$;

drop policy if exists "users read own row" on public.users;
create policy "users read own row" on public.users
  for select to authenticated
  using (lower(user_email) = lower(auth.jwt() ->> 'email'));
-- No insert/update/delete policy: plan changes only happen from the webhook (service role).

drop policy if exists "credits read own row" on public.user_credits;
create policy "credits read own row" on public.user_credits
  for select to authenticated
  using (lower(user_email) = lower(auth.jwt() ->> 'email'));

-- ---------------------------------------------------------------------------
-- 4. SUPPLIERS: fast lookup of photo-verified rows
-- ---------------------------------------------------------------------------
create index if not exists suppliers_lookup_idx on suppliers (tiktok_product_id, platform, similarity desc);

-- ---------------------------------------------------------------------------
-- 5. SHOPS + CREATORS: columns the importer writes (import_kalodata.py shops / creators)
-- ---------------------------------------------------------------------------
alter table if exists shops add column if not exists item_sold             int;
alter table if exists shops add column if not exists product_count         int;
alter table if exists shops add column if not exists avg_unit_price        numeric;
alter table if exists shops add column if not exists live_revenue          numeric;
alter table if exists shops add column if not exists video_revenue         numeric;
alter table if exists shops add column if not exists product_card_revenue  numeric;
alter table if exists shops add column if not exists affiliate_revenue     numeric;
alter table if exists shops add column if not exists self_operated_revenue numeric;
alter table if exists shops add column if not exists seller_type           text;
alter table if exists shops add column if not exists follower_count        int;
alter table if exists shops add column if not exists category              text;
alter table if exists shops add column if not exists shop_created_at       date;
alter table if exists shops add column if not exists shop_url              text;
alter table if exists shops add column if not exists image_url             text;
alter table if exists shops add column if not exists data_end_date         date;
create unique index if not exists shops_name_uniq on shops (shop_name);

alter table if exists creators add column if not exists creator_name      text;
alter table if exists creators add column if not exists profile_url       text;
alter table if exists creators add column if not exists category          text;
alter table if exists creators add column if not exists product_count     int;
alter table if exists creators add column if not exists products_promoted int;
alter table if exists creators add column if not exists video_gmv         numeric;
alter table if exists creators add column if not exists live_gmv          numeric;
alter table if exists creators add column if not exists conversion_rate   numeric;
alter table if exists creators add column if not exists item_sold         int;
alter table if exists creators add column if not exists avg_unit_price    numeric;
alter table if exists creators add column if not exists new_followers     int;
alter table if exists creators add column if not exists debut_date        date;
