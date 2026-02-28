"""
ProfitScout — Supplier Matching Engine v3
==========================================
Multi-platform supplier search with credit system.
Searches Alibaba, AliExpress, and Amazon individually.
Each search costs 1 credit.

v3 Changes:
- Better keyword normalization (keeps product identity, not just generic words)
- Image URL now saved to suppliers table
- MOQ accuracy fix (maps price tiers correctly)
- GPT ranking now filters out irrelevant products first
- Cleaner error handling throughout
"""

from apify_client import ApifyClient
from openai import OpenAI
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, timezone
import os
import json

load_dotenv()

# ─── CLIENTS ──────────────────────────────────────────────────────────────────
apify    = ApifyClient(os.getenv("APIFY_API_TOKEN"))
openai   = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)


# ==============================================================================
# CREDIT SYSTEM
# ==============================================================================
def check_credits(user_email: str) -> dict:
    """Check user's remaining credits."""
    try:
        result = supabase.table("user_credits").select("*").eq("user_email", user_email).execute()
        if not result.data:
            return {"credits_remaining": 0, "plan": "free", "credits_total": 0, "credits_used": 0}

        record = result.data[0]
        now = datetime.now(timezone.utc)
        period_end = datetime.fromisoformat(record["period_end"].replace("Z", "+00:00"))

        # If period expired, reset credits
        if now > period_end:
            new_total = 50 if record["plan"] == "pro" else 120 if record["plan"] == "ultimate" else 0
            import datetime as dt
            next_month = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            supabase.table("user_credits").update({
                "credits_used": 0,
                "credits_total": new_total,
                "period_start": now.isoformat(),
                "period_end": next_month.isoformat()
            }).eq("user_email", user_email).execute()
            return {"credits_remaining": new_total, "plan": record["plan"], "credits_total": new_total, "credits_used": 0}

        remaining = record["credits_total"] - record["credits_used"]
        return {
            "credits_remaining": max(remaining, 0),
            "plan": record["plan"],
            "credits_total": record["credits_total"],
            "credits_used": record["credits_used"]
        }
    except Exception as e:
        print(f"Credit check error: {e}")
        return {"credits_remaining": 0, "plan": "free", "credits_total": 0, "credits_used": 0}


def deduct_credit(user_email: str, product_id: str, platform: str) -> bool:
    """Deduct 1 credit and log the usage. Returns True if successful."""
    try:
        result = supabase.table("user_credits").select("credits_used").eq("user_email", user_email).execute()
        if not result.data:
            return False

        current_used = result.data[0]["credits_used"]
        supabase.table("user_credits").update({
            "credits_used": current_used + 1
        }).eq("user_email", user_email).execute()

        # Log the usage — table must exist in Supabase
        try:
            supabase.table("credit_logs").insert({
                "user_email": user_email,
                "product_id": product_id,
                "platform": platform,
                "credits_deducted": 1
            }).execute()
        except Exception as log_err:
            # Don't fail the whole deduction if logging fails
            print(f"Credit log warning (non-fatal): {log_err}")

        return True
    except Exception as e:
        print(f"Credit deduction error: {e}")
        return False


