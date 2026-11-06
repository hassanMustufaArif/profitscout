-- ============================================================================
-- TikProfitPro: supplier review queue (approve near-miss matches by hand)
-- Run once in Supabase -> SQL Editor, after 20261106_launch_hardening.sql. Safe to re-run.
-- ============================================================================

-- Near-misses from sourcing (photo similarity 0.60-0.79 by default): same product,
-- different bottle / label / angle. An admin approves or rejects them.
create table if not exists supplier_candidates (
  id                  bigserial primary key,
  tiktok_product_id   text        not null,
  platform            text        not null,          -- alibaba | aliexpress
  supplier_name       text,
  product_title       text,
  unit_cost_min       numeric,
  unit_cost_max       numeric,
  moq                 int,
  supplier_url        text,
  image_url           text,
  similarity          numeric,
  match_method        text,                          -- photo | text (how the candidate was found)
  external_product_id text,
  orders              int,
  rating              numeric,
  years_on_platform   int,
  verified_supplier   boolean,
  status              text        not null default 'pending',   -- pending | approved | rejected
  reviewed_by         text,
  reviewed_at         timestamptz,
  supplier_id         bigint,                        -- the suppliers row created on approve
  created_at          timestamptz not null default now()
);
create index if not exists supplier_candidates_queue_idx on supplier_candidates (status, tiktok_product_id, platform);
alter table supplier_candidates enable row level security;
revoke all on supplier_candidates from anon;

-- Live suppliers: admins can hide a wrong auto-match, and hand-approved rows are marked.
alter table if exists suppliers add column if not exists hidden      boolean not null default false;
alter table if exists suppliers add column if not exists reviewed_by text;
alter table if exists suppliers add column if not exists reviewed_at timestamptz;
