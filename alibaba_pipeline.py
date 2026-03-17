"""
TikProfitPro — Alibaba Auto Supplier Pipeline
==============================================
Fetches every active product from Supabase one by one, searches Alibaba
using Oxylabs, calculates profit for each result, then saves the top 3
most profitable suppliers to the suppliers table.

Location : F:/supplier_tool/alibaba_pipeline.py

Requirements:
    pip install requests python-dotenv beautifulsoup4 lxml

.env file (same folder):
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=your_service_role_key

Usage:
    python alibaba_pipeline.py                   # process all products
    python alibaba_pipeline.py --limit 10        # first 10 products
    python alibaba_pipeline.py --min-margin 20   # only save if margin >= 20%
    python alibaba_pipeline.py --skip-existing   # skip products with 3+ suppliers
    python alibaba_pipeline.py --test            # dry run (no DB writes)
"""

import os
import re
import uuid
import json
import time
import argparse
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

# ── Credentials ────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
OXYLABS_USERNAME  = "Hassan_Yb7vy"
OXYLABS_PASSWORD  = "Private=12364"

# ── Profit assumptions ─────────────────────────────────────────────────────────
TIKTOK_FEE_PCT    = 0.08    # 8% TikTok Shop commission
PAYMENT_FEE_PCT   = 0.029   # 2.9% payment processing
CUSTOMER_SHIP     = 0.0     # shipping cost to customer (0 = free shipping)
MIN_MARGIN_PCT    = 15.0    # default minimum margin to save a supplier

# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }

def sb_get(table, params=""):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{params}",
        headers=_headers(), timeout=15
    )
    r.raise_for_status()
    return r.json()

def sb_insert(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**_headers(), "Prefer": "return=representation"},
        json=data, timeout=15
    )
    r.raise_for_status()
    return r.json()

def supplier_count(product_id):
    """Return how many supplier rows already exist for this product."""
    try:
        rows = sb_get("suppliers", f"tiktok_product_id=eq.{product_id}&select=id")
        return len(rows)
    except Exception:
        return 0

# ══════════════════════════════════════════════════════════════════════════════
# FETCH PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_products(limit):
    params = (
        "select=id,product_name,category,tiktok_price,"
        "virality_score,trend_phase,thumbnail_url"
        "&is_active=eq.true"
        "&order=virality_score.desc"
        f"&limit={limit}"
    )
    return sb_get("tiktok_products", params)

# ══════════════════════════════════════════════════════════════════════════════
# ALIBABA SEARCH  (your son's scraper, integrated)
# ══════════════════════════════════════════════════════════════════════════════

def search_alibaba(keyword: str) -> list:
    """
    Search Alibaba via Oxylabs.
    Returns list of dicts: {name, price, seller, url}
    """
    payload = {
        "source":     "alibaba_search",
        "query":      keyword,
        "start_page": 1,
        "render":     "html",
    }

    try:
        resp = requests.post(
            "https://realtime.oxylabs.io/v1/queries",
            auth=(OXYLABS_USERNAME, OXYLABS_PASSWORD),
            json=payload,
            timeout=60,
        )
    except requests.exceptions.Timeout:
        print("    ⚠️  Oxylabs request timed out")
        return []
    except Exception as e:
        print(f"    ⚠️  Network error: {e}")
        return []

    if resp.status_code != 200:
        print(f"    ⚠️  Oxylabs returned HTTP {resp.status_code}: {resp.text[:120]}")
        return []

    try:
        html = resp.json()["results"][0]["content"]
    except (KeyError, IndexError):
        print("    ⚠️  Unexpected Oxylabs response structure")
        return []

    soup = BeautifulSoup(html, "html.parser")

    # Multiple selector fallbacks — Alibaba changes layout often
    cards = (
        soup.select("div.fy23-search-card") or
        soup.select("div[data-content='productItem']") or
        soup.select("div.organic-list-offer-outter") or
        soup.select("div.list-no-v2-main")
    )

    results = []
    for card in cards:
        name   = (card.select_one("h2.search-card-e-title span") or
                  card.select_one(".search-card-e-title"))
        price  = (card.select_one("div.search-card-e-price-main") or
                  card.select_one(".search-card-e-price-main"))
        seller = (card.select_one("a.search-card-e-company") or
                  card.select_one(".search-card-e-company"))
        link   = (card.select_one("a.search-card-e-slider__link") or
                  card.select_one("a[href*='product-detail']") or
                  card.select_one("a[href*='alibaba.com']"))

        name_txt   = name.get_text(strip=True)   if name   else None
        price_txt  = price.get_text(strip=True)  if price  else None
        seller_txt = seller.get_text(strip=True) if seller else None

        href = link["href"] if link and link.has_attr("href") else None
        if href:
            url = "https:" + href if href.startswith("//") else href
        else:
            url = None

        if not name_txt:
            continue

        results.append({
            "name":   name_txt,
            "price":  price_txt,
            "seller": seller_txt or "Unknown",
            "url":    url,
        })

    return results[:5]   # top 5 cards

