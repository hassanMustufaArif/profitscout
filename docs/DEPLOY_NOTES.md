# Deploy notes (Render)

## Start command and workers: keep ONE worker for now

Current start command (Procfile / Render): `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`.
**Keep it at a single worker.** Do not add `--workers 2` yet.

Why: several things live in the memory of one Python process, and each extra worker gets its own copy:

| In-memory state | With 2+ workers |
|---|---|
| Admin CSV import jobs (`_IMPORT_JOBS`) | `GET /admin/import/{id}` can hit the other worker and answer "Import not found" while the import is still running |
| One-import-at-a-time lock (`_IMPORT_LOCK`) | Two imports could run at once (one per worker) |
| Rate limits (slowapi, in-memory) | Each limit is per worker, so the real limit is N x the number of workers |
| One Amazon search per user (`_amazon_inflight`) | A user could run one per worker |
| Free top-10 cache (5 min) | Harmless (each worker has its own cache) |

The event-loop freeze that made extra workers urgent (audit H1) is fixed differently: every route that does
slow blocking work (Supabase, Apify, OpenAI/Groq, image fetches, CSV parsing) is now a plain `def` route or
runs in `run_in_threadpool`, so FastAPI runs it in its threadpool (40 threads by default) while the event loop
keeps answering other requests.

When you want more than one worker later, first move the import job state + lock to a table (e.g.
`import_jobs` with a row lock) and the rate limits to Redis (`storage_uri="redis://..."` in `main.py`), then use
`--workers 2`.

## Run the migration before deploying

Run `supabase/migrations/20261107_security_fixes.sql` in Supabase -> SQL Editor **before** this backend goes
live. Without it the webhook still works (it falls back to the old per-event logic and logs an error), but the
out-of-order protection and "best active subscription" logic are off.

## Tests

```
pip install -r requirements.txt pytest
python -m pytest tests -q
```
The tests are offline: Supabase, Lemon Squeezy, OpenAI and DNS are stubbed.
