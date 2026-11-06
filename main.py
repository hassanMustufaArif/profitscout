"""
ProfitScout — FastAPI Backend
=============================
Run with:  uvicorn main:app --reload
API Docs:  http://127.0.0.1:8000/docs
"""
from fastapi.responses import PlainTextResponse
import os
import httpx
import jwt
import time
from fastapi import APIRouter
from fastapi import Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import FastAPI, HTTPException, Query, Security, Request
from datetime import datetime, timezone, timedelta
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supplier_engine import find_suppliers, find_suppliers_by_platform, initialize_credits
import credits as credits_v2
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from openai import AsyncOpenAI
from supabase import create_client, Client
from dotenv import load_dotenv
from typing import Optional
import hmac
import hashlib
import json
import pdfplumber
import tempfile


load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ADZUNA_ID  = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_KEY = os.environ.get("ADZUNA_APP_KEY", "")

class GenerateListingRequest(BaseModel):
    product_name: str
    price: float
    category: str = ""
    image_url: str = ""

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

def scheduled_autopilot():
    try:
        from autopilot import run
        print("⏰ Scheduled autopilot starting...")
        run()
    except Exception as e:
        print(f"❌ Autopilot error: {e}")

# Off by default: with more than one web instance the post would be published twice.
# Prefer a Render Cron Job running `python autopilot.py`; set RUN_SCHEDULER=true only on ONE instance.
if os.getenv("RUN_SCHEDULER", "false") == "true":
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_autopilot, 'cron', hour=9, minute=0)
    scheduler.start()
    print("Autopilot scheduler started — runs daily at 9AM UTC")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://tikprofitpro.shop,https://www.tikprofitpro.shop,http://localhost:8080,http://localhost:5173").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Key Auth ---
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(request: Request, api_key: str = Security(API_KEY_HEADER)):
    if request.method == "OPTIONS":
        return None
    if not api_key or api_key != os.getenv("API_KEY"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return api_key


ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")


def require_admin(request: Request):
    """Internal tools (blog, autopilot, job board). Header X-Admin-Secret must match ADMIN_SECRET."""
    given = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or not hmac.compare_digest(given, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


FREE_PRODUCT_LIMIT = 10
MIN_SIMILARITY = float(os.getenv("SOURCING_MIN_SIMILARITY", "0.80"))


def gate_list(plan: str, limit: int, offset: int) -> tuple[int, int]:
    """Free (and signed-out) users see the top 10 only, on every list endpoint."""
    if plan not in ("pro", "ultimate"):
        return min(limit, FREE_PRODUCT_LIMIT), 0
    return limit, offset


async def get_user_plan(request: Request):
    """Extract user email from Supabase auth token and return their plan."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        print("get_user_plan: No auth header found")
        return "free"

    token = auth_header.replace("Bearer ", "")
    try:
        user_response = supabase.auth.get_user(token)
        email = user_response.user.email
        print(f"get_user_plan: email={email}")
        result = supabase.table("users").select("plan").eq("user_email", email).execute()
        print(f"get_user_plan: result={result.data}")
        plan = result.data[0].get("plan", "free") if result.data else "free"
        ensure_user_credits(email, plan)  # Auto-create credits if not exist
        return plan
    except Exception as e:
        print(f"get_user_plan ERROR: {e}")
        return "free"

def ensure_user_credits(user_email: str, plan: str):
    """Create/roll over the credit row. Plan comes from users.plan inside the DB function."""
    try:
        credits_v2.get_balance(user_email)
    except Exception as e:
        print(f"ensure_user_credits error: {e}")

# ==============================================================================
# ENDPOINT 1 — Health Check
# ==============================================================================
@app.get("/")
def root():
    return {
        "status": "running",
        "app": "ProfitScout API",
        "version": "2.0.0",
        "docs": "/docs"
    }


@app.get("/ping")
def ping():
    return {"status": "ok", "message": "FastAPI is working!"}


# ==============================================================================
# ENDPOINT 2 — Get All Products (with filters + plan gating)
# ==============================================================================
@app.get("/products")
async def get_products(
    request:     Request,
    min_score:   int            = Query(default=0,   ge=0, le=100),
    max_score:   int            = Query(default=100, ge=0, le=100),
    trend_phase: Optional[str]  = Query(default=None),
    category:    Optional[str]  = Query(default=None),
    min_price:   Optional[float]= Query(default=None),
    max_price:   Optional[float]= Query(default=None),
    limit:       int            = Query(default=20,  ge=1, le=100),
    offset:      int            = Query(default=0,   ge=0),
    api_key:     str            = Security(verify_api_key),
):
    try:
        plan = await get_user_plan(request)

        # Free users: max 10 products, no offset
        if plan == "free":
            limit = min(limit, 10)
            offset = 0

        query = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .gte("virality_score", min_score)
            .lte("virality_score", max_score)
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
        )

        if trend_phase:
            query = query.eq("trend_phase", trend_phase)
        if category:
            query = query.ilike("category", f"%{category}%")
        if min_price is not None:
            query = query.gte("tiktok_price", min_price)
        if max_price is not None:
            query = query.lte("tiktok_price", max_price)

        result = query.execute()

        return {
            "success":  True,
            "count":    len(result.data),
            "offset":   offset,
            "limit":    limit,
            "plan":     plan,
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 3 — Get Single Product Detail
# ==============================================================================
@app.get("/products/{product_id}")
def get_product(product_id: str, api_key: str = Security(verify_api_key)):
    try:
        product_result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("id", product_id)
            .single()
            .execute()
        )

        if not product_result.data:
            raise HTTPException(status_code=404, detail="Product not found")

        product = product_result.data
        # Suppliers are NOT returned here: they cost a credit via /products/{id}/suppliers/{platform}.

        return {
            "success": True,
            "product": product
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 4 — Get Products by Virality Tier
# ==============================================================================
@app.get("/products/tier/{tier}")
async def get_products_by_tier(
    tier:    str,
    request: Request,
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    tier_ranges = {
        "hot":    (80, 100),
        "rising": (65, 79),
        "watch":  (50, 64),
        "pass":   (0,  49),
    }

    if tier not in tier_ranges:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier '{tier}'. Use: hot, rising, watch, or pass"
        )

    min_score, max_score = tier_ranges[tier]
    limit, offset = gate_list(await get_user_plan(request), limit, offset)

    try:
        result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .gte("virality_score", min_score)
            .lte("virality_score", max_score)
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

        return {
            "success":  True,
            "tier":     tier,
            "range":    f"{min_score}-{max_score}",
            "count":    len(result.data),
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 5 — Dashboard Stats
# ==============================================================================
@app.get("/stats")
def get_stats(api_key: str = Security(verify_api_key)):
    try:
        all_products = (
            supabase.table("tiktok_products")
            .select("virality_score, trend_phase, category")
            .eq("is_active", True)
            .execute()
        )

        products = all_products.data or []

        hot    = len([p for p in products if p["virality_score"] >= 80])
        rising = len([p for p in products if 65 <= p["virality_score"] < 80])
        watch  = len([p for p in products if 50 <= p["virality_score"] < 65])
        total  = len(products)

        categories = {}
        for p in products:
            cat = p.get("category") or "Other"
            categories[cat] = categories.get(cat, 0) + 1
        top_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "success": True,
            "stats": {
                "total_products": total,
                "hot":            hot,
                "rising":         rising,
                "watch":          watch,
                "top_categories": [{"name": k, "count": v} for k, v in top_categories]
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 6 — Profit Calculator
# ==============================================================================
class ProfitRequest(BaseModel):
    product_id:                str = "manual"
    supplier_id:               Optional[str] = None
    sale_price:                Optional[float] = None
    selling_price:             Optional[float] = None
    cogs:                      Optional[float] = None
    product_cost:              Optional[float] = None
    shipping_cost:             Optional[float] = 0.0
    quantity:                  int = 1
    affiliate_commission_rate: float = 15.0
    fbt_enabled:               bool  = False
    product_weight_oz:         float = 8.0
    inbound_shipping:          float = 0.0
    return_rate_pct:           float = 8.0
    ad_spend_per_unit:         float = 0.0
    is_new_seller:             bool  = False


@app.post("/calculate-profit")
def calculate_profit(req: ProfitRequest, api_key: str = Security(verify_api_key)):
    try:
        # Normalize field names from frontend
        sale_price = req.sale_price or req.selling_price or 0
        cogs = req.cogs or ((req.product_cost or 0) + (req.shipping_cost or 0))

        referral_rate   = 0.03 if req.is_new_seller else 0.06
        referral_fee    = round(sale_price * referral_rate, 2)
        transaction_fee = round(sale_price * 0.029, 2)
        affiliate_amt   = round(sale_price * (req.affiliate_commission_rate / 100), 2)

        if req.fbt_enabled:
            if req.product_weight_oz <= 4:    fbt_fee = 2.68
            elif req.product_weight_oz <= 8:  fbt_fee = 3.58
            elif req.product_weight_oz <= 12: fbt_fee = 4.20
            elif req.product_weight_oz <= 16: fbt_fee = 4.88
            else:                             fbt_fee = 5.50
        else:
            fbt_fee = 0.0

        refund_admin_fee = min(referral_fee * 0.20, 5.0)
        return_buffer    = round((req.return_rate_pct / 100) * refund_admin_fee, 2)

        total_costs = round(
            cogs + referral_fee + transaction_fee + affiliate_amt
            + fbt_fee + req.inbound_shipping + return_buffer + req.ad_spend_per_unit,
            2
        )

        net_profit = round(sale_price - total_costs, 2)
        margin_pct = round((net_profit / sale_price) * 100, 1) if sale_price > 0 else 0

        if margin_pct >= 35:   rating = "green"
        elif margin_pct >= 20: rating = "amber"
        else:                  rating = "red"

        warnings = []
        total_fees_pct = ((referral_fee + transaction_fee + affiliate_amt) / sale_price) * 100 if sale_price > 0 else 0
        if total_fees_pct > 35:
            warnings.append(f"TikTok fees + affiliate = {total_fees_pct:.1f}% of sale price. Very tight margins.")
        if req.affiliate_commission_rate > 15:
            warnings.append(f"Affiliate rate of {req.affiliate_commission_rate}% is high. Consider reducing.")
        if req.return_rate_pct > 20:
            warnings.append(f"Return rate of {req.return_rate_pct}% will significantly erode profit.")
        if margin_pct < 0:
            warnings.append("This product is LOSING money at this price. Raise sale price or find cheaper supplier.")

        return {
            "success": True,
            "rating":  rating,
            "summary": {
                "sale_price":  sale_price,
                "total_costs": total_costs,
                "net_profit":  net_profit,
                "margin_pct":  margin_pct,
            },
            "breakdown": {
                "cost_of_goods":        cogs,
                "referral_fee":         referral_fee,
                "transaction_fee":      transaction_fee,
                "affiliate_commission": affiliate_amt,
                "fbt_fulfillment":      fbt_fee,
                "inbound_shipping":     req.inbound_shipping,
                "return_buffer":        return_buffer,
                "ad_spend":             req.ad_spend_per_unit,
            },
            "warnings": warnings
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 7 — Search Products
# ==============================================================================
@app.get("/search")
async def search_products(
    request: Request,
    q:       str = Query(..., min_length=2, description="Search keyword"),
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    limit, offset = gate_list(await get_user_plan(request), limit, offset)
    try:
        result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .ilike("product_name", f"%{q}%")
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )

        return {
            "success":  True,
            "query":    q,
            "count":    len(result.data),
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


    # ==============================================================================
# ==============================================================================
# ENDPOINT 8/9 — Find Suppliers (credits v2 + photo search)
# GET /products/{product_id}/suppliers/{platform}?photo=true
# platform = alibaba | aliexpress | amazon
#
# Credit rules (see credits.py):
#   - products you already unlocked are FREE (cached results still cost 1 credit the first time)
#   - otherwise 1 credit is reserved before the search and REFUNDED if nothing usable is found
#   - the engine never charges; this endpoint is the only place credits move
# ==============================================================================
def _dedupe_key(r: dict):
    ext = r.get("external_product_id")
    if ext:
        return ("ext", str(ext))
    return ("name", (r.get("supplier_name") or "").strip().lower(), round(float(r.get("unit_cost_min") or 0), 2))


def verified_suppliers(product: dict, platform: str, keep: int = 5) -> list[dict]:
    """Saved suppliers that passed the photo check, best match first, no duplicates.
    Rows without a similarity score (old keyword-only results) are never shown or charged for."""
    from supplier_engine import calculate_profit
    rows = (
        supabase.table("suppliers").select("*")
        .eq("tiktok_product_id", product["id"]).eq("platform", platform)
        .or_(f"similarity.gte.{MIN_SIMILARITY},match_method.eq.manual")   # auto-verified or hand-approved
        .not_.is_("hidden", "true")
        .execute().data or []
    )

    def score(r):  # a hand-approved row ranks as a verified match
        sim = float(r.get("similarity") or 0)
        return max(sim, MIN_SIMILARITY) if r.get("match_method") == "manual" else sim

    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (-round(score(r), 2), float(r.get("unit_cost_min") or 1e9))):
        k = _dedupe_key(r)
        if k in seen:
            continue
        seen.add(k)
        cogs = r.get("unit_cost_min") or r.get("unit_cost_max") or 0
        r["_profit"] = calculate_profit(sale_price=product.get("tiktok_price") or 0, cogs=cogs)
        r["_image_url"] = r.get("image_url", "")
        out.append(r)
    return out[:keep]


def _balance(email: str) -> Optional[dict]:
    try:
        return credits_v2.get_balance(email)
    except credits_v2.CreditError:
        return None


def _charge_or_402(user_email: str, product_id: str, platform: str, mode: str) -> bool:
    try:
        credit = credits_v2.consume(user_email, product_id, platform, mode)
    except credits_v2.CreditError as e:
        print(f"credit system error: {e}")
        raise HTTPException(status_code=503, detail="Credits are temporarily unavailable. Please try again in a minute.")
    if not credit.get("allowed"):
        raise HTTPException(status_code=402, detail={
            "code": "no_credits",
            "message": f"You've used all {credit.get('credits_total', 0)} credits for this period.",
            "credits": credit,
        })
    return bool(credit.get("charged"))


# ==============================================================================
# Supplier lookup — credit rules (credits.py + SQL functions do the accounting):
#   - Alibaba / AliExpress are PRE-SOURCED in a batch (sourcing/presource.py).
#   - A user pays 1 credit the first time they open a product+platform that has at least one
#     photo-verified supplier (similarity >= SOURCING_MIN_SIMILARITY). Reopening is free.
#   - No verified supplier yet (not sourced, no match, only old unscored rows) -> nothing shown, nothing charged.
#   - Amazon keeps the legacy live search: charged up front, refunded if nothing usable is found.
# ==============================================================================
def _supplier_search(product_id: str, platform: str, request: Request, photo: bool):
    if platform not in ("alibaba", "aliexpress", "amazon"):
        raise HTTPException(status_code=400, detail="Platform must be: alibaba, aliexpress, or amazon")

    user_email = credits_v2.email_from_request(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="Sign in to search suppliers")

    product = (
        supabase.table("tiktok_products")
        .select("id, product_name, tiktok_price, thumbnail_url, image_storage_url")
        .eq("id", product_id).single().execute().data
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if platform in ("alibaba", "aliexpress"):
        rows = verified_suppliers(product, platform)
        if rows:
            charged = False
            if not credits_v2.is_unlocked(user_email, product_id, platform):
                charged = _charge_or_402(user_email, product_id, platform, "image")
            return {"success": True, "cached": True, "platform": platform, "match_method": "photo",
                    "suppliers": rows, "credit_charged": charged, "credits": _balance(user_email)}

        last = (
            supabase.table("sourcing_jobs").select("status")
            .eq("product_id", str(product_id)).eq("platform", platform)
            .order("id", desc=True).limit(1).execute().data
        )
        state = last[0]["status"] if last else "pending"
        msg = (f"No {platform.title()} supplier matched this product's photo closely enough. No credit used."
               if state == "no_match" else
               f"{platform.title()} suppliers for this product are being prepared. Check back soon. No credit used.")
        return {"success": False, "status": "no_match" if state == "no_match" else "not_sourced",
                "platform": platform, "suppliers": [], "credit_charged": False, "message": msg,
                "credits": _balance(user_email)}

    # Amazon: legacy live search (Apify). Charge first, refund if nothing usable.
    image_url = product.get("image_storage_url") or product.get("thumbnail_url") or ""
    charged = False
    if not credits_v2.is_unlocked(user_email, product_id, platform):
        charged = _charge_or_402(user_email, product_id, platform, "image" if photo else "text")
    try:
        result = find_suppliers_by_platform(
            product_id=product["id"], product_title=product["product_name"],
            tiktok_price=product["tiktok_price"] or 0, platform=platform,
            user_email=user_email, product_image_url=image_url, use_photo=photo,
        )
    except Exception as e:
        print(f"amazon search error: {e}")
        if charged:
            credits_v2.refund(user_email, product_id, platform)
        raise HTTPException(status_code=500, detail="Supplier search failed. Your credit was not used.")
    if charged and not result.get("success"):
        credits_v2.refund(user_email, product_id, platform)
        charged = False
    result["credit_charged"] = charged
    result["credits"] = _balance(user_email)
    return result


@app.get("/products/{product_id}/suppliers/{platform}")
async def get_suppliers_by_platform(
    product_id: str,
    platform:   str,
    request:    Request,
    photo:      bool = Query(default=True, description="Use the product photo to match suppliers"),
    api_key:    str = Security(verify_api_key),
):
    return _supplier_search(product_id, platform, request, photo)


@app.get("/products/{product_id}/suppliers")
async def get_suppliers(product_id: str, request: Request, api_key: str = Security(verify_api_key)):
    """Legacy route. Used to give unlimited free Alibaba searches; now same rules as above."""
    return _supplier_search(product_id, "alibaba", request, True)


@app.get("/sourcing/jobs/{job_id}")
async def get_sourcing_job(job_id: int, request: Request, api_key: str = Security(verify_api_key)):
    """Frontend polls this every 3s after a photo search was queued (worker mode only)."""
    email = credits_v2.email_from_request(request)
    if not email:
        raise HTTPException(status_code=401, detail="Sign in required")
    job = supabase.table("sourcing_jobs").select("*").eq("id", job_id).single().execute().data
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    out = {"job_id": job_id, "status": job["status"]}
    # Suppliers only for users who actually unlocked this product+platform.
    if job["status"] == "done" and credits_v2.is_unlocked(email, job["product_id"], job["platform"]):
        product = supabase.table("tiktok_products").select("id, tiktok_price").eq("id", job["product_id"]).single().execute().data
        out["suppliers"] = verified_suppliers(product, job["platform"]) if product else []
    if job["status"] in ("done", "no_match", "failed"):
        out["credits"] = _balance(email)
    return out


# ==============================================================================
# ENDPOINT 10 — Get User Credits (single source of truth for the UI)
# ==============================================================================
@app.get("/credits")
async def get_credits(request: Request, api_key: str = Security(verify_api_key)):
    user_email = credits_v2.email_from_request(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return {"success": True, "credits": credits_v2.get_balance(user_email)}
    except credits_v2.CreditError as e:
        print(f"credit system error: {e}")
        raise HTTPException(status_code=503, detail="Credits are temporarily unavailable")


# ==============================================================================
# ENDPOINT 11 — LemonSqueezy Webhook
# ==============================================================================
LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET")
PRO_VARIANT_ID = os.getenv("LEMONSQUEEZY_PRODUCT_ID")
ULTIMATE_VARIANT_ID = os.getenv("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID")


# Lemon Squeezy subscription statuses -> do they keep the paid plan?
#   active, on_trial            paid
#   cancelled                   paid until ends_at (LS sends subscription_expired when it really ends)
#   past_due                    paid (grace while LS retries the card)
#   unpaid, expired, paused     free
PAID_STATUSES = {"active", "on_trial", "cancelled", "past_due"}


def _plan_for_variant(variant_id: str) -> Optional[str]:
    if variant_id and variant_id == str(ULTIMATE_VARIANT_ID or ""):
        return "ultimate"
    if variant_id and variant_id == str(PRO_VARIANT_ID or ""):
        return "pro"
    return None


def _set_plan(email: str, plan: str, new_period: bool) -> None:
    res = supabase.table("users").update({"plan": plan}).ilike("user_email", email).execute()
    if not res.data:
        print(f"Webhook: no users row for {email}; plan {plan} not applied")
        return
    try:
        if new_period:
            credits_v2.start_period(email)      # fresh 30-day allowance on each successful payment
        else:
            credits_v2.get_balance(email)       # applies the new allowance, keeps usage
    except credits_v2.CreditError as e:
        print(f"Webhook credit update failed for {email}: {e}")


@app.post("/lemonsqueezy-webhook")
async def lemonsqueezy_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-signature") or ""
    if not LEMONSQUEEZY_WEBHOOK_SECRET or not signature:
        raise HTTPException(status_code=401, detail="No signature")
    digest = hmac.new(LEMONSQUEEZY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    meta = payload.get("meta", {}) or {}
    event_name = meta.get("event_name") or ""
    data = payload.get("data", {}) or {}
    attrs = data.get("attributes", {}) or {}

    # Idempotency: LS retries deliveries; process each (event, object, updated_at) once.
    event_key = f"{event_name}:{data.get('type')}:{data.get('id')}:{attrs.get('updated_at') or attrs.get('created_at')}"
    try:
        supabase.table("webhook_events").insert({"id": event_key, "event_name": event_name}).execute()
    except Exception as e:
        if "duplicate" in str(e).lower() or "23505" in str(e):
            return {"status": "ok", "duplicate": True}
        print(f"webhook_events insert failed (continuing): {e}")

    custom = meta.get("custom_data", {}) or {}
    email = (custom.get("email") or custom.get("user_email") or attrs.get("user_email") or "").strip().lower()
    if not email:
        print(f"Webhook {event_name}: no email found")
        return {"status": "ok"}

    if event_name.startswith("subscription_payment_"):
        # Payment events carry an invoice; only a success starts a new credit period.
        if event_name == "subscription_payment_success":
            plan = (supabase.table("users").select("plan").ilike("user_email", email).limit(1).execute().data or [{}])[0].get("plan")
            if plan in ("pro", "ultimate"):
                credits_v2.start_period(email)
        return {"status": "ok"}

    if not event_name.startswith("subscription_"):
        return {"status": "ok"}

    variant_id = str(attrs.get("variant_id") or "")
    plan = _plan_for_variant(variant_id)
    if plan is None:
        print(f"Webhook {event_name}: unknown variant {variant_id} for {email}; ignored")
        return {"status": "ok"}

    status = (attrs.get("status") or "").lower()
    if event_name == "subscription_expired" or status not in PAID_STATUSES:
        _set_plan(email, "free", new_period=False)
        print(f"{email} -> free ({event_name}, status={status})")
    else:
        _set_plan(email, plan, new_period=(event_name == "subscription_created"))
        print(f"{email} -> {plan} ({event_name}, status={status})")
    return {"status": "ok"}


# ==============================================================================
# ENDPOINT 12 — Create Checkout (supports Pro + Ultimate)
# ==============================================================================
class CheckoutRequest(BaseModel):
    email: str
    plan:  str = "pro"


@app.post("/create-checkout")
async def create_checkout(checkout_data: CheckoutRequest, request: Request, api_key: str = Security(verify_api_key)):
    signed_in = credits_v2.email_from_request(request)
    if not signed_in:
        raise HTTPException(status_code=401, detail="Sign in to subscribe")
    checkout_data.email = signed_in.lower()   # never trust the email in the body
    if checkout_data.plan not in ("pro", "ultimate"):
        raise HTTPException(status_code=400, detail="Unknown plan")
    if checkout_data.plan == "ultimate":
        variant_id = os.getenv("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID")
    else:
        variant_id = os.getenv("LEMONSQUEEZY_PRODUCT_ID")

    # Debug — print what we're sending
    print(f"Checkout: plan={checkout_data.plan}, email={checkout_data.email}")
    print(f"Variant ID: {variant_id}")
    print(f"Store ID: {os.getenv('LEMONSQUEEZY_STORE_ID')}")

    headers = {
        "Authorization": f"Bearer {os.getenv('LEMONSQUEEZY_API_KEY')}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": checkout_data.email,
                    "custom": {
                        "email": checkout_data.email
                    }
                }
            },
            "relationships": {
                "store": {
                    "data": {
                        "type": "stores",
                        "id": str(os.getenv("LEMONSQUEEZY_STORE_ID"))
                    }
                },
                "variant": {
                    "data": {
                        "type": "variants",
                        "id": str(variant_id)
                    }
                }
            }
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.lemonsqueezy.com/v1/checkouts",
            json=payload,
            headers=headers
        )

    # Print full LemonSqueezy response for debugging
    print(f"LemonSqueezy status: {response.status_code}")
    if response.status_code not in (200, 201):
        print(f"LemonSqueezy response: {response.text[:500]}")

    if response.status_code != 200 and response.status_code != 201:
        raise HTTPException(status_code=400, detail="Checkout creation failed")

    data = response.json()
    checkout_url = data.get("data", {}).get("attributes", {}).get("url")
    return {"success": True, "checkout_url": checkout_url}

# ==============================================================================
# ENDPOINT 13 — Create Checkout (supports Pro + Ultimate)
# ==============================================================================
from fastapi import Request
@app.post("/publish-blog-post")
async def publish_blog_post(request: Request):
    require_admin(request)
    import httpx, jwt, time, os, json

    try:
           body = await request.json()
    except Exception:
           body = {}

    ghost_url     = os.environ.get("GHOST_URL", "https://blog.tikprofitpro.shop")
    admin_api_key = os.environ.get("GHOST_ADMIN_API_KEY", "")

    if not admin_api_key or ":" not in admin_api_key:
        return {"errors": [{"message": "GHOST_ADMIN_API_KEY missing or invalid"}]}

    title         = body.get("title", "Untitled")  # ← change request → body
    slug          = body.get("slug", "")
    meta          = body.get("meta", "")
    content       = body.get("content", "")
    feature_image = body.get("feature_image", None)

    # Build Ghost JWT
    key_id, key_secret = admin_api_key.split(":")
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    token = jwt.encode(
        payload,
        bytes.fromhex(key_secret),
        algorithm="HS256",
        headers={"kid": key_id}
    )

    # Build mobiledoc
    mobiledoc = json.dumps({
        "version": "0.3.1",
        "atoms": [],
        "cards": [["markdown", {"markdown": content}]],
        "markups": [],
        "sections": [[10, 0]]
    })

    post_data = {
        "posts": [{
            "title":          title,
            "slug":           slug,
            "custom_excerpt": meta,
            "mobiledoc":      mobiledoc,
            "status":         "published",
        }]
    }

    if feature_image:
        post_data["posts"][0]["feature_image"] = feature_image

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{ghost_url}/ghost/api/admin/posts/",
            headers={
                "Authorization":  f"Ghost {token}",
                "Content-Type":   "application/json",
                "Accept-Version": "v5.0",
            },
            json=post_data,
            timeout=30.0,
        )

    data = res.json()

    # ── Auto-ping Google & Bing after successful publish ──
    if data.get("posts"):
        published_slug = data["posts"][0].get("slug", slug)
        published_url  = f"https://blog.tikprofitpro.shop/{published_slug}/"
        await ping_google(published_url)
        print(f"✅ Published and pinged: {published_url}")

    return data

# ─── Blog Post Generator via OpenAI ──────────────────────────────
# ─── Blog Post Generator via Groq ──────────────────────────────
@app.post("/generate-blog-post")
async def generate_blog_post(raw: Request):
    require_admin(raw)
    request = await raw.json()
    import httpx, os
    api_key = os.environ.get("GROQ_API_KEY", "")
    prompt  = request.get("prompt", "")

    if not api_key:
        return {"content": None, "error": "GROQ_API_KEY not set in Render"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      "llama-3.3-70b-versatile",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120.0,
            )
        data = res.json()

        if "choices" not in data:
            return {"content": None, "error": f"Groq error: {data}"}

        content = data["choices"][0]["message"]["content"]
        return data
        
    except Exception as e:
        return {"content": None, "error": str(e)}


@app.get("/get-image")
async def get_image(request: Request, query: str = "ecommerce"):
    require_admin(request)
    import httpx, os

    access_key = os.environ.get("UNSPLASH_ACCESS_KEY", "")

    if not access_key:
        return {"url": None, "error": "UNSPLASH_ACCESS_KEY not set in Railway"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {access_key}"},
                timeout=15.0,
            )
        data = res.json()
        results = data.get("results", [])
        if results:
            return {
                "url":         results[0]["urls"]["regular"],
                "thumb":       results[0]["urls"]["thumb"],
                "author":      results[0]["user"]["name"],
                "description": results[0].get("alt_description", ""),
            }
        return {"url": None, "error": f"No results from Unsplash. Total: {data.get('total', 0)}"}
    except Exception as e:
        return {"url": None, "error": str(e)}
    
    # ─── Google Search Console Auto-Ping ─────────────────────────────
async def ping_google(url: str):
    """Ping Google to index a new URL immediately after publishing"""
    import httpx
    try:
        # Method 1: Google Ping (IndexNow)
        async with httpx.AsyncClient() as client:
            # Ping Google Search via IndexNow protocol
            await client.get(
                f"https://www.google.com/ping?sitemap=https://blog.tikprofitpro.shop/sitemap.xml",
                timeout=10.0,
            )
            # Also ping Bing (IndexNow) — they share signals with Google
            await client.post(
                "https://api.indexnow.org/indexnow",
                json={
                    "host":    "blog.tikprofitpro.shop",
                    "key":     os.environ.get("INDEXNOW_KEY", ""),
                    "urlList": [url],
                },
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
    except Exception as e:
        print(f"Ping error (non-fatal): {e}")


@app.get("/indexnow")
async def indexnow_verify():
    indexnow_key = os.environ.get("INDEXNOW_KEY", "")
    return PlainTextResponse(indexnow_key)

@app.post("/generate-listing")
async def generate_listing(req: GenerateListingRequest, request: Request):
    """
    Draft a TikTok Shop listing. Rewritten to stop generic "AI slop":
    - looks at the product photo, so it describes the real item
    - banned-word list (no "elevate", "game-changer", "must-have", emojis...)
    - concrete specs only; unknown facts become [fill in] placeholders instead of invented claims
    - respects TikTok Shop rules: no medical claims, no "best/#1", no competitor brands
    Requires a signed-in user (it spends OpenAI money).
    """
    email = credits_v2.email_from_request(request)
    if not email:
        raise HTTPException(status_code=401, detail="Sign in to generate listings")
    if credits_v2.plan_for(email) not in ("pro", "ultimate"):
        raise HTTPException(status_code=402, detail="The listing generator is a Pro feature")
    if not (GROQ_API_KEY or openai_client):
        raise HTTPException(status_code=503, detail="Listing generator is not configured")

    image_part = []
    if req.image_url:
        from image_search import fetch_image_bytes, _data_url
        img = fetch_image_bytes(req.image_url)
        if img:
            image_part = [{"type": "image_url", "image_url": {"url": _data_url(img), "detail": "low"}}]

    system = """You write TikTok Shop product listings for a US seller who will sell their OWN white-label version of this product.

Write like a careful human merchandiser, not a marketer:
- Plain, specific, short sentences. Say what it is, what it's made of, size, what's in the box, who it's for.
- Only state facts you can see in the photo or read in the input. If a spec is unknown, write [fill in: size], [fill in: material] etc. Never invent numbers, certifications, ingredients or results.
- BANNED words/phrases: elevate, game-changer, must-have, revolutionary, transform, unleash, ultimate, perfect, effortless, seamless, luxurious, premium quality, take it to the next level, say goodbye, look no further, whether you're, in today's world, viral, best, #1, guaranteed. No emojis. No exclamation marks.
- TikTok Shop policy: no medical or cure claims, no before/after promises, no other brands' names.
- Title: max 120 characters, format "<Product type> - <key spec/material> - <key use>", no brand, no ALL CAPS.

Respond ONLY with JSON:
{"title": "...",
 "description": "3-4 short factual sentences",
 "sellingPoints": "5 lines separated by \\n, each starting with the feature, then the concrete benefit",
 "category": "one of: Beauty & Personal Care, Health, Fashion Accessories, Womenswear & Underwear, Home Supplies, Kitchenware, Phones & Electronics, Pet Supplies, Sports & Outdoor, Tools & Hardware, Toys & Hobbies",
 "missing_info": ["specs the seller must fill in before publishing"]}"""

    user_text = f"""Reference product title (from a competitor, may contain their brand; do NOT copy it): {req.product_name}
Selling price: ${req.price}
Category hint: {req.category}"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [{"type": "text", "text": user_text}] + image_part},
    ]
    try:
        if GROQ_API_KEY:
            # Groq (OpenAI-compatible, much cheaper). Llama 4 Scout reads images.
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": os.getenv("LISTING_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                          "temperature": 0.3, "max_tokens": 700,
                          "response_format": {"type": "json_object"}, "messages": messages},
                )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        else:
            resp = await openai_client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.3, max_tokens=700,
                response_format={"type": "json_object"}, messages=messages,
            )
            content = resp.choices[0].message.content
        return json.loads(content or "{}")
    except Exception as e:
        print(f"generate-listing error: {e}")
        raise HTTPException(status_code=502, detail="The listing generator couldn't finish. Try again in a minute.")


