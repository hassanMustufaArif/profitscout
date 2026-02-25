"""
ProfitScout — Supplier Matching Engine
=======================================
Finds the best Alibaba/AliExpress suppliers for a TikTok viral product.
Now includes automatic profit calculation for each supplier.

Flow:
1. Takes a product title from TikTok
2. Uses GPT-4o to normalize the title (removes marketing language)
3. Searches Alibaba via Apify actor
4. Uses GPT-4o to rank and score suppliers
5. Calculates net profit for each supplier after ALL TikTok fees
6. Saves top 3 suppliers to Supabase with profit data
"""

from apify_client import ApifyClient
from openai import OpenAI
from supabase import create_client, Client
from dotenv import load_dotenv
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
# PROFIT CALCULATOR — Calculates net profit after ALL TikTok fees
# Uses the same formula as POST /calculate-profit endpoint
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
    """
    Calculate exact net profit after every TikTok Shop fee.
    Returns profit amount, margin %, and rating.
    """
    # TikTok Referral Fee: 6% standard, 3% for new sellers (first 30 days)
    referral_rate = 0.03 if is_new_seller else 0.06
    referral_fee = sale_price * referral_rate

    # Transaction Fee: 2.9% of sale price
    transaction_fee = sale_price * 0.029

    # Affiliate Commission
    affiliate_commission = sale_price * (affiliate_commission_rate / 100)

    # FBT Fulfillment Fee (based on weight)
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

    # Return Buffer
    return_buffer = sale_price * (return_rate_pct / 100)

    # Total Costs
    total_fees = referral_fee + transaction_fee + affiliate_commission + fbt_fee + inbound_shipping + return_buffer + ad_spend_per_unit
    total_cost = cogs + total_fees

    # Net Profit
    net_profit = sale_price - total_cost
    margin_pct = (net_profit / sale_price * 100) if sale_price > 0 else 0

    # Rating
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
        "ad_spend": round(ad_spend_per_unit, 2),
        "total_fees": round(total_fees, 2),
        "net_profit": round(net_profit, 2),
        "margin_pct": round(margin_pct, 1),
        "rating": rating,
        "profit_label": label,
    }


# ==============================================================================
# STEP 1 — Normalize Product Title
# ==============================================================================
def normalize_product_title(raw_title: str) -> str:
    print(f"  Normalizing title: {raw_title[:60]}...")

    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """You are a product search expert for Alibaba sourcing.
Your job is to convert TikTok product titles into simple, clean search keywords
that will find matching products on Alibaba.

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
# STEP 2 — Search Alibaba via Apify
# ==============================================================================
def search_alibaba(keyword: str, max_results: int = 15) -> list:
    print(f"  Searching Alibaba for: '{keyword}'...")

    keyword_encoded = keyword.replace(' ', '+')
    search_url = (
        f"https://www.alibaba.com/trade/search?"
        f"spm=a2700.galleryofferlist.the-new-header_fy23_pc_search_bar.keydown__Enter"
        f"&tab=all&SearchText={keyword_encoded}&has4Tab=true"
    )

    print(f"  Search URL: {search_url}")

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
            print("  Primary actor returned 0 — trying backup actor...")
            results = search_alibaba_backup(keyword, max_results)

        return results

    except Exception as e:
        print(f"  Primary Alibaba actor error: {e}")
        print("  Trying backup actor...")
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

        print(f"  Backup actor found {len(results)} results")
        return results

    except Exception as e:
        print(f"  Backup Alibaba actor error: {e}")
        return []


# ==============================================================================
# STEP 2B — Search AliExpress via Apify (backup / low MOQ option)
# ==============================================================================
def search_aliexpress(keyword: str, max_results: int = 10) -> list:
    print(f"  Searching AliExpress for: '{keyword}'...")

    try:
        run = apify.actor("bebity/aliexpress-scraper").call(
            run_input={
                "searchQuery": keyword,
                "maxItems":    max_results,
            }
        )

        results = []
        for item in apify.dataset(run["defaultDatasetId"]).iterate_items():
            results.append(item)

        print(f"  Found {len(results)} AliExpress results")
        return results

    except Exception as e:
        print(f"  AliExpress search error: {e}")
        return []


# ==============================================================================
# STEP 3 — Rank and Score Suppliers with GPT-4o
# ==============================================================================
def rank_suppliers(
    raw_suppliers:  list,
    tiktok_price:   float,
    product_title:  str
) -> list:
    print(f"  Ranking {len(raw_suppliers)} suppliers with GPT-4o...")

    if not raw_suppliers:
        return []

    supplier_summaries = []
    for i, s in enumerate(raw_suppliers[:15]):
        price_obj  = s.get("price") or {}
        ladder     = price_obj.get("productLadderPrices") or []
        unit_price = float(ladder[0].get("dollarPrice") or 0) if ladder else 0

        supplier_info = s.get("supplierInfo") or {}

        supplier_summaries.append({
            "index":         i,
            "name":          supplier_info.get("name") or "Unknown",
            "product_title": s.get("subject") or "",
            "price_min":     unit_price,
            "price_max":     unit_price,
            "moq":           s.get("moq") or 1,
            "rating":        0,
            "years":         supplier_info.get("joinYears") or 0,
            "transactions":  0,
        })

    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are an expert Alibaba sourcing agent helping a TikTok Shop seller find the best supplier.

TikTok Sale Price: ${tiktok_price}
Product Looking For: {product_title}

Rank these suppliers and return the TOP 3 best ones.

Ranking criteria (in order of importance):
1. PROFITABILITY FIRST: The unit cost MUST be low enough to be profitable after TikTok fees.
   TikTok takes roughly 30-35% of sale price in fees (referral 6%, transaction 2.9%, affiliate 15%, fulfillment ~$3.15, shipping $1.20, returns 8%).
   So unit cost must be UNDER {round(tiktok_price * 0.40, 2)} to have any margin.
   Ideal unit cost is UNDER {round(tiktok_price * 0.25, 2)} for a healthy 35%+ margin.
2. Supplier trust: higher years in business = better
3. MOQ feasibility: lower MOQ = better for new sellers (under 50 units ideal)
4. Product match: does the title actually match what we need?

REJECT suppliers where unit cost is above {round(tiktok_price * 0.50, 2)} — they are guaranteed unprofitable.
If all suppliers are too expensive, still return top 3 cheapest but note they are likely unprofitable.

Return ONLY a JSON array of top 3 suppliers in this exact format:
[
  {{
    "index": 0,
    "rank_reason": "Best overall — lowest price with good trust",
    "whitelabel_likely": true,
    "recommendation_tag": "Best Value",
    "profitable": true
  }},
  ...
]
Do not include any text outside the JSON array."""
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
        ranked  = json.loads(content)
        print(f"  GPT ranked top {len(ranked)} suppliers")
        return ranked

    except Exception as e:
        print(f"  Ranking error: {e}")
        return [{"index": i, "rank_reason": "Auto-selected", "whitelabel_likely": False, "recommendation_tag": f"Option {i+1}", "profitable": False} for i in range(min(3, len(raw_suppliers)))]


