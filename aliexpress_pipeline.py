"""
TikProfitPro — AliExpress Auto Supplier Pipeline
=================================================
- Fetches every active product from Supabase
- Checks if product already has an AliExpress supplier
- If missing → searches AliExpress via Oxylabs Web Scraper API
- Parses _dida_config_ embedded JSON for real prices
- Calculates profit for every result
- Saves top 3 most profitable AliExpress suppliers to suppliers table

Location : F:/supplier_tool/aliexpress_pipeline.py

Requirements:
    pip install requests python-dotenv beautifulsoup4 lxml anthropic

.env file (same folder):
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=your_service_role_key
    OXYLABS_USER=Hassan_Yb7vy
    OXYLABS_PASS=Private=12364
    ANTHROPIC_API_KEY=your_anthropic_api_key

Usage:
    python aliexpress_pipeline.py               # process all products
    python aliexpress_pipeline.py --limit 10    # first 10 products
    python aliexpress_pipeline.py --min-margin 20
    python aliexpress_pipeline.py --test        # first 3 products only
    python aliexpress_pipeline.py --dry-run     # no DB writes
    python aliexpress_pipeline.py --refill      # only products missing AliExpress supplier
"""

import os
import re
import uuid
import json
import time
import argparse
import requests
import urllib.parse
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env from script directory AND cwd
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, ".env"), override=True)
load_dotenv(override=False)   # fallback: try cwd

# ── Credentials ────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
OXYLABS_USER      = os.getenv("OXYLABS_USER", "Sodewala45_wDk02")
OXYLABS_PASS      = os.getenv("OXYLABS_PASS", "Private=12364")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Profit assumptions ─────────────────────────────────────────────────────────
TIKTOK_FEE_PCT   = 0.08     # 8%  TikTok Shop commission
PAYMENT_FEE_PCT  = 0.029    # 2.9% payment processing
CUSTOMER_SHIP    = 0.0      # $0  (free shipping to buyer assumed)
MIN_MARGIN_PCT   = 15.0     # default minimum margin % to save a supplier
MAX_SUPPLIERS    = 3        # max AliExpress suppliers to save per product

# ── AliExpress shipping estimate ───────────────────────────────────────────────
ALIEXPRESS_SHIP_DAYS = 15   # average days for AliExpress standard shipping


# ══════════════════════════════════════════════════════════════════════════════
#  SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }

def sb_get(table, params=""):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{params}",
        headers=_sb_headers(), timeout=15
    )
    r.raise_for_status()
    return r.json()

def sb_insert(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**_sb_headers(), "Prefer": "return=representation"},
        json=data, timeout=15
    )
    r.raise_for_status()
    return r.json()


# ── Fetch all active products ──────────────────────────────────────────────────

def fetch_products(limit: int) -> list:
    params = (
        "select=id,product_name,category,tiktok_price,"
        "virality_score,trend_phase,thumbnail_url,"
        "review_count,units_sold_7d,gmv_7d,gmv_growth_pct"
        "&is_active=eq.true"
        "&order=virality_score.desc"
        f"&limit={limit}"
    )
    return sb_get("tiktok_products", params)


# ── Check existing AliExpress suppliers ───────────────────────────────────────

def aliexpress_supplier_count(product_id: str) -> int:
    """Return how many AliExpress supplier rows already exist for this product."""
    try:
        rows = sb_get(
            "suppliers",
            f"tiktok_product_id=eq.{product_id}&platform=eq.aliexpress&select=id"
        )
        return len(rows)
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
#  ALIEXPRESS SCRAPER  (corrected + enhanced from your son's version)
# ══════════════════════════════════════════════════════════════════════════════

def _build_search_keyword(product_name: str, category: str = "") -> str:
    """
    Use Claude AI as a Strategic Sourcing Specialist to generate the best
    AliExpress search keyword for a given branded product name.

    The AI deconstructs the product into its Generic DNA:
    - Active ingredients / core materials
    - Delivery form / product type
    - Key specs (dosage, size, count)
    - Strips brand names to find white-label manufacturers

    Falls back to simple regex cleaning if the API call fails.
    """
    keyword = _ai_sourcing_keyword(product_name)
    if keyword:
        return keyword

    # ── Fallback: simple regex cleanup ────────────────────────────────────
    print(f"    💡 AI keyword failed — using smart local fallback")
    return _smart_local_keyword(product_name)


