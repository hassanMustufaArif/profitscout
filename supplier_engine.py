"""
ProfitScout — Supplier Matching Engine v4
==========================================
Changes from v3:
- Replaced Apify with ScraperAPI (~10x cheaper)
- Replaced GPT-4o ranking with GPT-4o-mini (~60x cheaper)
- Total cost per 3-platform search: ~$0.036 vs $0.37–0.73 originally

Cost breakdown per search:
- ScraperAPI: $0.001 per request × ~35 requests = $0.035
- GPT-4o-mini normalize: $0.0001
- GPT-4o-mini rank: $0.0003
- Total: ~$0.036 per full 3-platform search

Setup:
- Add SCRAPERAPI_KEY=your_key to .env
- Get free key at https://www.scraperapi.com (1,000 free calls)
- Hobby plan $29/month = 250,000 calls (covers 50 Pro users easily)
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode, quote_plus

from openai import OpenAI
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ─── CLIENTS ──────────────────────────────────────────────────────────────────
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")
SCRAPERAPI_BASE = "https://api.scraperapi.com"

# Request settings
REQUEST_TIMEOUT = 60      # seconds per request
MAX_RETRIES     = 2       # retries on failure
RETRY_DELAY     = 3       # seconds between retries


# ==============================================================================
# SCRAPERAPI — Core Request Function
# ==============================================================================
def scraper_get(url: str, render_js: bool = False, country: str = "us") -> requests.Response | None:
    """
    Make a GET request via ScraperAPI.
    render_js=True for JavaScript-heavy pages (costs 5 credits instead of 1).
    """
    params = {
        "api_key":    SCRAPERAPI_KEY,
        "url":        url,
        "country_code": country,
    }
    if render_js:
        params["render"] = "true"

    api_url = f"{SCRAPERAPI_BASE}?{urlencode(params)}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(api_url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response
            elif response.status_code == 429:
                print(f"    Rate limited — waiting {RETRY_DELAY * attempt}s...")
                time.sleep(RETRY_DELAY * attempt)
            elif response.status_code == 403:
                print(f"    Blocked (403) — page may require login")
                return None
            else:
                print(f"    HTTP {response.status_code} on attempt {attempt}")
                time.sleep(RETRY_DELAY)
        except requests.Timeout:
            print(f"    Timeout on attempt {attempt}")
            time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"    Request error on attempt {attempt}: {e}")
            time.sleep(RETRY_DELAY)

    print(f"    All {MAX_RETRIES} attempts failed for: {url[:80]}")
    return None


def scraper_post(url: str, payload: dict) -> requests.Response | None:
    """Make a POST request via ScraperAPI structured data endpoint."""
    api_url = f"{SCRAPERAPI_BASE}/structured/alibaba/search"
    params  = {"api_key": SCRAPERAPI_KEY}

    try:
        response = requests.post(api_url, params=params, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response
        print(f"    POST {response.status_code}: {response.text[:200]}")
        return None
    except Exception as e:
        print(f"    POST error: {e}")
        return None


# ==============================================================================
# CREDIT SYSTEM
# ==============================================================================
def check_credits(user_email: str) -> dict:
    """Check user's remaining credits."""
    try:
        result = supabase.table("user_credits").select("*").eq("user_email", user_email).execute()
        if not result.data:
            return {"credits_remaining": 0, "plan": "free", "credits_total": 0, "credits_used": 0}

        record     = result.data[0]
        now        = datetime.now(timezone.utc)
        period_end = datetime.fromisoformat(record["period_end"].replace("Z", "+00:00"))

        if now > period_end:
            import datetime as dt
            new_total  = 50 if record["plan"] == "pro" else 120 if record["plan"] == "ultimate" else 0
            next_month = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            supabase.table("user_credits").update({
                "credits_used":  0,
                "credits_total": new_total,
                "period_start":  now.isoformat(),
                "period_end":    next_month.isoformat()
            }).eq("user_email", user_email).execute()
            return {"credits_remaining": new_total, "plan": record["plan"], "credits_total": new_total, "credits_used": 0}

        remaining = max(record["credits_total"] - record["credits_used"], 0)
        return {
            "credits_remaining": remaining,
            "plan":              record["plan"],
            "credits_total":     record["credits_total"],
            "credits_used":      record["credits_used"]
        }
    except Exception as e:
        print(f"Credit check error: {e}")
        return {"credits_remaining": 0, "plan": "free", "credits_total": 0, "credits_used": 0}


