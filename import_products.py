"""
ProfitScout — FastMoss / Kalodata CSV Importer
===============================================
1. Export CSV from FastMoss or Kalodata
2. Place the CSV file in the same folder as this script
3. Add your Supabase URL and Key below
4. Run:  python import_products.py your_file.csv
"""

import csv
import sys
import re
import os
from datetime import datetime, timezone
from supabase import create_client, Client

# ─── YOUR SUPABASE CREDENTIALS ────────────────────────────────────────────────
# Get these from: Supabase Dashboard → Settings → API
SUPABASE_URL = "https://daxptnybsofquelczugc.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRheHB0bnlic29mcXVlbGN6dWdjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzA4MDIyNzksImV4cCI6MjA4NjM3ODI3OX0.b4ZzDY524NoXrr7C8vHhbHmzY1aRmoJocauwaN5C0Pw"   # Use service_role key for server scripts
# ─── COLUMN NAME MAPPINGS ─────────────────────────────────────────────────────
# FastMoss and Kalodata use different column names — both are handled below.
# If your CSV uses different names, just add them to the lists.

COLUMN_MAP = {
    "title": [
        "Product Name", "product_name", "Title", "title",
        "Product Title", "Name"
    ],
    "category": [
        "Category", "category", "Product Category", "Category Name"
    ],
    "tiktok_price": [
        "Price($)", "Price", "price", "Sale Price",
        "Avg. Unit Price($)", "Min Price"
    ],
    "gmv_7d": [
        "Revenue($)", "Revenue", "revenue", "GMV", "gmv",
        "7D Revenue", "Sales Revenue"
    ],
    "gmv_growth_pct": [
        "Revenue Growth", "revenue_growth", "GMV Growth",
        "Growth Rate", "Revenue Growth Rate", "7D Growth"
    ],
    "units_sold_7d": [
        "Item Sold", "Sales Volume", "sales_volume", "Units Sold",
        "units_sold", "Sold Count", "7D Sales", "Volume"
    ],
    "creator_count": [
        "Creator Number", "Related Creators", "related_creators",
        "Creators", "creator_count", "Influencers", "KOL Count"
    ],
    "review_count": [
        "Product Rating", "Reviews", "review_count", "Review Count",
        "Total Reviews", "Ratings Count"
    ],
    "tiktok_product_url": [
        "TikTokUrl", "TikTok URL", "Product Link", "product_link",
        "URL", "url", "Link"
    ],
    "thumbnail_url": [
        "img_url", "Image", "image", "Image URL", "thumbnail",
        "Product Image", "Cover Image", "Thumbnail"
    ],
    # Kalodata extra columns — stored as bonus data
    "live_revenue": [
        "Live Revenue($)", "Live Revenue"
    ],
    "video_revenue": [
        "Video Revenue($)", "Video Revenue"
    ],
    "commission_rate": [
        "Commission Rate", "commission_rate"
    ],
    "launch_date": [
        "Launch Date", "launch_date"
    ],
    "kalodata_url": [
        "KalodataUrl", "Kalodata URL"
    ],
}


# ─── HELPER: CLEAN NUMERIC VALUES ─────────────────────────────────────────────
def clean_number(value):
    """Remove $, commas, %, K, M from values like '$1,234.56' or '45.2%' or '1.2M'"""
    if not value or str(value).strip() == "":
        return None
    value = str(value).strip()
    value = value.replace("$", "").replace(",", "").replace("%", "").replace(" ", "")

    # Handle K and M suffixes (e.g. 1.2K = 1200, 2.5M = 2500000)
    if value.endswith("K") or value.endswith("k"):
        try:
            return float(value[:-1]) * 1000
        except ValueError:
            return None
    if value.endswith("M") or value.endswith("m"):
        try:
            return float(value[:-1]) * 1000000
        except ValueError:
            return None

    try:
        return float(value)
    except ValueError:
        return None


