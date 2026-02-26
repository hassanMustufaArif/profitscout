"""
ProfitScout — Supplier Matching Engine v2
==========================================
Multi-platform supplier search with credit system.
Searches Alibaba, AliExpress, and Amazon individually.
Each search costs 1 credit.
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
            supabase.table("user_credits").update({
                "credits_used": 0,
                "credits_total": new_total,
                "period_start": now.isoformat(),
                "period_end": (now.replace(day=1) + __import__('datetime').timedelta(days=32)).replace(day=1).isoformat()
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
        # Increment credits_used
        result = supabase.table("user_credits").select("credits_used").eq("user_email", user_email).execute()
        if not result.data:
            return False
        
        current_used = result.data[0]["credits_used"]
        supabase.table("user_credits").update({
            "credits_used": current_used + 1
        }).eq("user_email", user_email).execute()
        
        # Log the usage
        supabase.table("credit_logs").insert({
            "user_email": user_email,
            "product_id": product_id,
            "platform": platform,
            "credits_deducted": 1
        }).execute()
        
        return True
    except Exception as e:
        print(f"Credit deduction error: {e}")
        return False


def initialize_credits(user_email: str, plan: str):
    """Create or update credits when user subscribes."""
    credits = 50 if plan == "pro" else 120 if plan == "ultimate" else 0
    now = datetime.now(timezone.utc)
    
    try:
        existing = supabase.table("user_credits").select("id").eq("user_email", user_email).execute()
        if existing.data:
            supabase.table("user_credits").update({
                "plan": plan,
                "credits_total": credits,
                "credits_used": 0,
                "period_start": now.isoformat(),
                "period_end": (now.replace(day=1) + __import__('datetime').timedelta(days=32)).replace(day=1).isoformat()
            }).eq("user_email", user_email).execute()
        else:
            supabase.table("user_credits").insert({
                "user_email": user_email,
                "plan": plan,
                "credits_total": credits,
                "credits_used": 0,
                "period_start": now.isoformat(),
                "period_end": (now.replace(day=1) + __import__('datetime').timedelta(days=32)).replace(day=1).isoformat()
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
# STEP 1 — Normalize Product Title (using GPT-4o-mini for cost savings)
# ==============================================================================
def normalize_product_title(raw_title: str) -> str:
    print(f"  Normalizing title: {raw_title[:60]}...")

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """You are a product search expert for Alibaba sourcing.
Convert TikTok product titles into simple, clean search keywords for Alibaba.

Rules:
- Remove brand names, shop names in brackets like [Brand Name]
- Remove marketing words: viral, official, trending, best, amazing, 2024, NEW
- Remove symbols: !, ?, [], ()
- Keep only the core product type (2-5 words maximum)
- Use generic terms a manufacturer would use
- Output ONLY the clean search keyword, nothing else

Examples:
[Dr.Melaxin Official] Cemenrete Calcium Multi Balm Eye Care → calcium eye balm
COSRX The Peptide Collagen Hydrogel Eye Patch 60 Patches → collagen eye patch
Viral Magnetic Eyelashes Kit Women Professional → magnetic false eyelashes
Portable Mini Face Massager Roller Skincare Tool 2024 → face massager roller"""
                },
                {
                    "role": "user",
                    "content": raw_title
                }
            ],
            max_tokens=30,
            temperature=0
        )
        normalized = response.choices[0].message.content.strip().lower()
        print(f"  Normalized to: '{normalized}'")
        return normalized

    except Exception as e:
        print(f"  GPT failed, using basic cleanup: {e}")
        import re
        cleaned = re.sub(r'\[.*?\]', '', raw_title)
        cleaned = re.sub(r'\d+', '', cleaned)
        words   = cleaned.split()[:4]
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
                "keyword": keyword,
                "maxItems": max_results,
                "country": "US",
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
# STEP 3 — Rank Suppliers with GPT-4o
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
            unit_price = float(ladder[0].get("dollarPrice") or 0) if ladder else 0
            supplier_info = s.get("supplierInfo") or {}
            name = supplier_info.get("name") or "Unknown"
            years = supplier_info.get("joinYears") or 0
            moq = s.get("moq") or 1
            title = s.get("subject") or ""
        elif platform == "aliexpress":
            unit_price = float(s.get("price") or s.get("salePrice") or 0)
            name = s.get("sellerName") or s.get("storeName") or "Unknown"
            years = 0
            moq = 1
            title = s.get("title") or s.get("name") or ""
        elif platform == "amazon":
            price_str = str(s.get("price") or s.get("priceCurrency") or "0")
            price_str = price_str.replace("$", "").replace(",", "").strip()
            try:
                unit_price = float(price_str)
            except:
                unit_price = 0
            name = s.get("sellerName") or s.get("brand") or "Amazon Seller"
            years = 0
            moq = 1
            title = s.get("title") or s.get("name") or ""

        supplier_summaries.append({
            "index": i,
            "name": name,
            "product_title": title[:100],
            "price": unit_price,
            "moq": moq,
            "years": years,
            "platform": platform,
        })

    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are an expert sourcing agent helping a TikTok Shop seller.

TikTok Sale Price: ${tiktok_price}
Product: {product_title}
Platform: {platform}

Rank these suppliers and return TOP 3.

Ranking criteria:
1. PROFITABILITY: unit cost must be under ${round(tiktok_price * 0.40, 2)} for profit after TikTok fees.
   Ideal cost under ${round(tiktok_price * 0.25, 2)} for 35%+ margin.
2. Product match: title must match what we need
3. Trust: more years = better (for Alibaba)
4. MOQ: lower = better for new sellers

Return ONLY a JSON array of top 3:
[
  {{
    "index": 0,
    "rank_reason": "Lowest price with good match",
    "whitelabel_likely": true,
    "recommendation_tag": "Best Value",
    "profitable": true
  }}
]
No text outside JSON."""
                },
                {
                    "role": "user",
                    "content": json.dumps(supplier_summaries)
                }
            ],
            max_tokens=500,
            temperature=0
        )

        content = response.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        ranked = json.loads(content)
        print(f"  GPT ranked top {len(ranked)} suppliers")
        return ranked

    except Exception as e:
        print(f"  Ranking error: {e}")
        return [{"index": i, "rank_reason": "Auto-selected", "whitelabel_likely": False, "recommendation_tag": f"Option {i+1}", "profitable": False} for i in range(min(3, len(raw_suppliers)))]