# ─── Get Shops ───────────────────────────────────────────────────
@app.get("/shops")
async def get_shops(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: str = "gmv_total",
    api_key: str = Security(verify_api_key),
):
    limit, offset = gate_list(await get_user_plan(request), limit, offset)
    try:
        # Valid sort columns
        allowed_sorts = ["gmv_total", "gmv_growth_pct", "created_at", "follower_count"]
        order_col = sort_by if sort_by in allowed_sorts else "gmv_total"

        res = supabase.table("shops")\
                .select("*", count="exact")\
                .order(order_col, desc=True)\
                .range(offset, offset + limit - 1)\
                .execute()
        return {
            "shops": res.data,
            "total": res.count,
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        print(f"shops error: {e}")
        return {"shops": [], "error": "Shops are temporarily unavailable"}

# ─── Get Creators ─────────────────────────────────────────────────
@app.get("/creators")
async def get_creators(
    request: Request,
    creator_type: str = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: str = "gmv_total",
    api_key: str = Security(verify_api_key),
):
    plan = await get_user_plan(request)
    limit, offset = gate_list(plan, limit, offset)
    order_col = sort_by if sort_by in ("gmv_total", "followers", "engagement_rate", "video_gmv", "live_gmv") else "gmv_total"
    try:
        query = supabase.table("creators").select("*")
        if creator_type:
            query = query.eq("creator_type", creator_type)
        res = query.order(order_col, desc=True, nullsfirst=False).range(offset, offset + limit - 1).execute()
        rows = res.data or []
        if plan not in ("pro", "ultimate"):
            for r in rows:                      # contact details are a paid feature
                r.pop("contact_email", None)
                r["contact_locked"] = True
        return {"creators": rows, "plan": plan}
    except Exception as e:
        print(f"creators error: {e}")
        return {"creators": [], "error": "Creators are temporarily unavailable"}
    
# ================================================================
# /api/jobs — real jobs from Remotive + Adzuna
# ================================================================
@app.get("/api/jobs")
async def get_jobs(request: Request, niches: str = "", location: str = "remote", job_type: str = "Any"):
    require_admin(request)
    niche_list = [n.strip().lower() for n in niches.split(",") if n.strip()]
    all_jobs = []
 
    # 1. REMOTIVE — free, no key needed
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for niche in niche_list[:3]:
                r = await client.get(
                    f"https://remotive.com/api/remote-jobs?search={niche.replace(' ','+')}&limit=20"
                )
                if r.status_code == 200:
                    for j in r.json().get("jobs", []):
                        all_jobs.append({
                            "id":          str(j.get("id")),
                            "title":       j.get("title", ""),
                            "company":     j.get("company_name", ""),
                            "source":      "Remotive",
                            "location":    j.get("candidate_required_location", "Remote"),
                            "type":        j.get("job_type", "full_time").replace("_", "-").title(),
                            "salary":      j.get("salary") or "Not listed",
                            "description": (j.get("description", "")[:300] + "...").strip(),
                            "url":         j.get("url", ""),
                            "applyEmail":  "",
                            "postedDate":  j.get("publication_date", "")[:10],
                            "skills":      j.get("tags", [])[:6],
                            "badge":       "new",
                            "real":        True,
                        })
    except Exception as e:
        print(f"[Remotive error] {e}")
 
    # 2. ADZUNA — free key from developer.adzuna.com
    if ADZUNA_ID and ADZUNA_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for niche in niche_list[:2]:
                    r = await client.get(
                        f"https://api.adzuna.com/v1/api/jobs/us/search/1"
                        f"?app_id={ADZUNA_ID}&app_key={ADZUNA_KEY}"
                        f"&results_per_page=10&what={niche.replace(' ','%20')}"
                        f"&content-type=application/json"
                    )
                    if r.status_code == 200:
                        for j in r.json().get("results", []):
                            sal = ""
                            if j.get("salary_min") and j.get("salary_max"):
                                sal = f"${int(j['salary_min']):,} – ${int(j['salary_max']):,}"
                            all_jobs.append({
                                "id":          str(j.get("id")),
                                "title":       j.get("title", ""),
                                "company":     j.get("company", {}).get("display_name", ""),
                                "source":      "Adzuna",
                                "location":    j.get("location", {}).get("display_name", "Remote"),
                                "type":        "Full-time",
                                "salary":      sal or "Not listed",
                                "description": (j.get("description", "")[:300] + "...").strip(),
                                "url":         j.get("redirect_url", ""),
                                "applyEmail":  "",
                                "postedDate":  j.get("created", "")[:10],
                                "skills":      [],
                                "badge":       "new",
                                "real":        True,
                            })
        except Exception as e:
            print(f"[Adzuna error] {e}")
 
    # Deduplicate
    seen, unique = set(), []
    for j in all_jobs:
        key = f"{j['title'].lower()}|{j['company'].lower()}"
        if key not in seen:
            seen.add(key)
            unique.append(j)
 
    # Score against niches
    def score(job):
        text = (job["title"] + " " + job["description"] + " " + " ".join(job.get("skills",[]))).lower()
        s = 50
        for n in niche_list:
            words = n.split()
            if all(w in text for w in words): s += 30
            elif any(w in text for w in words): s += 15
        if job.get("salary") and job["salary"] != "Not listed": s += 5
        if job.get("postedDate", "") >= "2026-04": s += 8
        return min(s, 99)
 
    for j in unique:
        j["match"] = score(j)
        j["badge"] = "hot" if j["match"] >= 85 else "featured" if j["match"] >= 70 else "new"
 
    unique.sort(key=lambda x: x["match"], reverse=True)
 
    return JSONResponse({
        "jobs": unique[:15],
        "total": len(unique),
        "sources": {
            "remotive": sum(1 for j in unique[:15] if j["source"] == "Remotive"),
            "adzuna":   sum(1 for j in unique[:15] if j["source"] == "Adzuna"),
        }
    })
 
 
# ================================================================
# /api/parse-cv — upload PDF, GPT extracts structured profile
# ================================================================
@app.post("/api/parse-cv")
async def parse_cv(request: Request, file: UploadFile = File(...)):
    require_admin(request)
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files supported"}, status_code=400)
 
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
 
    try:
        text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
        os.unlink(tmp_path)
    except Exception as e:
        return JSONResponse({"error": f"PDF read error: {str(e)}"}, status_code=500)
 
    if not text.strip():
        return JSONResponse({"error": "Could not extract text from PDF"}, status_code=400)
 
    prompt = f"""Extract structured info from this CV. Return ONLY valid JSON, no markdown:
{{
  "name": "full name",
  "email": "email or empty string",
  "role": "current or most recent job title",
  "linkedin": "linkedin url or empty string",
  "skills": "comma separated top 10 technical skills",
  "bio": "2-sentence professional summary",
  "projects": "comma separated key projects max 5",
  "experience_years": "number as string",
  "education": "degree and institution"
}}
CV:
{text[:4000]}"""
 
    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or ""
        s, e = raw.find("{"), raw.rfind("}")
        return JSONResponse({"profile": json.loads(raw[s:e+1]), "cv_text": text[:2000]})
    except Exception as ex:
        return JSONResponse({"error": f"Parse error: {str(ex)}"}, status_code=500)
 
 
# ================================================================
# /api/extract-email — fetch job page, GPT extracts apply email
# ================================================================
@app.get("/api/extract-email")
async def extract_email(request: Request, url: str = ""):
    require_admin(request)
    if openai_client is None:
        return JSONResponse({"email": "", "error": "AI not configured"})
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"email": ""})
    """
    Fetches the job posting page and uses GPT to extract
    any apply/contact email address from the HTML content.
    Returns {"email": "found@email.com"} or {"email": ""}
    """
    if not url:
        return JSONResponse({"email": ""})
 
    # Fetch the job page
    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        ) as client:
            r = await client.get(url)
            html = r.text
    except Exception as e:
        print(f"[extract-email fetch error] {e}")
        return JSONResponse({"email": "", "error": str(e)})
 
    # Strip HTML tags — keep only visible text
    import re
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
 
    # Quick regex pass first — fast and free
    email_pattern = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
    # Filter out common non-apply emails
    exclude = ['sentry', 'noreply', 'no-reply', 'example', 'placeholder', 'wixpress',
               '.png', '.jpg', 'support@', 'info@lever', 'info@greenhouse']
    candidates = [e for e in email_pattern if not any(x in e.lower() for x in exclude)]
 
    if candidates:
        # Return the first clean candidate directly — no GPT needed
        return JSONResponse({"email": candidates[0], "all": candidates[:5], "method": "regex"})
 
    # No email found via regex — try GPT on page text
    try:
        prompt = f"""Look at this job posting page text and find any email address for applying or contacting about the job.
Return ONLY valid JSON: {{"email": "found@email.com"}} or {{"email": ""}} if none found.
Do not invent or guess emails. Only return real emails visible in the text.
 
Page text (first 2000 chars):
{text[:2000]}"""
 
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
        s, e = raw.find("{"), raw.rfind("}")
        result = json.loads(raw[s:e+1])
        result["method"] = "gpt"
        return JSONResponse(result)
    except Exception as ex:
        return JSONResponse({"email": "", "error": str(ex)})
 
 