def initialize_credits(user_email: str, plan: str):
    """Create or update credits when user subscribes."""
    credits = 50 if plan == "pro" else 120 if plan == "ultimate" else 0
    now = datetime.now(timezone.utc)
    import datetime as dt
    next_month = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)

    try:
        existing = supabase.table("user_credits").select("id").eq("user_email", user_email).execute()
        if existing.data:
            supabase.table("user_credits").update({
                "plan": plan,
                "credits_total": credits,
                "credits_used": 0,
                "period_start": now.isoformat(),
                "period_end": next_month.isoformat()
            }).eq("user_email", user_email).execute()
        else:
            supabase.table("user_credits").insert({
                "user_email": user_email,
                "plan": plan,
                "credits_total": credits,
                "credits_used": 0,
                "period_start": now.isoformat(),
                "period_end": next_month.isoformat()
            }).execute()
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
    referral_rate = 0.03 if is_new_seller else 0.06
    referral_fee = sale_price * referral_rate
    transaction_fee = sale_price * 0.029
    affiliate_commission = sale_price * (affiliate_commission_rate / 100)

    if fbt_enabled:
        if product_weight_oz <= 8:
            fbt_fee = 3.15
        elif product_weight_oz <= 16:
            fbt_fee = 3.75
        elif product_weight_oz <= 32:
            fbt_fee = 5.25
        elif product_weight_oz <= 48:
            fbt_fee = 6.50
        else:
            fbt_fee = 8.00
    else:
        fbt_fee = 0.0

    return_buffer = sale_price * (return_rate_pct / 100)
    total_fees = referral_fee + transaction_fee + affiliate_commission + fbt_fee + inbound_shipping + return_buffer + ad_spend_per_unit
    total_cost = cogs + total_fees
    net_profit = sale_price - total_cost
    margin_pct = (net_profit / sale_price * 100) if sale_price > 0 else 0

    if margin_pct >= 35:
        rating = "green"
        label = "Highly Profitable"
    elif margin_pct >= 20:
        rating = "green"
        label = "Profitable"
    elif margin_pct >= 10:
        rating = "amber"
        label = "Low Margin"
    elif margin_pct >= 0:
        rating = "amber"
        label = "Breakeven"
    else:
        rating = "red"
        label = "Unprofitable"

    return {
        "sale_price": round(sale_price, 2),
        "cogs": round(cogs, 2),
        "referral_fee": round(referral_fee, 2),
        "transaction_fee": round(transaction_fee, 2),
        "affiliate_commission": round(affiliate_commission, 2),
        "fbt_fee": round(fbt_fee, 2),
        "inbound_shipping": round(inbound_shipping, 2),
        "return_buffer": round(return_buffer, 2),
        "total_fees": round(total_fees, 2),
        "net_profit": round(net_profit, 2),
        "margin_pct": round(margin_pct, 1),
        "rating": rating,
        "profit_label": label,
    }


# ==============================================================================
# STEP 1 — Normalize Product Title (v3: keeps product identity)
# ==============================================================================
def normalize_product_title(raw_title: str) -> str:
    print(f"  Normalizing title: {raw_title[:80]}...")

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """You are a product sourcing expert for Alibaba, AliExpress, and Amazon.
Convert TikTok product titles into precise search keywords that will find the SAME product on supplier platforms.

Rules:
- Keep the CORE product identity — what the product actually IS
- Keep key differentiators: target audience, key ingredient, form factor, mechanism
- Remove ONLY: brand names in brackets like [Brand], year numbers (2024, 2025), pure marketing words (viral, official, trending, best, amazing)
- Keep: health claims if they describe the product, key ingredients, product form (capsule, serum, patch, roller, device, lamp)
- Keep: target body part or condition (vaginal, hair growth, eye, face, knee)
- Output 4-8 words — specific enough to find the right product
- Output ONLY the search keyword, nothing else

Examples:
Happy V Vaginal Prebiotic + Probiotic for Women 60 Vegan Capsules → women vaginal probiotic supplement capsules
[Dr.Melaxin Official] Cemenrete Calcium Multi Balm Eye Care 2024 → calcium eye balm stick
COSRX The Peptide Collagen Hydrogel Eye Patch 60 Patches → collagen hydrogel eye patch
Portable Mini Face Massager Roller Skincare Tool → face massager roller tool
LED Sunset Projection Lamp Moon Room Decor Night Light → LED sunset projection night lamp
Magnetic Eyelashes Kit Professional Waterproof Women → magnetic false eyelashes kit
Knee Pain Relief Red Light Therapy Wrap Device → red light therapy knee wrap device
Scalp Massager Shampoo Brush Hair Growth Stimulator → scalp massager shampoo brush hair growth"""
                },
                {
                    "role": "user",
                    "content": raw_title
                }
            ],
            max_tokens=40,
            temperature=0
        )
        normalized = response.choices[0].message.content.strip().lower()
        # Remove any quotes GPT might add
        normalized = normalized.strip('"\'')
        print(f"  Normalized to: '{normalized}'")
        return normalized

    except Exception as e:
        print(f"  GPT normalization failed, using basic cleanup: {e}")
        import re
        cleaned = re.sub(r'\[.*?\]', '', raw_title)   # remove [Brand]
        cleaned = re.sub(r'\b20\d{2}\b', '', cleaned)  # remove years
        cleaned = re.sub(r'[^\w\s]', ' ', cleaned)     # remove punctuation
        words = [w for w in cleaned.split() if len(w) > 2][:6]
        return ' '.join(words).lower().strip()


