# Photo sourcing pipeline

```
User clicks Alibaba/AliExpress ─▶ API charges 1 credit, inserts sourcing_jobs row ─▶ returns job_id
                                                                     │
Frontend polls /sourcing/jobs/{id} every 3s ◀── worker (Railway service) ──┘
                                                 1. download TikTok product photo
                                                 2. Oxylabs Agent Browser (US residential IP)
                                                 3. open alibaba.com / aliexpress.com, click camera, upload photo
                                                 4. read the site's own result JSON (XHR + page data)
                                                 5. download result photos, score with CLIP + pHash (no LLM)
                                                 6. keep matches ≥ 0.80 similarity with real margin, best 5
                                                 7. save to suppliers (shared cache) ─ or refund on fail
```

## Setup (one time)
1. Supabase SQL editor: run `20261027_credits_v2_and_snapshots.sql` then `20261027b_sourcing_jobs.sql`.
2. Oxylabs dashboard → **Agent Browser** → create user. (Alibaba/AliExpress may count as
   "restricted targets"; if sessions get refused, ask Oxylabs support to enable them.)
3. Railway → New service from this repo → Dockerfile path `Dockerfile.worker`. Variables:
   `SUPABASE_URL`, `SUPABASE_KEY`, `OXYLABS_AB_USER`, `OXYLABS_AB_PASS`
   (or `OXYLABS_RESI_USER` / `OXYLABS_RESI_PASS` for residential proxies + local Chromium).
4. First live test from the worker shell:
   `python -m sourcing.worker --test <product_uuid> --platform aliexpress`
   If it fails, send Hark the log + files in `/tmp/sourcing_debug` (screenshot + HTML).
5. Pre-source the catalogue so users see suppliers instantly:
   `python -m sourcing.worker --batch 100 --platform alibaba` (and `aliexpress`).

## Tuning
| Variable | Default | Meaning |
|---|---|---|
| SOURCING_MIN_SIMILARITY | 0.80 | raise to 0.85 if wrong products slip through |
| SOURCING_CONCURRENCY | 3 | parallel browser sessions |
| SOURCING_MAX_ATTEMPTS | 3 | fresh IP per attempt |
| PHOTO_WORKER_ENABLED (API) | false | set true on the API service once the worker service is running |
