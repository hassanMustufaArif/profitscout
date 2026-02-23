"""
ProfitScout — Supplier Matching Engine
=======================================
Finds the best Alibaba/AliExpress suppliers for a TikTok viral product.

Flow:
1. Takes a product title from TikTok
2. Uses GPT-4o to normalize the title (removes marketing language)
3. Searches Alibaba via Apify actor
4. Uses GPT-4o to rank and score suppliers
5. Saves top 3 suppliers to Supabase
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
# STEP 1 — Normalize Product Title
# Removes TikTok marketing language so Alibaba search works better
# Example:
#   IN:  "[Dr.Melaxin Official] Cemenrete Calcium Multi Balm Eye Care 2024 VIRAL"
#   OUT: "calcium eye balm cream"
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
        # Fallback: basic cleanup without GPT
        print(f"  GPT failed, using basic cleanup: {e}")
        import re
        cleaned = re.sub(r'\[.*?\]', '', raw_title)  # Remove [brand]
        cleaned = re.sub(r'\d+', '', cleaned)          # Remove numbers
        words   = cleaned.split()[:4]                  # First 4 words
        return ' '.join(words).lower().strip()


# ==============================================================================
# STEP 2 — Search Alibaba via Apify
# Returns raw list of supplier results
# ==============================================================================
def search_alibaba(keyword: str, max_results: int = 15) -> list:
    print(f"  Searching Alibaba for: '{keyword}'...")

    # Use exact same URL format that worked in manual Apify test
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
    """
    Backup Alibaba actor: piotrv1001/alibaba-listings-scraper
    Uses search query directly — good fallback if primary fails
    """
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
        # Verified real actor: bebity/aliexpress-scraper
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
# Takes raw Apify results and returns top 3 ranked suppliers
# ==============================================================================
def rank_suppliers(
    raw_suppliers:  list,
    tiktok_price:   float,
    product_title:  str
) -> list:
    print(f"  Ranking {len(raw_suppliers)} suppliers with GPT-4o...")

    if not raw_suppliers:
        return []

    # Build a simplified summary for GPT (avoid sending too much data)
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
1. Price margin: unit cost should be less than 30% of TikTok sale price (ideally under 20%)
2. Supplier trust: higher rating and years in business = better
3. MOQ feasibility: lower MOQ = better for new sellers (under 50 units ideal)
4. Product match: does the title actually match what we need?

Return ONLY a JSON array of top 3 suppliers in this exact format:
[
  {{
    "index": 0,
    "rank_reason": "Best overall — lowest price with high trust score",
    "whitelabel_likely": true,
    "recommendation_tag": "Best Value"
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
        # Clean any markdown code blocks if present
        content = content.replace("```json", "").replace("```", "").strip()
        ranked  = json.loads(content)
        print(f"  GPT ranked top {len(ranked)} suppliers")
        return ranked

    except Exception as e:
        print(f"  Ranking error: {e}")
        # Fallback: return first 3 by lowest price
        return [{"index": i, "rank_reason": "Auto-selected", "whitelabel_likely": False, "recommendation_tag": f"Option {i+1}"} for i in range(min(3, len(raw_suppliers)))]


# ==============================================================================
# STEP 4 — Save Suppliers to Supabase
# ==============================================================================
def save_suppliers(
    product_id:    str,
    raw_suppliers: list,
    ranked:        list,
    platform:      str = "alibaba"
) -> list:
    print(f"  Saving top {len(ranked)} suppliers to Supabase...")

    saved = []
    for rank_info in ranked:
        idx = rank_info.get("index", 0)
        if idx >= len(raw_suppliers):
            continue

        raw = raw_suppliers[idx]

        # ── Extract fields using EXACT field names from Alibaba actor ──────────

        # Product title → "subject" field
        product_title = raw.get("subject") or ""

        # Supplier info → nested under "supplierInfo"
        supplier_info = raw.get("supplierInfo") or {}
        supplier_name = supplier_info.get("name") or "Unknown Supplier"
        years         = supplier_info.get("joinYears") or 0
        supplier_url  = raw.get("url") or supplier_info.get("homeUrl") or ""

        # MOQ → top-level "moq" field (e.g. "500")
        moq_raw = raw.get("moq") or "1"
        try:
            moq = int(str(moq_raw).replace(",", "").strip())
        except:
            moq = 1

        # Price → use ladder prices (price breaks by quantity)
        # price/productLadderPrices/0/dollarPrice = price for minimum MOQ tier
        price_obj   = raw.get("price") or {}
        ladder      = price_obj.get("productLadderPrices") or []

        if ladder:
            # First tier = highest price (lowest MOQ)
            price_min = float(ladder[-1].get("dollarPrice") or ladder[0].get("dollarPrice") or 0)
            price_max = float(ladder[0].get("dollarPrice") or price_min)
        else:
            # Fallback to range prices
            range_prices = price_obj.get("productRangePrices") or {}
            price_min = float(range_prices.get("dollarPriceRangeLow") or 0)
            price_max = float(range_prices.get("dollarPriceRangeHigh") or price_min)

        # Image → use first media item normal image
        media_items = raw.get("mediaItems") or []
        image_url   = ""
        if media_items:
            image_url_obj = media_items[0].get("imageUrl") or {}
            image_url = image_url_obj.get("normal") or image_url_obj.get("big") or ""

        # Whitelabel check → brand name = OEM/ODM means whitelabel available
        basic_props = raw.get("productBasicProperties") or []
        whitelabel  = False
        for prop in basic_props:
            if prop.get("attrName") == "brand name":
                val = str(prop.get("attrValue") or "").upper()
                if "OEM" in val or "ODM" in val or "CUSTOM" in val:
                    whitelabel = True

        supplier_record = {
            "tiktok_product_id":       product_id,
            "platform":                platform,
            "supplier_name":           supplier_name,
            "product_title":           product_title[:500],   # trim long titles
            "unit_cost_min":           round(price_min, 2),
            "unit_cost_max":           round(price_max, 2),
            "moq":                     moq,
            "shipping_estimate_days":  15,     # Alibaba default estimate
            "shipping_cost_estimate":  1.50,   # Estimate — varies by product
            "supplier_trust_score":    min(float(years) / 2, 5.0),  # Scale years → 0-5 score
            "years_on_platform":       int(years),
            "whitelabel_available":    whitelabel,
            "supplier_url":            supplier_url,
        }

        try:
            result = supabase.table("suppliers").insert(supplier_record).execute()
            record = result.data[0] if result.data else supplier_record
            record["_rank_reason"]        = rank_info.get("rank_reason", "")
            record["_recommendation_tag"] = rank_info.get("recommendation_tag", "")
            record["_image_url"]          = image_url
            saved.append(record)
            print(f"    Saved: {supplier_name} — ${price_min}-${price_max} | MOQ: {moq} | Whitelabel: {whitelabel}")
        except Exception as e:
            print(f"  Error saving supplier: {e}")

    print(f"  Saved {len(saved)} suppliers successfully")
    return saved


# ==============================================================================
# MAIN FUNCTION — Called from FastAPI endpoint
# Full pipeline: title → normalize → search → rank → save → return
# ==============================================================================
def find_suppliers(product_id: str, product_title: str, tiktok_price: float) -> dict:
    print(f"\n{'='*50}")
    print(f"Finding suppliers for: {product_title[:50]}...")
    print(f"{'='*50}")

    # Check if suppliers already exist for this product
    existing = (
        supabase.table("suppliers")
        .select("*")
        .eq("tiktok_product_id", product_id)
        .execute()
    )

    if existing.data and len(existing.data) > 0:
        print(f"  Suppliers already exist — returning cached results")
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
            saved   = save_suppliers(product_id, aliexpress_results, ranked, platform="aliexpress")
            return {"success": True, "cached": False, "suppliers": saved}
        else:
            return {
                "success":  False,
                "message":  "No suppliers found on Alibaba or AliExpress for this product",
                "keyword":  normalized_keyword,
                "suppliers": []
            }

    # Step 3: Rank with GPT
    ranked = rank_suppliers(alibaba_results, tiktok_price, product_title)

    # Step 4: Save and return
    saved = save_suppliers(product_id, alibaba_results, ranked, platform="alibaba")

    print(f"\n✅ Supplier matching complete — {len(saved)} suppliers found")
    return {
        "success":            True,
        "cached":             False,
        "normalized_keyword": normalized_keyword,
        "suppliers":          saved
    }