# ==============================================================================
# STEP 2 — Platform-Specific Search Functions
# ==============================================================================

def search_alibaba(keyword: str, max_results: int = 15) -> list:
    print(f"  Searching Alibaba for: '{keyword}'...")

    keyword_encoded = keyword.replace(' ', '+')
    search_url = (
        f"https://www.alibaba.com/trade/search?"
        f"spm=a2700.galleryofferlist.the-new-header_fy23_pc_search_bar.keydown__Enter"
        f"&tab=all&SearchText={keyword_encoded}&has4Tab=true"
    )

    try:
        run = apify.actor("shareze001/scrape-alibaba-products-by-keywords").call(
            run_input={
                "search_url":          search_url,
                "size":                max_results,
                "skip_page_parameter": True,
            },
            timeout_secs=120
        )

        results = []
        for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
            results.append(item)

        print(f"  Found {len(results)} Alibaba results")

        if not results:
            print("  Primary actor returned 0 — trying backup...")
            results = search_alibaba_backup(keyword, max_results)

        return results

    except Exception as e:
        print(f"  Primary Alibaba error: {e}")
        return search_alibaba_backup(keyword, max_results)


def search_alibaba_backup(keyword: str, max_results: int = 15) -> list:
    try:
        run = apify.actor("piotrv1001/alibaba-listings-scraper").call(
            run_input={
                "searchQuery": keyword,
                "maxItems":    max_results,
            }
        )

        results = []
        for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
            results.append(item)

        print(f"  Backup found {len(results)} results")
        return results

    except Exception as e:
        print(f"  Backup error: {e}")
        return []


def search_aliexpress(keyword: str, max_results: int = 10) -> list:
    print(f"  Searching AliExpress for: '{keyword}'...")

    try:
        run = apify.actor("bebity/aliexpress-scraper").call(
            run_input={
                "searchQuery": keyword,
                "maxItems":    max_results,
            },
            timeout_secs=120
        )

        results = []
        for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
            results.append(item)

        print(f"  Found {len(results)} AliExpress results")
        return results

    except Exception as e:
        print(f"  AliExpress error: {e}")
        return []


def search_amazon(keyword: str, max_results: int = 10) -> list:
    print(f"  Searching Amazon for: '{keyword}'...")

    try:
        run = apify.actor("junglee/amazon-crawler").call(
            run_input={
                "keyword":  keyword,
                "maxItems": max_results,
                "country":  "US",
            },
            timeout_secs=120
        )

        results = []
        for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
            results.append(item)

        print(f"  Found {len(results)} Amazon results")
        return results

    except Exception as e:
        print(f"  Amazon error: {e}")
        return []


# ==============================================================================
# STEP 3 — Rank Suppliers with GPT-4o (v3: filters irrelevant products first)
# ==============================================================================
def rank_suppliers(
    raw_suppliers: list,
    tiktok_price: float,
    product_title: str,
    platform: str = "alibaba"
) -> list:
    print(f"  Ranking {len(raw_suppliers)} {platform} suppliers with GPT-4o...")

    if not raw_suppliers:
        return []

    supplier_summaries = []
    for i, s in enumerate(raw_suppliers[:15]):
        if platform == "alibaba":
            price_obj  = s.get("price") or {}
            ladder     = price_obj.get("productLadderPrices") or []
            # Use highest-MOQ tier price (lowest unit cost) for profitability check
            if ladder:
                prices = [float(t.get("dollarPrice", 0)) for t in ladder if t.get("dollarPrice")]
                unit_price = min(prices) if prices else 0
            else:
                range_prices = price_obj.get("productRangePrices") or {}
                unit_price = float(range_prices.get("dollarPriceRangeLow") or 0)
            supplier_info = s.get("supplierInfo") or {}
            name  = supplier_info.get("name") or "Unknown"
            years = supplier_info.get("joinYears") or 0
            moq   = s.get("moq") or 1
            title = s.get("subject") or ""

        elif platform == "aliexpress":
            unit_price = float(s.get("price") or s.get("salePrice") or 0)
            name  = s.get("sellerName") or s.get("storeName") or "Unknown"
            years = 0
            moq   = 1
            title = s.get("title") or s.get("name") or ""

        elif platform == "amazon":
            price_str = str(s.get("price") or s.get("priceCurrency") or "0")
            price_str = price_str.replace("$", "").replace(",", "").strip()
            try:
                unit_price = float(price_str)
            except Exception:
                unit_price = 0
            name  = s.get("sellerName") or s.get("brand") or "Amazon Seller"
            years = 0
            moq   = 1
            title = s.get("title") or s.get("name") or ""

        else:
            unit_price = 0
            name  = "Unknown"
            years = 0
            moq   = 1
            title = ""

        supplier_summaries.append({
            "index":         i,
            "name":          name,
            "product_title": title[:120],
            "price":         unit_price,
            "moq":           moq,
            "years":         years,
            "platform":      platform,
        })

    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are an expert sourcing agent helping a TikTok Shop seller find suppliers.

