"""
TikProfitPro — Photo search for suppliers
==========================================
Three layers, cheapest first:

1. vision_keyword()      GPT-4o-mini looks at the TikTok product photo and writes the
                         sourcing keyword. Fixes the biggest problem today: TikTok titles
                         are stuffed with brand names and marketing words, so the text
                         keyword finds the wrong products.

2. aliexpress_image_search()
                         True reverse image search through the OFFICIAL AliExpress
                         Dropshipping API (aliexpress.ds.image.search). Only runs when
                         ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET and
                         ALIEXPRESS_ACCESS_TOKEN are set in Railway.

3. verify_by_image()     GPT-4o-mini compares the TikTok photo with each candidate's photo
                         and drops anything that is not the same product. This is what
                         stops "knife shown as a supplier for mascara".

Every function returns something safe on failure, so photo search can never break the
normal text search.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

import httpx
from openai import OpenAI

_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if os.getenv("OPENAI_API_KEY") else None
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")

AE_APP_KEY = os.getenv("ALIEXPRESS_APP_KEY")
AE_APP_SECRET = os.getenv("ALIEXPRESS_APP_SECRET")
AE_ACCESS_TOKEN = os.getenv("ALIEXPRESS_ACCESS_TOKEN")
AE_GATEWAY = os.getenv("ALIEXPRESS_GATEWAY", "https://api-sg.aliexpress.com/sync")

_UA = {"User-Agent": "Mozilla/5.0 (TikProfitPro image fetch)"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fetch_image_bytes(url: str, max_bytes: int = 4_000_000) -> Optional[bytes]:
    """Download an image. TikTok CDN links expire, so this can return None."""
    if not url:
        return None
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers=_UA) as c:
            r = c.get(url)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            return None
        return r.content[:max_bytes]
    except Exception:
        return None


def _data_url(img: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(img).decode()


def photo_search_available() -> bool:
    return bool(AE_APP_KEY and AE_APP_SECRET and AE_ACCESS_TOKEN)


# ---------------------------------------------------------------------------
# 1. photo → keyword
# ---------------------------------------------------------------------------
def vision_keyword(image_url: str, title: str) -> Optional[str]:
    img = fetch_image_bytes(image_url)
    if not img:
        return None
    try:
        r = _openai.chat.completions.create(
            model=VISION_MODEL,
            temperature=0,
            max_tokens=30,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write wholesale search keywords for Alibaba/AliExpress. "
                        "Look at the product photo (the title is only a hint and may be wrong). "
                        "Describe WHAT THE PRODUCT IS in 3-7 plain words: product type, material or form, "
                        "key feature. No brand names, no marketing words. Output only the keyword."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Title hint: {title[:200]}"},
                        {"type": "image_url", "image_url": {"url": _data_url(img), "detail": "low"}},
                    ],
                },
            ],
        )
        kw = (r.choices[0].message.content or "").strip().strip("\"'").lower()
        return kw or None
    except Exception as e:
        print(f"vision_keyword failed: {e}")
        return None


# ---------------------------------------------------------------------------
# 2. official AliExpress reverse image search
# ---------------------------------------------------------------------------
def _ae_sign(params: dict) -> str:
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(AE_APP_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def aliexpress_image_search(image_url: str, ship_to: str = "US", limit: int = 15) -> list:
    """Returns items shaped like the AliExpress scraper output used by supplier_engine
    (title, price, sellerName, url, imageUrl), so the rest of the pipeline is unchanged."""
    if not photo_search_available():
        return []
    img = fetch_image_bytes(image_url)
    if not img:
        return []

    params = {
        "app_key": AE_APP_KEY,
        "method": "aliexpress.ds.image.search",
        "session": AE_ACCESS_TOKEN,
        "sign_method": "sha256",
        "timestamp": str(int(time.time() * 1000)),
        "shpt_to": ship_to,
        "target_currency": "USD",
        "target_language": "EN",
    }
    params["sign"] = _ae_sign(params)  # file bytes are not part of the signature

    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(AE_GATEWAY, data=params, files={"image_file_bytes": ("product.jpg", img, "image/jpeg")})
        data = r.json()
    except Exception as e:
        print(f"aliexpress_image_search failed: {e}")
        return []

    # The response nests the product list a few levels deep; find it defensively.
    products = _find_product_list(data)
    out = []
    for p in products[:limit]:
        pid = p.get("product_id") or p.get("productId") or p.get("item_id")
        out.append(
            {
                "title": p.get("product_title") or p.get("subject") or p.get("title") or "",
                "price": p.get("target_sale_price") or p.get("sale_price") or p.get("app_sale_price") or p.get("price") or 0,
                "sellerName": p.get("shop_name") or p.get("store_name") or "AliExpress Seller",
                "url": p.get("product_detail_url") or p.get("promotion_link")
                or (f"https://www.aliexpress.com/item/{pid}.html" if pid else ""),
                "imageUrl": p.get("product_main_image_url") or p.get("image_url") or p.get("main_image") or "",
                "_source": "aliexpress_image_api",
            }
        )
    print(f"  AliExpress image API returned {len(out)} items")
    return out


def _find_product_list(obj) -> list:
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(
            k in obj[0] for k in ("product_id", "productId", "product_title", "subject")
        ):
            return obj
        for x in obj:
            found = _find_product_list(x)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = _find_product_list(v)
            if found:
                return found
    return []


# ---------------------------------------------------------------------------
# 3. photo-vs-photo verification
# ---------------------------------------------------------------------------
def verify_by_image(product_image_url: str, candidates: list[dict], min_score: int = 70) -> Optional[list[dict]]:
    """candidates: [{"index": int, "image_url": str, "title": str}]
    Returns [{"index", "score", "same_product"}] for candidates that pass, best first.
    Returns None when verification could not run (caller should fall back to text ranking)."""
    src = fetch_image_bytes(product_image_url)
    if not src:
        return None
    usable = []
    content = [
        {"type": "text", "text": "REFERENCE product (what the seller wants to source):"},
        {"type": "image_url", "image_url": {"url": _data_url(src), "detail": "low"}},
    ]
    for c in candidates[:8]:
        img = fetch_image_bytes(c.get("image_url", ""))
        if not img:
            continue
        usable.append(c["index"])
        content.append({"type": "text", "text": f"Candidate {c['index']}: {c.get('title', '')[:100]}"})
        content.append({"type": "image_url", "image_url": {"url": _data_url(img), "detail": "low"}})
    if not usable:
        return None

    content.append(
        {
            "type": "text",
            "text": (
                "For each candidate, score 0-100 how likely it is the SAME product as the reference "
                "(same type, shape, function; colour/packaging may differ). A different product type is 0-20. "
                'Reply only JSON: {"results":[{"index":N,"score":S}]}'
            ),
        }
    )
    try:
        r = _openai.chat.completions.create(
            model=VISION_MODEL,
            temperature=0,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": content}],
        )
        parsed = json.loads(r.choices[0].message.content or "{}")
        res = [
            {"index": int(x["index"]), "score": int(x["score"]), "same_product": int(x["score"]) >= min_score}
            for x in parsed.get("results", [])
            if int(x.get("index", -1)) in usable
        ]
        res.sort(key=lambda x: x["score"], reverse=True)
        return [x for x in res if x["same_product"]]
    except Exception as e:
        print(f"verify_by_image failed: {e}")
        return None