# ══════════════════════════════════════════════════════════════════════════════
# PRICE PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_price(price_str) -> float | None:
    """
    Handle formats like:
      '$1.50 - $3.20'   → 1.50  (take minimum)
      'US $0.85/piece'  → 0.85
      '¥10.00'          → None  (skip non-USD)
    """
    if not price_str:
        return None
    # Strip everything except digits, dots, hyphens, spaces
    cleaned = re.sub(r'[^\d.\-\s]', ' ', price_str)
    numbers = re.findall(r'\d+\.?\d*', cleaned)
    if not numbers:
        return None
    try:
        return float(numbers[0])   # first number = min price
    except ValueError:
        return None

# ══════════════════════════════════════════════════════════════════════════════
# PROFIT CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def calc_profit(tiktok_price: float, supplier_cost: float) -> dict:
    """
    Returns full profit breakdown.
    Costs assumed:
      - TikTok fee  : 8% of sale price
      - Payment fee : 2.9% of sale price
      - Customer shipping: passed in (default 0)
    """
    tiktok_fee  = tiktok_price * TIKTOK_FEE_PCT
    payment_fee = tiktok_price * PAYMENT_FEE_PCT
    total_cost  = supplier_cost + CUSTOMER_SHIP + tiktok_fee + payment_fee
    net_profit  = tiktok_price - total_cost
    margin_pct  = (net_profit / tiktok_price * 100) if tiktok_price > 0 else 0
    roi_pct     = (net_profit / total_cost * 100)   if total_cost > 0 else 0

    return {
        "supplier_cost":  round(supplier_cost, 2),
        "tiktok_fee":     round(tiktok_fee, 2),
        "payment_fee":    round(payment_fee, 2),
        "total_cost":     round(total_cost, 2),
        "net_profit":     round(net_profit, 2),
        "margin_pct":     round(margin_pct, 1),
        "roi_pct":        round(roi_pct, 1),
    }

def margin_label(pct: float) -> str:
    if pct >= 40: return "🟢 Excellent"
    if pct >= 25: return "🟡 Good"
    if pct >= 15: return "🟠 Low"
    return "🔴 Poor"

# ══════════════════════════════════════════════════════════════════════════════
# SAVE SUPPLIER  (uses exact schema from existing supplier_tool.py)
# ══════════════════════════════════════════════════════════════════════════════

