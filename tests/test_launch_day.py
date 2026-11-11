"""Launch-day fixes (2026-11-11). Offline, same stubs as test_security.py."""
from __future__ import annotations

import json
import uuid

import pytest

import test_security as ts          # sets env + patches supabase before importing main
from test_security import DB, H, client, main, seed_products, send_webhook, plans, _sign


@pytest.fixture(autouse=True)
def _reset():
    DB.reset()
    main._free_cache.clear()
    main.limiter.reset()
    main._manual_limits.storage.reset()
    yield


def auto_supplier(pid, sid=1, platform="alibaba", **kw):
    row = {"id": sid, "tiktok_product_id": pid, "platform": platform, "supplier_name": f"S{sid}",
           "unit_cost_min": 1.0, "similarity": None, "match_method": "auto", "hidden": False,
           "external_product_id": f"e{sid}"}
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# Webhook: processed only after success, 500 on failure, multi-variant, emails, missing row
# ---------------------------------------------------------------------------
def test_webhook_failure_returns_500_and_retry_applies_plan():
    DB.add_user("t", "retry@example.com")
    DB.fail_tables["users"] = "connection reset"
    r = send_webhook("subscription_created", user_email="retry@example.com")
    assert r.status_code == 500
    assert DB.tables.get("webhook_events", []) == []          # not marked processed
    del DB.fail_tables["users"]
    r = send_webhook("subscription_created", user_email="retry@example.com")
    assert r.status_code == 200 and not r.json().get("duplicate")
    assert plans()["retry@example.com"] == "pro"
    assert len(DB.tables["webhook_events"]) == 1
    # a third delivery of the same event is a duplicate
    assert send_webhook("subscription_created", user_email="retry@example.com").json().get("duplicate") is True


def test_webhook_credit_period_failure_is_retried(monkeypatch):
    DB.add_user("t", "cp@example.com")
    calls = {"n": 0}

    def boom(email):
        calls["n"] += 1
        if calls["n"] == 1:
            raise main.credits_v2.CreditError("db down")
        return {}
    monkeypatch.setattr(main.credits_v2, "start_period", boom)
    assert send_webhook("subscription_created", user_email="cp@example.com").status_code == 500
    assert send_webhook("subscription_created", user_email="cp@example.com").status_code == 200
    assert calls["n"] == 2 and plans()["cp@example.com"] == "pro"


def test_webhook_multiple_variant_ids(monkeypatch):
    monkeypatch.setenv("LEMON_PRO_VARIANT_IDS", "333, 444")
    monkeypatch.setenv("LEMON_ULTIMATE_VARIANT_IDS", "555,666")
    for i, (variant, want) in enumerate([("111", "pro"), ("444", "pro"), ("666", "ultimate"),
                                         ("222", "ultimate"), ("999", "free")]):
        email = f"v{i}@example.com"
        DB.add_user(f"t{i}", email)
        send_webhook("subscription_created", sub_id=f"s{i}", variant=variant, user_email=email)
        assert plans()[email] == want, variant


def test_checkout_uses_first_listed_variant_when_single_var_unset(monkeypatch):
    monkeypatch.delenv("LEMONSQUEEZY_PRODUCT_ID", raising=False)
    monkeypatch.setenv("LEMON_PRO_VARIANT_IDS", "777,778")
    assert main.pro_variant_ids() == ["777", "778"]
    monkeypatch.delenv("LEMON_PRO_VARIANT_IDS")
    DB.add_user("t", "nochk@example.com")
    r = client.post("/create-checkout", json={"plan": "pro"}, headers=H("t"))
    assert r.status_code == 503          # no variant configured: clear error, no LS call


def test_webhook_unsigned_prefers_order_email_over_custom_email():
    DB.add_user("a", "buyer@example.com")
    DB.add_user("b", "friend@example.com")
    send_webhook("subscription_created", custom={"email": "friend@example.com"}, user_email="buyer@example.com")
    p = plans()
    assert p["buyer@example.com"] == "pro" and p["friend@example.com"] == "free"
    # no order email at all: the custom email is still used
    DB.add_user("c", "only@example.com")
    send_webhook("subscription_created", sub_id="s2", custom={"email": "only@example.com"}, user_email="")
    assert plans()["only@example.com"] == "pro"