# ─── HELPER: CALCULATE VIRALITY SCORE ─────────────────────────────────────────
def calculate_virality_score(row):
    """
    Calculate virality score 0-100 based on 6 signals.
    Weights match the ProfitScout algorithm from the build plan.
    """
    score = 0

    # Signal 1: GMV Growth Rate (35% weight — max 35 points)
    growth = row.get("gmv_growth_pct") or 0
    if growth >= 200:   score += 35
    elif growth >= 100: score += 28
    elif growth >= 50:  score += 21
    elif growth >= 20:  score += 14
    elif growth >= 0:   score += 7
    # Negative growth = 0 points

    # Signal 2: Sales Velocity — units/day (25% weight — max 25 points)
    units = row.get("units_sold_7d") or 0
    daily_units = units / 7
    if daily_units >= 500:  score += 25
    elif daily_units >= 200: score += 20
    elif daily_units >= 100: score += 15
    elif daily_units >= 50:  score += 10
    elif daily_units >= 20:  score += 5

    # Signal 3: Creator Count (15% weight — max 15 points)
    creators = row.get("creator_count") or 0
    if creators >= 500:  score += 15
    elif creators >= 200: score += 12
    elif creators >= 100: score += 9
    elif creators >= 50:  score += 6
    elif creators >= 10:  score += 3

    # Signal 4: Review Count as proxy for buyer trust (10% weight — max 10 points)
    reviews = row.get("review_count") or 0
    if reviews >= 5000:  score += 10
    elif reviews >= 1000: score += 8
    elif reviews >= 500:  score += 6
    elif reviews >= 100:  score += 4
    elif reviews >= 10:   score += 2

    # Signal 5: GMV absolute value as market size indicator (10% — max 10 points)
    gmv = row.get("gmv_7d") or 0
    if gmv >= 500000:   score += 10
    elif gmv >= 100000:  score += 8
    elif gmv >= 50000:   score += 6
    elif gmv >= 10000:   score += 4
    elif gmv >= 1000:    score += 2

    # Signal 6: Price range check — sweet spot $15-$60 for TikTok impulse buys (5% — max 5 points)
    price = row.get("tiktok_price") or 0
    if 15 <= price <= 60: score += 5
    elif 10 <= price <= 80: score += 3
    elif price > 0:          score += 1

    return min(score, 100)  # Cap at 100


# ─── HELPER: DETERMINE TREND PHASE ────────────────────────────────────────────
def determine_trend_phase(growth_pct):
    """Simple trend classification based on growth rate."""
    if growth_pct is None:
        return "peak"
    if growth_pct >= 50:
        return "rising"
    elif growth_pct >= 0:
        return "peak"
    else:
        return "declining"


# ─── HELPER: FIND COLUMN VALUE ────────────────────────────────────────────────
def get_value(csv_row, field_name, headers):
    """Find value using any of the possible column name variations."""
    possible_names = COLUMN_MAP.get(field_name, [])
    for name in possible_names:
        if name in csv_row:
            return csv_row[name]
    return None


