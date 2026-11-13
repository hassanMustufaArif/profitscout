"""Upload / import audit (2026-11-13). Offline: a small fake Supabase that behaves like prod
(missing columns -> PGRST204, the old trend_phase check, PostgREST's columns=<union> upsert)."""
from __future__ import annotations

import copy
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import import_kalodata as k  # noqa: E402

XLSX = "/workspace/files/uploads/Product_Search_Sales_Rank_20261111_051446-850f15ebd8e4.xlsx"


# ─── fake prod ──────────────────────────────────────────────────────────────
class APIError(Exception):
    pass


class Q:
    def __init__(self, db, table):
        self.db, self.t, self.f, self.op, self.payload, self.conf, self.lim, self.desc = db, table, [], "select", None, None, None, None

    def select(self, *a, **kw):
        return self

    def eq(self, c, v):
        self.f.append(lambda r: r.get(c) == v)
        return self

    def in_(self, c, vals):
        vals = set(vals)
        self.f.append(lambda r: r.get(c) in vals)
        return self

    def lt(self, c, v):
        self.f.append(lambda r: r.get(c) is not None and str(r.get(c)) < str(v))
        return self

    def gte(self, c, v):
        self.f.append(lambda r: r.get(c) is not None and str(r.get(c)) >= str(v))
        return self

    def is_(self, c, v):
        self.f.append(lambda r: r.get(c) is None)
        return self

    def order(self, c, desc=False):
        self.desc = (c, desc)
        return self

    def limit(self, n):
        self.lim = n
        return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.payload, self.conf = "upsert", rows, on_conflict
        return self

    def update(self, vals):
        self.op, self.payload = "update", vals
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.t, [])
        if self.op == "upsert":
            self.db.calls.append((self.t, len(self.payload)))
            cols = set().union(*[set(r) for r in self.payload])     # postgrest-py: columns=<union>
            have = self.db.columns.get(self.t)
            if have is not None and cols - have:
                raise APIError({"message": f"Could not find the '{sorted(cols - have)[0]}' column of '{self.t}' in the schema cache", "code": "PGRST204"})
            for r in self.payload:
                if self.t == "tiktok_products" and self.db.v1_check and r.get("trend_phase") not in (None, "hot", "rising", "watch", "pass"):
                    raise APIError({"message": 'new row for relation "tiktok_products" violates check constraint "tiktok_products_trend_phase_check"', "code": "23514"})
                if self.t == "tiktok_products" and self.db.creator_not_null and "creator_count" in r and r["creator_count"] is None:
                    raise APIError({"message": 'null value in column "creator_count" violates not-null constraint', "code": "23502"})
            keys = self.conf.split(",")
            for r in self.payload:
                full = {c: r.get(c) for c in cols}                    # missing keys -> NULL
                cur = next((x for x in rows if all(x.get(kk) == full.get(kk) for kk in keys)), None)
                if cur:
                    cur.update(full)
                else:
                    rows.append(dict(full))
            return type("R", (), {"data": []})()
        match = [r for r in rows if all(fn(r) for fn in self.f)]
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return type("R", (), {"data": copy.deepcopy(match)})()
        if self.desc:
            match.sort(key=lambda r: str(r.get(self.desc[0])), reverse=self.desc[1])
        if self.lim:
            match = match[:self.lim]
        return type("R", (), {"data": copy.deepcopy(match)})()


class FakeProd:
    def __init__(self, columns=None, v1_check=False, creator_not_null=False):
        self.tables, self.calls = {}, []
        self.columns = columns or {}
        self.v1_check, self.creator_not_null = v1_check, creator_not_null
        for t, cols in self.columns.items():       # one existing row so select * shows the columns
            self.tables[t] = [{c: None for c in cols}]

    def table(self, t):
        return Q(self, t)


PROD_PRODUCT_COLS = {
    "ad_spend", "category", "commission_rate", "created_at", "creator_conversion_pct", "creator_count",
    "data_end_date", "data_start_date", "first_seen_at", "gmv_7d", "gmv_growth_pct", "growth_source", "id",
    "image_storage_url", "is_active", "launch_date", "organic_orders", "price_max", "price_min", "product_name",
    "rating", "revenue_window", "review_count", "review_growth_7d", "scraped_at", "shop_created_at", "shop_id",
    "shop_name", "thumbnail_url", "tiktok_price", "tiktok_product_id", "tiktok_product_url", "trend_phase",
    "units_sold_7d", "units_window", "video_url", "video_views", "virality_score", "window_days"}