def test_webhook_missing_users_row_is_created():
    r = send_webhook("subscription_created", user_email="Ghost@Example.com")
    assert r.status_code == 200
    assert plans()["ghost@example.com"] == "pro"


def test_webhook_users_insert_failure_is_500():
    real_execute = ts.fake_supabase.Query.execute

    def execute(self):
        if self.table == "users" and self.op == "insert":
            raise Exception('null value in column "id" violates not-null constraint (23502)')
        return real_execute(self)
    try:
        ts.fake_supabase.Query.execute = execute
        r = send_webhook("subscription_created", user_email="norow@example.com")
    finally:
        ts.fake_supabase.Query.execute = real_execute
    assert r.status_code == 500 and DB.tables.get("webhook_events", []) == []


def test_webhook_non_object_json_is_400():
    body = b"[1,2]"
    r = client.post("/lemonsqueezy-webhook", content=body, headers={"x-signature": _sign(body)})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Free plan: suppliers only inside the fixed top 10, checked before charging
# ---------------------------------------------------------------------------
def test_free_user_suppliers_outside_top10_is_403_without_charge():
    prods = seed_products(30)
    outside, inside = prods[25]["id"], prods[0]["id"]
    DB.tables["suppliers"] = [auto_supplier(outside, 1), auto_supplier(inside, 2)]
    DB.add_user("f", "free@example.com", plan="free")
    r = client.get(f"/products/{outside}/suppliers/alibaba", headers=H("f"))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "upgrade_required"
    assert DB.consumed == []
    r = client.get(f"/products/{inside}/suppliers/alibaba", headers=H("f"))
    assert r.status_code == 200 and r.json()["credit_charged"] is True
    # amazon is gated too (it charges up front)
    assert client.get(f"/products/{outside}/suppliers/amazon", headers=H("f")).status_code == 403
    # legacy route
    assert client.get(f"/products/{outside}/suppliers", headers=H("f")).status_code == 403


def test_free_user_keeps_previously_unlocked_product():
    prods = seed_products(30)
    pid = prods[25]["id"]
    DB.tables["suppliers"] = [auto_supplier(pid, 1)]
    DB.tables["credit_unlocks"] = [{"id": 1, "user_email": "old@example.com", "product_id": pid, "platform": "alibaba"}]
    DB.add_user("f", "old@example.com", plan="free")
    r = client.get(f"/products/{pid}/suppliers/alibaba", headers=H("f"))
    assert r.status_code == 200 and r.json()["credit_charged"] is False


def test_paid_user_suppliers_anywhere():
    prods = seed_products(30)
    pid = prods[25]["id"]
    DB.tables["suppliers"] = [auto_supplier(pid, 1)]
    DB.add_user("p", "pro@example.com", plan="pro")
    assert client.get(f"/products/{pid}/suppliers/alibaba", headers=H("p")).status_code == 200


# ---------------------------------------------------------------------------
# Paid lists: stable order inside a score tie
# ---------------------------------------------------------------------------
def test_paid_lists_tiebreak_by_id():
    ids = [str(uuid.UUID(int=i)) for i in (5, 3, 4, 1, 2)]
    DB.tables["tiktok_products"] = [
        {"id": pid, "product_name": f"tie mug {n}", "virality_score": 70, "is_active": True, "tiktok_price": 9}
        for n, pid in enumerate(ids)]
    DB.add_user("p", "pro2@example.com", plan="pro")
    want = sorted(ids)
    for url in ("/products?limit=5", "/products/tier/rising?limit=5", "/search?q=mug&limit=5"):
        got = [p["id"] for p in client.get(url, headers=H("p")).json()["products"]]
        assert got == want, url
    page1 = [p["id"] for p in client.get("/products?limit=2&offset=0", headers=H("p")).json()["products"]]
    page2 = [p["id"] for p in client.get("/products?limit=2&offset=2", headers=H("p")).json()["products"]]
    assert not set(page1) & set(page2)


# ---------------------------------------------------------------------------
# PostgREST max-rows: /stats, presource, paging helper
# ---------------------------------------------------------------------------
def test_stats_counts_past_max_rows():
    DB.tables["tiktok_products"] = [
        {"id": f"{i:06d}", "virality_score": [90, 70, 55, 10][i % 4], "category": "A" if i % 3 else "B",
         "is_active": True} for i in range(2500)]
    DB.max_rows = 1000
    s = client.get("/stats", headers=H()).json()["stats"]
    assert s["total_products"] == 2500
    assert (s["hot"], s["rising"], s["watch"]) == (625, 625, 625)
    assert sum(c["count"] for c in s["top_categories"]) == 2500