def deduct_credit(user_email: str, product_id: str, platform: str) -> bool:
    """Deduct 1 credit. Returns True if successful."""
    try:
        result = supabase.table("user_credits").select("credits_used").eq("user_email", user_email).execute()
        if not result.data:
            return False

        current_used = result.data[0]["credits_used"]
        supabase.table("user_credits").update({
            "credits_used": current_used + 1
        }).eq("user_email", user_email).execute()

        # Log — non-fatal if table missing
        try:
            supabase.table("credit_logs").insert({
                "user_email":        user_email,
                "product_id":        product_id,
                "platform":          platform,
                "credits_deducted":  1
            }).execute()
        except Exception as log_err:
            print(f"Credit log warning (non-fatal): {log_err}")

        return True
    except Exception as e:
        print(f"Credit deduction error: {e}")
        return False


def initialize_credits(user_email: str, plan: str):
    """Create or update credits when user subscribes."""
    import datetime as dt
    credits    = 50 if plan == "pro" else 120 if plan == "ultimate" else 0
    now        = datetime.now(timezone.utc)
    next_month = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)

    try:
        existing = supabase.table("user_credits").select("id").eq("user_email", user_email).execute()
        data = {
            "plan":          plan,
            "credits_total": credits,
            "credits_used":  0,
            "period_start":  now.isoformat(),
            "period_end":    next_month.isoformat()
        }
        if existing.data:
            supabase.table("user_credits").update(data).eq("user_email", user_email).execute()
        else:
            data["user_email"] = user_email
            supabase.table("user_credits").insert(data).execute()
    except Exception as e:
        print(f"Credit initialization error: {e}")


# ==============================================================================
# PROFIT CALCULATOR
# ==============================================================================
def calculate_profit(
    sale_price: float,
    cogs: float,
    affiliate_commission_rate: float = 15.0,
    fbt_enabled: bool = True,
    product_weight_oz: float = 8.0,
    inbound_shipping: float = 1.20,
    return_rate_pct: float = 8.0,
    ad_spend_per_unit: float = 0.0,
    is_new_seller: bool = False,
) -> dict:
    referral_rate        = 0.03 if is_new_seller else 0.06
    referral_fee         = sale_price * referral_rate
    transaction_fee      = sale_price * 0.029
    affiliate_commission = sale_price * (affiliate_commission_rate / 100)

    if fbt_enabled:
        if product_weight_oz <= 8:   fbt_fee = 3.15
        elif product_weight_oz <= 16: fbt_fee = 3.75
        elif product_weight_oz <= 32: fbt_fee = 5.25
        elif product_weight_oz <= 48: fbt_fee = 6.50
        else:                         fbt_fee = 8.00
    else:
        fbt_fee = 0.0

    return_buffer = sale_price * (return_rate_pct / 100)
    total_fees    = (referral_fee + transaction_fee + affiliate_commission +
                     fbt_fee + inbound_shipping + return_buffer + ad_spend_per_unit)
    total_cost    = cogs + total_fees
    net_profit    = sale_price - total_cost
    margin_pct    = (net_profit / sale_price * 100) if sale_price > 0 else 0

    if margin_pct >= 35:   rating, label = "green",  "Highly Profitable"
    elif margin_pct >= 20: rating, label = "green",  "Profitable"
    elif margin_pct >= 10: rating, label = "amber",  "Low Margin"
    elif margin_pct >= 0:  rating, label = "amber",  "Breakeven"
    else:                  rating, label = "red",    "Unprofitable"

    return {
        "sale_price":           round(sale_price, 2),
        "cogs":                 round(cogs, 2),
        "referral_fee":         round(referral_fee, 2),
        "transaction_fee":      round(transaction_fee, 2),
        "affiliate_commission": round(affiliate_commission, 2),
        "fbt_fee":              round(fbt_fee, 2),
        "inbound_shipping":     round(inbound_shipping, 2),
        "return_buffer":        round(return_buffer, 2),
        "total_fees":           round(total_fees, 2),
        "net_profit":           round(net_profit, 2),
        "margin_pct":           round(margin_pct, 1),
        "rating":               rating,
        "profit_label":         label,
    }


