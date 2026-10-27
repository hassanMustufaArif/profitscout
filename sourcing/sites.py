"""
The two site flows. Both do exactly what a person does:
open the homepage → click the camera icon → upload the TikTok product photo → read the results.

We read results from the site's OWN JSON (XHR responses + embedded page data) instead of
scraping visual layout, which is what keeps this stable when they restyle the page.
"""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import Page, Response

from . import parse
from .browser import looks_blocked, save_debug, upload_image

CAMERA_SELECTORS = [
    "[class*='picSearch' i]", "[class*='pic-search' i]", "[class*='image-search' i]",
    "[class*='imageSearch' i]", "[class*='img-search' i]", "[class*='imgsearch' i]",
    "[class*='camera' i]", "[aria-label*='image' i]", "[title*='image' i]",
]


class Blocked(Exception):
    """Site served a challenge page. Caller retries with a fresh IP/session."""


class NoResults(Exception):
    pass


async def _collect_json(page: Page, sink: list):
    async def on_response(r: Response):
        try:
            ct = r.headers.get("content-type", "")
            if r.request.resource_type in ("xhr", "fetch") and ("json" in ct or "javascript" in ct):
                body = await r.text()
                if len(body) > 500 and ("productId" in body or "offerId" in body or "subject" in body):
                    body = body.strip()
                    if body.startswith(("{", "[")):
                        sink.append(json.loads(body))
                    else:  # JSONP: callback({...})
                        start = body.find("(")
                        if start > 0:
                            sink.append(json.loads(body[start + 1 : body.rfind(")")]))
        except Exception:
            pass

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))


async def _reveal_file_input(page: Page) -> None:
    if await page.locator("input[type=file]").count():
        return
    for sel in CAMERA_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=4000)
                await page.wait_for_selector("input[type=file]", state="attached", timeout=6000)
                return
        except Exception:
            continue
    raise RuntimeError("camera / file input not found")


async def _dismiss_popups(page: Page) -> None:
    for sel in ["button:has-text('Accept')", "button:has-text('Got it')", "[class*='close' i][role='button']",
                "[class*='pop-close' i]", "[class*='_close' i]"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1500)
        except Exception:
            pass


async def _run(page: Page, home: str, done_url_keys: tuple, image: bytes, tag: str) -> tuple[list, str]:
    blobs: list = []
    await _collect_json(page, blobs)
    await page.goto(home, wait_until="domcontentloaded")
    if looks_blocked(page.url, await page.content()):
        await save_debug(page, f"{tag}_blocked_home")
        raise Blocked(tag)
    await _dismiss_popups(page)
    await _reveal_file_input(page)
    await upload_image(page, "input[type=file]", image)

    try:
        await page.wait_for_url(lambda u: any(k in u.lower() for k in done_url_keys), timeout=30_000)
    except Exception:
        pass  # some versions render results in place; JSON sink still catches them
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await page.mouse.wheel(0, 2500)          # trigger lazy-loaded result batches
    await asyncio.sleep(2.5)

    html = await page.content()
    if looks_blocked(page.url, html):
        await save_debug(page, f"{tag}_blocked_results")
        raise Blocked(tag)
    blobs.extend(parse.json_blobs_from_html(html))
    return blobs, html


async def aliexpress_by_image(page: Page, image: bytes) -> list[dict]:
    blobs, _ = await _run(
        page, "https://www.aliexpress.com/", ("imageaddress", "image", "/w/", "pic"), image, "aliexpress"
    )
    offers = parse.aliexpress_offers(blobs)
    if not offers:
        await save_debug(page, "aliexpress_no_results")
        raise NoResults("aliexpress")
    return offers


async def alibaba_by_image(page: Page, image: bytes) -> list[dict]:
    blobs, _ = await _run(
        page, "https://www.alibaba.com/", ("picture/search", "imageaddress", "image_search", "imagesearch"), image, "alibaba"
    )
    offers = parse.alibaba_offers(blobs)
    if not offers:  # fall back to reading product cards from the DOM
        cards = await page.evaluate(
            """() => [...document.querySelectorAll("a[href*='product-detail'], a[href*='/offer/']")].slice(0, 80).map(a => {
                 const card = a.closest('[class*=card], [class*=item], [class*=offer]') || a;
                 const img = card.querySelector('img');
                 const comp = card.querySelector('[class*=company], [class*=supplier]');
                 return { href: a.href, title: a.getAttribute('title') || a.innerText.slice(0, 200),
                          img: img ? (img.currentSrc || img.src || img.dataset.src) : null,
                          company: comp ? comp.innerText.trim() : null, text: card.innerText.slice(0, 600) };
               })"""
        )
        offers = parse.alibaba_offers_from_dom(cards)
    if not offers:
        await save_debug(page, "alibaba_no_results")
        raise NoResults("alibaba")
    return offers


SITE_FLOWS = {"aliexpress": aliexpress_by_image, "alibaba": alibaba_by_image}
