"""
Browser sessions through Oxylabs.

Primary:  Oxylabs Agent Browser over CDP (wss://USER:PASS@hb.oxylabs.io?p_cc=US&solve_captcha=true)
Fallback: local Chromium via Playwright, routed through Oxylabs Residential Proxies.

Every attempt gets a FRESH session id, so a retry comes from a different exit IP.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import time
import uuid
from urllib.parse import quote

from playwright.async_api import Browser, Page, async_playwright

from . import config


def _ab_url(session_id: str) -> str:
    user, pwd = quote(config.AB_USER, safe=""), quote(config.AB_PASS, safe="")
    return (
        f"wss://{user}:{pwd}@{config.AB_HOST}"
        f"?p_cc={config.COUNTRY}&solve_captcha=true&page_handler=true"
        f"&proxy_resi_ses_id={session_id}&proxy_resi_ses_time=10"
    )


@contextlib.asynccontextmanager
async def open_page():
    """Yields a Playwright Page. Always closes the remote session (Oxylabs counts open ones)."""
    session_id = uuid.uuid4().hex[:20]
    async with async_playwright() as p:
        browser: Browser | None = None
        try:
            if config.AB_USER and config.AB_PASS:
                last = None
                for attempt in range(4):  # Agent Browser allows 10 new sessions/sec; back off on bursts
                    try:
                        browser = await p.chromium.connect_over_cdp(_ab_url(session_id), timeout=60_000)
                        break
                    except Exception as e:
                        last = e
                        await asyncio.sleep(1.5 * 2 ** attempt)
                if browser is None:
                    raise RuntimeError(f"Agent Browser connect failed: {last}")
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            elif config.RESI_USER and config.RESI_PASS:
                browser = await p.chromium.launch(
                    headless=True,
                    proxy={
                        "server": config.RESI_SERVER,
                        "username": f"{config.RESI_USER}-sessid-{session_id}",
                        "password": config.RESI_PASS,
                    },
                )
                ctx = await browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            else:
                raise RuntimeError("Set OXYLABS_AB_USER/OXYLABS_AB_PASS or OXYLABS_RESI_USER/OXYLABS_RESI_PASS")

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(config.PAGE_TIMEOUT_MS)
            yield page
        finally:
            if browser:
                with contextlib.suppress(Exception):
                    await browser.close()


async def upload_image(page: Page, file_input_selector: str, image: bytes, filename: str = "product.jpg") -> None:
    """Put the product photo into a (usually hidden) <input type=file>.

    1) Playwright set_input_files with an in-memory buffer (works on local and remote browsers).
    2) If that fails on a remote CDP browser, build a File in the page with DataTransfer and fire
       the same 'change' event the site's own uploader listens for.
    """
    loc = page.locator(file_input_selector).first
    await loc.wait_for(state="attached")
    try:
        await loc.set_input_files({"name": filename, "mimeType": "image/jpeg", "buffer": image})
        return
    except Exception as e:
        print(f"  set_input_files failed ({e}); using DataTransfer fallback")

    b64 = base64.b64encode(image).decode()
    await loc.evaluate(
        """(input, [b64, name]) => {
            const bin = atob(b64); const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            const file = new File([arr], name, { type: 'image/jpeg' });
            const dt = new DataTransfer(); dt.items.add(file);
            input.files = dt.files;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [b64, filename],
    )


async def save_debug(page: Page, tag: str) -> str | None:
    """Screenshot + HTML for any failure, so selector drift is fixed in minutes, not days."""
    try:
        os.makedirs(config.DEBUG_DIR, exist_ok=True)
        base = os.path.join(config.DEBUG_DIR, f"{int(time.time())}_{tag}")
        await page.screenshot(path=base + ".png", full_page=False)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(await page.content())
        return base
    except Exception:
        return None


def looks_blocked(url: str, html: str) -> bool:
    u = url.lower()
    h = html[:200_000].lower()
    return any(
        k in u or k in h
        for k in ("punish", "/_____tmd_____", "captcha", "slide to verify", "unusual traffic", "x5secdata", "baxia")
    )