# ==============================================================================
# STEP 1 — Normalize Product Title (GPT-4o-mini)
# ==============================================================================
def normalize_product_title(raw_title: str) -> str:
    print(f"  Normalizing: {raw_title[:80]}...")

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """You are a product sourcing expert for Alibaba, AliExpress, and Amazon.
Convert TikTok product titles into precise search keywords that find the SAME product on supplier platforms.

Rules:
- Keep core product identity — what the product IS
- Keep: target audience, key ingredient, form factor, mechanism, body part
- Remove ONLY: brand names in brackets [Brand], year numbers, marketing words (viral, official, trending, best, amazing)
- Keep: health terms, ingredients, product form (capsule, serum, patch, roller, device, lamp, brush)
- Output 4-8 words — specific enough to find the right product
- Output ONLY the search keyword, nothing else, no quotes

Examples:
Happy V Vaginal Prebiotic + Probiotic for Women 60 Vegan Capsules → women vaginal probiotic supplement capsules
[Dr.Melaxin Official] Cemenrete Calcium Multi Balm Eye Care 2024 → calcium eye balm stick
COSRX Peptide Collagen Hydrogel Eye Patch 60 Patches → collagen hydrogel eye patch
Portable Mini Face Massager Roller Skincare Tool → face massager roller tool
LED Sunset Projection Lamp Moon Room Decor → LED sunset projection night lamp
Magnetic Eyelashes Kit Professional Waterproof Women → magnetic false eyelashes kit
Knee Pain Relief Red Light Therapy Wrap Device → red light therapy knee wrap device
Scalp Massager Shampoo Brush Hair Growth Stimulator → scalp massager hair growth brush"""
                },
                {"role": "user", "content": raw_title}
            ],
            max_tokens=40,
            temperature=0
        )
        normalized = response.choices[0].message.content.strip().lower().strip('"\'')
        print(f"  → '{normalized}'")
        return normalized

    except Exception as e:
        print(f"  GPT normalize failed: {e} — using basic cleanup")
        cleaned = re.sub(r'\[.*?\]', '', raw_title)
        cleaned = re.sub(r'\b20\d{2}\b', '', cleaned)
        cleaned = re.sub(r'[^\w\s]', ' ', cleaned)
        words   = [w for w in cleaned.split() if len(w) > 2][:6]
        return ' '.join(words).lower().strip()


# ==============================================================================
# STEP 2 — Platform Search Functions (ScraperAPI)
# ==============================================================================

def parse_price(value) -> float:
    """Safely parse a price value to float."""
    if value is None:
        return 0.0
    try:
        cleaned = str(value).replace("$", "").replace(",", "").replace("USD", "").strip()
        # Take first number if range like "1.50-3.00"
        match = re.search(r'\d+\.?\d*', cleaned)
        return float(match.group()) if match else 0.0
    except Exception:
        return 0.0


def search_alibaba(keyword: str, max_results: int = 15) -> list:
    """Search Alibaba via ScraperAPI structured endpoint."""
    print(f"  Searching Alibaba for: '{keyword}'...")

    # Try ScraperAPI structured Alibaba endpoint first
    results = _search_alibaba_structured(keyword, max_results)

    if not results:
        print("  Structured endpoint returned 0 — trying search page scrape...")
        results = _search_alibaba_html(keyword, max_results)

    print(f"  Found {len(results)} Alibaba results")
    return results