# ==============================================================================
# STEP 4 — Save Suppliers to Supabase (now with profit data)
# ==============================================================================
def save_suppliers(
    product_id:    str,
    raw_suppliers: list,
    ranked:        list,
    tiktok_price:  float,
    platform:      str = "alibaba"
) -> list:
    print(f"  Saving top {len(ranked)} suppliers to Supabase...")

    saved = []
    for rank_info in ranked:
        idx = rank_info.get("index", 0)
        if idx >= len(raw_suppliers):
            continue

        raw = raw_suppliers[idx]

        # ── Extract fields ─────────────────────────────────────────────────
        product_title = raw.get("subject") or ""
        supplier_info = raw.get("supplierInfo") or {}
        supplier_name = supplier_info.get("name") or "Unknown Supplier"
        years         = supplier_info.get("joinYears") or 0
        supplier_url  = raw.get("url") or supplier_info.get("homeUrl") or ""

        moq_raw = raw.get("moq") or "1"
        try:
            moq = int(str(moq_raw).replace(",", "").strip())
        except:
            moq = 1

        price_obj   = raw.get("price") or {}
        ladder      = price_obj.get("productLadderPrices") or []

        if ladder:
            price_min = float(ladder[-1].get("dollarPrice") or ladder[0].get("dollarPrice") or 0)
            price_max = float(ladder[0].get("dollarPrice") or price_min)
        else:
            range_prices = price_obj.get("productRangePrices") or {}
            price_min = float(range_prices.get("dollarPriceRangeLow") or 0)
            price_max = float(range_prices.get("dollarPriceRangeHigh") or price_min)

        media_items = raw.get("mediaItems") or []
        image_url   = ""
        if media_items:
            image_url_obj = media_items[0].get("imageUrl") or {}
            image_url = image_url_obj.get("normal") or image_url_obj.get("big") or ""

        basic_props = raw.get("productBasicProperties") or []
        whitelabel  = False
        for prop in basic_props:
            if prop.get("attrName") == "brand name":
                val = str(prop.get("attrValue") or "").upper()
                if "OEM" in val or "ODM" in val or "CUSTOM" in val:
                    whitelabel = True

        # ── Calculate profit using lowest supplier cost ────────────────────
        profit_data = calculate_profit(
            sale_price=tiktok_price,
            cogs=price_min if price_min > 0 else price_max,
        )

        supplier_record = {
            "tiktok_product_id":       product_id,
            "platform":                platform,
            "supplier_name":           supplier_name,
            "product_title":           product_title[:500],
            "unit_cost_min":           round(price_min, 2),
            "unit_cost_max":           round(price_max, 2),
            "moq":                     moq,
            "shipping_estimate_days":  15,
            "shipping_cost_estimate":  1.50,
            "supplier_trust_score":    min(float(years) / 2, 5.0),
            "years_on_platform":       int(years),
            "whitelabel_available":    whitelabel,
            "supplier_url":            supplier_url,
        }

        try:
            result = supabase.table("suppliers").insert(supplier_record).execute()
            record = result.data[0] if result.data else supplier_record

            # Add profit data and ranking info (not saved to DB, returned to frontend)
            record["_rank_reason"]        = rank_info.get("rank_reason", "")
            record["_recommendation_tag"] = rank_info.get("recommendation_tag", "")
            record["_image_url"]          = image_url
            record["_profit"]             = profit_data

            saved.append(record)
            print(f"    Saved: {supplier_name} — ${price_min}-${price_max} | MOQ: {moq} | Profit: ${profit_data['net_profit']} ({profit_data['margin_pct']}%) | {profit_data['profit_label']}")
        except Exception as e:
            print(f"  Error saving supplier: {e}")

    # Sort by profit margin — highest first
    saved.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)

    print(f"  Saved {len(saved)} suppliers successfully")
    return saved


