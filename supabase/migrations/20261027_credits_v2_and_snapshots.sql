-- ============================================================================
-- TikProfitPro: credits v2 + product snapshots
-- Run once in Supabase → SQL Editor. Safe to re-run (IF NOT EXISTS everywhere).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. CREDITS
-- ---------------------------------------------------------------------------
-- One row per user (already exists). Make sure the columns we rely on exist.
alter table if exists user_credits add column if not exists period_start timestamptz default now();
alter table if exists user_credits add column if not exists period_end   timestamptz default now() + interval '30 days';
create unique index if not exists user_credits_email_uniq on user_credits (user_email);

-- A user who already unlocked product X on platform Y can reopen it for free.
create table if not exists credit_unlocks (
  id          bigserial primary key,
  user_email  text        not null,
  product_id  text        not null,
  platform    text        not null,
  mode        text        not null default 'text',  -- text | image
  created_at  timestamptz not null default now(),
  unique (user_email, product_id, platform)
);

-- Monthly allowance by plan. ONE place to change it.
create or replace function plan_allowance(p_plan text) returns int
language sql immutable as $$
  select case lower(coalesce(p_plan, 'free'))
           when 'ultimate' then 120
           when 'pro'      then 50
           else 5
         end;
$$;

-- Returns the user's balance, creating the row or rolling the 30-day period
-- over when needed. Plan always comes from users.plan (single source of truth).
create or replace function get_credit_balance(p_email text) returns json
language plpgsql security definer as $$
declare
  v_plan  text;
  v_allow int;
  v_row   user_credits%rowtype;
begin
  select coalesce(lower(plan), 'free') into v_plan from users where user_email = p_email;
  v_plan  := coalesce(v_plan, 'free');
  v_allow := plan_allowance(v_plan);

  select * into v_row from user_credits where user_email = p_email for update;

  if not found then
    insert into user_credits (user_email, plan, credits_total, credits_used, period_start, period_end)
    values (p_email, v_plan, v_allow, 0, now(), now() + interval '30 days')
    returning * into v_row;
  elsif v_row.period_end is null or now() > v_row.period_end then
    -- New period: reset to the plan allowance (free users get 5 again, never 0).
    update user_credits
       set plan = v_plan, credits_total = v_allow, credits_used = 0,
           period_start = now(), period_end = now() + interval '30 days'
     where user_email = p_email
    returning * into v_row;
  elsif v_row.plan is distinct from v_plan or v_row.credits_total is distinct from v_allow then
    -- Plan changed (upgrade/downgrade) mid-period: new allowance, keep usage.
    update user_credits set plan = v_plan, credits_total = v_allow
     where user_email = p_email
    returning * into v_row;
  end if;

  return json_build_object(
    'plan',              v_row.plan,
    'credits_total',     v_row.credits_total,
    'credits_used',      v_row.credits_used,
    'credits_remaining', greatest(v_row.credits_total - v_row.credits_used, 0),
    'period_end',        v_row.period_end
  );
end;
$$;

-- Atomically spends 1 credit. Never goes below zero. Logs the spend.
create or replace function consume_credit(p_email text, p_product_id text, p_platform text, p_mode text default 'text')
returns json
language plpgsql security definer as $$
declare
  v_bal  json;
  v_rows int;
begin
  v_bal := get_credit_balance(p_email);           -- creates/rolls over the row

  -- Already unlocked → free.
  if exists (select 1 from credit_unlocks
              where user_email = p_email and product_id = p_product_id and platform = p_platform) then
    return (json_build_object('allowed', true, 'charged', false)::jsonb || v_bal::jsonb)::json;
  end if;

  update user_credits
     set credits_used = credits_used + 1
   where user_email = p_email and credits_used < credits_total;
  get diagnostics v_rows = row_count;

  if v_rows = 0 then
    return (json_build_object('allowed', false, 'charged', false)::jsonb || get_credit_balance(p_email)::jsonb)::json;
  end if;

  insert into credit_unlocks (user_email, product_id, platform, mode)
  values (p_email, p_product_id, p_platform, p_mode)
  on conflict do nothing;

  begin
    insert into credit_logs (user_email, product_id, platform, credits_deducted)
    values (p_email, p_product_id, p_platform, 1);
  exception when others then null;  -- logging is non-fatal
  end;

  return (json_build_object('allowed', true, 'charged', true)::jsonb || get_credit_balance(p_email)::jsonb)::json;
