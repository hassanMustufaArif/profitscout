"""
TikProfitPro — Kalodata importer v2
===================================
Replaces import_products.py. Fixes the data glitches in v1:

  v1 problem                                   v2
  ------------------------------------------   ---------------------------------------------
  30-day revenue saved as "gmv_7d"             saved as revenue_window + window_days; gmv_7d
                                               becomes a real 7-day average
  daily sales = 30-day units / 7 (4x too high) units / window_days
  growth was GUESSED from video-revenue share  growth comes from comparing two real exports
  (80/40/10/-10) -> fake trend phases          of the same product; unknown until 2nd export
  "Product Rating" (4.2) saved as review_count saved as rating
  price "28.99-39.90" -> None                  price_min / price_max, avg unit price as price
  insert-only -> duplicates, no history        upsert on tiktok_product_id + snapshot history
  scraped_at = import time                     data_start_date / data_end_date from the export
  TikTok CDN image links expire                optional --rehost copies images to Supabase Storage

Usage
-----
  python import_kalodata.py products data/kitchen_export.csv            # import
  python import_kalodata.py products data/*.csv --dry-run              # check only, no writes
  python import_kalodata.py products data/kitchen_export.csv --rehost   # also copy images
  python import_kalodata.py creators data/creators_export.csv

Weekly routine (see docs/DATA_PIPELINE.md): export the same Kalodata views with the same
date-range length every week, run this script, done. After the 2nd week, growth and stages
become real.
"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

BATCH = 200


# ─── parsing helpers ─────────────────────────────────────────────────────────
def num(v):
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    if s in ("", "-", "N/A", "n/a", "--"):
        return None
    mult = 1
    if s[-1:].upper() == "K":
        mult, s = 1_000, s[:-1]
    elif s[-1:].upper() == "M":
        mult, s = 1_000_000, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def price_range(v):
    if not v:
        return None, None
    parts = [num(p) for p in str(v).split("-")]
    parts = [p for p in parts if p is not None]
    if not parts:
        return None, None
    return min(parts), max(parts)


def parse_date(v):
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def date_range(v):
    """'2026-01-25~2026-02-23' -> (start, end, days)"""
    if not v or "~" not in str(v):
        return None, None, None
    a, b = str(v).split("~", 1)
    s, e = parse_date(a), parse_date(b)
    if not s or not e:
        return None, None, None
    return s, e, (e - s).days + 1


def tiktok_id(row):
    for key in ("TikTokUrl", "KalodataUrl"):
        url = row.get(key) or ""
        m = re.search(r"/pdp/(\d+)", url) or re.search(r"[?&]id=(\d+)", url)
        if m:
            return m.group(1)
    return None


def col(row, *names):
    for n in names:
        if n in row and str(row[n]).strip() not in ("", "-"):
            return row[n]
    return None


# ─── scoring (only from real numbers) ────────────────────────────────────────
def stage(growth_pct, launch: date | None, creators: int, end: date):
    """Emerging / Rising / Peaking / Saturated / Unknown, from real history only."""
    age_days = (end - launch).days if launch else None
    if growth_pct is None:
        if age_days is not None and age_days <= 45:
            return "emerging"
        return "unknown"
    if age_days is not None and age_days <= 60 and growth_pct >= 30:
        return "emerging"
    if growth_pct >= 30:
        return "rising"
    if growth_pct >= -15:
        return "saturated" if creators >= 400 else "peaking"
    return "saturated"


def momentum_score(rev_per_day, growth_pct, creators, conv_pct, rating):
    """0-100. Log-scaled revenue so a $3k/day product isn't 'Pass' next to a $300k/day one."""
    s = 0.0
    if rev_per_day:
        s += min(40, max(0, (math.log10(rev_per_day + 1) - 2) * 16))   # $100/day≈0 … $31k/day≈40
    if growth_pct is not None:
        s += max(0, min(30, growth_pct / 5 + 10))                        # -50%→0, 0%→10, +100%→30
    else:
        s += 10                                                          # neutral when unknown
    if creators:
        s += min(15, math.log10(creators + 1) * 6)
    if conv_pct:
        s += min(10, conv_pct / 5)
    if rating:
        s += max(0, (rating - 4.0) * 5)                                  # 4.0→0, 5.0→5
    return int(round(min(s, 100)))


# ─── products ────────────────────────────────────────────────────────────────
def load_rows(paths):
    rows = []
    for p in paths:
        for f in glob.glob(p):
            try:
                with open(f, newline="", encoding="utf-8-sig") as fh:
                    rows += [dict(r, _file=os.path.basename(f)) for r in csv.DictReader(fh)]
            except UnicodeDecodeError:
                with open(f, newline="", encoding="latin-1") as fh:
                    rows += [dict(r, _file=os.path.basename(f)) for r in csv.DictReader(fh)]
    return rows


