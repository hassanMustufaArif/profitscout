"""
ProfitScout — Kalodata / FastMoss CSV Importer
===============================================
Usage:
    python import_products.py data/haircare_export.csv
    python import_products.py data/kitchen_export.csv
"""

import csv
import sys
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client

# ── Load credentials from .env ─────────────────────────────────────────────────
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in your .env file")
    sys.exit(1)

# ── Column name mappings ───────────────────────────────────────────────────────
COLUMN_MAP = {
    "product_name": ["Product Name", "product_name", "Title", "product_name", "Product Title", "Name"],
    "category": ["Category", "category", "Product Category", "Category Name"],
    "tiktok_price": ["Price($)", "Price", "price", "Sale Price", "Avg. Unit Price($)", "Min Price"],
    "gmv_7d": ["Revenue($)", "Revenue", "revenue", "GMV", "gmv", "7D Revenue", "Sales Revenue"],
    "gmv_growth_pct": ["Revenue Growth", "revenue_growth", "GMV Growth", "Growth Rate", "Revenue Growth Rate", "7D Growth"],
    "units_sold_7d": ["Item Sold", "Sales Volume", "sales_volume", "Units Sold", "units_sold", "Sold Count", "7D Sales", "Volume"],
    "creator_count": ["Creator Number", "Related Creators", "related_creators", "Creators", "creator_count", "Influencers", "KOL Count"],
    "review_count": ["Product Rating", "Reviews", "review_count", "Review Count", "Total Reviews", "Ratings Count"],
    "tiktok_product_url": ["TikTokUrl", "TikTok URL", "Product Link", "product_link", "URL", "url", "Link"],
    "thumbnail_url": ["img_url", "Image", "image", "Image URL", "thumbnail", "Product Image", "Cover Image", "Thumbnail"],
    "live_revenue": ["Live Revenue($)", "Live Revenue"],
    "video_revenue": ["Video Revenue($)", "Video Revenue"],
}


def clean_number(value):
    if not value or str(value).strip() in ("", "-", "N/A", "n/a"):
        return None
    value = str(value).strip().replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    if value.upper().endswith("K"):
        try: return float(value[:-1]) * 1_000
        except: return None
    if value.upper().endswith("M"):
        try: return float(value[:-1]) * 1_000_000
        except: return None
    try: return float(value)
    except: return None


def get_value(row, field):
    for name in COLUMN_MAP.get(field, []):
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def calculate_virality_score(p):
    score = 0
    growth = p.get("gmv_growth_pct") or 0
    if   growth >= 200: score += 35
    elif growth >= 100: score += 28
    elif growth >= 50:  score += 21
    elif growth >= 20:  score += 14
    elif growth >= 0:   score += 7

    daily = (p.get("units_sold_7d") or 0) / 7
    if   daily >= 500: score += 25
    elif daily >= 200: score += 20
    elif daily >= 100: score += 15
    elif daily >= 50:  score += 10
    elif daily >= 20:  score += 5

    creators = p.get("creator_count") or 0
    if   creators >= 500: score += 15
    elif creators >= 200: score += 12
    elif creators >= 100: score += 9
    elif creators >= 50:  score += 6
    elif creators >= 10:  score += 3

    reviews = p.get("review_count") or 0
    if   reviews >= 5000: score += 10
    elif reviews >= 1000: score += 8
    elif reviews >= 500:  score += 6
    elif reviews >= 100:  score += 4
    elif reviews >= 10:   score += 2

    gmv = p.get("gmv_7d") or 0
    if   gmv >= 500_000: score += 10
    elif gmv >= 100_000: score += 8
    elif gmv >= 50_000:  score += 6
    elif gmv >= 10_000:  score += 4
    elif gmv >= 1_000:   score += 2

    price = p.get("tiktok_price") or 0
    if   15 <= price <= 60: score += 5
    elif 10 <= price <= 80: score += 3
    elif price > 0:         score += 1

    return min(score, 100)


def determine_trend_phase(growth):
    if growth is None: return "peak"
    if growth >= 50:   return "rising"
    if growth >= 0:    return "peak"
    return "declining"


def estimate_growth(gmv_7d, video_rev):
    if not gmv_7d or gmv_7d <= 0: return 10
    share = ((video_rev or 0) / gmv_7d) * 100
    if   share >= 60: return 80
    elif share >= 40: return 40
    elif share >= 20: return 10
    else:             return -10


