"""
ProfitScout — Export Top Products for Sheet
============================================
Prints your top products with their IDs so you can
easily copy them into the Google Sheet.

Run: python export_products_for_sheet.py
"""

from supabase import create_client
from dotenv import load_dotenv
import os
import csv

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

def export_top_products(limit: int = 50):
    print("Fetching top products from Supabase...\n")

    result = (
        supabase.table("tiktok_products")
        .select("id, product_name, tiktok_price, virality_score, category")
        .eq("is_active", True)
        .order("virality_score", desc=True)
        .limit(limit)
        .execute()
    )

    products = result.data
    print(f"Found {len(products)} products\n")

    # Print table
    print(f"{'#':<4} {'Virality':<10} {'Price':<8} {'Category':<20} {'Product Name':<50} {'ID'}")
    print("─" * 130)

    for i, p in enumerate(products, 1):
        name     = (p.get("product_name") or "")[:48]
        category = (p.get("category") or "")[:18]
        price    = f"${p.get('tiktok_price', 0)}"
        score    = p.get("virality_score", 0)
        pid      = p.get("id", "")
        print(f"{i:<4} {score:<10} {price:<8} {category:<20} {name:<50} {pid}")

    # Also save as CSV for easy copy-paste into Google Sheet
    csv_file = "products_for_sheet.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "product_name",
            "tiktok_product_id",
            "tiktok_price",
            "platform",
            "supplier_url",
            "notes",
            "status",
            "scraped_at"
        ])
        for p in products:
            # Write one row per product — platform and URL left blank for you to fill
            writer.writerow([
                p.get("product_name", ""),
                p.get("id", ""),
                p.get("tiktok_price", 0),
                "alibaba",   # default platform — change if needed
                "",          # supplier_url — you fill this in
                "",          # notes
                "",          # status — leave blank
                ""           # scraped_at — leave blank
            ])

    print(f"\n✅ Saved {len(products)} products to {csv_file}")
    print(f"   Open {csv_file} and fill in the supplier_url column")
    print(f"   Then import into your Google Sheet")

if __name__ == "__main__":
    export_top_products(limit=100)
