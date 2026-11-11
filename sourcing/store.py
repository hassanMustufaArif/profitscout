"""Writes matched suppliers to the shared `suppliers` table (one set per product + platform)."""

from __future__ import annotations

from datetime import datetime, timezone


def _int(v):
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def save_suppliers(sb, product_id: str, platform: str, offers: list[dict], method: str = "photo") -> None:
    # Keep what an admin decided: hand-approved rows stay, hidden rows stay hidden (and their
    # listing is not re-added), only the previous automatic set is replaced.
    existing = (sb.table("suppliers").select("id, match_method, hidden, external_product_id, supplier_url")
                .eq("tiktok_product_id", product_id).eq("platform", platform).execute().data or [])
    keep_keys = {r.get("external_product_id") or r.get("supplier_url")
                 for r in existing if r.get("hidden") or r.get("match_method") == "manual"}
    offers = [o for o in offers if (o.get("product_id") or o.get("url")) not in keep_keys]
    old_ids = [r["id"] for r in existing if not r.get("hidden") and r.get("match_method") != "manual"]
    if not offers:
        return
    rows = [
        {
            "tiktok_product_id": product_id,
            "platform": platform,
            "supplier_name": o["supplier_name"],
            "product_title": o["title"],
            "unit_cost_min": o["price_min"],
            "unit_cost_max": o["price_max"],
            "moq": _int(o["moq"]),
            "shipping_estimate_days": 20 if platform == "alibaba" else 10,
            "shipping_cost_estimate": None,
            "years_on_platform": _int(o.get("supplier_years")),
            "supplier_trust_score": min((o.get("supplier_years") or 0) / 2, 5.0),
            "whitelabel_available": platform == "alibaba",
            "supplier_url": o["url"],
            "image_url": o.get("image_url"),
            "similarity": o.get("similarity"),
            "match_method": method,
            "external_product_id": o["product_id"],
            "orders": _int(o.get("orders")),
            "rating": o.get("rating"),
            "verified_supplier": o.get("verified"),
            "sourced_at": datetime.now(timezone.utc).isoformat(),
        }
        for o in offers
    ]
    # Insert first, then drop the old automatic set, so a failed insert never wipes existing suppliers.
    sb.table("suppliers").insert(rows).execute()
    if old_ids:
        sb.table("suppliers").delete().in_("id", old_ids).execute()


def save_candidates(sb, product_id: str, platform: str, offers: list[dict]) -> None:
    """Near-misses (e.g. same product, different bottle/label) for the admin review queue.
    Replaces this product+platform's PENDING candidates; approved/rejected history is kept,
    and a listing an admin already rejected is never queued again."""
    pid = str(product_id)
    done = (sb.table("supplier_candidates").select("external_product_id, supplier_url")
            .eq("tiktok_product_id", pid).eq("platform", platform).in_("status", ["approved", "rejected"])
            .execute().data or [])
    seen = {r.get("external_product_id") or r.get("supplier_url") for r in done}
    rows = []
    for o in offers:
        key = o.get("product_id") or o.get("url")
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "tiktok_product_id": pid,
            "platform": platform,
            "supplier_name": o.get("supplier_name"),
            "product_title": o.get("title"),
            "unit_cost_min": o.get("price_min"),
            "unit_cost_max": o.get("price_max"),
            "moq": _int(o.get("moq")),
            "supplier_url": o.get("url"),
            "image_url": o.get("image_url"),
            "similarity": round(float(o["similarity"]), 4),
            "match_method": o.get("_method"),
            "external_product_id": o.get("product_id"),
            "orders": _int(o.get("orders")),
            "rating": o.get("rating"),
            "years_on_platform": _int(o.get("supplier_years")),
            "verified_supplier": o.get("verified"),
            "status": "pending",
        })
    sb.table("supplier_candidates").delete().eq("tiktok_product_id", pid).eq("platform", platform).eq("status", "pending").execute()
    if rows:
        sb.table("supplier_candidates").insert(rows).execute()