def _search_alibaba_structured(keyword: str, max_results: int) -> list:
    """Use ScraperAPI's structured Alibaba search."""
    try:
        params = {
            "api_key": SCRAPERAPI_KEY,
            "query":   keyword,
            "country": "us",
        }
        url      = f"https://api.scraperapi.com/structured/alibaba/search?{urlencode(params)}"
        response = requests.get(url, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            return []

        data    = response.json()
        results = data.get("results") or data.get("products") or data.get("items") or []

        # Normalize to internal format
        normalized = []
        for item in results[:max_results]:
            normalized.append({
                "subject":      item.get("name") or item.get("title") or "",
                "moq":          item.get("min_order") or item.get("moq") or 1,
                "supplier_name": item.get("company") or item.get("supplier") or "",
                "url":          item.get("url") or item.get("product_url") or "",
                "image_url":    item.get("image") or item.get("thumbnail") or "",
                "price_min":    parse_price(item.get("price") or item.get("min_price")),
                "price_max":    parse_price(item.get("max_price") or item.get("price")),
                "years":        item.get("years_on_platform") or 0,
                "_source":      "structured",
            })
        return normalized

    except Exception as e:
        print(f"  Structured endpoint error: {e}")
        return []


def _search_alibaba_html(keyword: str, max_results: int) -> list:
    """Fallback: scrape Alibaba search results page HTML and parse with GPT."""
    keyword_encoded = quote_plus(keyword)
    search_url      = f"https://www.alibaba.com/trade/search?SearchText={keyword_encoded}&tab=all"

    response = scraper_get(search_url, render_js=True, country="us")
    if not response:
        return []

    # Use GPT to extract product listings from HTML
    html_snippet = response.text[:8000]  # limit tokens
    return _gpt_extract_from_html(html_snippet, "alibaba", max_results)


def search_aliexpress(keyword: str, max_results: int = 10) -> list:
    """Search AliExpress via ScraperAPI."""
    print(f"  Searching AliExpress for: '{keyword}'...")

    # Try structured endpoint first
    results = _search_aliexpress_structured(keyword, max_results)

    if not results:
        print("  Trying AliExpress HTML scrape...")
        results = _search_aliexpress_html(keyword, max_results)

    print(f"  Found {len(results)} AliExpress results")
    return results


def _search_aliexpress_structured(keyword: str, max_results: int) -> list:
    """Use ScraperAPI's structured AliExpress search."""
    try:
        params = {
            "api_key": SCRAPERAPI_KEY,
            "query":   keyword,
        }
        url      = f"https://api.scraperapi.com/structured/aliexpress/search?{urlencode(params)}"
        response = requests.get(url, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            return []

        data    = response.json()
        results = data.get("results") or data.get("products") or data.get("items") or []

        normalized = []
        for item in results[:max_results]:
            price = parse_price(item.get("price") or item.get("sale_price"))
            normalized.append({
                "title":        item.get("name") or item.get("title") or "",
                "price":        price,
                "seller_name":  item.get("store_name") or item.get("seller") or "AliExpress Seller",
                "url":          item.get("url") or item.get("product_url") or "",
                "image_url":    item.get("image") or item.get("thumbnail") or "",
                "moq":          1,
                "years":        0,
                "_source":      "structured",
            })
        return normalized

    except Exception as e:
        print(f"  AliExpress structured error: {e}")
        return []


def _search_aliexpress_html(keyword: str, max_results: int) -> list:
    """Fallback: scrape AliExpress search HTML."""
    keyword_encoded = quote_plus(keyword)
    search_url      = f"https://www.aliexpress.com/wholesale?SearchText={keyword_encoded}"

    response = scraper_get(search_url, render_js=True, country="us")
    if not response:
        return []

    html_snippet = response.text[:8000]
    return _gpt_extract_from_html(html_snippet, "aliexpress", max_results)


def search_amazon(keyword: str, max_results: int = 10) -> list:
    """Search Amazon via ScraperAPI structured endpoint."""
    print(f"  Searching Amazon for: '{keyword}'...")

    results = _search_amazon_structured(keyword, max_results)

    if not results:
        print("  Trying Amazon HTML scrape...")
        results = _search_amazon_html(keyword, max_results)

    print(f"  Found {len(results)} Amazon results")
    return results


def _search_amazon_structured(keyword: str, max_results: int) -> list:
    """Use ScraperAPI's structured Amazon search."""
    try:
        params = {
            "api_key": SCRAPERAPI_KEY,
            "query":   keyword,
            "country": "us",
        }
        url      = f"https://api.scraperapi.com/structured/amazon/search?{urlencode(params)}"
        response = requests.get(url, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            return []

        data    = response.json()
        results = data.get("results") or data.get("products") or data.get("items") or []

        normalized = []
        for item in results[:max_results]:
            price = parse_price(item.get("price") or item.get("sale_price"))
            normalized.append({
                "title":       item.get("name") or item.get("title") or "",
                "price":       price,
                "brand":       item.get("brand") or item.get("seller") or "Amazon Seller",
                "url":         item.get("url") or item.get("product_url") or "",
                "image_url":   item.get("image") or item.get("thumbnail") or "",
                "rating":      item.get("rating") or 0,
                "reviews":     item.get("reviews_count") or 0,
                "moq":         1,
                "years":       0,
                "_source":     "structured",
            })
        return normalized

    except Exception as e:
        print(f"  Amazon structured error: {e}")
        return []


def _search_amazon_html(keyword: str, max_results: int) -> list:
    """Fallback: scrape Amazon search HTML."""
    keyword_encoded = quote_plus(keyword)
    search_url      = f"https://www.amazon.com/s?k={keyword_encoded}"

    response = scraper_get(search_url, render_js=False, country="us")
    if not response:
        return []

    html_snippet = response.text[:8000]
    return _gpt_extract_from_html(html_snippet, "amazon", max_results)


def _gpt_extract_from_html(html: str, platform: str, max_results: int) -> list:
    """
    Use GPT-4o-mini to extract product listings from raw HTML.
    Fallback when structured endpoints fail.
    """
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""Extract product listings from this {platform} HTML page.
Return a JSON array of up to {max_results} products with these fields:
[{{
  "title": "product title",
  "price": 0.00,
  "supplier_name": "company or seller name",
  "url": "product URL if found",
  "image_url": "image URL if found",
  "moq": 1,
  "years": 0
}}]
Return ONLY valid JSON array. If no products found return []."""
                },
                {"role": "user", "content": html[:6000]}
            ],
            max_tokens=1500,
            temperature=0
        )
        content = response.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        print(f"  GPT HTML extraction error: {e}")
        return []


# ==============================================================================
# STEP 3 — Rank Suppliers with GPT-4o-mini (replaces GPT-4o)
# ==============================================================================
def rank_suppliers(
    raw_suppliers: list,
    tiktok_price: float,
    product_title: str,
    platform: str = "alibaba"
) -> list:
    print(f"  Ranking {len(raw_suppliers)} {platform} suppliers with GPT-4o-mini...")

    if not raw_suppliers:
        return []

    # Build summary for GPT
    summaries = []
    for i, s in enumerate(raw_suppliers[:15]):
        if platform == "alibaba":
            price      = s.get("price_min") or parse_price(s.get("price") or s.get("subject_price") or 0)
            name       = s.get("supplier_name") or (s.get("supplierInfo") or {}).get("name") or "Unknown"
            years      = s.get("years") or (s.get("supplierInfo") or {}).get("joinYears") or 0
            moq        = s.get("moq") or 1
            title      = s.get("subject") or s.get("title") or ""
        elif platform == "aliexpress":
            price      = parse_price(s.get("price") or s.get("sale_price") or 0)
            name       = s.get("seller_name") or s.get("sellerName") or s.get("storeName") or "AliExpress Seller"
            years      = 0
            moq        = 1
            title      = s.get("title") or s.get("name") or ""
        elif platform == "amazon":
            price      = parse_price(s.get("price") or 0)
            name       = s.get("brand") or s.get("sellerName") or "Amazon Seller"
            years      = 0
            moq        = 1
            title      = s.get("title") or s.get("name") or ""
        else:
            price, name, years, moq, title = 0, "Unknown", 0, 1, ""

        summaries.append({
            "index":  i,
            "title":  title[:100],
            "price":  price,
            "moq":    moq,
            "years":  years,
            "seller": name,
        })

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a TikTok Shop sourcing expert.

Target product: {product_title}
TikTok sale price: ${tiktok_price}
Platform: {platform}
Ideal COGS target: under ${round(tiktok_price * 0.30, 2)} (for 30%+ margin)

STEP 1 — FILTER: Remove any supplier whose title does NOT match the target product.
Be strict — wrong product type = exclude regardless of price.

STEP 2 — RANK remaining by:
1. Product match (most important)
2. Price (lower = better profit)
3. Trust (more years = better, for Alibaba)
4. MOQ (lower = better for new sellers)

Return TOP 3 as JSON array:
[{{
  "index": 0,
  "rank_reason": "Exact match, lowest price",
  "recommendation_tag": "Best Value",
  "whitelabel_likely": false,
  "profitable": true,
  "product_match": true
}}]

If fewer than 3 match, return only matching ones.
If none match return [].
Return ONLY valid JSON."""
                },
                {"role": "user", "content": json.dumps(summaries)}
            ],
            max_tokens=500,
            temperature=0
        )

        content = response.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        ranked  = json.loads(content)
        print(f"  GPT-4o-mini selected {len(ranked)} matching suppliers")
        return ranked

    except Exception as e:
        print(f"  Ranking error: {e}")
        return [
            {
                "index":            i,
                "rank_reason":      "Auto-selected",
                "recommendation_tag": f"Option {i+1}",
                "whitelabel_likely": False,
                "profitable":       False,
                "product_match":    False
            }
            for i in range(min(3, len(raw_suppliers)))
        ]


