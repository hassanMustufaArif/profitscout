"""
Supplier sources over plain HTTP + Oxylabs Web Scraper API. No browser.

  alibaba_by_image(photo)   upload the photo to Alibaba's image-search storage and call Alibaba's own
                            image-search JSON API. Both are plain web requests: free. Oxylabs is only
                            used as a fallback if Alibaba rate-limits us.
  alibaba_by_text(title)    Oxylabs source alibaba_search (1 result)
  aliexpress_by_text(title) Oxylabs source aliexpress_search (1 result)

Every list is then photo-checked against the TikTok picture with CLIP + pHash (match.py),
so text search is only a way to get candidates; the photo decides what is a match.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from urllib.parse import quote

import httpx

from . import parse
from .oxylabs import Budget, OxylabsError, scrape

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Origin": "https://www.alibaba.com",
    "Referer": "https://www.alibaba.com/",
}
SIGN_URL = "https://open-s.alibaba.com/openservice/ossUploadSecretKeyDataService?appKey=a5m1ismomeptugvfmkkjnwwqnwyrhpb1&appName=magellan"


class MarketplaceBlocked(RuntimeError):
    """The marketplace answered with a challenge / empty shell instead of a result list.
    NOT a 'no match': the product keeps its old suppliers and is retried next run."""


def _rand(k: int) -> str:
    return "".join(random.choices(string.ascii_letters, k=k))


def alibaba_upload(photo: bytes, with_host: bool = False):
    """Signed OSS upload (open-s, free, works from datacenter IPs). Returns the imageAddress key,
    or (host, key) with with_host=True."""
    sign = httpx.get(SIGN_URL, headers=UA, timeout=20).json().get("data") or {}
    for k in ("host", "policy", "accessid", "signature", "imagePath"):
        if not sign.get(k):
            raise RuntimeError(f"alibaba upload signature missing {k}")
    name = _rand(5) + ".jpg"
    key = f"{sign['imagePath']}/{_rand(10)}{int(time.time() * 1000)}.jpg"
    files = {
        "name": (None, name), "key": (None, key), "policy": (None, sign["policy"]),
        "OSSAccessKeyId": (None, sign["accessid"]), "success_action_status": (None, "200"),
        "callback": (None, ""), "signature": (None, sign["signature"]), "file": (name, photo, "image/jpeg"),
    }
    r = httpx.post(sign["host"], files=files, headers=UA, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"alibaba upload failed: HTTP {r.status_code}")
    return (sign["host"].rstrip("/"), key) if with_host else key


def _alibaba_from_html(html: str) -> list[dict]:
    offers = parse.alibaba_offers(parse.json_blobs_from_html(html))
    if offers:
        return offers
    # DOM fallback: product cards as plain HTML
    cards = []
    for m in re.finditer(r'<a[^>]+href="([^"]*(?:product-detail|/offer/)[^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        start = max(0, m.start() - 1500)
        block = html[start : m.end() + 1500]
        img = re.search(r'<img[^>]+(?:data-src|src)="([^"]+)"', block)
        text = re.sub(r"<[^>]+>", " ", block)
        cards.append({"href": href if href.startswith("http") else "https:" + href,
                      "title": re.sub(r"<[^>]+>", "", inner).strip()[:200],
                      "img": img.group(1) if img else None, "company": None, "text": text})
    return parse.alibaba_offers_from_dom(cards)


IMG_SEARCH_API = "https://open-s.alibaba.com/openservice/imageSearchViewService"


def _as_jpeg(photo: bytes) -> bytes:
    """TikTok/AliExpress CDNs often serve WebP/AVIF; Alibaba's image search only reads real JPEGs."""
    import io
    from PIL import Image
    try:
        buf = io.BytesIO()
        Image.open(io.BytesIO(photo)).convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return photo


def _site_jpeg(photo: bytes) -> bytes:
    """Exactly what alibaba.com's search bar uploads: white background, longer side 500 px, JPEG q80.
    The detected object regions are in this image's pixels, so both calls must use it."""
    import io
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(photo))
        im = im.convert("RGBA")
        canvas = Image.new("RGB", im.size, "white")
        canvas.paste(im, mask=im.split()[-1])
        canvas.thumbnail((500, 500))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return _as_jpeg(photo)


ITS_API = "https://www.alibaba.com/search/api/imageTextSearch"


def _json_from(text: str) -> dict:
    """JSON body, also when a scraper wrapped it in <html><pre>...</pre>."""
    import html as _html
    try:
        return json.loads(text)
    except Exception:
        i = text.find("{")
        j = text.rfind("}")
        if i < 0 or j <= i:
            return {}
        try:
            return json.loads(_html.unescape(text[i : j + 1]))
        except Exception:
            return {}


