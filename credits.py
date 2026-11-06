"""
TikProfitPro — Credits v2
=========================
One place for all credit logic. The rules:

- Allowance per 30-day period: Free 5, Pro 50, Ultimate 120 (see plan_allowance() in SQL).
- Plan is read from users.plan only. user_credits.plan just mirrors it.
- When a period ends, credits reset to the plan allowance. Free resets to 5, never 0.
- Opening a product+platform costs 1 credit only when it has photo-verified suppliers.
- A new period starts on every successful subscription payment (Lemon Squeezy webhook).
- Reopening a product+platform you already unlocked is free.
- Cached results are free.
- If the database is down we fail CLOSED (no free unlimited searches).

All the heavy lifting is done atomically in Postgres functions
(supabase/migrations/20261027_credits_v2_and_snapshots.sql).
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


class CreditError(Exception):
    """Raised when the credit system itself is unavailable."""


def _rpc(name: str, params: dict):
    try:
        res = _supabase.rpc(name, params).execute()
    except Exception as e:  # network / missing function
        raise CreditError(f"{name} failed: {e}") from e
    return res.data


def get_balance(user_email: str) -> dict:
    """{plan, credits_total, credits_used, credits_remaining, period_end}"""
    data = _rpc("get_credit_balance", {"p_email": user_email})
    if not isinstance(data, dict):
        raise CreditError("get_credit_balance returned no data")
    return data


def is_unlocked(user_email: str, product_id: str, platform: str) -> bool:
    try:
        res = (
            _supabase.table("credit_unlocks")
            .select("id")
            .eq("user_email", user_email)
            .eq("product_id", str(product_id))
            .eq("platform", platform)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        return False


def consume(user_email: str, product_id: str, platform: str, mode: str = "text") -> dict:
    """Spend 1 credit atomically. Returns {allowed, charged, ...balance}."""
    data = _rpc(
        "consume_credit",
        {"p_email": user_email, "p_product_id": str(product_id), "p_platform": platform, "p_mode": mode},
    )
    if not isinstance(data, dict):
        raise CreditError("consume_credit returned no data")
    return data


def start_period(user_email: str) -> dict:
    """New 30-day period at the plan allowance (called on each successful subscription payment)."""
    data = _rpc("start_credit_period", {"p_email": user_email})
    return data if isinstance(data, dict) else {}


def refund(user_email: str, product_id: str, platform: str) -> None:
    try:
        _rpc("refund_credit", {"p_email": user_email, "p_product_id": str(product_id), "p_platform": platform})
    except CreditError as e:
        print(f"refund failed (non-fatal): {e}")


def email_from_request(request) -> Optional[str]:
    """Return the signed-in user's email from the Supabase JWT, or None."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    try:
        user = _supabase.auth.get_user(auth[7:]).user
        return user.email if user else None
    except Exception:
        return None


def plan_for(user_email: Optional[str]) -> str:
    if not user_email:
        return "free"
    try:
        res = _supabase.table("users").select("plan").eq("user_email", user_email).limit(1).execute()
        raw = (res.data[0].get("plan") if res.data else "free") or "free"
        raw = raw.lower()
        return raw if raw in ("pro", "ultimate") else "free"
    except Exception:
        return "free"