def test_fetch_all_pages_until_short_page():
    from db_paging import fetch_all
    DB.tables["t"] = [{"id": i} for i in range(2345)]
    DB.max_rows = 1000
    rows = fetch_all(lambda: main.supabase.table("t").select("id").order("id"))
    assert [r["id"] for r in rows] == list(range(2345))
    assert len(fetch_all(lambda: main.supabase.table("t").select("id").order("id"), max_rows=1500)) == 1500


def test_presource_new_skips_sourced_products_past_max_rows():
    presource = pytest.importorskip("sourcing.presource")
    DB.tables["tiktok_products"] = [
        {"id": f"p{i:05d}", "product_name": f"P{i}", "virality_score": 50, "is_active": True} for i in range(1500)]
    DB.tables["sourcing_jobs"] = [
        {"id": i + 1, "product_id": f"p{i:05d}", "platform": "alibaba", "status": "done",
         "finished_at": "2026-11-10T00:00:00+00:00"} for i in range(1400)]
    DB.max_rows = 1000
    targets = presource._targets(new=True, stale_days=None, limit=0, platforms=("alibaba",))
    assert sorted(p["id"] for p, _ in targets) == [f"p{i:05d}" for i in range(1400, 1500)]


# ---------------------------------------------------------------------------
# Admin review: 'auto' rows users see are live
# ---------------------------------------------------------------------------
def test_admin_review_marks_auto_rows_visible(monkeypatch):
    prods = seed_products(3)
    pid = prods[0]["id"]
    DB.tables["suppliers"] = [
        auto_supplier(pid, 1),
        auto_supplier(pid, 2, hidden=True),
        auto_supplier(pid, 3, match_method="text"),
        auto_supplier(pid, 4, match_method="photo", similarity=0.9),
        auto_supplier(pid, 5, match_method="photo", similarity=0.5),
        auto_supplier(pid, 6, match_method="manual"),
    ]
    DB.add_user("a", "boss@example.com", confirmed=True)
    r = client.get(f"/admin/review/products/{pid}", headers=H("a"))
    vis = {s["id"]: s["visible"] for s in r.json()["suppliers"]}
    assert vis == {1: True, 2: False, 3: False, 4: True, 5: False, 6: True}
    monkeypatch.setattr(main, "SOURCING_MATCHING", True)
    assert main.supplier_visible(auto_supplier(pid, 1)) is False


# ---------------------------------------------------------------------------
# FastMoss import
# ---------------------------------------------------------------------------
def _fm_row(**extra):
    row = {"Product ID": "1729384756123456789", "Product Name": "Glow serum", "Price": "$10.00",
           "Revenue": "700", "Orders": "70", "Products Category": "Beauty", "Total Units Sold": "900",
           "_file": "Product_Search_Sales_Rank_20261111_051446.xlsx"}
    row.update(extra)
    return row


def test_fastmoss_creator_count_unknown_and_window_from_file_name():
    import import_kalodata as k
    (p, snap), err = k.parse_fastmoss_product(_fm_row())
    assert err is None
    assert p["creator_count"] is None and snap["creator_count"] is None
    assert (p["data_start_date"], p["data_end_date"], p["window_days"]) == ("2026-11-10", "2026-11-10", 1)
    assert p["gmv_7d"] == 4900.0
    assert k.stage(None, None, p["creator_count"] or 0, k.parse_date(p["data_end_date"])) == "unknown"


def test_fastmoss_date_range_column_wins():
    import import_kalodata as k
    (p, _), _ = k.parse_fastmoss_product(_fm_row(**{"Date Range": "2026-11-04 ~ 2026-11-10"}))
    assert (p["data_start_date"], p["data_end_date"], p["window_days"]) == ("2026-11-04", "2026-11-10", 7)
    assert p["gmv_7d"] == 700.0                       # a real 7-day window: no extrapolation
    (p, _), _ = k.parse_fastmoss_product(_fm_row(**{"Time Range": "2026/10/14 - 2026/11/10"}))
    assert p["window_days"] == 28
    out, err = k.parse_fastmoss_product(_fm_row(**{"Date Range": "2099-01-01~2099-01-07"}))
    assert out is None and "future" in err
