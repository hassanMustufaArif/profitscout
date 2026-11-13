"""Dashboard / Products API audit fixes (2026-11-13). Offline, same stubs as test_security.py."""
from __future__ import annotations

import uuid

import pytest

import test_security as ts          # sets env + patches supabase before importing main
from test_security import DB, H, client, main


@pytest.fixture(autouse=True)
def _reset():
    DB.reset()
    main._free_cache.clear()
    main.limiter.reset()
    main._manual_limits.storage.reset()
    yield


def pid(i):
    return str(uuid.UUID(int=i + 1))


def fm_row(i, phase, growth=10.0, score=60, active=True, gmv=1000.0, **kw):
    """A FastMoss row as import_kalodata.py writes it while the old v1 check constraint exists."""
    row = {"id": pid(i), "product_name": f"P{i}", "virality_score": score, "tiktok_price": 10.0,
           "category": "Home", "trend_phase": phase, "is_active": active, "gmv_growth_pct": growth,
           "data_end_date": "2026-11-10", "data_start_date": "2026-11-10", "window_days": 1,
           "revenue_window": gmv / 7, "gmv_7d": gmv, "units_sold_7d": 70, "growth_source": "fastmoss"}
    row.update(kw)
    return row


def hidden_march(i, score=85):
    return {"id": pid(i), "product_name": f"March {i}", "virality_score": score, "tiktok_price": 10.0,
            "category": "Old", "trend_phase": "hot", "is_active": False, "data_end_date": None}


def pro(token="pro", email="pro@example.com"):
    DB.add_user(token, email, plan="pro")
    return H(token)


# ---------------------------------------------------------------------------
# Stage names: v1 names stored, v2 names served
# ---------------------------------------------------------------------------
def test_v1_stage_names_are_served_as_v2():
    assert main.product_stage({"trend_phase": "hot", "data_end_date": "2026-11-10"}) == "emerging"
    assert main.product_stage({"trend_phase": "pass", "data_end_date": "2026-11-10"}) == "saturated"
    assert main.product_stage({"trend_phase": "watch", "data_end_date": "2026-11-10", "gmv_growth_pct": 3}) == "peaking"
    assert main.product_stage({"trend_phase": "watch", "data_end_date": "2026-11-10", "gmv_growth_pct": None}) == "unknown"
    assert main.product_stage({"trend_phase": "rising", "data_end_date": "2026-11-10"}) == "rising"
    assert main.product_stage({"trend_phase": "emerging", "data_end_date": "2026-11-10"}) == "emerging"
    # old v1 rows (no data_end_date) keep their own meaning
    assert main.product_stage({"trend_phase": "hot", "data_end_date": None}) == "hot"


def test_products_list_serves_v2_stage_and_real_per_day_revenue():
    DB.tables["tiktok_products"] = [fm_row(0, "hot", score=70), fm_row(1, "pass", score=69),
                                    fm_row(2, "watch", growth=None, score=68)]
    for h in (H(), pro()):
        prods = client.get("/products", headers=h).json()["products"]
        assert [p["trend_phase"] for p in prods] == ["emerging", "saturated", "unknown"]
        assert prods[0]["revenue_per_day"] == pytest.approx(1000.0 / 7, abs=0.01)
        assert prods[0]["gmv_7d_estimated"] is True


def test_trend_phase_filter_accepts_v2_names():
    DB.tables["tiktok_products"] = [fm_row(0, "hot"), fm_row(1, "watch", growth=5), fm_row(2, "watch", growth=None),
                                    fm_row(3, "pass")]
    h = pro()
    ids = lambda q: [p["id"] for p in client.get(f"/products?trend_phase={q}", headers=h).json()["products"]]
    assert ids("emerging") == [pid(0)]
    assert ids("peaking") == [pid(1)]
    assert ids("unknown") == [pid(2)]
    assert ids("saturated") == [pid(3)]


# ---------------------------------------------------------------------------
# /stats: active rows only, stage counts, data window
# ---------------------------------------------------------------------------
def test_stats_ignore_hidden_rows_and_report_stages_and_window():
    DB.tables["tiktok_products"] = ([fm_row(i, ph) for i, ph in enumerate(["hot", "rising", "pass", "pass"])]
                                    + [hidden_march(10 + i) for i in range(5)])
    st = client.get("/stats", headers=H()).json()["stats"]
    assert st["total_products"] == 4 and st["hot"] == 0          # the 5 hidden score-85 rows don't count
    assert st["stages"]["emerging"] == 1 and st["stages"]["saturated"] == 2 and st["stages"]["rising"] == 1
    assert st["data_end_date"] == "2026-11-10" and st["window_days"] == 1
    assert {c["name"] for c in st["top_categories"]} == {"Home"}


