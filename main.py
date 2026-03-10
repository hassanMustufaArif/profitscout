"""
ProfitScout — FastAPI Backend
=============================
Run with:  uvicorn main:app --reload
API Docs:  http://127.0.0.1:8000/docs
"""

import os
import httpx
from fastapi import FastAPI, HTTPException, Query, Security, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone, timedelta
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supplier_engine import find_suppliers, find_suppliers_by_platform, check_credits, initialize_credits
from supabase import create_client, Client
from dotenv import load_dotenv
from typing import Optional
import hmac
import hashlib
import json

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tikprofitpro.shop",
        "https://www.tikprofitpro.shop",
        "https://tikprofitpro.pages.dev",
        "https://tiktokscout.lovable.app",
    ],
    allow_credentials=True,
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
        print(f"get_user_plan: email={email}")  # ADD THIS
        result = supabase.table("users").select("plan").eq("user_email", email).execute()
        print(f"get_user_plan: result={result.data}")  # ADD THIS
        if result.data and len(result.data) > 0:
            return result.data[0].get("plan", "free")
        return "free"
    except Exception as e:
        print(f"get_user_plan ERROR: {e}")  # ADD THIS
        return "free"


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

        suppliers_result = (
            supabase.table("suppliers")
            .select("*")
            .eq("tiktok_product_id", product_id)
            .order("supplier_trust_score", desc=True)
            .execute()
        )

        product["suppliers"] = suppliers_result.data or []

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
def get_products_by_tier(
    tier:    str,
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
    q:       str = Query(..., min_length=2, description="Search keyword"),
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    try:
        result = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .ilike("title", f"%{q}%")
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
    # ENDPOINT 8 — Find Suppliers (legacy — Alibaba only, backward compatible)
    # ==============================================================================
    @app.get("/products/{product_id}/suppliers")
    async def get_suppliers(product_id: str, request: Request, api_key: str = Security(verify_api_key)):
        try:
            plan = await get_user_plan(request)

            # Get user email
            auth_header = request.headers.get("Authorization")
            user_email = "anonymous"
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.replace("Bearer ", "")
                try:
                    user_response = supabase.auth.get_user(token)
                    user_email = user_response.user.email
                except Exception:
                    pass

            # Get product
            product_result = (
                supabase.table("tiktok_products")
                .select("id, title, tiktok_price")
                .eq("id", product_id)
                .single()
                .execute()
            )
            if not product_result.data:
                raise HTTPException(status_code=404, detail="Product not found")
            product = product_result.data

            # ── Single atomic credit check + deduct (BEFORE search) ───────────
            # Passes "free_user" to engine so it won't deduct a second time
            credit = check_and_deduct_credit(user_email, plan, product_id, "alibaba")
            if not credit["allowed"]:
                raise HTTPException(
                    status_code=402,
                    detail=f"No credits remaining. You've used {credit['credits_used']}/{credit['credits_total']} credits. Please upgrade your plan."
                )

            # Run supplier matching — "free_user" prevents engine deducting again
            result = find_suppliers_by_platform(
                product_id=product["id"],
                product_title=product["title"],
                tiktok_price=product["tiktok_price"] or 0,
                platform="alibaba",
                user_email="free_user"
            )

            return result

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    # ==============================================================================
    # ENDPOINT 9 — Find Suppliers by Platform (credit-based)
    # GET /products/{product_id}/suppliers/{platform}
    # platform = alibaba | aliexpress | amazon
    # ==============================================================================
    @app.get("/products/{product_id}/suppliers/{platform}")
    async def get_suppliers_by_platform(
        product_id: str,
        platform:   str,
        request:    Request,
        api_key:    str = Security(verify_api_key),
    ):
        """Search a specific platform. Costs 1 credit."""

        if platform not in ["alibaba", "aliexpress", "amazon"]:
            raise HTTPException(status_code=400, detail="Platform must be: alibaba, aliexpress, or amazon")

        try:
            # Get user email
            auth_header = request.headers.get("Authorization")
            user_email = None
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.replace("Bearer ", "")
                try:
                    user_response = supabase.auth.get_user(token)
                    user_email = user_response.user.email
                except Exception:
                    pass

            if not user_email:
                raise HTTPException(status_code=401, detail="Authentication required for supplier search")

            plan = await get_user_plan(request)

            # Block free users entirely
            if plan == "free":
                raise HTTPException(
                    status_code=403,
                    detail="Supplier search requires a Pro or Ultimate plan. Please upgrade."
                )

            # Get product
            product_result = (
                supabase.table("tiktok_products")
                .select("id, product_name, tiktok_price")
                .eq("id", product_id)
                .single()
                .execute()
            )
            if not product_result.data:
                raise HTTPException(status_code=404, detail="Product not found")
            product = product_result.data

            # ── Single atomic credit check + deduct (BEFORE search) ───────────
            # Passes "free_user" to engine so it won't deduct a second time
            credit = check_and_deduct_credit(user_email, plan, product_id, platform)
            if not credit["allowed"]:
                raise HTTPException(
                    status_code=402,
                    detail=f"No credits remaining. You've used {credit['credits_used']}/{credit['credits_total']} credits. Please upgrade your plan."
                )

            # Run supplier search — "free_user" prevents engine deducting again
            result = find_suppliers_by_platform(
                product_id=product["id"],
                product_title=product["product_name"],
                tiktok_price=product["tiktok_price"] or 0,
                platform=platform,
                user_email="free_user"
            )

            return result

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================


# ==============================================================================
# ENDPOINT 11 — LemonSqueezy Webhook
# ==============================================================================
LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET")
PRO_VARIANT_ID = os.getenv("LEMONSQUEEZY_PRODUCT_ID")
ULTIMATE_VARIANT_ID = os.getenv("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID")


@app.post("/lemonsqueezy-webhook")
async def lemonsqueezy_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-signature")

    if not signature:
        raise HTTPException(status_code=401, detail="No signature")

    digest = hmac.new(
        LEMONSQUEEZY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    event_name = payload.get("meta", {}).get("event_name")
    custom_data = payload.get("meta", {}).get("custom_data", {})
    user_email = custom_data.get("email") or custom_data.get("user_email")

    if not user_email:
        user_email = payload.get("data", {}).get("attributes", {}).get("user_email")

    if not user_email:
        print(f"Webhook {event_name}: no email found")
        return {"status": "ok"}

    # Determine plan from variant ID
    variant_id = str(payload.get("data", {}).get("attributes", {}).get("variant_id") or "")

    if variant_id == ULTIMATE_VARIANT_ID:
        plan = "ultimate"
    elif variant_id == PRO_VARIANT_ID:
        plan = "pro"
    else:
        plan = "pro"

    if event_name == "subscription_created":
        supabase.table("users").update({"plan": plan}).eq("user_email", user_email).execute()
        initialize_credits(user_email, plan)
        print(f"Upgraded {user_email} to {plan}")

    elif event_name in ["subscription_cancelled", "subscription_expired"]:
        supabase.table("users").update({"plan": "free"}).eq("user_email", user_email).execute()
        initialize_credits(user_email, "free")
        print(f"Downgraded {user_email} to free")

    elif event_name == "subscription_updated":
        status = payload.get("data", {}).get("attributes", {}).get("status")
        if status == "active":
            supabase.table("users").update({"plan": plan}).eq("user_email", user_email).execute()
            initialize_credits(user_email, plan)
        elif status in ["cancelled", "expired", "past_due"]:
            supabase.table("users").update({"plan": "free"}).eq("user_email", user_email).execute()
            initialize_credits(user_email, "free")
        print(f"Updated {user_email}: status={status}, plan={plan}")

    return {"status": "ok"}


# ==============================================================================
# ENDPOINT 12 — Create Checkout (supports Pro + Ultimate)
# ==============================================================================
class CheckoutRequest(BaseModel):
    email: str
    plan:  str = "pro"


@app.post("/create-checkout")
async def create_checkout(checkout_data: CheckoutRequest, api_key: str = Security(verify_api_key)):
    if checkout_data.plan == "ultimate":
        variant_id = os.getenv("LEMONSQUEEZY_ULTIMATE_PRODUCT_ID")
    else:
        variant_id = os.getenv("LEMONSQUEEZY_PRODUCT_ID")

    # Debug — print what we're sending
    print(f"Checkout: plan={checkout_data.plan}, email={checkout_data.email}")
    print(f"Variant ID: {variant_id}")
    print(f"Store ID: {os.getenv('LEMONSQUEEZY_STORE_ID')}")
    print(f"API Key exists: {bool(os.getenv('LEMONSQUEEZY_API_KEY'))}")

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
    print(f"LemonSqueezy response: {response.text}")

    if response.status_code != 200 and response.status_code != 201:
        raise HTTPException(status_code=400, detail="Checkout creation failed")

    data = response.json()
    checkout_url = data.get("data", {}).get("attributes", {}).get("url")
    return {"success": True, "checkout_url": checkout_url}