# ==============================================================================
# STEP 4 — Extract & Save Suppliers (platform-aware)
# ==============================================================================
def extract_supplier_data(raw: dict, platform: str) -> dict:
    """Extract supplier fields based on platform."""

    if platform == "alibaba":
        product_title = raw.get("subject") or ""
        supplier_info = raw.get("supplierInfo") or {}
        supplier_name = supplier_info.get("name") or "Unknown Supplier"
        years = supplier_info.get("joinYears") or 0
        supplier_url = raw.get("url") or supplier_info.get("homeUrl") or ""

        moq_raw = raw.get("moq") or "1"
        try:
            moq = int(str(moq_raw).replace(",", "").strip())
        except:
            moq = 1

        price_obj = raw.get("price") or {}
        ladder = price_obj.get("productLadderPrices") or []

        if ladder:
            prices = [float(tier.get("dollarPrice", 0)) for tier in ladder if tier.get("dollarPrice")]
            price_min = min(prices) if prices else 0
            price_max = max(prices) if prices else 0
        else:
            range_prices = price_obj.get("productRangePrices") or {}
            price_min = float(range_prices.get("dollarPriceRangeLow") or 0)
            price_max = float(range_prices.get("dollarPriceRangeHigh") or price_min)

        media_items = raw.get("mediaItems") or []
        image_url = ""
        if media_items:
            image_url_obj = media_items[0].get("imageUrl") or {}
            image_url = image_url_obj.get("normal") or image_url_obj.get("big") or ""

        basic_props = raw.get("productBasicProperties") or []
        whitelabel = False
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
        years = 0
        supplier_url = raw.get("url") or raw.get("productUrl") or ""
        moq = 1

        price_val = raw.get("price") or raw.get("salePrice") or 0
        try:
            price_min = float(str(price_val).replace("$", "").replace(",", "").strip())
        except:
            price_min = 0
        price_max = price_min

        image_url = raw.get("imageUrl") or raw.get("image") or ""
        whitelabel = False
        shipping_days = 10
        shipping_cost = 0

    elif platform == "amazon":
        product_title = raw.get("title") or raw.get("name") or ""
        supplier_name = raw.get("sellerName") or raw.get("brand") or "Amazon Seller"
        years = 0
        supplier_url = raw.get("url") or raw.get("productUrl") or ""
        moq = 1

        price_val = raw.get("price") or 0
        try:
            price_min = float(str(price_val).replace("$", "").replace(",", "").strip())
        except:
            price_min = 0
        price_max = price_min

        image_url = raw.get("imageUrl") or raw.get("image") or raw.get("thumbnailImage") or ""
        whitelabel = False
        shipping_days = 2
        shipping_cost = 0

    else:
        return {}

    return {
        "product_title": product_title[:500],
        "supplier_name": supplier_name,
        "years": years,
        "supplier_url": supplier_url,
        "moq": moq,
        "price_min": round(price_min, 2),
        "price_max": round(price_max, 2),
        "image_url": image_url,
        "whitelabel": whitelabel,
        "shipping_days": shipping_days,
        "shipping_cost": shipping_cost,
    }


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

        raw = raw_suppliers[idx]
        data = extract_supplier_data(raw, platform)

        if not data:
            continue

        # Calculate profit
        cogs = data["price_min"] if data["price_min"] > 0 else data["price_max"]
        profit_data = calculate_profit(
            sale_price=tiktok_price,
            cogs=cogs,
            inbound_shipping=data["shipping_cost"],
        )

        supplier_record = {
            "tiktok_product_id":       product_id,
            "platform":                platform,
            "supplier_name":           data["supplier_name"],
            "product_title":           data["product_title"],
            "unit_cost_min":           data["price_min"],
            "unit_cost_max":           data["price_max"],
            "moq":                     data["moq"],
            "shipping_estimate_days":  data["shipping_days"],
            "shipping_cost_estimate":  data["shipping_cost"],
            "supplier_trust_score":    min(float(data["years"]) / 2, 5.0),
            "years_on_platform":       data["years"],
            "whitelabel_available":    data["whitelabel"],
            "supplier_url":            data["supplier_url"],
        }

        try:
            result = supabase.table("suppliers").insert(supplier_record).execute()
            record = result.data[0] if result.data else supplier_record
            record["_rank_reason"]        = rank_info.get("rank_reason", "")
            record["_recommendation_tag"] = rank_info.get("recommendation_tag", "")
            record["_image_url"]          = data["image_url"]
            record["_profit"]             = profit_data
            saved.append(record)
            print(f"    Saved: {data['supplier_name']} — ${data['price_min']} | {profit_data['profit_label']} ({profit_data['margin_pct']}%)")
        except Exception as e:
            print(f"  Error saving supplier: {e}")

    saved.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
    print(f"  Saved {len(saved)} suppliers")
    return saved