# ==============================================================================
# MAIN FUNCTION — Called from FastAPI endpoint
# ==============================================================================
def find_suppliers(product_id: str, product_title: str, tiktok_price: float) -> dict:
    print(f"\n{'='*50}")
    print(f"Finding suppliers for: {product_title[:50]}...")
    print(f"TikTok Price: ${tiktok_price}")
    print(f"Max COGS for profit: ${round(tiktok_price * 0.40, 2)}")
    print(f"{'='*50}")

    # Check if suppliers already exist for this product
    existing = (
        supabase.table("suppliers")
        .select("*")
        .eq("tiktok_product_id", product_id)
        .execute()
    )

    if existing.data and len(existing.data) > 0:
        print(f"  Suppliers already exist — returning cached results with profit calc")
        # Add profit calculation to cached suppliers too
        for supplier in existing.data:
            cogs = supplier.get("unit_cost_min") or supplier.get("unit_cost_max") or 0
            supplier["_profit"] = calculate_profit(
                sale_price=tiktok_price,
                cogs=cogs,
            )
        # Sort cached by profit
        existing.data.sort(key=lambda x: x.get("_profit", {}).get("margin_pct", -999), reverse=True)
        return {
            "success":   True,
            "cached":    True,
            "suppliers": existing.data
        }

    # Step 1: Normalize title
    normalized_keyword = normalize_product_title(product_title)

    # Step 2: Search Alibaba
    alibaba_results = search_alibaba(normalized_keyword, max_results=15)

    # Step 2b: If Alibaba returns nothing, try AliExpress
    if not alibaba_results:
        print("  Alibaba returned no results — trying AliExpress...")
        aliexpress_results = search_aliexpress(normalized_keyword, max_results=10)
        if aliexpress_results:
            ranked  = rank_suppliers(aliexpress_results, tiktok_price, product_title)
            saved   = save_suppliers(product_id, aliexpress_results, ranked, tiktok_price, platform="aliexpress")

            profitable_count = sum(1 for s in saved if s.get("_profit", {}).get("margin_pct", 0) >= 20)

            return {
                "success": True,
                "cached": False,
                "profitable_count": profitable_count,
                "suppliers": saved
            }
        else:
            return {
                "success":  False,
                "message":  "No suppliers found on Alibaba or AliExpress for this product",
                "keyword":  normalized_keyword,
                "suppliers": []
            }

    # Step 3: Rank with GPT
    ranked = rank_suppliers(alibaba_results, tiktok_price, product_title)

    # Step 4: Save and return with profit data
    saved = save_suppliers(product_id, alibaba_results, ranked, tiktok_price, platform="alibaba")

    profitable_count = sum(1 for s in saved if s.get("_profit", {}).get("margin_pct", 0) >= 20)

    print(f"\n✅ Supplier matching complete — {len(saved)} suppliers found, {profitable_count} profitable")
    return {
        "success":            True,
        "cached":             False,
        "normalized_keyword": normalized_keyword,
        "profitable_count":   profitable_count,
        "suppliers":          saved
    }
