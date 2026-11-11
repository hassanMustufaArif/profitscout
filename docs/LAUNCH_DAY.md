# Launch day (2026-11-11): what changed and what to do

## 1. Run the SQL first
Supabase -> SQL Editor -> paste `supabase/migrations/20261111_launch_day.sql` -> Run.
It is idempotent (safe to run twice). Then run the commented checks at the bottom of the file.

What it does:
- Full unique indexes `tiktok_products_tt_id_full` and `creators_handle_full`, so the importer's
  `upsert(on_conflict=...)` works (the old partial indexes can't be used by PostgREST: error 42P10).
  Duplicate keys are resolved first without deleting rows: the newest row keeps the key, older
  copies get NULL (and `is_active=false` for products). Logged in `launch_day_dedupe_log`.
- `tiktok_products.creator_count` made nullable (FastMoss has no creator count: stored as NULL).
- Re-asserts `suppliers.hidden / match_method / similarity (nullable)` and the other supplier
  columns, `webhook_events`, `supplier_candidates`, `subscriptions`, `upsert_subscription()`,
  and the `users.plan` default/check.
- Trigger `zz_tpp_create_public_user` on `auth.users`: every signup gets a `public.users` row
  (plan `free`). Never blocks a signup. Plus a one-time backfill of missing rows.

## 2. Render env
- `LEMON_PRO_VARIANT_IDS`, `LEMON_ULTIMATE_VARIANT_IDS` (new, optional): comma-separated Lemon
  Squeezy **variant** ids, e.g. `123456,123457`. Every variant listed grants that plan.
  `LEMONSQUEEZY_PRODUCT_ID` / `LEMONSQUEEZY_ULTIMATE_PRODUCT_ID` keep working, count as part
  of the list, and are the variant used for new checkouts (else the first id of the list).
- `FASTMOSS_WINDOW_DAYS` (unchanged): only used when the .xlsx has no date-range column
  (`Date Range`, `Time Range`, `Data Range`, `Period`, ...). If one exists it wins.

## 3. Behaviour changes
- Webhook: the event id is recorded only after successful processing. Any failure answers 500,
  so Lemon Squeezy retries. A missing `users` row is created. Unsigned checkouts use the order
  email (`attributes.user_email`) before `custom_data.email`.
- Free users get 403 `{"code": "upgrade_required"}` (before any charge) for suppliers of a
  product outside their fixed top 10, unless they unlocked it before.
- Paid lists (`/products`, `/products/tier/*`, `/search`) order by score, then `id` (no
  duplicates / gaps between pages).
- `/stats` uses exact counts; presource and the admin review queue page through all rows
  (PostgREST max-rows no longer causes re-sourcing).
- Admin review: `auto` rows (matching off) are `visible: true` (live), not "below threshold".