def parse_product(row):
    pid = tiktok_id(row)
    name = (col(row, "Product Name") or "").strip()
    start, end, days = date_range(col(row, "Date Range"))
    if not pid or not name or not end:
        return None, f"skipped (missing id/name/date range): {name[:50]}"
    pmin, pmax = price_range(col(row, "Price($)"))
    avg_price = num(col(row, "Avg. Unit Price($)")) or pmin
    revenue = num(col(row, "Revenue($)"))
    units = num(col(row, "Item Sold"))
    p = {
        "tiktok_product_id": pid,
        "product_name": name[:500],
        "category": (col(row, "Category") or "General").strip(),
        "tiktok_price": round(avg_price, 2) if avg_price else None,
        "price_min": pmin,
        "price_max": pmax,
        "rating": num(col(row, "Product Rating")),
        "commission_rate": num(col(row, "Commission Rate")),
        "launch_date": str(parse_date(col(row, "Launch Date"))) if parse_date(col(row, "Launch Date")) else None,
        "creator_count": int(num(col(row, "Creator Number")) or 0),
        "creator_conversion_pct": num(col(row, "Creator Conversion Ratio")),
        "revenue_window": revenue,
        "units_window": int(units) if units else None,
        "data_start_date": str(start),
        "data_end_date": str(end),
        "window_days": days,
        # legacy columns the frontend reads, now honest 7-day averages
        "gmv_7d": round(revenue / days * 7, 2) if revenue and days else None,
        "units_sold_7d": int(units / days * 7) if units and days else 0,
        "live_revenue": num(col(row, "Live Revenue($)")),
        "video_revenue": num(col(row, "Video Revenue($)")),
        "tiktok_product_url": f"https://www.tiktok.com/shop/pdp/{pid}",
        "thumbnail_url": col(row, "img_url") or "",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
    }
    snap = {
        "tiktok_product_id": pid,
        "data_start_date": str(start),
        "data_end_date": str(end),
        "window_days": days,
        "revenue": revenue,
        "units_sold": p["units_window"],
        "avg_unit_price": avg_price,
        "creator_count": p["creator_count"],
        "video_revenue": p["video_revenue"],
        "live_revenue": p["live_revenue"],
        "mall_revenue": num(col(row, "Shopping Mall Revenue($)")),
        "rating": p["rating"],
    }
    return (p, snap), None


def quality_flags(p):
    flags = []
    if p["price_min"] and p["tiktok_price"] and p["tiktok_price"] > p["price_min"] * 3:
        flags.append("avg price >3x list price")
    if p["tiktok_price"] and p["tiktok_price"] > 500:
        flags.append("price > $500, check")
    if not p["thumbnail_url"]:
        flags.append("no image")
    if p["units_window"] and p["revenue_window"] and p["tiktok_price"]:
        implied = p["revenue_window"] / p["units_window"]
        if abs(implied - p["tiktok_price"]) / p["tiktok_price"] > 0.6:
            flags.append("revenue/units doesn't match price")
    return flags


def rehost_image(sb: Client, pid: str, url: str) -> str | None:
    from image_search import fetch_image_bytes
    img = fetch_image_bytes(url)
    if not img:
        return None
    path = f"products/{pid}.jpg"
    try:
        sb.storage.from_("product-images").upload(path, img, {"content-type": "image/jpeg", "upsert": "true"})
        return sb.storage.from_("product-images").get_public_url(path)
    except Exception as e:
        print(f"  image upload failed for {pid}: {e}")
        return None


def growth_from_history(sb: Client, pid: str, end: str, days: int):
    """% change in revenue/day vs the previous snapshot with the same window length."""
    res = (
        sb.table("product_snapshots").select("revenue, window_days, data_end_date")
        .eq("tiktok_product_id", pid).lt("data_end_date", end)
        .order("data_end_date", desc=True).limit(5).execute()
    )
    prev = next((r for r in res.data or [] if r["window_days"] == days and r["revenue"]), None)
    return prev