def _get_direct(url: str) -> str | None:
    """Plain GET with a browser TLS fingerprint when curl_cffi is installed (Akamai blocks
    python's default fingerprint), else httpx. None when blocked."""
    hdr = {"Referer": "https://www.alibaba.com/", "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9"}
    try:
        try:
            from curl_cffi import requests as cr
            r = cr.get(url, headers=hdr, impersonate="chrome", timeout=30)
        except ImportError:
            r = httpx.get(url, headers={**UA, **hdr}, timeout=30)
        if r.status_code == 200 and '"success"' in r.text[:200]:
            return r.text
    except Exception:
        pass
    return None


def alibaba_by_image(photo: bytes, budget: Budget) -> list[dict]:
    """Alibaba photo search that returns the SAME list as alibaba.com's camera button
    (verified 2026-11-13: identical order to the site's results page).

    1. upload the 500 px image (free)
    2. imageSearchViewService (free) -> the object regions Alibaba detects in it
    3. /search/api/imageTextSearch with imagePath + the first two regions = the site's own list.
       Direct first; through Oxylabs (1 paid result) when Akamai blocks this IP.
    If step 3 fails entirely, the step-2 list is used (older image-only ranking, close but not identical)."""
    img = _site_jpeg(photo)
    host, key = alibaba_upload(img, with_host=True)
    # appKey below is Alibaba's PUBLIC web client id (sent by every browser on alibaba.com), not a secret.
    params = {"appName": "magellan", "appKey": "a5m1ismomeptugvfmkkjnwwqnwyrhpb1", "imageType": "oss",  # gitleaks:allow
              "imageAddress": "/" + key, "beginPage": 1, "pageSize": 40, "language": "en_US"}
    view = {}
    try:
        j = httpx.get(IMG_SEARCH_API, params=params, headers=UA, timeout=30).json()
        if "SUCCESS" in " ".join(j.get("ret") or []):
            view = j.get("data") or {}
    except Exception:
        pass
    regions = [r for r in (view.get("regions") or []) if isinstance(r, str)][:2]
    if not regions and view.get("region"):
        regions = [view["region"]]

    its_url = str(httpx.URL(ITS_API, params={
        "imagePath": f"{host}/{key}", "regions": json.dumps(regions, separators=(",", ":")),
        "tab": "all", "from": "pcHomeContent"}))
    text = _get_direct(its_url)
    if text is None:
        try:
            text = scrape({"source": "universal", "url": its_url, "geo_location": "United States"}, budget)
        except OxylabsError:
            raise
        except Exception:
            text = None
    if text:
        model = (_json_from(text).get("model") or {})
        offers = parse.alibaba_its_offers(model.get("offers") or [])
        if offers:
            return offers
    if view.get("offers"):
        return parse.alibaba_offers([view.get("offers") or []])
    if not view and not text:
        raise MarketplaceBlocked("Alibaba image search did not answer")
    return []


def alibaba_by_text(keyword: str, budget: Budget) -> list[dict]:
    html = scrape({"source": "alibaba_search", "query": keyword, "render": "html"}, budget)
    return _alibaba_from_html(html)


def aliexpress_by_text(keyword: str, budget: Budget) -> list[dict]:
    html = scrape({"source": "aliexpress_search", "query": keyword, "render": "html",
                   "geo_location": "United States"}, budget)
    return parse.aliexpress_offers(parse.json_blobs_from_html(html))


# ─── keyword from a TikTok title, no AI ──────────────────────────────────────
_STOP = set("""
a an the and or for with of to in on by from your you my our new hot best top sale free shipping pack set pcs pc piece pieces
2024 2025 2026 tiktok viral trending gift gifts premium upgraded original official authentic genuine women men womens mens
""".split())


AE_COOKIES = {"aep_usuc_f": "site=glo&c_tp=USD&region=US&b_locale=en_US", "intl_locale": "en_US"}
AE_MTOP_API = "mtop.relationrecommend.aliexpressrecommend.recommend"
AE_MTOP_KEY = "24815441"   # AliExpress web app key (public, from its own page header script)


def _ae_mtop(client: httpx.Client, data: str) -> dict:
    import hashlib
    tk = (client.cookies.get("_m_h5_tk") or "").split("_")[0]
    t = str(int(time.time() * 1000))
    sign = hashlib.md5(f"{tk}&{t}&{AE_MTOP_KEY}&{data}".encode()).hexdigest()
    q = {"jsv": "2.5.1", "appKey": AE_MTOP_KEY, "t": t, "sign": sign, "api": AE_MTOP_API, "v": "1.0",
         "type": "originaljson", "dataType": "json", "timeout": "50000"}
    r = client.post(f"https://acs.aliexpress.com/h5/{AE_MTOP_API}/1.0/", params=q, data={"data": data},
                    headers={**UA, "Origin": "https://www.aliexpress.com", "Referer": "https://www.aliexpress.com/"},
                    timeout=60)
    return r.json()