# ================================================================
# /api/ai-proxy — cover email + CV summary generation (unchanged)
# ================================================================
@app.post("/api/ai-proxy")
async def ai_proxy(request: Request, api_key: str = Security(verify_api_key)):
    require_admin(request)
    if openai_client is None:
        return JSONResponse({"error": "AI not configured"}, status_code=503)
    # Was open to the whole internet: anyone could spend the OpenAI balance.
    try:
        body = await request.json()
        resp = await openai_client.chat.completions.create(
            model       = body.get("model", "gpt-4o-mini"),
            messages    = body.get("messages", []),
            max_tokens  = body.get("max_tokens", 2000),
            temperature = body.get("temperature", 0.7),
        )
        return JSONResponse({"content": resp.choices[0].message.content or ""})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
 
 
# ================================================================
# /jobs — serves job_hunt.html
# ================================================================
@app.get("/jobs", response_class=HTMLResponse)
async def job_hunt_ui(request: Request):
    require_admin(request)
    path = r"F:\profitscout\job_hunt.html"
    if not os.path.exists(path):
        return HTMLResponse(f"<h2>job_hunt.html not found at {path}</h2>", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
 

 # ─── Daily Blog Autopilot Endpoint ───────────────────────────────
@app.post("/run-autopilot")
async def run_autopilot(request: Request):
    expected = os.environ.get("AUTOPILOT_SECRET", "")
    given = request.headers.get("X-Autopilot-Secret", "")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from autopilot import run
        import threading
        thread = threading.Thread(target=run)
        thread.start()
        return {"status": "started", "message": "Autopilot running in background"}
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# ADMIN — supplier review queue
# Admins are the signed-in emails listed in ADMIN_EMAILS (comma separated).
#   Pending   = near-miss photo matches (default 0.60-0.79): same product, different bottle/label/angle
#   Live      = what users see (auto-verified >= 0.80, or hand-approved)
#   Rejected  = never queued again for that product
# ==============================================================================
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def require_admin_user(request: Request) -> str:
    email = (credits_v2.email_from_request(request) or "").lower()
    if not email:
        raise HTTPException(status_code=401, detail="Sign in required")
    if email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admins only")
    return email


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.get("/admin/me")
async def admin_me(request: Request, api_key: str = Security(verify_api_key)):
    email = (credits_v2.email_from_request(request) or "").lower()
    return {"admin": bool(email and email in ADMIN_EMAILS)}


@app.get("/admin/review/queue")
async def admin_review_queue(
    request: Request,
    view: str = Query(default="pending", pattern="^(pending|no_match|live)$"),
    platform: Optional[str] = Query(default=None, pattern="^(alibaba|aliexpress)$"),
    limit: int = Query(default=50, ge=1, le=200),
    api_key: str = Security(verify_api_key),
):
    """Products to look at. pending: has near-misses waiting. no_match: last sourcing found nothing
    verified. live: has suppliers users can see (to spot wrong auto-matches)."""
    require_admin_user(request)
    counts: dict = {}

    def bump(pid, plat, key):
        c = counts.setdefault((str(pid), plat), {"pending": 0, "live": 0, "status": None})
        c[key] = c.get(key, 0) + 1

    if view in ("pending", "no_match"):
        q = supabase.table("supplier_candidates").select("tiktok_product_id, platform").eq("status", "pending")
        if platform:
            q = q.eq("platform", platform)
        for r in q.limit(5000).execute().data or []:
            bump(r["tiktok_product_id"], r["platform"], "pending")
    if view in ("no_match", "live"):
        q = supabase.table("sourcing_jobs").select("product_id, platform, status, error, id").in_("status", ["done", "no_match"])
        if platform:
            q = q.eq("platform", platform)
        last = {}
        for r in q.order("id", desc=True).limit(20000).execute().data or []:
            last.setdefault((str(r["product_id"]), r["platform"]), r)
        for key, r in last.items():
            if (view == "no_match" and r["status"] == "no_match") or (view == "live" and r["status"] == "done"):
                c = counts.setdefault(key, {"pending": 0, "live": 0, "status": None})
                c["status"], c["note"] = r["status"], (r.get("error") or "")[:200]
        if view == "no_match":
            counts = {k: v for k, v in counts.items() if v["status"] == "no_match"}
    if view == "live":
        counts = {k: v for k, v in counts.items() if v["status"] == "done"}

    ids = list({pid for pid, _ in counts})[:1000]
    products = {}
    for i in range(0, len(ids), 200):
        for p in supabase.table("tiktok_products").select(
            "id, product_name, tiktok_price, thumbnail_url, image_storage_url, virality_score"
        ).in_("id", ids[i:i + 200]).execute().data or []:
            products[str(p["id"])] = p
    items = [
        {**products[pid], "platform": plat, **c}
        for (pid, plat), c in counts.items() if pid in products
    ]
    items.sort(key=lambda x: (-(x.get("pending") or 0), -(x.get("virality_score") or 0)))
    return {"view": view, "count": len(items), "items": items[:limit]}


@app.get("/admin/review/products/{product_id}")
async def admin_review_product(product_id: str, request: Request, api_key: str = Security(verify_api_key)):
    """Everything about one product's sourcing: live suppliers, near-misses, rejected, last runs."""
    require_admin_user(request)
    product = (supabase.table("tiktok_products").select(
        "id, product_name, tiktok_price, thumbnail_url, image_storage_url, category, virality_score"
    ).eq("id", product_id).single().execute().data)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    suppliers = (supabase.table("suppliers").select("*").eq("tiktok_product_id", product_id)
                 .order("similarity", desc=True, nullsfirst=False).execute().data or [])
    for s in suppliers:
        s["visible"] = (not s.get("hidden")) and (
            s.get("match_method") == "manual" or float(s.get("similarity") or 0) >= MIN_SIMILARITY)
    candidates = (supabase.table("supplier_candidates").select("*").eq("tiktok_product_id", product_id)
                  .in_("status", ["pending", "rejected"]).order("similarity", desc=True).execute().data or [])
    jobs = (supabase.table("sourcing_jobs").select("platform, status, error, result_count, finished_at")
            .eq("product_id", product_id).order("id", desc=True).limit(6).execute().data or [])
    return {"product": product, "suppliers": suppliers, "candidates": candidates, "jobs": jobs,
            "min_similarity": MIN_SIMILARITY}


@app.post("/admin/candidates/{candidate_id}/approve")
async def admin_approve_candidate(candidate_id: int, request: Request, api_key: str = Security(verify_api_key)):
    admin = require_admin_user(request)
    c = supabase.table("supplier_candidates").select("*").eq("id", candidate_id).single().execute().data
    if not c:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if c["status"] == "approved":
        return {"success": True, "supplier_id": c.get("supplier_id")}
    row = {
        "tiktok_product_id": c["tiktok_product_id"], "platform": c["platform"],
        "supplier_name": c.get("supplier_name"), "product_title": c.get("product_title"),
        "unit_cost_min": c.get("unit_cost_min"), "unit_cost_max": c.get("unit_cost_max"),
        "moq": c.get("moq"), "supplier_url": c.get("supplier_url"), "image_url": c.get("image_url"),
        "similarity": c.get("similarity"), "match_method": "manual",
        "external_product_id": c.get("external_product_id"), "orders": c.get("orders"),
        "rating": c.get("rating"), "years_on_platform": c.get("years_on_platform"),
        "supplier_trust_score": min((c.get("years_on_platform") or 0) / 2, 5.0),
        "verified_supplier": c.get("verified_supplier"),
        "shipping_estimate_days": 20 if c["platform"] == "alibaba" else 10,
        "whitelabel_available": c["platform"] == "alibaba",
        "sourced_at": _now(), "reviewed_by": admin, "reviewed_at": _now(), "hidden": False,
    }
    saved = supabase.table("suppliers").insert(row).execute().data
    sid = saved[0]["id"] if saved else None
    supabase.table("supplier_candidates").update(
        {"status": "approved", "reviewed_by": admin, "reviewed_at": _now(), "supplier_id": sid}
    ).eq("id", candidate_id).execute()
    return {"success": True, "supplier_id": sid}


@app.post("/admin/candidates/{candidate_id}/reject")
async def admin_reject_candidate(candidate_id: int, request: Request, api_key: str = Security(verify_api_key)):
    admin = require_admin_user(request)
    supabase.table("supplier_candidates").update(
        {"status": "rejected", "reviewed_by": admin, "reviewed_at": _now()}
    ).eq("id", candidate_id).execute()
    return {"success": True}


@app.post("/admin/candidates/{candidate_id}/reopen")
async def admin_reopen_candidate(candidate_id: int, request: Request, api_key: str = Security(verify_api_key)):
    """Undo a reject (or an approve: the supplier row it created is hidden)."""
    admin = require_admin_user(request)
    c = supabase.table("supplier_candidates").select("status, supplier_id").eq("id", candidate_id).single().execute().data
    if not c:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if c.get("supplier_id"):
        supabase.table("suppliers").update({"hidden": True, "reviewed_by": admin, "reviewed_at": _now()}).eq("id", c["supplier_id"]).execute()
    supabase.table("supplier_candidates").update(
        {"status": "pending", "reviewed_by": None, "reviewed_at": None, "supplier_id": None}
    ).eq("id", candidate_id).execute()
    return {"success": True}


class SupplierVisibility(BaseModel):
    hidden: bool


@app.post("/admin/suppliers/{supplier_id}/visibility")
async def admin_supplier_visibility(supplier_id: int, body: SupplierVisibility, request: Request,
                                    api_key: str = Security(verify_api_key)):
    """Hide a wrong auto-match from users (or show it again). Re-sourcing keeps it hidden."""
    admin = require_admin_user(request)
    supabase.table("suppliers").update(
        {"hidden": body.hidden, "reviewed_by": admin, "reviewed_at": _now()}
    ).eq("id", supplier_id).execute()
    return {"success": True}


class ManualSupplier(BaseModel):
    product_id:    str
    platform:      str
    supplier_url:  str
    supplier_name: str
    unit_cost_min: float
    unit_cost_max: Optional[float] = None
    moq:           Optional[int] = None
    image_url:     Optional[str] = None
    product_title: Optional[str] = None


@app.post("/admin/suppliers")
async def admin_add_supplier(body: ManualSupplier, request: Request, api_key: str = Security(verify_api_key)):
    """Add a supplier the search missed (paste the listing link)."""
    admin = require_admin_user(request)
    if body.platform not in ("alibaba", "aliexpress", "amazon"):
        raise HTTPException(status_code=400, detail="Platform must be alibaba, aliexpress or amazon")
    if not body.supplier_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Paste the full https:// link")
    row = {
        "tiktok_product_id": body.product_id, "platform": body.platform,
        "supplier_name": body.supplier_name.strip(), "product_title": body.product_title,
        "unit_cost_min": body.unit_cost_min, "unit_cost_max": body.unit_cost_max or body.unit_cost_min,
        "moq": body.moq, "supplier_url": body.supplier_url.strip(), "image_url": body.image_url,
        "similarity": None, "match_method": "manual", "hidden": False,
        "shipping_estimate_days": 20 if body.platform == "alibaba" else 10,
        "whitelabel_available": body.platform == "alibaba",
        "sourced_at": _now(), "reviewed_by": admin, "reviewed_at": _now(),
    }
    saved = supabase.table("suppliers").insert(row).execute().data
    return {"success": True, "supplier": saved[0] if saved else row}


# ==============================================================================
# ADMIN: weekly data upload + supplier sourcing (no PC needed)
#   /admin/upload page in the app uses these.
#   1) POST /admin/import          upload Kalodata CSVs; dry_run=true previews, false imports (background)
#   2) GET  /admin/import/{job}    progress + log of an import
#   3) POST /admin/sourcing/run    starts the "Find suppliers" GitHub Actions workflow
#   4) GET  /admin/sourcing/runs   last runs of that workflow
# Render env for 3/4: GITHUB_ACTIONS_TOKEN (fine-grained token, Actions: read and write on the
# backend repo), optional GITHUB_REPO (default sodewala45/profitscout), GITHUB_REF (default main).
# ==============================================================================
import threading as _threading
import uuid as _uuid
import shutil as _shutil
from typing import List as _List
from fastapi import Form

import import_kalodata as _kalo

_IMPORT_JOBS: dict = {}
_IMPORT_LOCK = _threading.Lock()
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_GH_REPO = os.getenv("GITHUB_REPO", "sodewala45/profitscout")
_GH_REF = os.getenv("GITHUB_REF", "main")
_GH_WORKFLOW = os.getenv("GITHUB_SOURCING_WORKFLOW", "presource.yml")


def _run_import(job: dict, files: list, dry_run: bool, rehost: bool, deactivate: bool):
    def log(msg=""):
        job["log"].append(str(msg))
        job["log"] = job["log"][-400:]
    try:
        groups = {"products": [], "creators": [], "shops": []}
        for f in files:
            if f["kind"] in groups:
                groups[f["kind"]].append(f["path"])
        summary = {}
        if groups["products"]:
            log(f"== Products ({len(groups['products'])} file(s))")
            summary["products"] = _kalo.import_products(groups["products"], dry_run, rehost, deactivate, log=log)
        if groups["creators"]:
            log(f"== Creators ({len(groups['creators'])} file(s))")
            _kalo.import_creators(groups["creators"], dry_run, log=log)
        if groups["shops"]:
            log(f"== Shops ({len(groups['shops'])} file(s))")
            _kalo.import_shops(groups["shops"], dry_run, log=log)
        job["summary"] = summary
        job["status"] = "done"
    except Exception as e:
        job["status"] = "failed"
        job["error"] = type(e).__name__
        log(f"Stopped: {type(e).__name__}: {str(e)[:300]}")
    finally:
        job["finished_at"] = _now()
        _shutil.rmtree(job["dir"], ignore_errors=True)
        if not dry_run and _IMPORT_LOCK.locked():
            _IMPORT_LOCK.release()


@app.post("/admin/import")
async def admin_import(
    request: Request,
    files: _List[UploadFile] = File(...),
    dry_run: bool = Form(True),
    deactivate: bool = Form(True),
    rehost: bool = Form(True),
    api_key: str = Security(verify_api_key),
):
    """Upload Kalodata CSV exports. Each file's type (products / creators / shops) is read from its columns."""
    admin = require_admin_user(request)
    if not files:
        raise HTTPException(status_code=400, detail="Add at least one CSV file")
    folder = tempfile.mkdtemp(prefix="kalo_")
    saved, total = [], 0
    for up in files:
        name = os.path.basename(up.filename or "file.csv")
        if not name.lower().endswith(".csv"):
            _shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"{name} is not a .csv file")
        data = await up.read()
        total += len(data)
        if total > _MAX_UPLOAD_BYTES:
            _shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(status_code=413, detail="Files are larger than 30 MB in total")
        path = os.path.join(folder, f"{len(saved)}_{name}")
        with open(path, "wb") as fh:
            fh.write(data)
        saved.append({"name": name, "path": path, "kind": _kalo.detect_kind(path), "bytes": len(data)})

    unknown = [f["name"] for f in saved if not f["kind"]]
    job = {"id": _uuid.uuid4().hex[:12], "status": "running", "dry_run": dry_run, "by": admin,
           "started_at": _now(), "finished_at": None, "log": [], "dir": folder,
           "files": [{k: f[k] for k in ("name", "kind", "bytes")} for f in saved]}
    if unknown:
        job["log"].append(f"Not recognised as a Kalodata export, skipped: {', '.join(unknown)}")
    if not any(f["kind"] for f in saved):
        _shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=400, detail="None of these files look like Kalodata product, creator or shop exports")

    if dry_run:  # fast, writes nothing: run it now and answer with the preview
        _run_import(job, saved, True, False, False)
        return {k: v for k, v in job.items() if k != "dir"}

    if not _IMPORT_LOCK.acquire(blocking=False):
        _shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=409, detail="An import is already running. Wait for it to finish.")
    _IMPORT_JOBS[job["id"]] = job
    for old in sorted(_IMPORT_JOBS, key=lambda j: _IMPORT_JOBS[j]["started_at"])[:-10]:
        _IMPORT_JOBS.pop(old, None)
    _threading.Thread(target=_run_import, args=(job, saved, False, rehost, deactivate), daemon=True).start()
    return {k: v for k, v in job.items() if k != "dir"}