end;
$$;

-- Gives the credit back when a search found nothing usable.
create or replace function refund_credit(p_email text, p_product_id text, p_platform text)
returns void
language plpgsql security definer as $$
begin
  delete from credit_unlocks where user_email = p_email and product_id = p_product_id and platform = p_platform;
  update user_credits set credits_used = greatest(credits_used - 1, 0) where user_email = p_email;
  begin
    insert into credit_logs (user_email, product_id, platform, credits_deducted)
    values (p_email, p_product_id, p_platform, -1);
  exception when others then null;
  end;
end;
$$;

-- Only the backend (service role) may call these.
revoke all on function get_credit_balance(text)            from public, anon, authenticated;
revoke all on function consume_credit(text, text, text, text) from public, anon, authenticated;
revoke all on function refund_credit(text, text, text)     from public, anon, authenticated;

-- One-off repair: anyone stuck at 0/0 (free reset bug) gets their real allowance back.
update user_credits uc
   set credits_total = plan_allowance(coalesce(u.plan, 'free')),
       credits_used  = least(uc.credits_used, plan_allowance(coalesce(u.plan, 'free'))),
       plan          = coalesce(lower(u.plan), 'free')
  from users u
 where u.user_email = uc.user_email
   and uc.credits_total = 0;

-- ---------------------------------------------------------------------------
-- 2. PRODUCT DATA: stable ids, data dates, history
-- ---------------------------------------------------------------------------
alter table if exists tiktok_products add column if not exists tiktok_product_id text;
alter table if exists tiktok_products add column if not exists data_start_date   date;
alter table if exists tiktok_products add column if not exists data_end_date     date;
alter table if exists tiktok_products add column if not exists window_days       int;
alter table if exists tiktok_products add column if not exists revenue_window    numeric;  -- revenue for the export window (NOT 7 days)
alter table if exists tiktok_products add column if not exists units_window      int;
alter table if exists tiktok_products add column if not exists price_min         numeric;
alter table if exists tiktok_products add column if not exists price_max         numeric;
alter table if exists tiktok_products add column if not exists rating            numeric;
alter table if exists tiktok_products add column if not exists commission_rate   numeric;
alter table if exists tiktok_products add column if not exists launch_date       date;
alter table if exists tiktok_products add column if not exists creator_conversion_pct numeric;
alter table if exists tiktok_products add column if not exists growth_source     text;     -- 'snapshot' | 'unknown'
alter table if exists tiktok_products add column if not exists image_storage_url text;     -- re-hosted copy (TikTok CDN links expire)
create unique index if not exists tiktok_products_tt_id_uniq on tiktok_products (tiktok_product_id) where tiktok_product_id is not null;

-- One row per product per export window. Growth and stages are computed from this.
create table if not exists product_snapshots (
  id                bigserial primary key,
  tiktok_product_id text    not null,
  data_start_date   date    not null,
  data_end_date     date    not null,
  window_days       int     not null,
  revenue           numeric,
  units_sold        int,
  avg_unit_price    numeric,
  creator_count     int,
  video_revenue     numeric,
  live_revenue      numeric,
  mall_revenue      numeric,
  rating            numeric,
  imported_at       timestamptz not null default now(),
  unique (tiktok_product_id, data_end_date, window_days)
);
create index if not exists product_snapshots_pid_idx on product_snapshots (tiktok_product_id, data_end_date desc);

-- Same idea for creators.
alter table if exists creators add column if not exists tiktok_handle   text;
alter table if exists creators add column if not exists data_end_date   date;
create unique index if not exists creators_handle_uniq on creators (tiktok_handle) where tiktok_handle is not null;

create table if not exists product_creators (
  tiktok_product_id text not null,
  tiktok_handle     text not null,
  revenue           numeric,
  data_end_date     date,
  primary key (tiktok_product_id, tiktok_handle)
);