def aliexpress_upload(photo: bytes) -> str:
    """Same call AliExpress's camera button makes: sends the photo, returns its image-search fileId. Free."""
    import base64
    params = {"platform": "pc", "page": 1, "pageSize": 60, "isNewImageSearch": True, "osf": "pc_web_image_search",
              "clientType": "pc", "searchBizScene": "imageSearch", "subScenario": "imageUpload",
              "image_base64": base64.b64encode(_as_jpeg(photo)).decode(), "shpt_co": "US", "_currency": "USD",
              "sortType": "default", "sortOrder": "default", "contentType": "imageUpload", "lang": "en",
              "locale": "en_US"}
    data = json.dumps({"appId": 21738, "params": json.dumps(params)}, separators=(",", ":"))
    with httpx.Client(follow_redirects=True, cookies=AE_COOKIES) as c:
        j = _ae_mtop(c, data)                      # 1st call only hands out the session token
        if "TOKEN" in " ".join(j.get("ret") or []):
            j = _ae_mtop(c, data)
    d = j.get("data") or {}
    if not d.get("fileId"):
        raise RuntimeError(f"AliExpress image upload refused: {(j.get('ret') or ['?'])[0][:80]}")
    return d["fileId"]


def _ae_debug(html: str, why: str) -> None:
    import os
    d = os.getenv("SOURCING_DEBUG_DIR", "sourcing_debug")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"aliexpress_{int(time.time() * 1000)}.html"), "w", encoding="utf-8") as f:
            f.write(f"<!-- {why} -->\n" + (html or ""))
    except Exception:
        pass


def _ae_page_offers(html: str) -> list[dict]:
    """Result cards from the server-rendered results page (window._dida_config_ ... "itemList").
    Raises MarketplaceBlocked when the page is a challenge or has no result block at all."""
    if not html:
        raise MarketplaceBlocked("AliExpress results page empty")
    if "_____tmd_____" in html or "punish" in html[:5000] or "x5secdata" in html:
        _ae_debug(html, "challenge")
        raise MarketplaceBlocked(f"AliExpress challenge page ({len(html)} bytes)")
    offers = parse.aliexpress_offers(parse.json_blobs_from_html(html))
    if offers:
        return offers
    if '"itemList"' in html and '"content":[]' in html.replace(" ", ""):
        return []                       # a real, empty result list
    _ae_debug(html, "no itemList")
    title = (re.search(r"<title>(.*?)</title>", html, re.S | re.I) or [None, ""])[1].strip()[:60]
    raise MarketplaceBlocked(f"AliExpress page had no result list ({len(html)} bytes, title '{title}')")


def aliexpress_by_image(photo: bytes, budget: Budget) -> list[dict]:
    """AliExpress photo search, exactly like its camera button: upload (free) -> fileId, then the
    results page /w/wholesale-.html?isNewImageSearch=y&filename=<fileId>&imageId=<ms>, which is
    server-rendered with the 60 cards embedded. Direct first; Oxylabs only if blocked."""
    file_id = aliexpress_upload(photo)
    url = ("https://www.aliexpress.us/w/wholesale-.html?isNewImageSearch=y&filename=" + quote(file_id, safe="")
           + f"&imageId={int(time.time() * 1000)}")
    try:
        r = httpx.get(url, headers={**UA, "Origin": "https://www.aliexpress.us", "Referer": "https://www.aliexpress.us/",
                                    "Accept-Language": "en-US"},
                      cookies=AE_COOKIES, follow_redirects=True, timeout=40)
        if r.status_code == 200:
            return _ae_page_offers(r.text)
    except Exception:
        pass
    # blocked from this IP: same page through Oxylabs (1 paid result). The page is server-rendered,
    # so no JavaScript rendering is needed (JS rendering rewrote the page and lost the data).
    html = scrape({"source": "universal", "url": url, "geo_location": "United States"}, budget)
    return _ae_page_offers(html)


def keyword_from_title(title: str, words: int = 6) -> str:
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title or "")          # drop (...) [...] notes
    t = re.split(r"[|,;:/]| - ", t)[0] if len(t) > 60 else t       # keep the head of long titles
    t = re.sub(r"\b\d+(\.\d+)?\s*(ml|oz|g|kg|lb|lbs|cm|mm|inch|in|pcs|pack|count|ct|x)\b", " ", t, flags=re.I)
    toks = [w for w in re.findall(r"[A-Za-z][A-Za-z\-']+", t) if w.lower() not in _STOP and len(w) > 1]
    # brand names are usually the first CamelCase / ALLCAPS token: skip it when the title is long enough
    if len(toks) > words and (toks[0].isupper() or (toks[0][0].isupper() and any(c.isupper() for c in toks[0][1:]))):
        toks = toks[1:]
    return " ".join(toks[:words]) or (title or "")[:60]