@pytest.fixture
def prod(monkeypatch):
    db = FakeProd(columns={"tiktok_products": PROD_PRODUCT_COLS}, v1_check=True)
    monkeypatch.setattr(k, "create_client", lambda *a, **kw: db)
    monkeypatch.setenv("SUPABASE_URL", "https://stub.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "stub")
    return db


def products(db):
    return {r["tiktok_product_id"]: r for r in db.tables["tiktok_products"] if r.get("tiktok_product_id")}


def fm_row(pid="1729384756123456789", **extra):
    row = {"Product ID": pid, "Product Name": "Glow serum", "Price": "$10.00", "Revenue": "700",
           "Orders": "70", "Products Category": "Beauty", "Total Units Sold": "900", "Shop Name": "Glow Co",
           "Order Growth Rate": "12%", "_file": "Product_Search_Sales_Rank_20261111_051446.xlsx"}
    row.update(extra)
    return row


# ─── tests ──────────────────────────────────────────────────────────────────
def test_score_same_scale_when_creators_rating_unknown():
    # FastMoss: no creators / conversion / rating -> must still be able to reach 100
    assert k.momentum_score(200_000, 200, None, None, None) == 100
    # Kalodata row with all parts: unchanged from the old formula
    assert k.momentum_score(31_623, 100, 999, 50, 5.0) == 100
    # a real 0 still scores 0 for that part (not left out)
    assert k.momentum_score(31_623, 100, 0, 0, 4.0) < k.momentum_score(31_623, 100, None, None, None)


@pytest.mark.skipif(not os.path.exists(XLSX), reason="sample FastMoss file not in the workspace")
def test_fastmoss_sample_file_mapping():
    rows = [r for r in k.load_rows([XLSX]) if r]
    assert len(rows) == 200
    by_id = {r["Product ID"]: r for r in rows}
    (p, snap), err = k.parse_fastmoss_product(by_id["1729405850428937058"])
    assert err is None
    assert p["shop_name"] == "Wonderskin"
    assert p["category"] == "Eyeliner & Lipliner"                 # trailing tab stripped
    assert (p["price_min"], p["price_max"]) == (16.2, 16.2)
    assert p["tiktok_price"] == 8.1                               # 123387.3 / 15233 orders
    assert p["revenue_window"] == 123387.3 and p["units_window"] == 15233
    assert p["data_end_date"] == "2026-11-10" and p["window_days"] == 1
    assert p["creator_count"] is None and p["_fastmoss_growth"] == 1149.63
    assert p["tiktok_product_url"].startswith("https://shop.tiktok.com/view/product/1729405850428937058")
    (p, _), _ = k.parse_fastmoss_product(by_id["1729508370969629931"])
    assert (p["price_min"], p["price_max"]) == (17.0, 3000.0)     # "$17.00 - 3,000.00"
    (p, _), _ = k.parse_fastmoss_product(by_id["1732831994522211312"])
    assert p["commission_rate"] is None and p["_fastmoss_growth"] == 2235.14


def test_import_against_prod_like_db(prod, monkeypatch):
    rows = [fm_row("1000000000000000001"), fm_row("1000000000000000002", **{"Order Growth Rate": "-"})]
    monkeypatch.setattr(k, "load_rows", lambda paths, log=print: rows)
    out = k.import_products(["x.xlsx"], log=lambda m: None)
    got = products(prod)
    assert out["products"] == 2
    a, b = got["1000000000000000001"], got["1000000000000000002"]
    # v1 fallback still works, but creator_count stays NULL (unknown), not 0
    assert a["trend_phase"] in ("hot", "rising", "watch", "pass") and a["creator_count"] is None
    assert a["shop_name"] == "Glow Co"
    assert any("trend_phase check" in w for w in out["warnings"])
    assert any("live_revenue" in w for w in out["warnings"])
    assert b["growth_source"] == "unknown" and b["trend_phase"] == "watch"


def test_creator_not_null_handled_after_v1_fallback(monkeypatch):
    db = FakeProd(columns={"tiktok_products": PROD_PRODUCT_COLS}, v1_check=True, creator_not_null=True)
    monkeypatch.setattr(k, "create_client", lambda *a, **kw: db)
    monkeypatch.setattr(k, "load_rows", lambda paths, log=print: [fm_row()])
    out = k.import_products(["x.xlsx"], log=lambda m: None)
    assert out["products"] == 1 and products(db)["1729384756123456789"]["creator_count"] == 0
    assert len(out["warnings"]) >= 2


def test_older_export_does_not_overwrite_newer_row(prod, monkeypatch):
    prod.tables["tiktok_products"].append({"tiktok_product_id": "1729384756123456789", "data_end_date": "2026-11-20",
                                           "product_name": "newer", "is_active": True})
    monkeypatch.setattr(k, "load_rows", lambda paths, log=print: [fm_row()])
    out = k.import_products(["x.xlsx"], log=lambda m: None)
    assert products(prod)["1729384756123456789"]["product_name"] == "newer"
    assert out["kept_newer"] == 1 and out["products"] == 0
    assert prod.tables["product_snapshots"]                      # history still saved


def test_failed_image_copy_keeps_the_old_copy(prod, monkeypatch):
    prod.tables["tiktok_products"].append({"tiktok_product_id": "1000000000000000001", "data_end_date": "2026-11-01",
                                           "image_storage_url": "https://old/copy.jpg", "product_name": "x"})
    monkeypatch.setattr(k, "load_rows", lambda paths, log=print: [
        fm_row("1000000000000000001", **{"Product Image": "https://img.example/a.webp"}),
        fm_row("1000000000000000002", **{"Product Image": "https://img.example/b.webp"})])
    monkeypatch.setattr(k, "rehost_image", lambda sb, pid, url, log=print:
                        None if pid.endswith("1") else f"https://new/{pid}.jpg")
    out = k.import_products(["x.xlsx"], rehost=True, log=lambda m: None)
    got = products(prod)
    assert got["1000000000000000001"]["image_storage_url"] == "https://old/copy.jpg"
    assert got["1000000000000000002"]["image_storage_url"] == "https://new/1000000000000000002.jpg"
    assert any("images could not be copied" in w for w in out["warnings"])


def test_dry_run_reports_schema_drift(prod, monkeypatch):
    monkeypatch.setattr(k, "load_rows", lambda paths, log=print: [fm_row()])
    out = k.import_products(["x.xlsx"], dry_run=True, log=lambda m: None)
    assert any("live_revenue" in w for w in out["warnings"])
    assert prod.calls == []                                       # nothing written


def test_snapshot_growth_ignores_stale_history(prod):
    prod.tables["product_snapshots"] = [
        {"tiktok_product_id": "p1", "data_end_date": "2026-10-01", "window_days": 1, "revenue": 10},
        {"tiktok_product_id": "p2", "data_end_date": "2026-11-09", "window_days": 1, "revenue": 10},
        {"tiktok_product_id": "p3", "data_end_date": "2026-11-09", "window_days": 30, "revenue": 10}]
    h = k.history_for(prod, ["p1", "p2", "p3"], "2026-11-10", 1)
    assert set(h) == {"p2"}


def test_shops_skip_columns_prod_lacks(monkeypatch, tmp_path):
    db = FakeProd(columns={"shops": {"id", "shop_name", "gmv_total", "data_end_date", "image_url", "shop_url"}})
    monkeypatch.setattr(k, "create_client", lambda *a, **kw: db)
    f = tmp_path / "shops.csv"
    f.write_text("Shop Name,Revenue($),Revenue Growth Rate\nAcme,1000,12%\n")
    out = k.import_shops([str(f)], log=lambda m: None)
    assert out["shops"] == 1 and any("gmv_growth_pct" in w for w in out["warnings"])
    assert any(r.get("shop_name") == "Acme" for r in db.tables["shops"])


def test_unreadable_xlsx_is_reported_in_the_job_log(tmp_path):
    bad = tmp_path / "broken.xlsx"
    bad.write_bytes(b"not a zip")
    seen = []
    assert k.load_rows([str(bad)], log=seen.append) == []
    assert any("could not read broken.xlsx" in m for m in seen)


# ─── main.py: job state ─────────────────────────────────────────────────────
def test_job_carries_warnings_and_real_error(monkeypatch, tmp_path):
    import test_security  # noqa: F401  (stubs supabase + env before main is imported)
    import main
    monkeypatch.setattr(main._kalo, "import_products",
                        lambda *a, **kw: {"products": 1, "warnings": ["tiktok_products: skipping columns x"]})
    job = {"log": [], "dir": str(tmp_path / "j1")}
    main._run_import(job, [{"kind": "products", "path": "a"}], False, False, False)
    assert job["status"] == "done" and job["warnings"] == ["tiktok_products: skipping columns x"]
    assert not main._IMPORT_LOCK.locked()

    def boom(*a, **kw):
        raise RuntimeError("Could not find the 'live_revenue' column")
    monkeypatch.setattr(main._kalo, "import_products", boom)
    job = {"log": [], "dir": str(tmp_path / "j2")}
    main._run_import(job, [{"kind": "products", "path": "a"}], False, False, False)
    assert job["status"] == "failed" and "live_revenue" in job["error"]


def test_find_suppliers_refused_while_import_runs(monkeypatch):
    import test_security as ts
    import main
    ts.DB.reset()
    ts.DB.add_user("boss", "boss@example.com")
    main.limiter.reset()
    assert main._IMPORT_LOCK.acquire(blocking=False)
    try:
        r = ts.client.post("/admin/sourcing/run", json={"mode": "new"}, headers=ts.H("boss"))
    finally:
        main._IMPORT_LOCK.release()
    assert r.status_code == 409