# ==============================================================================
# STEP 4 — Extract Supplier Data
# ==============================================================================
def extract_supplier_data(raw: dict, platform: str) -> dict:
    """Extract and normalize supplier fields from raw search result."""

    if platform == "alibaba":
        # Handle both structured API format and legacy Apify format
        product_title = raw.get("subject") or raw.get("title") or ""
        supplier_name = (
            raw.get("supplier_name")
            or (raw.get("supplierInfo") or {}).get("name")
            or "Unknown Supplier"
        )
        years = (
            raw.get("years")
            or (raw.get("supplierInfo") or {}).get("joinYears")
            or 0
        )
        supplier_url = raw.get("url") or raw.get("supplier_url") or ""

        # MOQ
        moq_raw = raw.get("moq") or 1
        try:
            moq = int(str(moq_raw).replace(",", "").strip())
        except Exception:
            moq = 1

        # Price — use pre-parsed fields if available
        price_min = raw.get("price_min") or 0
        price_max = raw.get("price_max") or 0

        if not price_min and not price_max:
            # Legacy Apify format
            price_obj = raw.get("price") or {}
            ladder    = price_obj.get("productLadderPrices") or []
            if ladder:
                prices    = [parse_price(t.get("dollarPrice")) for t in ladder if t.get("dollarPrice")]
                price_min = min(prices) if prices else 0
                price_max = max(prices) if prices else 0
            else:
                range_p   = price_obj.get("productRangePrices") or {}
                price_min = parse_price(range_p.get("dollarPriceRangeLow"))
                price_max = parse_price(range_p.get("dollarPriceRangeHigh") or range_p.get("dollarPriceRangeLow"))

        # Image — try multiple fields
        image_url = (
            raw.get("image_url")
            or raw.get("imageUrl")
            or raw.get("mainImage")
            or ""
        )
        if not image_url:
            media_items = raw.get("mediaItems") or []
            if media_items:
                img_obj   = media_items[0].get("imageUrl") or {}
                image_url = img_obj.get("normal") or img_obj.get("big") or ""

        # Whitelabel
        basic_props = raw.get("productBasicProperties") or []
        whitelabel  = False
        for prop in basic_props:
            if prop.get("attrName") == "brand name":
                val = str(prop.get("attrValue") or "").upper()
                if any(w in val for w in ["OEM", "ODM", "CUSTOM"]):
                    whitelabel = True

        shipping_days = 20
        shipping_cost = 1.50

    elif platform == "aliexpress":
        product_title = raw.get("title") or raw.get("name") or ""
        supplier_name = raw.get("seller_name") or raw.get("sellerName") or raw.get("storeName") or "AliExpress Seller"
        years         = 0
        supplier_url  = raw.get("url") or raw.get("productUrl") or ""
        moq           = 1
        price_val     = raw.get("price") or raw.get("salePrice") or 0
        price_min     = parse_price(price_val)
        price_max     = price_min
        image_url     = raw.get("image_url") or raw.get("imageUrl") or raw.get("image") or ""
        whitelabel    = False
        shipping_days = 10
        shipping_cost = 0.0

    elif platform == "amazon":
        product_title = raw.get("title") or raw.get("name") or ""
        supplier_name = raw.get("brand") or raw.get("sellerName") or "Amazon Seller"
        years         = 0
        supplier_url  = raw.get("url") or raw.get("productUrl") or ""
        moq           = 1
        price_min     = parse_price(raw.get("price") or 0)
        price_max     = price_min
        image_url     = raw.get("image_url") or raw.get("imageUrl") or raw.get("image") or ""
        whitelabel    = False
        shipping_days = 2
        shipping_cost = 0.0

    else:
        return {}

    return {
        "product_title": product_title[:500],
        "supplier_name": supplier_name,
        "years":         int(years),
        "supplier_url":  supplier_url,
        "moq":           moq,
        "price_min":     round(float(price_min), 2),
        "price_max":     round(float(price_max), 2),
        "image_url":     image_url,
        "whitelabel":    whitelabel,
        "shipping_days": shipping_days,
        "shipping_cost": shipping_cost,
    }