# ─── MAIN IMPORT FUNCTION ─────────────────────────────────────────────────────
def import_csv(filepath: str):
    print(f"\n{'='*60}")
    print(f"  ProfitScout — CSV Importer")
    print(f"  File: {filepath}")
    print(f"{'='*60}\n")

    # Connect to Supabase
    print("Connecting to Supabase...")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Connected!\n")

    # Read CSV
    with open(filepath, newline='', encoding='latin-1') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        rows = list(reader)

    print(f"Found {len(rows)} products in CSV")
    print(f"CSV Columns detected: {headers}\n")

    # ── Map and clean each row ──
    products = []
    skipped = 0

    for i, row in enumerate(rows):
        title = get_value(row, "title", headers)
        if not title or str(title).strip() == "":
            skipped += 1
            continue

        # Build clean product dict
        gmv_7d      = clean_number(get_value(row, "gmv_7d", headers))
        live_rev    = clean_number(get_value(row, "live_revenue", headers)) or 0
        video_rev   = clean_number(get_value(row, "video_revenue", headers)) or 0
        commission  = clean_number(get_value(row, "commission_rate", headers))

        # Kalodata does not export GMV growth % directly.
        # We estimate it from the ratio of video revenue to total revenue
        # (high video_revenue share = product is trending via creator videos = rising)
        # You can replace this with real growth data if you export a second date range.
        gmv_growth_pct = clean_number(get_value(row, "gmv_growth_pct", headers))
        if gmv_growth_pct is None and gmv_7d and gmv_7d > 0:
            video_share = (video_rev / gmv_7d) * 100 if gmv_7d else 0
            if video_share >= 60:   gmv_growth_pct = 80    # Heavy creator-driven = rising fast
            elif video_share >= 40: gmv_growth_pct = 40    # Mixed = growing
            elif video_share >= 20: gmv_growth_pct = 10    # Mostly mall/live = mature
            else:                   gmv_growth_pct = -10   # Low video = declining interest

        product = {
            "title":              str(title).strip(),
            "category":           get_value(row, "category", headers) or "Skincare",
            "tiktok_price":       clean_number(get_value(row, "tiktok_price", headers)),
            "gmv_7d":             gmv_7d,
            "gmv_growth_pct":     gmv_growth_pct,
            "units_sold_7d":      int(clean_number(get_value(row, "units_sold_7d", headers)) or 0),
            "creator_count":      int(clean_number(get_value(row, "creator_count", headers)) or 0),
            "review_count":       int(clean_number(get_value(row, "review_count", headers)) or 0),
            "review_growth_7d":   0,
            "tiktok_product_url": get_value(row, "tiktok_product_url", headers),
            "thumbnail_url":      get_value(row, "thumbnail_url", headers),
            "scraped_at":         datetime.now(timezone.utc).isoformat(),
            "is_active":          True,
        }

        # Log Kalodata extra fields to console (not stored in DB yet)
        # You can add columns to tiktok_products later for these
        product["_kalodata_commission"] = commission
        product["_kalodata_live_rev"]   = live_rev
        product["_kalodata_video_rev"]  = video_rev
        product["_kalodata_url"]        = get_value(row, "kalodata_url", headers)
        product["_launch_date"]         = get_value(row, "launch_date", headers)

        # Calculate virality score and trend phase
        product["virality_score"] = calculate_virality_score(product)
        product["trend_phase"]    = determine_trend_phase(product["gmv_growth_pct"])

        products.append(product)

        # Progress indicator
        if (i + 1) % 50 == 0:
            print(f"  Processed {i+1}/{len(rows)} rows...")

    print(f"\nProcessed: {len(products)} valid products")
    print(f"Skipped:   {skipped} rows (empty title)")

    # ── Show score distribution preview ──
    hot     = [p for p in products if p["virality_score"] >= 80]
    rising  = [p for p in products if 65 <= p["virality_score"] < 80]
    watch   = [p for p in products if 50 <= p["virality_score"] < 65]
    low     = [p for p in products if p["virality_score"] < 50]

    print(f"\nVirality Score Distribution:")
    print(f"  🔥 Hot (80-100):     {len(hot)} products")
    print(f"  📈 Rising (65-79):   {len(rising)} products")
    print(f"  👀 Watch (50-64):    {len(watch)} products")
    print(f"  ⏭️  Pass (<50):       {len(low)} products")

    # Show top 5 products preview
    top5 = sorted(products, key=lambda x: x["virality_score"], reverse=True)[:5]
    print(f"\nTop 5 products by virality score:")
    for p in top5:
        print(f"  [{p['virality_score']}] {p['title'][:60]} — ${p['tiktok_price'] or 'N/A'}")

    # ── Insert into Supabase ──
    print(f"\nInserting {len(products)} products into Supabase...")
    print("(Duplicates by title will be skipped automatically)\n")

    # Strip internal _kalodata_* tracking fields before sending to Supabase
    db_fields = [
        "title", "category", "tiktok_price", "gmv_7d", "gmv_growth_pct",
        "units_sold_7d", "creator_count", "review_count", "review_growth_7d",
        "tiktok_product_url", "thumbnail_url", "scraped_at", "is_active",
        "virality_score", "trend_phase"
    ]
    clean_products = [{k: v for k, v in p.items() if k in db_fields} for p in products]

    inserted = 0
    errors   = 0
    batch_size = 50

    for i in range(0, len(clean_products), batch_size):
        batch = clean_products[i:i + batch_size]
        try:
            result = supabase.table("tiktok_products").upsert(
                batch,
                on_conflict="title"   # Skip if same title already exists
            ).execute()
            inserted += len(batch)
            print(f"  Inserted batch {i//batch_size + 1} ({inserted}/{len(products)})")
        except Exception as e:
            errors += len(batch)
            print(f"  ERROR in batch {i//batch_size + 1}: {e}")

    # ── Final summary ──
    print(f"\n{'='*60}")
    print(f"  IMPORT COMPLETE")
    print(f"  Inserted:  {inserted} products")
    print(f"  Errors:    {errors}")
    print(f"  Hot products ready to show: {len(hot)}")
    print(f"{'='*60}\n")


# ─── RUN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:  python import_products.py your_export.csv")
        print("Example: python import_products.py fastmoss_export_2025.csv")
        sys.exit(1)

    csv_file = sys.argv[1]

    if not os.path.exists(csv_file):
        print(f"Error: File '{csv_file}' not found.")
        print("Make sure the CSV file is in the same folder as this script.")
        sys.exit(1)

    import_csv(csv_file)