def _smart_local_keyword(product_name: str) -> str:
    """Smart local keyword builder — no API needed. Strips brands, extracts product DNA."""
    name_lower = product_name.lower()

    ingredient_map = [
        (r"probiotic.*(women|vaginal|feminine)",    "women probiotic capsules 60ct"),
        (r"probiotic",                              "probiotic supplement capsules"),
        (r"pre.?workout|preworkout",                "pre workout powder supplement"),
        (r"whey|protein powder",                    "whey protein isolate powder"),
        (r"creatine",                               "creatine monohydrate powder"),
        (r"collagen.*(peptide|powder)",             "collagen peptide powder supplement"),
        (r"ashwagandha|ksm.?66",                   "ashwagandha supplement capsules"),
        (r"magnesium.*(complex|glycinate|malate)",  "magnesium complex capsules"),
        (r"magnesium",                              "magnesium supplement capsules"),
        (r"electrolyte",                            "electrolyte powder drink mix"),
        (r"greens.?powder|super greens",            "super greens powder supplement"),
        (r"omega.?3|fish oil",                      "omega 3 fish oil softgels"),
        (r"vitamin d3|vitamin d ",                  "vitamin D3 K2 drops supplement"),
        (r"multivitamin|vitapak",                   "multivitamin supplement daily"),
        (r"berberine",                              "berberine supplement capsules"),
        (r"turkesterone|tongkat ali",               "turkesterone tongkat ali capsules"),
        (r"shilajit",                               "shilajit resin supplement"),
        (r"maca root",                              "maca root powder capsules"),
        (r"fiber.*(gummies|supplement)",            "fiber supplement gummies"),
        (r"glucosamine.*chondroitin",               "glucosamine chondroitin msm"),
        (r"hydrogen.?tablet",                       "molecular hydrogen tablets"),
        (r"melatonin|sleep.*(gummies|aid)",         "melatonin sleep gummies supplement"),
        (r"calming|stress.*(drink|supplement)",     "calm supplement ashwagandha powder"),
        (r"eyelash.*(serum|growth|enhanc)",         "eyelash growth serum peptide"),
        (r"retinol",                                "retinol serum anti aging"),
        (r"eye.?(patch|pad|mask)",                  "collagen hydrogel eye patch"),
        (r"dark spot|hyperpigment",                 "dark spot brightening serum"),
        (r"ingrown hair",                           "ingrown hair treatment serum"),
        (r"lip.?(plump|gloss|oil|balm)",            "lip plumper oil gloss"),
        (r"pore.*(relief|balm|cream)",              "pore minimizer balm cream"),
        (r"minoxidil",                              "minoxidil hair growth serum"),
        (r"hair.*(growth|serum|roll.?on)",          "hair growth serum scalp oil"),
        (r"lace front.*wig|13x6.*wig",              "lace front wig human hair"),
        (r"beard.*(growth|serum|roller)",           "beard growth serum derma roller"),
        (r"hair.*(dye|color|shampoo bar)",          "hair dye shampoo color"),
        (r"insulated.*(tumbler|cup)|cup insulator|buddy.*cup|thicc.*buddy|universal.*buddy",
                                                    "insulated tumbler stainless steel"),
        (r"mixing bowl",                            "stainless steel mixing bowl set"),
        (r"charging station|wireless charg",        "wireless charging station multi"),
        (r"hearing aid",                            "hearing aid rechargeable otc"),
        (r"resistance band",                        "resistance band set exercise"),
        (r"pet hair.*dissolver|pet hair.*wash",     "pet hair remover washing machine"),
        (r"dog.*(shoe|croc|boot)",                  "dog shoes boots paw protector"),
        (r"cat.*(treat|grass|food topper)",         "cat food topper treat"),
        (r"hoodie.*(stress|anxiety|paw)",           "weighted hoodie anxiety comfort"),
    ]

    for pattern, keyword in ingredient_map:
        if re.search(pattern, name_lower):
            print(f"    💡 Smart keyword: '{keyword}'")
            return keyword

    # Generic brand stripping fallback
    cleaned = re.sub(r"[TM|R|C]", "", product_name)
    cleaned = re.sub(r"\b[A-Z][a-z]*\s+[A-Z][a-z]*\b", "", cleaned)  # Title Case Brand
    cleaned = re.sub(r"\b(by|from|official|authentic|signature)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d+\s*(mg|ml|oz|lb|g|ct|pcs|pack|count)s?\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[-|].*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    words = [w for w in cleaned.split() if len(w) > 2][:5]
    keyword = " ".join(words)
    print(f"    💡 Smart keyword: '{keyword}'")
    return keyword or product_name[:40]


def _ai_sourcing_keyword(product_name: str) -> str | None:
    """
    Call Claude claude-sonnet-4-20250514 with the Strategic Sourcing Specialist prompt
    to generate the optimal AliExpress white-label search keyword.
    Returns a clean keyword string, or None on failure.
    """
    try:
        import anthropic
    except (ImportError, ModuleNotFoundError) as ie:
        print(f"    ⚠️  AI keyword: import failed — {ie}")
        return None

    # Check API key exists before attempting call
    api_key = ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print(f"    ⚠️  AI keyword: ANTHROPIC_API_KEY not set — add to .env file")
        return None

    system_prompt = """# ROLE
You are an expert Strategic Sourcing Specialist specializing in identifying the original white-label manufacturers for consumer brands on AliExpress.

# CONTEXT
Many direct-to-consumer (DTC) brands use standard "off-the-shelf" products from AliExpress. Your mission is to bypass the brand and find the raw manufacturing source.

# OPERATIONAL PROTOCOL (THINK-ACT-OBSERVE)
For every request, follow these steps before providing a final answer:

1. ANALYSIS: Deconstruct the branded product into its core "Generic DNA":
   - Active Ingredients: Extract key compounds (e.g., Ashwagandha, Reusable Cup, Magnesium).
   - Delivery Form: (e.g., Gummies, Powder, Vegan Capsules).
   - Specifications: Dosage per serving and total unit count (e.g., 60ct, 500mg).
   - Aesthetics: Identify unique packaging shapes (e.g., amber glass bottle, matte black tub).

2. EXECUTION: Generate a search query using the title given exact without brand name.
   - Strict Rule: NEVER include the original Brand Name in the search query to avoid branded resellers.
   - Use generic ingredient/material names that AliExpress manufacturers would use.
   - Keep it 3-6 words for best results.

3. EVALUATION: Mentally compare results against the branded product using:
   - Formula Parity (Mandatory): Ingredients and dosages must be identical.
   - Visual Match: Same packaging form factor.
   - Service Match: Prioritize OEM / Custom Logo / Private Label manufacturers.

4. FINAL SELECTION: Output the single best search keyword based on highest parity score.

# CONSTRAINTS
- NEVER include brand names in the output keyword.
- Return ONLY the search keyword — no explanation, no punctuation, no quotes.
- 3-6 words maximum."""

    user_prompt = (
        "Find the white-label manufacturer search keyword for this branded TikTok product:\n\n"
        + product_name +
        "\n\nReturn ONLY the 3-6 word AliExpress search keyword. No brand name. No explanation."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=50,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        keyword = msg.content[0].text.strip().strip('"').strip("'").strip()
        # Sanity check — must be 2-8 words and not contain the brand
        words = keyword.split()
        if 2 <= len(words) <= 8 and len(keyword) < 80:
            print(f"    🤖 AI keyword: '{keyword}'")
            return keyword
        return None
    except Exception as e:
        import traceback
        print(f"    ⚠️  AI keyword error: {type(e).__name__}: {str(e)[:120]}")
        return None


def search_aliexpress(keyword: str, page: int = 1) -> list:
    """
    Search AliExpress via Oxylabs Web Scraper API (universal scraper).
    Renders the page with a US residential proxy, returns full HTML.
    Parses the embedded _dida_config_ JSON to extract real product data.
    Retries once with alternate URL if first attempt fails.
    """
    clean_keyword = re.sub(r"[^a-z0-9\s-]", "", keyword.lower()).strip()
    clean_keyword = re.sub(r"\s+", "-", clean_keyword)

    # Primary URL: /w/wholesale- format (most reliable)
    urls = [
        f"https://www.aliexpress.com/w/wholesale-{clean_keyword}.html"
        f"?page={page}&g=y&SortType=total_tranpro_desc",
        f"https://www.aliexpress.com/wholesale"
        f"?SearchText={urllib.parse.quote(keyword)}&SortType=total_tranpro_desc&page={page}",
    ]

    print(f"    🔍 Searching: {urls[0][:90]}...")

    for attempt, url in enumerate(urls, 1):
        html = _oxylabs_fetch(url)
        if html is None:
            if attempt == 1:
                print(f"    ⏳ Attempt {attempt} failed — waiting 10s then retrying...")
                time.sleep(10)
            continue

        print(f"    🔧 Got {len(html):,} chars — parsing HTML...")
        results = _parse_dida_config(html)
        if results:
            return results

        print(f"    ⚠️  No products in response (attempt {attempt})")
        if attempt == 1:
            time.sleep(5)

    print("    ⚠️  All attempts failed — no results")
    return []


def _oxylabs_fetch(url: str, retries: int = 2) -> str | None:
    """
    Fetch a URL via Oxylabs universal scraper with US geo.
    Returns rendered HTML string, or None on failure.
    Automatically retries on transient errors.
    """
    endpoint = "https://realtime.oxylabs.io/v1/queries"
    payload  = {
        "source":       "universal",
        "url":          url,
        "geo_location": "United States",
        "render":       "html",           # JS rendering ON
        "user_agent_type": "desktop",
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                endpoint,
                auth=(OXYLABS_USER, OXYLABS_PASS),
                json=payload,
                timeout=60,
            )
        except requests.exceptions.Timeout:
            print(f"    ⚠️  Oxylabs timeout (attempt {attempt}/{retries})")
            if attempt < retries:
                time.sleep(5)
            continue
        except requests.exceptions.RequestException as e:
            print(f"    ⚠️  Oxylabs request error: {e}")
            if attempt < retries:
                time.sleep(5)
            continue

        if resp.status_code == 200:
            try:
                data    = resp.json()
                results = data.get("results", [])
                if results:
                    content = results[0].get("content", "")
                    if len(content) > 10000:
                        return content
                    print(f"    ⚠️  Response too short ({len(content)} chars)")
            except Exception as e:
                print(f"    ⚠️  Oxylabs JSON parse error: {e}")
        elif resp.status_code == 401:
            print(f"    ❌ Oxylabs 401 — check OXYLABS_USER / OXYLABS_PASS in .env")
            return None   # No point retrying auth failure
        elif resp.status_code == 429:
            wait = 30 * attempt
            print(f"    ⚠️  Oxylabs rate limit (429) — waiting {wait}s...")
            time.sleep(wait)
        else:
            body_preview = resp.text[:150] if resp.text else ""
            print(f"    ⚠️  Oxylabs HTTP {resp.status_code}: {body_preview}")

        if attempt < retries:
            time.sleep(3)

    return None


def _parse_dida_config(html: str) -> list:
    """
    Extract products from AliExpress page HTML.
    Parses window._dida_config_ script tag which contains
    itemList.content[] — the full structured product array.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Find the large _dida_config_ script (always 400K+ chars with itemList)
    dida_script = None
    for s in soup.find_all("script"):
        text = s.string or ""
        if "_dida_config_" in text and "itemList" in text and len(text) > 50000:
            dida_script = text
            break

    if not dida_script:
        # Fallback: search raw HTML directly (sometimes not inside <script> tag)
        m = re.search(r"_dida_config_[^;]*itemList.{0,50000}", html, re.DOTALL)
        if m:
            dida_script = m.group(0)
        else:
            return []

    # Extract itemList.content array
    m = re.search(r'"itemList"\s*:\s*\{[^[]*(\[.+)', dida_script, re.DOTALL)
    if not m:
        return []

    raw = m.group(1)
    # Walk brackets to find end of the array
    depth, end = 0, 0
    for i, c in enumerate(raw):
        if   c == "[": depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        items = json.loads(raw[:end])
    except json.JSONDecodeError as e:
        print(f"    ⚠️  itemList JSON parse error: {e}")
        return []

    # Filter out ads and non-product items
    products = [p for p in items
                if p.get("productId") and
                p.get("itemType") not in ("banner", "ad", "promotion")]

    print(f"    📦 Found {len(products)} products via _dida_config_")
    return [_parse_dida_product(p) for p in products[:10]]


def _parse_dida_product(item: dict) -> dict:
    """
    Parse one product from AliExpress _dida_config_ itemList.content[].

    Confirmed field map (from debug output):
      prices.salePrice.minPrice      → sale price USD (float)
      prices.originalPrice.minPrice  → original price USD (float)
      title.displayTitle             → product name
      trade.tradeDesc                → "100K+ sold"
      trace.utLogMap.real_trade_count→ exact integer order count
      evaluation.starRating          → rating float
      image.imgUrl                   → main image (// prefix)
      productId                      → AliExpress product ID
      rainbow.title                  → store name (often empty)
    """
    # ── Price ──────────────────────────────────────────────────────────────
    prices       = item.get("prices") or {}
    sale         = prices.get("salePrice")     or {}
    original     = prices.get("originalPrice") or {}
    builder_type = prices.get("builderType", "")  # "skuCoupon" = Welcome Deal

    # Welcome Deal / new-buyer-only prices use builderType="skuCoupon" and are
    # capped at 1pc/shopper — unusable for dropshipping. Use originalPrice instead.
    is_welcome_deal = (
        builder_type == "skuCoupon" or
        "welcome" in str(prices.get("prefix", "")).lower() or
        sale.get("discount", 0) >= 85   # 85%+ discount is always a welcome deal
    )

    if is_welcome_deal:
        # Use original price — the real repeatable cost
        cost_min = _safe_float(original.get("minPrice"))
        cost_max = cost_min
    else:
        cost_min = _safe_float(sale.get("minPrice")) or _safe_float(original.get("minPrice"))
        cost_max = _safe_float(original.get("minPrice")) or cost_min

    # ── Title ──────────────────────────────────────────────────────────────
    title_raw = item.get("title") or {}
    title     = (title_raw.get("displayTitle") if isinstance(title_raw, dict)
                 else str(title_raw))
    title     = (title or "")[:200]

    # ── URL ────────────────────────────────────────────────────────────────
    product_id = str(item.get("productId") or "")
    url        = item.get("productDetailUrl") or ""
    if url and not url.startswith("http"):
        url = "https:" + url
    # Use clean direct product URL (avoids broken bundle redirects)
    if product_id:
        url = f"https://www.aliexpress.com/item/{product_id}.html"

    # ── Image ──────────────────────────────────────────────────────────────
    img_raw = item.get("image") or {}
    img_url = (img_raw.get("imgUrl") if isinstance(img_raw, dict) else str(img_raw)) or ""
    if img_url and not img_url.startswith("http"):
        img_url = "https:" + img_url

    # ── Rating ─────────────────────────────────────────────────────────────
    evaluation = item.get("evaluation") or {}
    rating     = _safe_float(evaluation.get("starRating") if isinstance(evaluation, dict)
                             else evaluation)

    # ── Orders — prefer exact count from trace ─────────────────────────────
    trade      = item.get("trade") or {}
    trade_desc = (trade.get("tradeDesc") if isinstance(trade, dict) else str(trade)) or ""
    trace      = item.get("trace") or {}
    utlog      = (trace.get("utLogMap") if isinstance(trace, dict) else {}) or {}
    orders     = _safe_int(utlog.get("real_trade_count")) or _parse_trade_count(trade_desc)

    # ── Store ──────────────────────────────────────────────────────────────
    rainbow    = item.get("rainbow") or {}
    store_name = (rainbow.get("title") if isinstance(rainbow, dict) else None)
    if not store_name:
        for key in ("storeInfo", "store", "seller", "shopInfo"):
            si = item.get(key)
            if isinstance(si, dict):
                store_name = (si.get("storeName") or si.get("name") or si.get("shopName"))
                if store_name:
                    break

    return {
        "product_id":   product_id,
        "is_welcome_deal": is_welcome_deal,
        "title":        title,
        "cost_min":     cost_min,
        "cost_max":     cost_max,
        "url":          url or None,
        "image_url":    img_url or None,
        "rating":       rating,
        "review_count": None,
        "orders":       orders,
        "ship_cost":    None,
        "ship_days":    ALIEXPRESS_SHIP_DAYS,
        "store_name":   store_name or "AliExpress Seller",
        "store_url":    None,
        "store_years":  None,
        "store_score":  None,
    }


def _parse_trade_count(desc: str) -> int | None:
    """Convert "100K+ sold" → 100000, "2,345 sold" → 2345"""
    if not desc:
        return None
    desc = desc.upper().replace(",", "")
    m = re.search(r"([\d.]+)\s*([KMB]?)\+?", desc)
    if not m:
        return None
    try:
        num = float(m.group(1))
        suffix = m.group(2)
        if suffix == "K": num *= 1_000
        elif suffix == "M": num *= 1_000_000
        elif suffix == "B": num *= 1_000_000_000
        return int(num)
    except ValueError:
        return None


def _parse_price(value) -> float | None:
    """
    Parse price from any format:
      '$12.50'  '12.50 - 15.00'  12.5  'US $3.99'
    Returns the minimum (first) number found, or None.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    cleaned = re.sub(r'[^\d.\-]', ' ', str(value))
    numbers = re.findall(r'\d+\.?\d*', cleaned)
    if not numbers:
        return None
    try:
        v = float(numbers[0])
        return v if v > 0 else None
    except ValueError:
        return None

def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None

def _safe_int(v) -> int | None:
    try:
        # handle "1,234 sold" style strings
        cleaned = re.sub(r'[^\d]', '', str(v))
        return int(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  PROFIT CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def calc_profit(tiktok_price: float, supplier_cost: float,
                ship_cost: float = 0.0) -> dict:
    """
    Full profit breakdown including all TikTok fees.
    """
    tiktok_fee  = tiktok_price * TIKTOK_FEE_PCT
    payment_fee = tiktok_price * PAYMENT_FEE_PCT
    total_cost  = supplier_cost + CUSTOMER_SHIP + ship_cost + tiktok_fee + payment_fee
    net_profit  = tiktok_price - total_cost
    margin_pct  = (net_profit / tiktok_price * 100) if tiktok_price > 0 else 0
    roi_pct     = (net_profit / total_cost * 100)   if total_cost  > 0 else 0

    return {
        "net_profit":     round(net_profit, 2),
        "margin_pct":     round(margin_pct, 1),
        "roi_pct":        round(roi_pct, 1),
        "total_cost":     round(total_cost, 2),
        "tiktok_fee":     round(tiktok_fee, 2),
        "payment_fee":    round(payment_fee, 2),
    }

def margin_label(pct: float) -> str:
    if pct >= 50: return "🟢 Excellent"
    if pct >= 35: return "🟢 Great"
    if pct >= 25: return "🟡 Good"
    if pct >= 15: return "🟠 Low"
    return "🔴 Poor"


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD & SAVE SUPPLIER RECORD  (all columns filled)
# ══════════════════════════════════════════════════════════════════════════════

def build_supplier_record(product_id: str, item: dict, profit: dict) -> dict:
    """
    Build a full supplier record using ALL available columns
    from the Supabase suppliers table schema.
    """
    return {
        # ── Identity ──────────────────────────────────────────────────────
        "id":                       str(uuid.uuid4()),
        "tiktok_product_id":        product_id,
        "platform":                 "aliexpress",

        # ── Supplier info ──────────────────────────────────────────────────
        "supplier_name":            (item.get("store_name") or "Unknown")[:100],
        "product_title":            (item.get("title")      or "Unknown")[:200],
        "supplier_url":             item.get("url"),
        "image_url":                item.get("image_url"),

        # ── Pricing ────────────────────────────────────────────────────────
        "unit_cost_min":            item.get("cost_min"),
        "unit_cost_max":            item.get("cost_max"),

        # ── Order info ────────────────────────────────────────────────────
        "moq":                      1,

        # ── Shipping ──────────────────────────────────────────────────────
        "shipping_estimate_days":   item.get("ship_days") or ALIEXPRESS_SHIP_DAYS,
        "shipping_cost_estimate":   item.get("ship_cost"),

        # ── Trust signals ─────────────────────────────────────────────────
        "supplier_trust_score":     item.get("store_score"),
        "years_on_platform":        item.get("store_years"),
        "review_count":             item.get("review_count"),
        "rating":                   item.get("rating"),
        "total_orders":             item.get("orders"),

        # ── Profit metrics ────────────────────────────────────────────────
        "profit_margin_pct":        profit.get("margin_pct"),
        "net_profit_usd":           profit.get("net_profit"),
        "roi_pct":                  profit.get("roi_pct"),

        # ── Product metadata ──────────────────────────────────────────────
        "aliexpress_product_id":    item.get("product_id") or None,
        "whitelabel_available":     False,

        # ── Timestamps ────────────────────────────────────────────────────
        "created_at":               datetime.now(timezone.utc).isoformat(),
        "updated_at":               datetime.now(timezone.utc).isoformat(),
    }

def save_supplier(product_id: str, item: dict, profit: dict, dry_run: bool) -> bool:
    record = build_supplier_record(product_id, item, profit)

    # Strip None/empty values — Supabase rejects wrong types on nullable cols
    record = {k: v for k, v in record.items()
              if v is not None and v != "" and v != []}

    if dry_run:
        store = (item.get("store_name") or "Unknown")[:35]
        cost  = item.get("cost_min", 0) or 0
        print(f"    [DRY RUN] Would save: {store:<35} "
              f"cost ${cost:.2f}  margin {profit['margin_pct']:.1f}%")
        return True

    try:
        sb_insert("suppliers", record)
        return True
    except requests.exceptions.HTTPError as e:
        # Print full Supabase error for debugging
        try:
            err_body = e.response.json()
            print(f"    ❌ DB error: {err_body.get('message', str(e))}")
            if err_body.get("details"):
                print(f"       Details: {err_body['details']}")
        except Exception:
            print(f"    ❌ DB insert failed: {e}")
        return False
    except Exception as e:
        print(f"    ❌ Unexpected error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  PROCESS ONE PRODUCT
# ══════════════════════════════════════════════════════════════════════════════

def process_product(product: dict, min_margin: float, dry_run: bool) -> dict:
    product_id   = product["id"]
    name         = (product.get("product_name") or "Unknown")
    tiktok_price = float(product.get("tiktok_price") or 0)

    # ── Build smart search keyword ─────────────────────────────────────────
    category = product.get('category', '')
    keyword  = _build_search_keyword(name, category)
    print(f'    🏷️  Keyword: "{keyword}"')

    # ── Search AliExpress ──────────────────────────────────────────────────
    items = search_aliexpress(keyword)

    if not items:
        return {"status": "no_results", "saved": 0}

    print(f"    📊 Scoring {len(items)} results  (TikTok: ${tiktok_price:.2f})")

    # ── Score each item ────────────────────────────────────────────────────
    scored = []
    for item in items:
        cost = item.get("cost_min")
        if not cost or cost <= 0:
            print(f"    ⚠️  No price for: {item.get('title','?')[:40]}")
            continue
        if tiktok_price <= 0:
            print(f"    ⚠️  Product has no TikTok price")
            continue
        if cost >= tiktok_price:
            print(f"    ⚠️  Cost ${cost:.2f} ≥ TikTok ${tiktok_price:.2f} — skipping")
            continue
        if cost < 0.10:
            print(f"    ⚠️  Cost ${cost:.2f} too low — likely sample price, skipping")
            continue

        # Use actual shipping cost in calc if available
        ship = item.get("ship_cost") or 0.0
        profit = calc_profit(tiktok_price, cost, ship)

        store = (item.get("store_name") or "Unknown")[:28]
        orders_txt  = f"{item['orders']:,} orders" if item.get("orders") else "? orders"
        rating_txt  = f"⭐{item['rating']:.1f}" if item.get("rating") else ""
        welcome_txt = " 🎁welcome" if item.get("is_welcome_deal") else ""

        print(f"    {'  ' + store:<30} "
              f"${cost:>6.2f}{welcome_txt}  "
              f"margin {profit['margin_pct']:>5.1f}%  "
              f"{margin_label(profit['margin_pct'])}  "
              f"{orders_txt}  {rating_txt}")

        scored.append({"item": item, "profit": profit})

    if not scored:
        return {"status": "no_valid_prices", "saved": 0}

    # ── Sort: primary = margin, secondary = orders (social proof) ──────────
    scored.sort(
        key=lambda x: (
            x["profit"]["margin_pct"],
            x["item"].get("orders") or 0
        ),
        reverse=True
    )

    # ── Deduplicate by AliExpress product_id ──────────────────────────────
    seen_ids, deduped = set(), []
    for s in scored:
        pid = s["item"].get("product_id") or ""
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        deduped.append(s)
    scored = deduped

    # ── Filter by minimum margin ───────────────────────────────────────────
    qualified = [s for s in scored if s["profit"]["margin_pct"] >= min_margin]

    if not qualified:
        best = scored[0]["profit"]["margin_pct"]
        print(f"    ⚠️  Best margin {best:.1f}% < threshold {min_margin}% — skipping")
        return {"status": "below_margin", "saved": 0, "best_margin": best}

    # ── Save top MAX_SUPPLIERS ─────────────────────────────────────────────
    top     = qualified[:MAX_SUPPLIERS]
    saved   = 0

    print(f"    💾 Saving top {len(top)} supplier(s)...")
    for entry in top:
        ok = save_supplier(product_id, entry["item"], entry["profit"], dry_run)
        if ok:
            saved += 1
            store  = (entry["item"].get("store_name") or "Unknown")[:40]
            margin = entry["profit"]["margin_pct"]
            cost   = entry["item"].get("cost_min", 0)
            print(f"    ✅ Saved: {store:<40}  ${cost:.2f}  {margin:.1f}%")

    return {
        "status":       "ok",
        "saved":        saved,
        "best_margin":  top[0]["profit"]["margin_pct"],
        "best_cost":    top[0]["item"].get("cost_min", 0),
        "tiktok_price": tiktok_price,
        "product_name": name,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def _test_oxylabs_token():
    """Test Oxylabs credentials with a simple request."""
    print(f"  User: {OXYLABS_USER}")
    try:
        resp = requests.post(
            "https://realtime.oxylabs.io/v1/queries",
            auth=(OXYLABS_USER, OXYLABS_PASS),
            json={"source": "universal", "url": "https://ip.oxylabs.io/location"},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            ip   = data.get("results", [{}])[0].get("content", "?")[:80]
            print(f"  ✅ Oxylabs working — IP/location: {ip.strip()}")
        elif resp.status_code == 401:
            print(f"  ❌ Oxylabs 401 — wrong username or password")
            print(f"     Check OXYLABS_USER and OXYLABS_PASS in your .env file")
        else:
            print(f"  ⚠️  Oxylabs HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"  ❌ Oxylabs test failed: {e}")
    print()


def run(limit: int, min_margin: float, refill_only: bool,
        delay: float, dry_run: bool):

    tag = "  [DRY RUN]" if dry_run else ""
    print("═" * 66)
    print(f"  TikProfitPro — AliExpress Supplier Pipeline{tag}")
    print("═" * 66)
    print(f"  Products limit    : {limit}")
    print(f"  Min margin        : {min_margin}%")
    print(f"  Refill missing    : {refill_only}")
    print(f"  Max suppliers     : {MAX_SUPPLIERS} per product")
    print(f"  Request delay     : {delay}s")
    print(f"  Dry run           : {dry_run}")
    print("═" * 66)

    # ── Validate credentials ───────────────────────────────────────────────
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ Missing SUPABASE_URL or SUPABASE_KEY in .env file")
        return
    if not OXYLABS_USER or not OXYLABS_PASS:
        print("❌ Missing OXYLABS_USER or OXYLABS_PASS in .env file")
        return

    # ── Show .env load diagnostics ────────────────────────────────────────────
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    print("\n🔧 .env path: " + env_path)
    key_status = ("✅ loaded (" + ANTHROPIC_API_KEY[:12] + "...)") if ANTHROPIC_API_KEY else "❌ NOT FOUND"
    print("   ANTHROPIC_API_KEY: " + key_status)
    print("   OXYLABS_USER:      " + ("✅ " + OXYLABS_USER if OXYLABS_USER else "❌ NOT FOUND"))

    # ── Test Oxylabs credentials ─────────────────────────────────────────────
    print("\n🔑 Testing Oxylabs credentials...")
    _test_oxylabs_token()

    # ── Fetch products ─────────────────────────────────────────────────────
    print(f"\n📦 Fetching products from Supabase...")
    try:
        products = fetch_products(limit)
    except Exception as e:
        print(f"❌ Cannot fetch products: {e}")
        return

    if not products:
        print("❌ No active products found.")
        return

    print(f"✅ {len(products)} products loaded\n")

    # ── Counters ───────────────────────────────────────────────────────────
    total        = len(products)
    processed    = 0
    skipped      = 0
    ok_count          = 0
    no_results        = 0
    consecutive_fails = 0
    below_margin = 0
    no_price     = 0
    total_saved  = 0
    results      = []

    # ── Process each product ───────────────────────────────────────────────
    for i, product in enumerate(products, 1):
        name         = (product.get("product_name") or "Unknown")[:55]
        tiktok_price = float(product.get("tiktok_price") or 0)
        product_id   = product["id"]

        print(f"\n{'─' * 66}")
        print(f"  [{i}/{total}]  {name}")
        print(f"  Price: ${tiktok_price:.2f}  |  "
              f"Virality: {product.get('virality_score', '?')}  |  "
              f"Trend: {product.get('trend_phase', '?')}")

        # Skip: no TikTok price
        if tiktok_price <= 0:
            print("  ⏭️  No TikTok price — skipping")
            skipped += 1
            no_price += 1
            continue

        # Skip: already has AliExpress suppliers (refill mode)
        if refill_only:
            existing = aliexpress_supplier_count(product_id)
            if existing >= MAX_SUPPLIERS:
                print(f"  ⏭️  Already has {existing} AliExpress supplier(s) — skipping")
                skipped += 1
                continue
            elif existing > 0:
                needed = MAX_SUPPLIERS - existing
                print(f"  ℹ️  Has {existing} AliExpress supplier(s), need {needed} more")

        processed += 1
        result = process_product(product, min_margin, dry_run)
        results.append({**result, "product_name": name})

        # Tally
        s = result["status"]
        if s == "ok":
            ok_count         += 1
            total_saved      += result.get("saved", 0)
            consecutive_fails = 0   # reset on success
            print(f"  ✅ Done — {result['saved']} supplier(s) saved  "
                  f"(best margin: {result['best_margin']:.1f}%)")
        elif s == "no_results":
            no_results        += 1
            consecutive_fails += 1
            print("  ❌ No AliExpress results found")
        elif s in ("below_margin", "no_valid_prices"):
            below_margin      += 1
            consecutive_fails  = 0

        # Circuit breaker: 3 consecutive 403 failures → long pause
        if consecutive_fails >= 3:
            print(f"\n  ⚠️  {consecutive_fails} consecutive failures — pausing 60s to avoid rate limit...")
            time.sleep(60)
            consecutive_fails = 0

        # Normal between-product delay
        if i < total:
            time.sleep(delay)

    # ── Final Summary ──────────────────────────────────────────────────────
    print(f"\n{'═' * 66}")
    print("  PIPELINE COMPLETE")
    print(f"{'═' * 66}")
    print(f"  Products total         : {total}")
    print(f"  Processed              : {processed}")
    print(f"  Skipped (no price)     : {no_price}")
    print(f"  Skipped (already full) : {skipped - no_price}")
    print(f"  ✅ Suppliers found     : {ok_count} products")
    print(f"  ❌ No AliExpress results: {no_results}")
    print(f"  ⚠️  Below {min_margin}% margin    : {below_margin}")
    print(f"  💾 Total rows saved    : {total_saved}")
    print(f"{'═' * 66}")

    # ── Top 10 by margin ───────────────────────────────────────────────────
    winners = sorted(
        [r for r in results if r.get("status") == "ok"],
        key=lambda x: x.get("best_margin", 0),
        reverse=True
    )[:10]

    if winners:
        print(f"\n🏆  TOP {len(winners)} MOST PROFITABLE PRODUCTS")
        print(f"  {'Product':<42}  {'TikTok':>7}  {'Cost':>7}  {'Margin':>8}")
        print(f"  {'─' * 42}  {'─' * 7}  {'─' * 7}  {'─' * 8}")
        for r in winners:
            print(f"  {r.get('product_name', '')[:42]:<42}  "
                  f"${r.get('tiktok_price', 0):>6.2f}  "
                  f"${r.get('best_cost', 0):>6.2f}  "
                  f"{r.get('best_margin', 0):>7.1f}%")

    if dry_run:
        print("\n⚠️  DRY RUN — nothing was written to Supabase")
    print(f"\n{'═' * 66}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TikProfitPro — AliExpress Auto Supplier Pipeline"
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max products to process (default 50)"
    )
    parser.add_argument(
        "--min-margin", type=float, default=MIN_MARGIN_PCT,
        help=f"Min profit margin %% to save supplier (default {MIN_MARGIN_PCT})"
    )
    parser.add_argument(
        "--refill", action="store_true",
        help="Only process products missing AliExpress supplier(s)"
    )
    parser.add_argument(
        "--delay", type=float, default=8.0,
        help="Seconds between Oxylabs requests (default 8.0)"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Process first 3 products only (quick test)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without writing anything to Supabase"
    )
    args = parser.parse_args()

    if args.test:
        print("🧪  TEST MODE — first 3 products only\n")
        args.limit = 3

    run(
        limit      = args.limit,
        min_margin = args.min_margin,
        refill_only= args.refill,
        delay      = args.delay,
        dry_run    = args.dry_run,
    )   