def save_supplier(product_id: str, item: dict, profit: dict, dry_run: bool):
    record = {
        "id":                      str(uuid.uuid4()),
        "tiktok_product_id":       product_id,
        "platform":                "alibaba",
        "supplier_name":           (item.get("seller") or "Unknown")[:100],
        "product_title":           (item.get("name")   or "Unknown")[:200],
        "unit_cost_min":           profit["supplier_cost"],
        "unit_cost_max":           profit["supplier_cost"],
        "moq":                     1,
        "shipping_estimate_days":  20,
        "whitelabel_available":    False,
        "supplier_url":            item.get("url"),
        "created_at":              datetime.now(timezone.utc).isoformat(),
    }
    # Strip None values to avoid Supabase type errors
    record = {k: v for k, v in record.items() if v is not None}

    if dry_run:
        print(f"    [DRY RUN] Would save: {record['supplier_name'][:40]} "
              f"@ ${profit['supplier_cost']:.2f} → margin {profit['margin_pct']:.1f}%")
        return True

    try:
        sb_insert("suppliers", record)
        return True
    except Exception as e:
        print(f"    ❌ DB insert failed: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# PROCESS ONE PRODUCT
# ══════════════════════════════════════════════════════════════════════════════

def process_product(product: dict, min_margin: float, dry_run: bool) -> dict:
    product_id   = product["id"]
    name         = (product.get("product_name") or "Unknown")
    tiktok_price = float(product.get("tiktok_price") or 0)

    # Clean up keyword for better Alibaba search results
    keyword = re.sub(
        r'\b(for (iPhone|Android|Women|Men|Kids)|Pack of \d+|Set of \d+)\b',
        '', name, flags=re.IGNORECASE
    ).strip()
    keyword = re.sub(r'\s+', ' ', keyword)[:80]  # noqa: W605

    # ── Search ──────────────────────────────────────────────────────────────
    alibaba_items = search_alibaba(keyword)

    if not alibaba_items:
        return {"status": "no_results", "saved": 0}

    print(f"    📊 Calculating profit for {len(alibaba_items)} results "
          f"(TikTok price: ${tiktok_price:.2f})")

    # ── Score each result ────────────────────────────────────────────────────
    scored = []
    for item in alibaba_items:
        cost = parse_price(item["price"])
        if cost is None or cost <= 0:
            print(f"    ⚠️  Skipping (no parseable price): {item['price']!r}")
            continue
        if tiktok_price <= 0:
            print(f"    ⚠️  Skipping (TikTok price is $0)")
            continue
        if cost >= tiktok_price:
            print(f"    ⚠️  Skipping (cost ${cost:.2f} ≥ TikTok price ${tiktok_price:.2f})")
            continue
        # Skip suspiciously cheap prices — likely sample/inquiry placeholders
        if cost < 0.10:
            print(f"    ⚠️  Skipping (cost ${cost:.2f} too low — likely sample price)")
            continue

        profit = calc_profit(tiktok_price, cost)
        scored.append({"item": item, "profit": profit})

        seller = (item.get("seller") or "Unknown")[:28]
        print(f"    {'  ' + seller:<30} cost ${cost:>6.2f}  "
              f"margin {profit['margin_pct']:>5.1f}%  {margin_label(profit['margin_pct'])}")

    if not scored:
        return {"status": "no_valid_prices", "saved": 0}

    # ── Sort by margin (best first) ──────────────────────────────────────────
    scored.sort(key=lambda x: x["profit"]["margin_pct"], reverse=True)

    # ── Filter by minimum margin ─────────────────────────────────────────────
    qualified = [s for s in scored if s["profit"]["margin_pct"] >= min_margin]

    if not qualified:
        best = scored[0]["profit"]["margin_pct"]
        print(f"    ⚠️  Best margin was {best:.1f}% — below {min_margin}% threshold, skipping")
        return {"status": "below_margin", "saved": 0, "best_margin": best}

    # ── Save top 3 ───────────────────────────────────────────────────────────
    top3  = qualified[:3]
    saved = 0

    print(f"    💾 Saving top {len(top3)} supplier(s)...")
    for entry in top3:
        ok = save_supplier(product_id, entry["item"], entry["profit"], dry_run)
        if ok:
            saved += 1

    return {
        "status":       "ok",
        "saved":        saved,
        "best_margin":  top3[0]["profit"]["margin_pct"],
        "best_cost":    top3[0]["profit"]["supplier_cost"],
        "tiktok_price": tiktok_price,
        "product_name": name,
    }

# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run(limit: int, min_margin: float, skip_existing: bool,
        delay: float, dry_run: bool):

    tag = " [DRY RUN]" if dry_run else ""
    print("=" * 66)
    print(f"  TikProfitPro — Alibaba Auto Supplier Pipeline{tag}")
    print("=" * 66)
    print(f"  Products limit   : {limit}")
    print(f"  Min margin       : {min_margin}%")
    print(f"  Skip existing    : {skip_existing}")
    print(f"  Request delay    : {delay}s")
    print(f"  Dry run          : {dry_run}")
    print("=" * 66)

    # ── Fetch products ───────────────────────────────────────────────────────
    print(f"\n📦 Fetching products from Supabase...")
    try:
        products = fetch_products(limit)
    except Exception as e:
        print(f"❌ Cannot fetch products: {e}")
        return

    if not products:
        print("❌ No active products found in Supabase.")
        return

    print(f"✅ {len(products)} products loaded\n")

    # ── Counters ─────────────────────────────────────────────────────────────
    total        = len(products)
    processed    = 0
    skipped      = 0
    ok_count     = 0
    no_results   = 0
    below_margin = 0
    total_saved  = 0
    results      = []

    # ── Loop through each product ────────────────────────────────────────────
    for i, product in enumerate(products, 1):
        name         = (product.get("product_name") or "Unknown")[:55]
        tiktok_price = float(product.get("tiktok_price") or 0)
        product_id   = product["id"]

        print(f"\n{'─'*66}")
        print(f"  [{i}/{total}]  {name}")
        print(f"  Price: ${tiktok_price:.2f}  |  "
              f"Virality: {product.get('virality_score','?')}  |  "
              f"Trend: {product.get('trend_phase','?')}")

        # Skip if no price
        if tiktok_price <= 0:
            print("  ⏭️  No TikTok price — skipping")
            skipped += 1
            continue

        # Skip if already has enough suppliers
        if skip_existing:
            n = supplier_count(product_id)
            if n >= 3:
                print(f"  ⏭️  Already has {n} suppliers — skipping")
                skipped += 1
                continue

        processed += 1
        result = process_product(product, min_margin, dry_run)
        results.append({**result, "product_name": name})

        if result["status"] == "ok":
            ok_count    += 1
            total_saved += result["saved"]
            print(f"  ✅ {result['saved']} supplier(s) saved  "
                  f"(best margin: {result['best_margin']:.1f}%)")
        elif result["status"] == "no_results":
            no_results += 1
        elif result["status"] in ("below_margin", "no_valid_prices"):
            below_margin += 1

        # Rate limit
        if i < total:
            time.sleep(delay)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*66}")
    print("  PIPELINE COMPLETE")
    print(f"{'═'*66}")
    print(f"  Products total       : {total}")
    print(f"  Processed            : {processed}")
    print(f"  Skipped              : {skipped}")
    print(f"  ✅ Suppliers found   : {ok_count} products")
    print(f"  ❌ No Alibaba results: {no_results}")
    print(f"  ⚠️  Below margin      : {below_margin}")
    print(f"  💾 Total rows saved  : {total_saved}")
    print(f"{'═'*66}")

    # ── Top 10 best products by margin ───────────────────────────────────────
    winners = sorted(
        [r for r in results if r.get("status") == "ok"],
        key=lambda x: x.get("best_margin", 0),
        reverse=True
    )[:10]

    if winners:
        print(f"\n🏆  TOP {len(winners)} MOST PROFITABLE PRODUCTS")
        print(f"  {'Product':<42}  {'TikTok':>7}  {'Cost':>7}  {'Margin':>8}")
        print(f"  {'─'*42}  {'─'*7}  {'─'*7}  {'─'*8}")
        for r in winners:
            print(f"  {r.get('product_name','')[:42]:<42}  "
                  f"${r.get('tiktok_price',0):>6.2f}  "
                  f"${r.get('best_cost',0):>6.2f}  "
                  f"{r.get('best_margin',0):>7.1f}%")

    print(f"\n{'═'*66}\n")

# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TikProfitPro — Alibaba Auto Supplier Pipeline"
    )
    parser.add_argument("--limit",         type=int,   default=50,
                        help="Max products to process (default 50)")
    parser.add_argument("--min-margin",    type=float, default=MIN_MARGIN_PCT,
                        help=f"Min profit margin %% to save (default {MIN_MARGIN_PCT})")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip products that already have 3+ suppliers")
    parser.add_argument("--delay",         type=float, default=2.0,
                        help="Seconds between Oxylabs requests (default 2.0)")
    parser.add_argument("--test",          action="store_true",
                        help="Process only first 3 products (for testing)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Run without writing anything to Supabase")
    args = parser.parse_args()

    if args.test:
        print("🧪  TEST MODE — first 3 products only\n")
        args.limit = 3

    run(
        limit         = args.limit,
        min_margin    = args.min_margin,
        skip_existing = args.skip_existing,
        delay         = args.delay,
        dry_run       = args.dry_run,
    )