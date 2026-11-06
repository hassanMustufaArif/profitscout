# Security fixes (branch `security-fixes`, audit of 2026-11-07)

Finding ids (C1, H2, ...) refer to the backend audit. Nothing in this branch contains a secret value.

## What changed

| Id | Change |
|---|---|
| C1 | Deleted `alibaba_pipeline.py`, `aliexpress_pipeline.py`, `aliexpress_Agent.py`, `test_apify.py` (hard-coded Oxylabs / Crawlbase / Anthropic values; nothing imported them). `autopilot.py` `INDEXNOW_KEY` has no default. `.gitignore` also covers `secrets*.txt`, `*.pem`, service-account JSON. New CI workflow `.github/workflows/secret-scan.yml` runs gitleaks on every push / PR (current tree only, image pinned by digest). |
| C2 | Webhook never uses `ilike`. Users are matched by exact lowercased email (`.eq`). `/create-checkout` puts the signed-in user's **id**, email and an HMAC signature of the id in Lemon Squeezy custom data. The webhook trusts `custom_data.user_id` only when the signature is valid and that user's email matches the order email (`attrs.user_email` or `custom_data.email`); otherwise it falls back to the exact email. Hosted buy links with hand-made custom data can't use a `%` / `_` pattern any more. |
| M4 | New `subscriptions` table (one row per LS subscription). `upsert_subscription()` ignores an event whose `updated_at` is older than the stored one (out-of-order deliveries). The user's plan is the best plan over their subscriptions that still pay, so one lapsed subscription no longer downgrades someone holding another active one. A paid event for an email with no `users` row is logged as an ERROR (hashed id) and still not applied (same behaviour as before, now visible). |
| H1 | Supplier search (Amazon/Apify/GPT), list endpoints, `/credits`, `/create-checkout`, `/generate-listing`, all `/admin/*` routes are plain `def` (FastAPI threadpool). Webhook DB work and the CSV dry run run in `run_in_threadpool`. One in-flight Amazon search per user. See `docs/DEPLOY_NOTES.md` for workers. |
| H2 / L3 | Unpaid callers (signed out, `free`, NULL or any unknown plan) get ONE fixed top 10 on `/products`, `/search`, `/products/tier/*` (ordered by score desc, then id) and on `/creators`, `/shops` (by GMV desc). Filters, sort, offset/page and `q` are ignored for them. `/products/{id}` answers 403 "Upgrade to Pro" for ids outside that top 10. One helper (`security.normalize_plan`) decides the plan everywhere. |
| H3 | Admin = user id in `ADMIN_USER_IDS`, or email in `ADMIN_EMAILS` **and** the email is confirmed (`email_confirmed_at`). Credits: a user with an unconfirmed email can't unlock suppliers ("Confirm your email to use credits"); already-unlocked products still open. Google sign-ins are confirmed automatically. |
| M1 | `security.safe_fetch()`: http(s) only, DNS resolved and private / loopback / link-local / reserved / multicast addresses refused, redirects followed by hand (max 3) and re-checked each hop, streamed with a byte cap and timeout. Used by `fetch_image_bytes` (listing generator, photo checks, CSV image re-hosting) and `/api/extract-email`. `/generate-listing` accepts `product_id` and looks up the product's own image; a client `image_url` is ignored when `product_id` is sent. |
| M2 | `presource.yml`: inputs reach the script through `env:`, are validated (numbers by regex, mode by `case`), top-level `permissions: contents: read`, actions pinned to commit SHAs (checkout v4.3.1, setup-python v5.6.0, cache v4.3.0). |
| M3 | Rate limits (slowapi, in-memory, keyed by signed-in user id, else the client IP from `CF-Connecting-IP`): list endpoints 60/min per IP, supplier lookups 10/min per user, Amazon search 3/min per user, `/generate-listing` 20/day per user, `/create-checkout` 5/min, `/admin/*` 30/min, `/credits` and `/sourcing/jobs/*` 60/min. 429 answers `{"detail": "Too many requests. Please wait a minute and try again."}`. `/api/ai-proxy`: model allowlist (`AI_PROXY_MODELS`, default `gpt-4o-mini`), `max_tokens` <= 2000. |
| M5 | No `detail=str(e)` left. Errors are logged with `logger.exception` and the client gets a generic message. Unknown / malformed product ids give 404. |
| M6 | `/admin/import` and `/api/parse-cv` no longer declare `File()` params, so FastAPI doesn't parse the body first: admin/auth and the `Content-Length` cap (30 MB) are checked before a byte is read, files are copied in 1 MB chunks with a running total, the dry run runs in the threadpool, parse-cv's temp file is always deleted. |
| L1 | `/docs`, `/redoc`, `/openapi.json` are off unless `ENABLE_DOCS=true`. |
| L2 | No more per-request email prints; webhook / checkout logs use the user id or a short hash. LS error bodies are no longer logged. |
| L4 | Customer-facing supplier rows use an explicit column list (no `reviewed_by`, `reviewed_at`, `hidden`). The Amazon cache path skips hidden suppliers. |
| L5 | Blog / job / CV / extract-email / ai-proxy / autopilot / get-image routes answer 404 unless `ENABLE_LEGACY_ENDPOINTS=true` (their secret headers still apply on top). |
| L6 | CSV import keeps only `http(s)` links for images / shop / profile URLs (others dropped), skips rows whose date range ends after tomorrow, never lets `--deactivate` use a future cutoff, and opens uploaded files by exact path (no glob). |
| L7 | Pinned `python-multipart`, `pdfplumber`, `pdfminer.six>=20251230`, `APScheduler`, `anthropic`, `crawlbase`, `slowapi`, `limits`; duplicate `pyjwt` removed. Sourcing / worker: `torch==2.8.0` (>= 2.6 for CVE-2025-32434; resolves with `open_clip_torch==2.26.1`). |
| L8 | Migration drops the anon SELECT policy that lets anyone list the `product-images` bucket. Public image URLs keep working. |
| L9 | `X-API-Key` compared with `hmac.compare_digest`. It is still public (it's in the JS bundle): it is not auth. |
| saved_products | Migration enables RLS, revokes anon, and adds own-rows select/insert/delete policies (on `user_id` if that column exists, else `user_email`). The app doesn't use the table today. |
| RLS check | `docs/check_rls.sql`: read-only queries listing public tables without RLS, anon grants, storage policies, plan values. |

## Render environment variables

New (all optional):

| Variable | Default | Set it to |
|---|---|---|
| `ADMIN_USER_IDS` | empty | Comma-separated Supabase user ids (UUIDs) of the admins. Recommended: find them in Supabase -> Authentication -> Users. |
| `ENABLE_DOCS` | `false` | `true` only on a staging service if you want `/docs`. |
| `ENABLE_LEGACY_ENDPOINTS` | `false` | `true` only if something still calls the blog / autopilot / job / CV / ai-proxy routes (e.g. a Render cron calling `/run-autopilot`). |
| `CHECKOUT_SIGNING_SECRET` | falls back to `LEMONSQUEEZY_WEBHOOK_SECRET` | Optional separate random value for signing the checkout user id. If you set or change it, checkouts opened before the change fall back to exact-email matching. |
| `AI_PROXY_MODELS` | `gpt-4o-mini` | Comma-separated allowlist for `/api/ai-proxy`. |
| `RATE_LIMIT_LIST`, `RATE_LIMIT_SUPPLIER`, `RATE_LIMIT_AMAZON`, `RATE_LIMIT_LISTING`, `RATE_LIMIT_CHECKOUT`, `RATE_LIMIT_ADMIN`, `RATE_LIMIT_POLL` | see M3 | Override a limit, e.g. `120/minute`. |
| `LOG_LEVEL` | `INFO` | |

Unchanged but now required for what they do: `ADMIN_EMAILS` (admins by email need a confirmed email), `INDEXNOW_KEY` (no built-in default any more).

## Migrations to run (Supabase -> SQL Editor, in order)

Earlier ones should already be applied: `20261027_credits_v2_and_snapshots.sql`, `20261027b_sourcing_jobs.sql`,
`20261106_launch_hardening.sql`, `20261106b_supplier_review.sql`. Then:

1. `supabase/migrations/20261107_security_fixes.sql` (run **before** deploying this backend).
2. Optional: `docs/check_rls.sql` (read-only) to confirm no public table is left without RLS.

## Owner actions (only you can do these)

1. **Supabase Auth** (Dashboard -> Authentication -> Providers / Settings):
   - turn on **Confirm email** (turn off auto-confirm);
   - turn on **Secure email change**;
   - enable **CAPTCHA** (Turnstile or hCaptcha) for sign-up / sign-in, and add the widget to the sign-up form;
   - set **minimum password length 8** (or more).
2. **Admins:** put your Supabase user id in `ADMIN_USER_IDS`; remove addresses from `ADMIN_EMAILS` that aren't registered and confirmed.
3. **Delete the branches that still hold `.env`** on `sodewala45/profitscout` (Hark can't reach that repo):
   `v2-features`, `hark/credit-fix`, `hark/credits-photo-search`, `hark/photo-sourcing-worker`, `hark/presourcing`.
   Then purge `.env`, the `*.txt` key files and the old inline keys from history (`git filter-repo --path .env --path-glob '*.txt' --invert-paths` plus `--replace-text`), force-push, and ask collaborators to re-clone. Turn on GitHub secret scanning + push protection.
4. **Rotate keys** (assume all old values are known): Supabase JWT secret (reissues anon + service_role; update Render and GitHub Actions secrets), Lemon Squeezy webhook secret + API key, OpenAI, Anthropic, Groq, Apify, Oxylabs, Crawlbase, ScraperAPI, SendGrid, Ghost admin/content, Unsplash, and the X-API-Key (update the frontend too).
5. **Merge to `main` on the origin repo** so the hardened `presource.yml` is the one that runs (the backend dispatches the workflow on `main`).
6. **Optional: Cloudflare rate-limiting rule** for `api.tikprofitpro.shop` on `/products*`, `/search`, `/creators`, `/shops` (e.g. 100 requests / minute per IP). It complements the in-app limits, which are per Render instance and reset on deploy.
7. **Optional hardening:** make Render only accept traffic from Cloudflare (or keep the `*.onrender.com` URL private): the rate limiter trusts the `CF-Connecting-IP` header.
8. **Frontend:** send `product_id` to `/generate-listing` (the server then ignores `image_url`).