def test_stats_are_cached_and_cleared_by_import():
    DB.tables["tiktok_products"] = [fm_row(0, "hot")]
    assert client.get("/stats", headers=H()).json()["stats"]["total_products"] == 1
    DB.tables["tiktok_products"].append(fm_row(1, "hot"))
    assert client.get("/stats", headers=H()).json()["stats"]["total_products"] == 1     # cached
    main.clear_list_caches()
    assert client.get("/stats", headers=H()).json()["stats"]["total_products"] == 2


# ---------------------------------------------------------------------------
# Hidden (inactive) products never leak
# ---------------------------------------------------------------------------
def test_hidden_product_detail_is_404_for_paid_users():
    DB.tables["tiktok_products"] = [fm_row(0, "hot"), hidden_march(1)]
    h = pro()
    assert client.get(f"/products/{pid(0)}", headers=h).status_code == 200
    assert client.get(f"/products/{pid(1)}", headers=h).status_code == 404


def test_hidden_product_suppliers_not_sold_but_kept_for_existing_unlocks():
    DB.tables["tiktok_products"] = [hidden_march(1)]
    DB.tables["suppliers"] = [{"id": "s1", "tiktok_product_id": pid(1), "platform": "alibaba", "supplier_name": "A",
                               "unit_cost_min": 1.0, "similarity": None, "match_method": "auto", "hidden": False,
                               "external_product_id": "e1"}]
    h = pro()
    r = client.get(f"/products/{pid(1)}/suppliers/alibaba", headers=h)
    assert r.status_code == 404
    DB.tables.setdefault("credit_unlocks", []).append(
        {"user_email": "pro@example.com", "product_id": pid(1), "platform": "alibaba"})
    r = client.get(f"/products/{pid(1)}/suppliers/alibaba", headers=h)
    assert r.status_code == 200 and r.json()["credit_charged"] is False


def test_hidden_rows_never_in_lists_or_free_top():
    DB.tables["tiktok_products"] = [hidden_march(i, score=99) for i in range(12)] + [fm_row(50, "hot", score=10)]
    for h in (H(), pro()):
        for path in ("/products", "/products/tier/hot", "/products/tier/pass", "/search?q=March"):
            prods = client.get(path, headers=h).json()["products"]
            assert all(p["is_active"] for p in prods), path


# ---------------------------------------------------------------------------
# Free tier: tier route is a subset of the fixed top 10, ties broken by revenue
# ---------------------------------------------------------------------------
def test_free_top10_ties_broken_by_revenue():
    DB.tables["tiktok_products"] = [fm_row(i, "rising", score=70, gmv=float(i)) for i in range(12)]
    ids = [p["id"] for p in client.get("/products", headers=H()).json()["products"]]
    assert ids == [pid(i) for i in range(11, 1, -1)]


def test_free_tier_route_only_returns_rows_in_the_tier():
    DB.tables["tiktok_products"] = [fm_row(i, "rising", score=70 - i) for i in range(12)]
    body = client.get("/products/tier/hot", headers=H()).json()
    assert body["range"] == "80-100" and body["products"] == []
    rising = client.get("/products/tier/rising", headers=H()).json()["products"]
    assert rising and all(65 <= p["virality_score"] <= 79 for p in rising)
    top10 = {p["id"] for p in client.get("/products", headers=H()).json()["products"]}
    assert {p["id"] for p in rising} <= top10


# ---------------------------------------------------------------------------
# Lists no longer touch the credit RPC; shops/creators errors are errors
# ---------------------------------------------------------------------------
def test_list_requests_do_not_call_credit_balance(monkeypatch):
    DB.tables["tiktok_products"] = [fm_row(0, "hot")]
    calls = []
    monkeypatch.setattr(main.credits_v2, "get_balance", lambda e: calls.append(e) or {})
    h = pro()
    assert client.get("/products?limit=100&offset=0", headers=h).status_code == 200
    assert client.get("/search?q=P0", headers=h).status_code == 200
    assert calls == []


def test_shops_and_creators_failures_are_500_not_empty_lists():
    h = pro()
    DB.fail_tables["shops"] = "connection reset"
    DB.fail_tables["creators"] = "connection reset"
    assert client.get("/shops", headers=h).status_code == 500
    assert client.get("/creators", headers=h).status_code == 500
