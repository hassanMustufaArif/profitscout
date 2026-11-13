"""
Oxylabs Web Scraper API client (the cheap one, billed per successful result, about $1.15-1.35 per 1,000).
No Agent Browser, no Playwright. Same account the old pipelines use.

Env: OXYLABS_USER / OXYLABS_PASS   (OXYLABS_USERNAME / OXYLABS_PASSWORD also accepted)
"""

from __future__ import annotations

import os
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

USER = os.getenv("OXYLABS_USER") or os.getenv("OXYLABS_USERNAME")
PASS = os.getenv("OXYLABS_PASS") or os.getenv("OXYLABS_PASSWORD")
ENDPOINT = "https://realtime.oxylabs.io/v1/queries"
# Newer Oxylabs "Web API" product: one API key (Bearer), endpoint webapi.oxylabs.io/v1/scrape.
WEB_API_KEY = (os.getenv("OXYLABS_WEB_API_KEY") or "").strip()
WEB_API_ENDPOINT = "https://webapi.oxylabs.io/v1/scrape"


class OxylabsError(Exception):
    pass


class Budget:
    """Hard cap on paid requests per run so a bug can never burn the account."""

    def __init__(self, max_requests: int):
        self.max = max_requests
        self.used = 0
        self._lock = threading.Lock()   # shared by parallel workers

    def take(self) -> None:
        with self._lock:
            if self.used >= self.max:
                raise OxylabsError(f"request budget of {self.max} reached")
            self.used += 1


def _web_api_scrape(payload: dict, budget: Budget, retries: int = 2) -> str:
    """Same job through the Web API key: fetch one URL, return its HTML."""
    url = payload.get("url")
    if not url:
        raise RuntimeError(f"Oxylabs Web API key only supports page URLs, not source={payload.get('source')}")
    body = {"url": url, "output": ["html"], "location": "US"}
    if payload.get("render"):
        body["run_js"] = True
    headers = {"Authorization": f"Bearer {WEB_API_KEY}", "Content-Type": "application/json"}
    last = "unknown"
    for attempt in range(retries + 1):
        budget.take()
        try:
            r = httpx.post(WEB_API_ENDPOINT, json=body, headers=headers, timeout=150)
        except httpx.HTTPError as e:
            last = f"network: {type(e).__name__}"
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise OxylabsError("Oxylabs rejected OXYLABS_WEB_API_KEY (401). Copy the key again from the Web API instance.")
        if r.status_code == 429 and attempt == retries:
            raise OxylabsError("Oxylabs Web API: rate limit or free quota used up (429). Check Billing in the dashboard.")
        if r.status_code in (408, 429) or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(3 * (attempt + 1))
            continue
        if not (200 <= r.status_code < 300):
            raise RuntimeError(f"Oxylabs Web API HTTP {r.status_code}: {r.text[:200]}")
        res = (r.json().get("results") or [{}])[0]
        html = res.get("html") or res.get("content") or ""
        if isinstance(html, list):
            html = "".join(map(str, html))
        if html:
            return html if isinstance(html, str) else str(html)
        last = "empty page"
        body["run_js"] = True          # docs: an empty shell means the page needs JavaScript
    raise RuntimeError(f"Oxylabs Web API failed: {last}")


def scrape(payload: dict, budget: Budget, retries: int = 2) -> str:
    """Returns page HTML. Raises OxylabsError on auth problems or repeated failure.
    Uses OXYLABS_WEB_API_KEY when set, else OXYLABS_USER/OXYLABS_PASS (classic Web Scraper API)."""
    if WEB_API_KEY:
        return _web_api_scrape(payload, budget, retries)
    if not (USER and PASS):
        # Not a run-stopper: the free direct photo search is the main path, Oxylabs only the fallback.
        raise RuntimeError("blocked and no Oxylabs fallback (set OXYLABS_WEB_API_KEY or OXYLABS_USER / OXYLABS_PASS)")
    last = "unknown"
    for attempt in range(retries + 1):
        budget.take()
        try:
            r = httpx.post(ENDPOINT, auth=(USER, PASS), json=payload, timeout=180)
        except httpx.HTTPError as e:
            last = f"network: {e}"
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise OxylabsError("Oxylabs rejected the username/password (401). Create a Web Scraper API user in the Oxylabs dashboard.")
        if r.status_code in (402, 403):
            raise OxylabsError(f"Oxylabs account has no traffic left ({r.status_code}). Top up in the dashboard.")
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(3 * (attempt + 1))
            continue
        res = (r.json().get("results") or [{}])[0]
        content = res.get("content") or ""
        if res.get("status_code", 200) >= 400 or not content:
            last = f"target returned {res.get('status_code')}"
            continue
        return content if isinstance(content, str) else str(content)
    raise OxylabsError(last)
