"""
TikProfitPro: shared security helpers
=====================================
- normalize_plan()  one place that turns users.plan into free / pro / ultimate
- client_ip()       caller IP behind Cloudflare / Render
- safe_fetch()      outbound HTTP fetch that refuses private/internal addresses (SSRF guard)
- is_http_url()     http(s) URL check for stored links
- parse_uuid()      UUID path params
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
import uuid
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("tikprofitpro.security")

PAID_PLANS = ("pro", "ultimate")


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------
def normalize_plan(raw) -> str:
    """None / '' / 'Free' / 'cancelled' / anything unknown -> 'free'. 'Pro ' -> 'pro'."""
    p = str(raw or "").strip().lower()
    return p if p in PAID_PLANS else "free"


def is_paid(plan) -> bool:
    return normalize_plan(plan) in PAID_PLANS


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------
def normalize_email(raw) -> str:
    return str(raw or "").strip().lower()


def email_hash(email: Optional[str]) -> str:
    """Short, non-reversible tag for logs (never log the email itself)."""
    if not email:
        return "-"
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:10]


def parse_uuid(value) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def client_ip(request) -> str:
    """Cloudflare puts the real visitor in CF-Connecting-IP. Without Cloudflare, Render's proxy
    appends the connecting address as the LAST X-Forwarded-For entry (earlier ones are client-supplied)."""
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    xff = request.headers.get("x-forwarded-for") or ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def is_http_url(value) -> bool:
    """True only for absolute http(s) URLs with a host (drops javascript:, data:, relative...)."""
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    if any(c in v for c in ("\n", "\r", "\t", " ")):
        return False
    try:
        parts = urlsplit(v)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


class UnsafeURL(Exception):
    """The URL (or a redirect) points somewhere we refuse to fetch."""


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified or not ip.is_global
    )


def _resolve_public(host: str, port: int) -> list[str]:
    """All addresses for host; raises UnsafeURL if ANY of them is non-public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"DNS lookup failed for {host}") from e
    addrs = sorted({i[4][0] for i in infos})
    if not addrs:
        raise UnsafeURL(f"no address for {host}")
    for a in addrs:
        if not _ip_is_public(ipaddress.ip_address(a.split("%")[0])):
            raise UnsafeURL(f"{host} resolves to a non-public address")
    return addrs


def check_url(url: str) -> None:
    """Raise UnsafeURL unless url is http(s) to a host that only resolves to public IPs."""
    if not is_http_url(url):
        raise UnsafeURL("only absolute http(s) URLs are allowed")
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL are not allowed")
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:  # literal IP: check directly (also catches decimal/odd forms that getaddrinfo normalises)
        if not _ip_is_public(ipaddress.ip_address(host)):
            raise UnsafeURL("non-public address")
        return
    except ValueError:
        pass
    if host.lower() in ("localhost",) or host.lower().endswith((".localhost", ".internal", ".local")):
        raise UnsafeURL("internal host name")
    _resolve_public(host, port)


_UA = {"User-Agent": "Mozilla/5.0 (TikProfitPro fetch)"}


class FetchResult:
    """What safe_fetch returns: final url, headers, and the (capped, already decoded) body."""

    def __init__(self, url: str, status_code: int, headers, content: bytes):
        self.url = url
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        enc = "utf-8"
        if "charset=" in ctype:
            enc = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def safe_fetch(url: str, max_bytes: int = 4_000_000, timeout: float = 15.0, max_redirects: int = 3,
               content_type_prefix: Optional[str] = None, headers: Optional[dict] = None) -> Optional[FetchResult]:
    """GET url with SSRF protection. Returns a FetchResult whose .content is at most max_bytes,
    or None when the fetch failed / was refused / was too large. Redirects are followed by hand
    (max_redirects) and every hop is re-checked.

    Note: the host is resolved and checked, then httpx resolves it again to connect. A DNS rebinding
    attacker with a very short TTL could in theory win that race; the timeout and byte cap bound the damage."""
    current = url
    hdrs = {**_UA, **(headers or {})}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=hdrs) as client:
            for _ in range(max_redirects + 1):
                check_url(current)
                with client.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        if not loc:
                            return None
                        current = urljoin(current, loc)
                        continue
                    if r.status_code != 200:
                        return None
                    ctype = r.headers.get("content-type", "")
                    if content_type_prefix and not ctype.startswith(content_type_prefix):
                        return None
                    clen = r.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        return None
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            return None
                    return FetchResult(current, 200, r.headers, bytes(buf))
            return None  # too many redirects
    except UnsafeURL as e:
        log.warning("safe_fetch refused: %s", e)
        return None
    except httpx.HTTPError as e:
        log.info("safe_fetch failed: %s", type(e).__name__)
        return None