def import_csv(filepath: str):
    print(f"\n{'='*60}")
    print(f"  ProfitScout — CSV Importer")
    print(f"  File: {filepath}")
    print(f"{'='*60}\n")

    print("Connecting to Supabase...")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Connected!\n")

    # Read CSV
    try:
        with open(filepath, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
    except UnicodeDecodeError:
        with open(filepath, newline='', encoding='latin-1') as f:
            rows = list(csv.DictReader(f))

    print(f"Found {len(rows)} rows in CSV\n")

    # Process rows
    products = []
    skipped  = 0

    for i, row in enumerate(rows):
        title = get_value(row, "product_name")
        if not title or str(title).strip() == "":
            skipped += 1
            continue

        gmv_7d    = clean_number(get_value(row, "gmv_7d"))
        video_rev = clean_number(get_value(row, "video_revenue")) or 0

        gmv_growth = clean_number(get_value(row, "gmv_growth_pct"))
        if gmv_growth is None:
            gmv_growth = estimate_growth(gmv_7d, video_rev)

        product = {
            "product_name":              str(title).strip()[:500],
            "category":           str(get_value(row, "category") or "General").strip(),
            "tiktok_price":       clean_number(get_value(row, "tiktok_price")),
            "gmv_7d":             gmv_7d,
            "gmv_growth_pct":     gmv_growth,
            "units_sold_7d":      int(clean_number(get_value(row, "units_sold_7d")) or 0),
            "creator_count":      int(clean_number(get_value(row, "creator_count"))  or 0),
            "review_count":       int(clean_number(get_value(row, "review_count"))   or 0),
            "review_growth_7d":   0,
            "tiktok_product_url": get_value(row, "tiktok_product_url") or "",
            "thumbnail_url":      get_value(row, "thumbnail_url") or "",
            "scraped_at":         datetime.now(timezone.utc).isoformat(),
            "is_active":          True,
        }
        product["virality_score"] = calculate_virality_score(product)
        product["trend_phase"]    = determine_trend_phase(product["gmv_growth_pct"])
        products.append(product)

        if (i + 1) % 25 == 0:
            print(f"  Processed {i+1}/{len(rows)} rows...")

    print(f"\nValid products:   {len(products)}")
    print(f"Skipped (empty):  {skipped}")

    hot    = [p for p in products if p["virality_score"] >= 80]
    rising = [p for p in products if 65 <= p["virality_score"] < 80]
    watch  = [p for p in products if 50 <= p["virality_score"] < 65]
    low    = [p for p in products if p["virality_score"] < 50]

    print(f"\nVirality Distribution:")
    print(f"  Hot    (80-100): {len(hot)}")
    print(f"  Rising (65-79):  {len(rising)}")
    print(f"  Watch  (50-64):  {len(watch)}")
    print(f"  Pass   (<50):    {len(low)}")

    top5 = sorted(products, key=lambda x: x["virality_score"], reverse=True)[:5]
    print(f"\nTop 5 products:")
    for p in top5:
        print(f"  [{p['virality_score']}] {p['product_name'][:60]} — ${p['tiktok_price'] or 'N/A'}")

    # ── Insert one by one with try/except to handle duplicates ────────────────
    print(f"\nInserting {len(products)} products into Supabase...\n")

    DB_FIELDS = [
        "product_name", "category", "tiktok_price", "gmv_7d", "gmv_growth_pct",
        "units_sold_7d", "creator_count", "review_count", "review_growth_7d",
        "tiktok_product_url", "thumbnail_url", "scraped_at", "is_active",
        "virality_score", "trend_phase"
    ]

    inserted   = 0
    duplicates = 0
    errors     = 0

    for idx, p in enumerate(products):
        record = {k: v for k, v in p.items() if k in DB_FIELDS}
        try:
            result = supabase.table("tiktok_products").insert(record).execute()
            if result.data:
                inserted += 1
            # Print progress every 10 inserts
            if (inserted + duplicates + errors) % 10 == 0:
                print(f"  Progress: {inserted} inserted | {duplicates} duplicates | {errors} errors")
        except Exception as e:
            msg = str(e).lower()
            if "duplicate" in msg or "unique" in msg or "23505" in msg:
                duplicates += 1
            else:
                errors += 1
                if errors <= 5:
                    print(f"  ERROR on '{p['product_name'][:40]}': {str(e)[:100]}")

    print(f"\n{'='*60}")
    print(f"  IMPORT COMPLETE")
    print(f"  Inserted:   {inserted}")
    print(f"  Duplicates: {duplicates}")
    print(f"  Errors:     {errors}")
    print(f"  Hot:        {len(hot)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:   python import_products.py data/your_export.csv")
        sys.exit(1)
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"ERROR: File not found -> {path}")
        sys.exit(1)
    import_csv(path)