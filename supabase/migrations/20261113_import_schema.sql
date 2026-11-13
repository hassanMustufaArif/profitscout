-- ============================================================================
-- TikProfitPro: upload / import schema drift (2026-11-13 audit)
-- Run in Supabase -> SQL Editor. Idempotent. NOT applied by the audit.
--
-- Prod differs from what import_kalodata.py writes:
--   * tiktok_products has no live_revenue / video_revenue (Kalodata columns; the importer now
--     skips them, so Kalodata live/video revenue is silently dropped until these exist)
--   * tiktok_products still has the v1 check tiktok_products_trend_phase_check
--     (hot / rising / watch / pass), so v2 stages are written as v1 names
--   * shops has no gmv_growth_pct (SHOP_MAP writes it from "Revenue Growth Rate")
-- ============================================================================

alter table if exists tiktok_products add column if not exists live_revenue  numeric;
alter table if exists tiktok_products add column if not exists video_revenue numeric;
alter table if exists shops           add column if not exists gmv_growth_pct numeric;

-- Allow both stage vocabularies: the importer writes v2 names once this runs, rows already
-- written with v1 names stay valid, and main.product_stage() maps either to v2 for the API.
alter table if exists tiktok_products drop constraint if exists tiktok_products_trend_phase_check;
alter table if exists tiktok_products add constraint tiktok_products_trend_phase_check
  check (trend_phase is null or trend_phase in
         ('emerging', 'rising', 'peaking', 'saturated', 'unknown',     -- v2 (import_kalodata.stage)
          'hot', 'watch', 'pass'));                                   -- v1 (what prod allows today)

-- creator_count must accept NULL (FastMoss has no creator count). Re-asserted from 20261111.
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'tiktok_products'
                and column_name = 'creator_count' and is_nullable = 'NO') then
    execute 'alter table public.tiktok_products alter column creator_count drop not null';
  end if;
end $$;

notify pgrst, 'reload schema';   -- PostgREST sees the new columns without waiting

-- OPTIONAL one-off data repair (review first, run by hand):
-- 1) The 2026-11-12 run copied 198 images to Storage, but that run's product upsert failed and
--    today's run had rehost off, so no row points at them. Re-link the copies that exist:
-- update tiktok_products p
--    set image_storage_url = '<SUPABASE_URL>/storage/v1/object/public/product-images/products/' || p.tiktok_product_id || '.jpg'
--  where p.image_storage_url is null and p.data_end_date is not null
--    and exists (select 1 from storage.objects o
--                 where o.bucket_id = 'product-images' and o.name = 'products/' || p.tiktok_product_id || '.jpg');
-- 2) The FastMoss rows got creator_count = 0 from the v1 fallback; 0 means "no creators" in the UI.
-- update tiktok_products set creator_count = null
--  where growth_source in ('fastmoss', 'unknown') and window_days = 1 and creator_count = 0;
