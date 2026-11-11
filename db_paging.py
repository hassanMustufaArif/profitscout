"""Read every row of a Supabase/PostgREST query.

PostgREST caps each response at the project's max-rows (Supabase default 1000) and ignores a
larger .limit(), silently. fetch_all() pages with .range() until a short page comes back."""
from __future__ import annotations

from typing import Callable

PAGE = 1000


def fetch_all(build: Callable[[], object], page: int = PAGE, max_rows: int | None = None) -> list[dict]:
    """build() must return a FRESH query builder each call (filters + a stable .order(), no range).
    Order by a unique column (e.g. "id") so pages don't overlap or skip rows."""
    out: list[dict] = []
    start = 0
    while True:
        end = start + page - 1
        if max_rows is not None:
            end = min(end, max_rows - 1)
            if end < start:
                break
        rows = build().range(start, end).execute().data or []
        out.extend(rows)
        if len(rows) < end - start + 1:
            break
        start = end + 1
    return out


def count_exact(build: Callable[[], object]) -> int:
    """Row count via count='exact' (no rows transferred). build() returns a builder whose
    .select() was called with count="exact"; one row is fetched."""
    res = build().limit(1).execute()
    return int(res.count or 0)
