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

from security import normalize_email, normalize_plan

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


class AuthUser:
    """The signed-in Supabase user behind a request (verified server-side by GoTrue)."""

    __slots__ = ("id", "email", "email_confirmed")

    def __init__(self, id: str, email: str, email_confirmed: bool):
        self.id = id
        self.email = email
        self.email_confirmed = email_confirmed


def _confirmed(user) -> bool:
    # Google OAuth users have email_confirmed_at set. A missing field counts as NOT confirmed.
    return bool(getattr(user, "email_confirmed_at", None) or getattr(user, "confirmed_at", None))


def user_from_request(request) -> Optional[AuthUser]:
    """Verified user for this request, or None. Cached on request.state so one request makes at
    most one GoTrue call (the rate limiter, auth checks and handlers all share it)."""
    state = getattr(request, "state", None)
    if state is not None and hasattr(state, "auth_user"):
        return state.auth_user
    out = None
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer ") and len(auth) > 7:
        try:
            user = _supabase.auth.get_user(auth[7:]).user
            if user and user.email:
                out = AuthUser(str(user.id), normalize_email(user.email), _confirmed(user))
        except Exception:
            out = None
    if state is not None:
        state.auth_user = out
    return out


def email_from_request(request) -> Optional[str]:
    """Return the signed-in user's email (lowercased) from the Supabase JWT, or None."""
    u = user_from_request(request)
    return u.email if u else None


def plan_for(user_email: Optional[str]) -> str:
    if not user_email:
        return "free"
    try:
        res = _supabase.table("users").select("plan").eq("user_email", normalize_email(user_email)).limit(1).execute()
        return normalize_plan(res.data[0].get("plan") if res.data else "free")
    except Exception:
        return "free"