def import_products(paths, dry_run=False, rehost=False, deactivate=False):
    rows = load_rows(paths)
    print(f"{len(rows)} rows from {len(paths)} file pattern(s)")
    parsed, problems = {}, []
    for r in rows:
        out, err = parse_product(r)
        if err:
            problems.append(err)
            continue
        p, snap = out
        # same product in several category files: keep the one with most revenue
        if p["tiktok_product_id"] in parsed and (parsed[p["tiktok_product_id"]][0]["revenue_window"] or 0) >= (p["revenue_window"] or 0):
            continue
        parsed[p["tiktok_product_id"]] = (p, snap)

    flagged = [(p["product_name"][:60], quality_flags(p)) for p, _ in parsed.values() if quality_flags(p)]
    print(f"{len(parsed)} unique products, {len(problems)} skipped, {len(flagged)} with quality flags")
    for name, f in flagged[:25]:
        print(f"  ⚠ {name}: {', '.join(f)}")

    if dry_run:
        print("Dry run: nothing written.")
        return

    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    items = list(parsed.values())

    # 1) snapshots first, so growth can be computed against history
    snaps = [s for _, s in items]
    for i in range(0, len(snaps), BATCH):
        sb.table("product_snapshots").upsert(snaps[i:i + BATCH], on_conflict="tiktok_product_id,data_end_date,window_days").execute()

    # 2) products with real growth / stage / score
    records = []
    for p, s in items:
        prev = growth_from_history(sb, p["tiktok_product_id"], p["data_end_date"], p["window_days"])
        rev_day = (p["revenue_window"] or 0) / (p["window_days"] or 1)
        if prev and prev["revenue"]:
            prev_day = prev["revenue"] / prev["window_days"]
            growth = round((rev_day - prev_day) / prev_day * 100, 1) if prev_day else None
            p["growth_source"] = "snapshot"
        else:
            growth = None
            p["growth_source"] = "unknown"
        p["gmv_growth_pct"] = growth
        launch = parse_date(p["launch_date"]) if p["launch_date"] else None
        p["trend_phase"] = stage(growth, launch, p["creator_count"], parse_date(p["data_end_date"]))
        p["virality_score"] = momentum_score(rev_day, growth, p["creator_count"], p["creator_conversion_pct"], p["rating"])
        if rehost and p["thumbnail_url"]:
            p["image_storage_url"] = rehost_image(sb, p["tiktok_product_id"], p["thumbnail_url"])
        records.append(p)

    for i in range(0, len(records), BATCH):
        sb.table("tiktok_products").upsert(records[i:i + BATCH], on_conflict="tiktok_product_id").execute()
        print(f"  upserted {min(i + BATCH, len(records))}/{len(records)}")

    # 3) cached suppliers were matched against the old title/price: keep them, the
    #    profit is recalculated live from tiktok_price on every request.

    # 4) retire stale rows: v1 rows (no data_end_date) and anything not seen for 3+ weeks
    if deactivate:
        newest = max(parse_date(p["data_end_date"]) for p, _ in items)
        cutoff = str(newest - timedelta(days=21))
        sb.table("tiktok_products").update({"is_active": False}).is_("data_end_date", "null").execute()
        sb.table("tiktok_products").update({"is_active": False}).lt("data_end_date", cutoff).execute()
        print(f"  retired v1 rows and products last seen before {cutoff}")
    print("Done.")


# ─── creators ────────────────────────────────────────────────────────────────
CREATOR_MAP = {
    "tiktok_handle": ["Creator Handle", "Handle", "Creator ID", "Username", "TikTok Handle", "Creator"],
    "creator_name": ["Creator Name", "Nickname", "Name"],
    "followers": ["Followers", "Follower Count", "Fans"],
    "gmv_total": ["Revenue($)", "GMV($)", "Revenue", "Sales($)"],
    "avg_views": ["Avg. Views", "Average Views", "Avg Video Views"],
    "engagement_rate": ["Engagement Rate", "Avg. Engagement Rate"],
    "category": ["Category", "Main Category"],
    "profile_url": ["TikTokUrl", "TikTok URL", "Profile URL"],
    "video_gmv": ["Video Revenue($)", "Video GMV($)"],
    "live_gmv": ["Live Revenue($)", "Live GMV($)"],
    "product_count": ["Product Number", "Products", "Product Count"],
    "conversion_rate": ["Conversion Rate", "Creator Conversion Ratio", "CVR"],
    "item_sold": ["Item Sold", "Items Sold", "Units Sold"],
    "avg_unit_price": ["Avg. Unit Price($)", "Average Unit Price($)"],
    "new_followers": ["New Followers", "Follower Growth"],
    "debut_date": ["Debut Date", "First Video Date"],
    "contact_email": ["Email", "Contact Email", "Business Email"],
}