# ==============================================================================
# MAIN FUNCTIONS — Called from FastAPI endpoints
# ==============================================================================

def find_suppliers_by_platform(
    product_id: str,
    product_title: str,
    tiktok_price: float,
    platform: str,
    user_email: str
) -> dict:
    """Search a single platform for suppliers. Deducts 1 credit."""

    print(f"\n{'='*50}")
    print(f"Finding {platform} suppliers for: {product_title[:50]}...")
    print(f"TikTok Price: ${tiktok_price} | User: {user_email}")
    print(f"{'='*50}")

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
        existing.data.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
        return {
            "success": True,
            "cached": True,
            "platform": platform,
            "credit_deducted": False,
            "suppliers": existing.data
        }

    # Check credits
    credit_info = check_credits(user_email)
    if credit_info["credits_remaining"] <= 0:
        return {
            "success": False,
            "message": f"No credits remaining. You've used {credit_info['credits_used']}/{credit_info['credits_total']} credits this month. Upgrade your plan for more credits.",
            "platform": platform,
            "credits": credit_info,
            "suppliers": []
        }

    # Deduct credit BEFORE search
    deducted = deduct_credit(user_email, product_id, platform)
    if not deducted:
        return {
            "success": False,
            "message": "Failed to deduct credit. Please try again.",
            "platform": platform,
            "suppliers": []
        }

    # Normalize title
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
            "success": False,
            "message": f"No suppliers found on {platform} for this product. 1 credit was used.",
            "platform": platform,
            "keyword": normalized_keyword,
            "credit_deducted": True,
            "suppliers": []
        }

    # Rank with GPT
    ranked = rank_suppliers(results, tiktok_price, product_title, platform)

    # Save and return
    saved = save_suppliers(product_id, results, ranked, tiktok_price, platform)

    profitable_count = sum(1 for s in saved if s.get("_profit", {}).get("margin_pct", 0) >= 20)

    # Get updated credit info
    updated_credits = check_credits(user_email)

    print(f"\n✅ {platform} search complete — {len(saved)} suppliers, {profitable_count} profitable")
    return {
        "success": True,
        "cached": False,
        "platform": platform,
        "normalized_keyword": normalized_keyword,
        "credit_deducted": True,
        "credits_remaining": updated_credits["credits_remaining"],
        "profitable_count": profitable_count,
        "suppliers": saved
    }


# Legacy function — still works for backward compatibility
def find_suppliers(product_id: str, product_title: str, tiktok_price: float) -> dict:
    """Original function — searches Alibaba only, no credit check."""
    return find_suppliers_by_platform(
        product_id=product_id,
        product_title=product_title,
        tiktok_price=tiktok_price,
        platform="alibaba",
        user_email="legacy"
    )