TikTok Sale Price: ${tiktok_price}
Target Product: {product_title}
Platform: {platform}

STEP 1 — FILTER: Remove any supplier whose product title does not match 
the target product type. A "women probiotic capsule" is NOT the same as 
a "face mask" or "hair serum". Be strict — if it's a different product, exclude it.

STEP 2 — RANK remaining suppliers by:
1. PRODUCT MATCH — must be the same type of product (most important)
2. PROFITABILITY — unit cost ideally under ${round(tiktok_price * 0.30, 2)} for good margins
3. TRUST — more years on platform = better (for Alibaba)
4. MOQ — lower minimum order = better for new sellers
5. PRICE — lower unit cost = better profit

Return TOP 3 matching suppliers as a JSON array:
[
  {{
    "index": 0,
    "rank_reason": "Exact product match, lowest price, 5 years on platform",
    "whitelabel_likely": true,
    "recommendation_tag": "Best Match",
    "profitable": true,
    "product_match": true
  }}
]

If fewer than 3 suppliers match the product, return only the ones that match.
If NONE match, return an empty array [].
Return ONLY valid JSON — no text outside the array."""
                },
                {
                    "role": "user",
                    "content": json.dumps(supplier_summaries)
                }
            ],
            max_tokens=600,
            temperature=0
        )

        content = response.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        ranked = json.loads(content)
        print(f"  GPT selected {len(ranked)} matching suppliers")
        return ranked

    except Exception as e:
        print(f"  Ranking error: {e}")
        # Fallback: return first 3 without filtering
        return [
            {
                "index": i,
                "rank_reason": "Auto-selected (ranking unavailable)",
                "whitelabel_likely": False,
                "recommendation_tag": f"Option {i+1}",
                "profitable": False,
                "product_match": False
            }
            for i in range(min(3, len(raw_suppliers)))
        ]


# ==============================================================================
# STEP 4 — Extract Supplier Data (v3: image + MOQ tier fix)
# ==============================================================================
def extract_supplier_data(raw: dict, platform: str) -> dict:
    """Extract supplier fields based on platform."""

    if platform == "alibaba":
        product_title = raw.get("subject") or ""
        supplier_info = raw.get("supplierInfo") or {}
        supplier_name = supplier_info.get("name") or "Unknown Supplier"
        years         = supplier_info.get("joinYears") or 0
        supplier_url  = raw.get("url") or supplier_info.get("homeUrl") or ""

        # MOQ — use top-level moq field (minimum order regardless of price tier)
        moq_raw = raw.get("moq") or "1"
        try:
            moq = int(str(moq_raw).replace(",", "").strip())
        except Exception:
            moq = 1

        # Price tiers — price_max = highest price (lowest MOQ), price_min = lowest price (highest MOQ)
        price_obj = raw.get("price") or {}
        ladder    = price_obj.get("productLadderPrices") or []

        if ladder:
            prices    = [float(tier.get("dollarPrice", 0)) for tier in ladder if tier.get("dollarPrice")]
            price_min = min(prices) if prices else 0   # best bulk price
            price_max = max(prices) if prices else 0   # single unit price
        else:
            range_prices = price_obj.get("productRangePrices") or {}
            price_min    = float(range_prices.get("dollarPriceRangeLow")  or 0)
            price_max    = float(range_prices.get("dollarPriceRangeHigh") or price_min)

        # Image — try multiple fields
        image_url   = ""
        media_items = raw.get("mediaItems") or []
        if media_items:
            first_media   = media_items[0]
            image_url_obj = first_media.get("imageUrl") or {}
            image_url     = (
                image_url_obj.get("normal")
                or image_url_obj.get("big")
                or image_url_obj.get("small")
                or first_media.get("url")
                or ""
            )
        if not image_url:
            # Fallback to mainImage or thumbnail fields
            image_url = raw.get("mainImage") or raw.get("thumbnailUrl") or raw.get("image") or ""

        # Whitelabel check
        basic_props = raw.get("productBasicProperties") or []
        whitelabel  = False
        for prop in basic_props:
            if prop.get("attrName") == "brand name":
                val = str(prop.get("attrValue") or "").upper()
                if "OEM" in val or "ODM" in val or "CUSTOM" in val:
                    whitelabel = True

        shipping_days = 20
        shipping_cost = 1.50

    elif platform == "aliexpress":
        product_title = raw.get("title") or raw.get("name") or ""
        supplier_name = raw.get("sellerName") or raw.get("storeName") or "AliExpress Seller"
        years         = 0
        supplier_url  = raw.get("url") or raw.get("productUrl") or ""
        moq           = 1

        price_val = raw.get("price") or raw.get("salePrice") or 0
        try:
            price_min = float(str(price_val).replace("$", "").replace(",", "").strip())
        except Exception:
            price_min = 0
        price_max = price_min

        # Image
        image_url = (
            raw.get("imageUrl")
            or raw.get("image")
            or raw.get("thumbnailUrl")
            or raw.get("mainImage")
            or ""
        )

        whitelabel    = False
        shipping_days = 10
        shipping_cost = 0.0

    elif platform == "amazon":
        product_title = raw.get("title") or raw.get("name") or ""
        supplier_name = raw.get("sellerName") or raw.get("brand") or "Amazon Seller"
        years         = 0
        supplier_url  = raw.get("url") or raw.get("productUrl") or ""
        moq           = 1

        price_val = raw.get("price") or 0
        try:
            price_min = float(str(price_val).replace("$", "").replace(",", "").strip())
        except Exception:
            price_min = 0
        price_max = price_min

        # Image
        image_url = (
            raw.get("imageUrl")
            or raw.get("image")
            or raw.get("thumbnailImage")
            or raw.get("mainImage")
            or ""
        )

        whitelabel    = False
        shipping_days = 2
        shipping_cost = 0.0

    else:
        return {}

    return {
        "product_title": product_title[:500],
        "supplier_name": supplier_name,
        "years":         years,
        "supplier_url":  supplier_url,
        "moq":           moq,
        "price_min":     round(price_min, 2),
        "price_max":     round(price_max, 2),
        "image_url":     image_url,
        "whitelabel":    whitelabel,
        "shipping_days": shipping_days,
        "shipping_cost": shipping_cost,
    }


# ==============================================================================
# STEP 5 — Save Suppliers (v3: includes image_url in DB record)
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

        raw  = raw_suppliers[idx]
        data = extract_supplier_data(raw, platform)

        if not data:
            continue

        # Calculate profit using bulk price (price_min) for best-case scenario
        cogs = data["price_min"] if data["price_min"] > 0 else data["price_max"]
        profit_data = calculate_profit(
            sale_price=tiktok_price,
            cogs=cogs,
            inbound_shipping=data["shipping_cost"],
        )

        supplier_record = {
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
            "image_url":              data["image_url"],   # v3: save image
        }

        try:
            result = supabase.table("suppliers").insert(supplier_record).execute()
            record = result.data[0] if result.data else supplier_record

            # Attach extra fields for API response (not stored in DB)
            record["_rank_reason"]        = rank_info.get("rank_reason", "")
            record["_recommendation_tag"] = rank_info.get("recommendation_tag", "")
            record["_product_match"]      = rank_info.get("product_match", True)
            record["_image_url"]          = data["image_url"]
            record["_profit"]             = profit_data

            saved.append(record)
            print(f"    ✓ {data['supplier_name']} — ${data['price_min']} | {profit_data['profit_label']} ({profit_data['margin_pct']}%) | Image: {'yes' if data['image_url'] else 'no'}")

        except Exception as e:
            print(f"  Error saving supplier: {e}")

    # Sort by margin — most profitable first
    saved.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
    print(f"  Saved {len(saved)} suppliers total")
    return saved


# ==============================================================================
# MAIN FUNCTION — Called from FastAPI endpoints
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
    print(f"[{platform.upper()}] Finding suppliers for: {product_title[:60]}...")
    print(f"TikTok Price: ${tiktok_price} | User: {user_email}")
    print(f"{'='*55}")

    # Check for cached results for this product + platform
    existing = (
        supabase.table("suppliers")
        .select("*")
        .eq("tiktok_product_id", product_id)
        .eq("platform", platform)
        .execute()
    )

    if existing.data and len(existing.data) > 0:
        print(f"  Cached {platform} suppliers found — no credit deducted")
        for supplier in existing.data:
            cogs = supplier.get("unit_cost_min") or supplier.get("unit_cost_max") or 0
            supplier["_profit"] = calculate_profit(sale_price=tiktok_price, cogs=cogs)
            # Ensure image is available under both keys
            if not supplier.get("_image_url"):
                supplier["_image_url"] = supplier.get("image_url", "")
        existing.data.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
        return {
            "success":        True,
            "cached":         True,
            "platform":       platform,
            "credit_deducted": False,
            "suppliers":      existing.data
        }

    # Check credits
    credit_info = check_credits(user_email)
    if credit_info["credits_remaining"] <= 0:
        return {
            "success":  False,
            "message":  f"No credits remaining. You've used {credit_info['credits_used']}/{credit_info['credits_total']} credits this month. Upgrade your plan for more credits.",
            "platform": platform,
            "credits":  credit_info,
            "suppliers": []
        }

    # Deduct credit BEFORE search (prevents abuse if search fails)
    deducted = deduct_credit(user_email, product_id, platform)
    if not deducted:
        return {
            "success":  False,
            "message":  "Failed to deduct credit. Please try again.",
            "platform": platform,
            "suppliers": []
        }

    # Normalize title to precise search keyword
    normalized_keyword = normalize_product_title(product_title)

    # Search the selected platform
    if platform == "alibaba":
        results = search_alibaba(normalized_keyword, max_results=15)
    elif platform == "aliexpress":
        results = search_aliexpress(normalized_keyword, max_results=10)
    elif platform == "amazon":
        results = search_amazon(normalized_keyword, max_results=10)
    else:
        return {"success": False, "message": f"Unknown platform: {platform}", "suppliers": []}

    if not results:
        return {
            "success":        False,
            "message":        f"No suppliers found on {platform} for '{normalized_keyword}'. 1 credit was used.",
            "platform":       platform,
            "keyword":        normalized_keyword,
            "credit_deducted": True,
            "suppliers":      []
        }

    # Rank with GPT-4o — filters irrelevant products
    ranked = rank_suppliers(results, tiktok_price, product_title, platform)

    if not ranked:
        return {
            "success":        False,
            "message":        f"Found {len(results)} results on {platform} but none matched '{product_title}'. Try a different platform. 1 credit was used.",
            "platform":       platform,
            "keyword":        normalized_keyword,
            "credit_deducted": True,
            "suppliers":      []
        }

    # Save and return
    saved = save_suppliers(product_id, results, ranked, tiktok_price, platform)

    profitable_count = sum(1 for s in saved if s.get("_profit", {}).get("margin_pct", 0) >= 20)
    updated_credits  = check_credits(user_email)

    print(f"\n✅ {platform} complete — {len(saved)} suppliers, {profitable_count} profitable, keyword: '{normalized_keyword}'")

    return {
        "success":            True,
        "cached":             False,
        "platform":           platform,
        "normalized_keyword": normalized_keyword,
        "credit_deducted":    True,
        "credits_remaining":  updated_credits["credits_remaining"],
        "profitable_count":   profitable_count,
        "suppliers":          saved
    }


# ==============================================================================
# LEGACY — backward compatibility
# ==============================================================================
def find_suppliers(product_id: str, product_title: str, tiktok_price: float) -> dict:
    """Original function — searches Alibaba only, no credit check."""
    return find_suppliers_by_platform(
        product_id=product_id,
        product_title=product_title,
        tiktok_price=tiktok_price,
        platform="alibaba",
        user_email="legacy"
    )