# ==============================================================================
# STEP 5 — Save Suppliers to Supabase
# ==============================================================================
def save_suppliers(
    product_id: str,
    raw_suppliers: list,
    ranked: list,
    tiktok_price: float,
    platform: str = "alibaba"
) -> list:
    print(f"  Saving top {len(ranked)} {platform} suppliers...")

    saved = []
    for rank_info in ranked:
        idx = rank_info.get("index", 0)
        if idx >= len(raw_suppliers):
            continue

        raw    = raw_suppliers[idx]
        data   = extract_supplier_data(raw, platform)
        if not data:
            continue

        cogs        = data["price_min"] if data["price_min"] > 0 else data["price_max"]
        profit_data = calculate_profit(
            sale_price=tiktok_price,
            cogs=cogs,
            inbound_shipping=data["shipping_cost"],
        )

        record = {
            "tiktok_product_id":      product_id,
            "platform":               platform,
            "supplier_name":          data["supplier_name"],
            "product_title":          data["product_title"],
            "unit_cost_min":          data["price_min"],
            "unit_cost_max":          data["price_max"],
            "moq":                    data["moq"],
            "shipping_estimate_days": data["shipping_days"],
            "shipping_cost_estimate": data["shipping_cost"],
            "supplier_trust_score":   min(float(data["years"]) / 2, 5.0),
            "years_on_platform":      data["years"],
            "whitelabel_available":   data["whitelabel"],
            "supplier_url":           data["supplier_url"],
            "image_url":              data["image_url"],
        }

        try:
            result = supabase.table("suppliers").insert(record).execute()
            saved_record = result.data[0] if result.data else record
            saved_record["_rank_reason"]        = rank_info.get("rank_reason", "")
            saved_record["_recommendation_tag"] = rank_info.get("recommendation_tag", "")
            saved_record["_product_match"]      = rank_info.get("product_match", True)
            saved_record["_image_url"]          = data["image_url"]
            saved_record["_profit"]             = profit_data
            saved.append(saved_record)
            print(f"    ✓ {data['supplier_name']} — ${data['price_min']} | {profit_data['profit_label']} ({profit_data['margin_pct']}%)")
        except Exception as e:
            print(f"  Save error: {e}")

    saved.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
    print(f"  Saved {len(saved)} suppliers")
    return saved