@app.get("/admin/import/{job_id}")
async def admin_import_status(job_id: str, request: Request, api_key: str = Security(verify_api_key)):
    require_admin_user(request)
    job = _IMPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import not found (the server may have restarted). Run it again; it's safe to repeat.")
    return {k: v for k, v in job.items() if k != "dir"}


@app.get("/admin/import")
async def admin_import_list(request: Request, api_key: str = Security(verify_api_key)):
    require_admin_user(request)
    jobs = sorted(_IMPORT_JOBS.values(), key=lambda j: j["started_at"], reverse=True)
    return {"imports": [{k: v for k, v in j.items() if k not in ("dir", "log")} for j in jobs]}


def _gh_headers():
    token = os.getenv("GITHUB_ACTIONS_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Supplier search isn't set up yet: add GITHUB_ACTIONS_TOKEN on Render")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


class SourcingRun(BaseModel):
    mode: str = "new"          # new = products never sourced; all = every active product again
    max_requests: int = 1500   # cap on paid Oxylabs results this run
    limit: int = 0             # 0 = no limit


@app.post("/admin/sourcing/run")
async def admin_sourcing_run(body: SourcingRun, request: Request, api_key: str = Security(verify_api_key)):
    admin = require_admin_user(request)
    if body.mode not in ("new", "all"):
        raise HTTPException(status_code=400, detail="mode must be new or all")
    max_req = max(10, min(int(body.max_requests), 5000))
    url = f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/{_GH_WORKFLOW}/dispatches"
    payload = {"ref": _GH_REF, "inputs": {"mode": body.mode, "max_requests": str(max_req),
                                          "limit": str(max(0, int(body.limit))), "started_by": admin}}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=_gh_headers(), json=payload)
    if r.status_code != 204:
        msg = {401: "GitHub token is wrong or expired", 403: "GitHub token can't run Actions on this repo",
               404: "Workflow not found: check GITHUB_REPO and that .github/workflows/presource.yml is on main",
               422: "GitHub refused the inputs"}.get(r.status_code, f"GitHub error {r.status_code}")
        raise HTTPException(status_code=502, detail=msg)
    return {"success": True, "message": "Supplier search started. It takes about 1 to 3 hours for a full run."}


@app.get("/admin/sourcing/runs")
async def admin_sourcing_runs(request: Request, api_key: str = Security(verify_api_key)):
    require_admin_user(request)
    url = f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/{_GH_WORKFLOW}/runs?per_page=5"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers=_gh_headers())
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitHub error {r.status_code}")
    runs = [{"id": x["id"], "status": x["status"], "conclusion": x.get("conclusion"),
             "created_at": x["created_at"], "updated_at": x["updated_at"], "url": x["html_url"],
             "title": x.get("display_title")} for x in r.json().get("workflow_runs", [])]
    return {"runs": runs}