def import_creators(paths, dry_run=False):
    rows = load_rows(paths)
    if rows:
        known = {n for names in CREATOR_MAP.values() for n in names} | {"_file", "Date Range", "KalodataUrl"}
        unmapped = [c for c in rows[0].keys() if c not in known]
        if unmapped:
            print(f"Columns not imported (tell Hark if you need them): {unmapped}")
    records = {}
    for r in rows:
        rec = {}
        for field, names in CREATOR_MAP.items():
            v = col(r, *names)
            if field in ("followers", "avg_views", "product_count", "item_sold", "new_followers"):
                v = int(num(v) or 0) if v not in (None, "") else None
            elif field in ("gmv_total", "engagement_rate", "video_gmv", "live_gmv", "conversion_rate", "avg_unit_price"):
                v = num(v)
            elif field == "debut_date":
                d = parse_date(v) if v else None
                v = str(d) if d else None
            rec[field] = v
        handle = (rec.get("tiktok_handle") or "").strip().lstrip("@")
        if not handle:
            continue
        rec["tiktok_handle"] = handle
        rec["profile_url"] = rec.get("profile_url") or f"https://www.tiktok.com/@{handle}"
        # columns the app reads (Products page > Creators tab)
        rec["username"] = handle
        rec["tiktok_url"] = rec["profile_url"]
        rec["niche"] = rec.get("category")
        rec["products_promoted"] = rec.get("product_count")
        _, end, _ = date_range(col(r, "Date Range"))
        rec["data_end_date"] = str(end) if end else None
        records[handle] = {k: v for k, v in rec.items() if v is not None}
    print(f"{len(records)} creators parsed")
    if dry_run or not records:
        return
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    vals = list(records.values())
    for i in range(0, len(vals), BATCH):
        sb.table("creators").upsert(vals[i:i + BATCH], on_conflict="tiktok_handle").execute()
    print("Done.")



# ─── shops ───────────────────────────────────────────────────────────────────
SHOP_MAP = {   # field names = what the app reads (Products page > New shops tab)
    "shop_name": ["Shop Name", "Shop", "Store Name", "Seller Name"],
    "gmv_total": ["Revenue($)", "GMV($)", "Revenue", "Sales($)"],
    "gmv_growth_pct": ["Revenue Growth Rate", "Growth Rate", "Revenue Growth"],
    "item_sold": ["Item Sold", "Items Sold", "Units Sold"],
    "product_count": ["Product Number", "Products", "Product Count"],
    "avg_unit_price": ["Avg. Unit Price($)", "Average Unit Price($)"],
    "live_revenue": ["Live Revenue($)"],
    "video_revenue": ["Video Revenue($)"],
    "product_card_revenue": ["Shopping Mall Revenue($)", "Product Card Revenue($)"],
    "affiliate_revenue": ["Affiliate Revenue($)", "Creator Revenue($)"],
    "self_operated_revenue": ["Self-operated Revenue($)", "Self Operated Revenue($)", "Official Account Revenue($)"],
    "seller_type": ["Seller Type", "Shop Type"],
    "follower_count": ["Followers", "Shop Followers"],
    "category": ["Category", "Main Category"],
    "shop_created_at": ["Shop Launch Date", "Launch Date", "Opening Date", "Created"],
    "shop_url": ["TikTokUrl", "TikTok URL", "Shop URL"],
}


def import_shops(paths, dry_run=False):
    rows = load_rows(paths)
    if rows:
        known = {n for names in SHOP_MAP.values() for n in names} | {"_file", "Date Range", "KalodataUrl", "img_url"}
        unmapped = [c for c in rows[0].keys() if c not in known]
        if unmapped:
            print(f"Columns not imported (tell Hark if you need them): {unmapped}")
    records = {}
    for r in rows:
        rec = {}
        for field, names in SHOP_MAP.items():
            v = col(r, *names)
            if field in ("item_sold", "product_count", "follower_count"):
                v = int(num(v) or 0) if v not in (None, "") else None
            elif field in ("gmv_total", "gmv_growth_pct", "avg_unit_price", "live_revenue", "video_revenue",
                           "product_card_revenue", "affiliate_revenue", "self_operated_revenue"):
                v = num(v)
            elif field == "shop_created_at":
                d = parse_date(v) if v else None
                v = str(d) if d else None
            rec[field] = v
        name = (rec.get("shop_name") or "").strip()
        if not name:
            continue
        rec["shop_name"] = name
        rec["image_url"] = col(r, "img_url", "Logo")
        _, end, _ = date_range(col(r, "Date Range"))
        rec["data_end_date"] = str(end) if end else None
        records[name.lower()] = {k: v for k, v in rec.items() if v is not None}
    print(f"{len(records)} shops parsed")
    if dry_run or not records:
        return
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    vals = list(records.values())
    for i in range(0, len(vals), BATCH):
        sb.table("shops").upsert(vals[i:i + BATCH], on_conflict="shop_name").execute()
    print("Done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["products", "creators", "shops"])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rehost", action="store_true", help="copy product images to Supabase Storage bucket 'product-images'")
    ap.add_argument("--deactivate", action="store_true",
                    help="after a FULL weekly import: hide v1 rows and products not seen for 3+ weeks")
    a = ap.parse_args()
    if a.kind == "products":
        import_products(a.files, a.dry_run, a.rehost, a.deactivate)
    elif a.kind == "shops":
        import_shops(a.files, a.dry_run)
    else:
        import_creators(a.files, a.dry_run)