# ==============================================================================
# MAIN FUNCTION — Called from FastAPI
# ==============================================================================
def find_suppliers_by_platform(
    product_id: str,
    product_title: str,
    tiktok_price: float,
    platform: str,
    user_email: str
) -> dict:
    """Search a single platform for suppliers. Deducts 1 credit."""

    print(f"\n{'='*55}")
    print(f"[{platform.upper()}] {product_title[:55]}")
    print(f"Price: ${tiktok_price} | User: {user_email}")
    print(f"{'='*55}")

    # Check cache first
    existing = (
        supabase.table("suppliers")
        .select("*")
        .eq("tiktok_product_id", product_id)
        .eq("platform", platform)
        .execute()
    )

    if existing.data:
        print(f"  Cached results found — no credit deducted")
        for s in existing.data:
            cogs = s.get("unit_cost_min") or s.get("unit_cost_max") or 0
            s["_profit"]    = calculate_profit(sale_price=tiktok_price, cogs=cogs)
            s["_image_url"] = s.get("image_url", "")
        existing.data.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
        return {
            "success":         True,
            "cached":          True,
            "platform":        platform,
            "credit_deducted": False,
            "suppliers":       existing.data
        }

    # Check credits
    credit_info = check_credits(user_email)
    if credit_info["credits_remaining"] <= 0:
        return {
            "success":  False,
            "message":  f"No credits remaining. Used {credit_info['credits_used']}/{credit_info['credits_total']} this month. Upgrade for more.",
            "platform": platform,
            "credits":  credit_info,
            "suppliers": []
        }

    # Deduct credit before search
    if not deduct_credit(user_email, product_id, platform):
        return {
            "success":  False,
            "message":  "Failed to deduct credit. Please try again.",
            "platform": platform,
            "suppliers": []
        }

    # Normalize title
    keyword = normalize_product_title(product_title)

    # Search platform
    if platform == "alibaba":
        results = search_alibaba(keyword, max_results=15)
    elif platform == "aliexpress":
        results = search_aliexpress(keyword, max_results=10)
    elif platform == "amazon":
        results = search_amazon(keyword, max_results=10)
    else:
        return {"success": False, "message": f"Unknown platform: {platform}", "suppliers": []}

    if not results:
        return {
            "success":         False,
            "message":         f"No results found on {platform} for '{keyword}'. 1 credit used.",
            "platform":        platform,
            "keyword":         keyword,
            "credit_deducted": True,
            "suppliers":       []
        }

    # Rank with GPT-4o-mini
    ranked = rank_suppliers(results, tiktok_price, product_title, platform)

    if not ranked:
        return {
            "success":         False,
            "message":         f"Found {len(results)} results but none matched '{product_title}'. Try another platform. 1 credit used.",
            "platform":        platform,
            "keyword":         keyword,
            "credit_deducted": True,
            "suppliers":       []
        }

    # Save and return
    saved            = save_suppliers(product_id, results, ranked, tiktok_price, platform)
    profitable_count = sum(1 for s in saved if s.get("_profit", {}).get("margin_pct", 0) >= 20)
    updated_credits  = check_credits(user_email)

    print(f"\n✅ Done — {len(saved)} suppliers, {profitable_count} profitable, keyword: '{keyword}'")

    return {
        "success":            True,
        "cached":             False,
        "platform":           platform,
        "normalized_keyword": keyword,
        "credit_deducted":    True,
        "credits_remaining":  updated_credits["credits_remaining"],
        "profitable_count":   profitable_count,
        "suppliers":          saved
    }


# ==============================================================================
# LEGACY — backward compatibility
# ==============================================================================
def find_suppliers(product_id: str, product_title: str, tiktok_price: float) -> dict:
    return find_suppliers_by_platform(
        product_id=product_id,
        product_title=product_title,
        tiktok_price=tiktok_price,
        platform="alibaba",
        user_email="legacy"
    )
