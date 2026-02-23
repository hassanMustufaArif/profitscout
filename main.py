"""
ProfitScout — FastAPI Backend
=============================
Run with:  uvicorn main:app --reload
API Docs:  http://127.0.0.1:8000/docs
"""
import os
import httpx
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from typing import Optional


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",  # or replace with your specific Lovable URL
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Key Auth ---
API_KEY_HEADER = APIKeyHeader(name="X-API-Key")

async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if api_key != os.getenv("API_KEY"):
        raise HTTPException(status_code=403)
    return api_key



# ==============================================================================
# ENDPOINT 1 — Health Check
# GET /
# Just to verify the API is running
# ==============================================================================
@app.get("/")
def root():
    return {
        "status": "running",
        "app": "ProfitScout API",
        "version": "1.0.0",
        "docs": "http://127.0.0.1:8000/docs"
    }


# ==============================================================================
# ENDPOINT 2 — Get All Products (with filters)
# GET /products
#
# Query params (all optional):
#   min_score   = minimum virality score (default 0)
#   max_score   = maximum virality score (default 100)
#   trend_phase = rising | peak | declining
#   category    = e.g. Skincare
#   min_price   = minimum product price
#   max_price   = maximum product price
#   limit       = number of results (default 20, max 100)
#   offset      = for pagination (default 0)
#
# Example: /products?min_score=65&trend_phase=rising&limit=10
# ==============================================================================
@app.get("/products")
async def get_products(api_key: str = Security(verify_api_key)):
    min_score:   int            = Query(default=0,   ge=0, le=100),
    max_score:   int            = Query(default=100, ge=0, le=100),
    trend_phase: Optional[str]  = Query(default=None),
    category:    Optional[str]  = Query(default=None),
    min_price:   Optional[float]= Query(default=None),
    max_price:   Optional[float]= Query(default=None),
    limit:       int            = Query(default=20,  ge=1, le=100),
    offset:      int            = Query(default=0,   ge=0),

    try:
        query = (
            supabase.table("tiktok_products")
            .select("*")
            .eq("is_active", True)
            .gte("virality_score", min_score)
            .lte("virality_score", max_score)
            .order("virality_score", desc=True)
            .range(offset, offset + limit - 1)
        )

        # Apply optional filters
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
            "success": True,
            "count":   len(result.data),
            "offset":  offset,
            "limit":   limit,
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 3 — Get Single Product Detail
# GET /products/{product_id}
#
# Returns full product data including suppliers if already sourced
# Example: /products/abc123-uuid-here
# ==============================================================================
@app.get("/products/{product_id}")
def get_product(product_id: str, api_key: str = Security(verify_api_key)):

    try:
        # Get product
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

        # Get suppliers for this product (if already sourced)
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
# GET /products/tier/{tier}
#
# tier options: hot | rising | watch | pass
# Shortcut endpoint — no need to remember score ranges
# Example: /products/tier/hot  →  returns all products scored 80-100
# ==============================================================================
@app.get("/products/tier/{tier}")
def get_products_by_tier(
    tier:   str,
    limit:  int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0,  ge=0),
    api_key: str = Security(verify_api_key),
):
    tier_ranges = {
        "hot":     (80, 100),
        "rising":  (65, 79),
        "watch":   (50, 64),
        "pass":    (0,  49),
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
            "success": True,
            "tier":    tier,
            "range":   f"{min_score}-{max_score}",
            "count":   len(result.data),
            "products": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# ENDPOINT 5 — Get Dashboard Stats
# GET /stats
#
# Returns summary counts for the dashboard header
# Example: /stats
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

        hot     = len([p for p in products if p["virality_score"] >= 80])
        rising  = len([p for p in products if 65 <= p["virality_score"] < 80])
        watch   = len([p for p in products if 50 <= p["virality_score"] < 65])
        total   = len(products)

        # Top categories
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
# POST /calculate-profit
#
# Send product details + your pricing → get exact net profit breakdown
# ==============================================================================
class ProfitRequest(BaseModel):
    product_id:               str
    supplier_id:              Optional[str] = None
    sale_price:               float
    cogs:                     float          # Cost of goods from supplier
    affiliate_commission_rate: float = 15.0  # % — default 15%
    fbt_enabled:              bool  = True   # Using TikTok FBT fulfillment?
    product_weight_oz:        float = 8.0    # For FBT fee calculation
    inbound_shipping:         float = 1.20   # Per unit cost to send to FBT
    return_rate_pct:          float = 8.0    # Expected return rate %
    ad_spend_per_unit:        float = 0.0    # Optional ad cost per unit
    is_new_seller:            bool  = False  # New sellers get 3% referral (first 30 days)


@app.post("/calculate-profit")
def calculate_profit(req: ProfitRequest, api_key: str = Security(verify_api_key)):
    try:
        # ── TikTok Fee Calculations ──────────────────────────────────────────

        # Referral fee: 3% for new sellers (first 30 days), 6% standard
        referral_rate  = 0.03 if req.is_new_seller else 0.06
        referral_fee   = round(req.sale_price * referral_rate, 2)

        # Transaction fee: ~2.9%
        transaction_fee = round(req.sale_price * 0.029, 2)

        # Affiliate commission
        affiliate_amt   = round(req.sale_price * (req.affiliate_commission_rate / 100), 2)

        # FBT fulfillment fee (based on weight)
        if req.fbt_enabled:
            if req.product_weight_oz <= 4:    fbt_fee = 2.68
            elif req.product_weight_oz <= 8:  fbt_fee = 3.58
            elif req.product_weight_oz <= 12: fbt_fee = 4.20
            elif req.product_weight_oz <= 16: fbt_fee = 4.88
            else:                             fbt_fee = 5.50
        else:
            fbt_fee = 0.0

        # Return buffer: return_rate% of (refund admin fee = 20% of referral fee, capped at $5)
        refund_admin_fee = min(referral_fee * 0.20, 5.0)
        return_buffer    = round((req.return_rate_pct / 100) * refund_admin_fee, 2)

        # ── Total Costs ──────────────────────────────────────────────────────
        total_costs = round(
            req.cogs
            + referral_fee
            + transaction_fee
            + affiliate_amt
            + fbt_fee
            + req.inbound_shipping
            + return_buffer
            + req.ad_spend_per_unit,
            2
        )

        # ── Net Profit ───────────────────────────────────────────────────────
        net_profit = round(req.sale_price - total_costs, 2)
        margin_pct = round((net_profit / req.sale_price) * 100, 1) if req.sale_price > 0 else 0

        # ── Profitability Rating ─────────────────────────────────────────────
        if margin_pct >= 35:   rating = "green"
        elif margin_pct >= 20: rating = "amber"
        else:                  rating = "red"

        # ── Warning Flags ────────────────────────────────────────────────────
        warnings = []
        total_fees_pct = ((referral_fee + transaction_fee + affiliate_amt) / req.sale_price) * 100
        if total_fees_pct > 35:
            warnings.append(f"TikTok fees + affiliate = {total_fees_pct:.1f}% of sale price. Very tight margins.")
        if req.affiliate_commission_rate > 20:
            warnings.append(f"Affiliate rate of {req.affiliate_commission_rate}% is very high. Consider reducing.")
        if req.return_rate_pct > 20:
            warnings.append(f"Return rate of {req.return_rate_pct}% will significantly erode profit.")
        if margin_pct < 0:
            warnings.append("This product is LOSING money at this price. Raise sale price or find cheaper supplier.")

        return {
            "success": True,
            "rating":  rating,
            "summary": {
                "sale_price":   req.sale_price,
                "total_costs":  total_costs,
                "net_profit":   net_profit,
                "margin_pct":   margin_pct,
            },
            "breakdown": {
                "cost_of_goods":        req.cogs,
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
# GET /search?q=serum
#
# Simple keyword search by product title
# Example: /search?q=vitamin+c&limit=10
# ==============================================================================
@app.get("/search")
async def search_products(q: str, api_key: str = Security(verify_api_key)):
    q:      str = Query(..., min_length=2, description="Search keyword"),
    limit:  int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0,  ge=0),

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
@app.get("/ping")
def ping():
    return {"status": "ok", "message": "FastAPI is working!"}
from supplier_engine import find_suppliers

# ==============================================================================
# ENDPOINT 8 — Find Suppliers for a Product
# GET /products/{product_id}/suppliers
# ==============================================================================
@app.get("/products/{product_id}/suppliers")
def get_suppliers(product_id: str, api_key: str = Security(verify_api_key)):
    try:
        # Get product from Supabase
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

        # Run supplier matching engine
        result = find_suppliers(
            product_id    = product["id"],
            product_title = product["title"],
            tiktok_price  = product["tiktok_price"] or 0
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/create-checkout")
async def create_checkout(api_key: str = Security(verify_api_key)):
    headers = {
        "Authorization": f"Bearer {os.getenv('LEMONSQUEEZY_API_KEY')}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }
    
    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {},
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
                        "id": str(os.getenv("LEMONSQUEEZY_PRODUCT_ID"))
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
    
    data = response.json()
    print("Lemon Squeezy response:", data)  # shows in Railway logs
    
    if "data" not in data:
        raise HTTPException(status_code=400, detail=str(data))
    
    checkout_url = data["data"]["attributes"]["url"]
    return {"url": checkout_url}