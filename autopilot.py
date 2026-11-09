"""
TikProfitPro — Daily Blog Autopilot
=====================================
Runs daily via APScheduler inside FastAPI.
Fetches trending products, writes blog post with Groq,
publishes to Ghost, pings IndexNow.

Usage (local test):
    python autopilot.py
"""

import os
import jwt
import time
import random
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
GHOST_ADMIN_API_KEY = os.getenv("GHOST_ADMIN_API_KEY")
GHOST_URL           = os.getenv("GHOST_URL", "https://blog.tikprofitpro.shop")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
INDEXNOW_KEY        = os.getenv("INDEXNOW_KEY", "")

KEYWORDS = [
    "how to find trending products on TikTok Shop USA 2026",
    "best TikTok Shop products under $20 USA",
    "TikTok Shop vs Amazon dropshipping which is better 2026",
    "how to use TikTok Shop for beginners step by step USA",
    "kalodata alternative free TikTok Shop research tool",
    "fastmoss alternative USA TikTok sellers 2026",
    "how to find winning TikTok Shop products without paid tools",
    "TikTok Shop beauty products trending USA 2026",
    "best TikTok Shop niches for beginners USA",
    "TikTok Shop health and wellness products USA 2026",
    "how to dropship on TikTok Shop USA complete guide",
    "TikTok Shop product research tool free USA",
    "best selling TikTok Shop products this week USA",
    "how to make money on TikTok Shop USA 2026",
    "TikTok Shop for small business owners USA guide",
    "trending TikTok Shop kitchen products USA 2026",
    "TikTok Shop electronics products trending USA",
    "how to find TikTok Shop suppliers USA guide",
    "TikTok Shop vs Shopify which is better for beginners",
    "TikTok Shop winning product criteria checklist USA",
    "best TikTok Shop tools for sellers USA 2026",
    "how to analyze TikTok Shop product trends USA",
    "TikTok Shop fashion products trending USA 2026",
    "TikTok Shop profit calculator free tool USA",
    "how to scale TikTok Shop business USA 2026",
]

INTERNAL_LINKS = [
    ("TikTok Shop product research tool", "https://tikprofitpro.shop"),
    ("find trending TikTok Shop products", "https://tikprofitpro.shop"),
    ("TikTok Shop profit calculator",      "https://tikprofitpro.shop/profit-calculator"),
    ("TikTok Shop supplier finder",        "https://tikprofitpro.shop"),
]

SOCIAL_LINKS = {
    "facebook":  "https://www.facebook.com/Ecomchains",
    "linkedin":  "https://www.linkedin.com/in/sodewala",
    "instagram": "https://www.instagram.com/arifsodewala/",
    "tiktok":    "https://tiktok.com/@tikprofitpro",
}

FALLBACK_IMAGES = [
    "https://images.unsplash.com/photo-1611162616475-46b635cb6868?w=1200",
    "https://images.unsplash.com/photo-1556742049-0cfed4f6a45d?w=1200",
    "https://images.unsplash.com/photo-1563013544-824ae1b704d3?w=1200",
    "https://images.unsplash.com/photo-1607082348824-0a96f2a4b9da?w=1200",
    "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=1200",
    "https://images.unsplash.com/photo-1472851294608-062f824d29cc?w=1200",
    "https://images.unsplash.com/photo-1571781926291-c477ebfd024b?w=1200",
    "https://images.unsplash.com/photo-1585771724684-38269d6639fd?w=1200",
    "https://images.unsplash.com/photo-1519389950473-47ba0277781c?w=1200",
    "https://images.unsplash.com/photo-1533750349088-cd871a92f312?w=1200",
]


def fetch_trending_products(limit=5):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = (
        sb.table("tiktok_products")
        .select("product_name, category, tiktok_price, gmv_7d, virality_score, trend_phase, tiktok_product_url")
        .eq("is_active", True)
        .in_("trend_phase", ["rising", "hot"])
        .order("virality_score", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def format_products_for_prompt(products):
    lines = []
    for p in products:
        gmv = p.get("gmv_7d") or 0
        gmv_fmt = "$" + (str(round(gmv/1000)) + "K" if gmv >= 1000 else str(round(gmv)))
        lines.append(
            "- " + str(p["product_name"]) +
            " | Category: " + str(p.get("category", "General")) +
            " | Price: $" + str(p.get("tiktok_price", 0)) +
            " | 7-Day GMV: " + gmv_fmt +
            " | Virality: " + str(p.get("virality_score", 0)) + "%" +
            " | Trend: " + str(p.get("trend_phase", "rising")).upper() +
            " | Link: " + str(p.get("tiktok_product_url", ""))
        )
    return "\n".join(lines)


def generate_blog_post(keyword, products):
    products_text = format_products_for_prompt(products)
    int_link = random.choice(INTERNAL_LINKS)

    fb = SOCIAL_LINKS["facebook"]
    li = SOCIAL_LINKS["linkedin"]
    ig = SOCIAL_LINKS["instagram"]
    tt = SOCIAL_LINKS["tiktok"]

    social_section = (
        "## Stay Connected\n"
        "Follow TikProfitPro for daily trending products and TikTok Shop tips:\n"
        "- Facebook: " + fb + "\n"
        "- LinkedIn: " + li + "\n"
        "- Instagram: " + ig + "\n"
        "- TikTok: " + tt + "\n\n"
        "## Read More\n"
        "- [TikTok Shop Product Research Tool](https://tikprofitpro.shop)\n"
        "- [Find Trending TikTok Shop Products](https://tikprofitpro.shop)\n"
        "- [TikTok Shop Profit Calculator](https://tikprofitpro.shop/profit-calculator)"
    )

    prompt = (
        "You are an expert TikTok Shop content writer for USA sellers.\n\n"
        "Write a detailed, SEO-optimized blog post for the keyword: " + keyword + "\n\n"
        "Include these REAL trending products naturally in the content:\n"
        + products_text + "\n\n"
        "REQUIREMENTS:\n"
        "1. Word count: 1000-1200 words\n"
        "2. Mention USA or US sellers at least 3 times\n"
        "3. Include H2 and H3 subheadings\n"
        "4. Bullet list of trending products with prices\n"
        "5. 2 CTAs mentioning TikProfitPro at " + int_link[0] + " (" + int_link[1] + ")\n"
        "6. End with this exact section:\n\n"
        + social_section + "\n\n"
        "7. Clean Markdown format\n"
        "8. Do NOT wrap response in code fences\n\n"
        "Write the full blog post now:"
    )

    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not set in .env")

    for attempt in range(3):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": "Bearer " + GROQ_API_KEY,
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "llama-3.3-70b-versatile",
                    "messages":    [{"role": "user", "content": prompt}],
                    "max_tokens":  2000,
                    "temperature": 0.7,
                },
                timeout=60
            )
            print("  Groq status: " + str(res.status_code))
            if res.status_code == 429:
                wait = 30 * (attempt + 1)
                print("  Rate limited - waiting " + str(wait) + "s...")
                time.sleep(wait)
                continue
            res.raise_for_status()
            data = res.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            print("  Attempt " + str(attempt+1) + " failed: " + str(e))
            if attempt == 2:
                raise Exception("Blog generation failed after 3 retries: " + str(e))
            time.sleep(20)


def fetch_unsplash_image(keyword):
    day = datetime.now(timezone.utc).timetuple().tm_yday
    return FALLBACK_IMAGES[day % len(FALLBACK_IMAGES)]


def ghost_token():
    key_id, secret = GHOST_ADMIN_API_KEY.split(":")
    iat = int(datetime.now(timezone.utc).timestamp())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    return jwt.encode(payload, bytes.fromhex(secret), algorithm="HS256",
                      headers={"alg": "HS256", "typ": "JWT", "kid": key_id})


def publish_to_ghost(title, content_md, feature_image_url=None):
    import json
    token = ghost_token()
    headers = {
        "Authorization": "Ghost " + token,
        "Content-Type": "application/json",
    }
    mobiledoc = json.dumps({
        "version": "0.3.1",
        "markups": [],
        "atoms":   [],
        "cards":   [["markdown", {"markdown": content_md}]],
        "sections": [[10, 0]]
    })
    post = {
        "title":        title,
        "mobiledoc":    mobiledoc,
        "status":       "published",
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    if feature_image_url:
        post["feature_image"] = feature_image_url

    body = {"posts": [post]}
    url  = GHOST_URL + "/ghost/api/admin/posts/"
    res  = requests.post(url, headers=headers, json=body, timeout=30)

    if res.status_code in (200, 201):
        post_data = res.json()["posts"][0]
        return post_data.get("url") or post_data.get("slug")
    else:
        print("Ghost error " + str(res.status_code) + ": " + res.text[:300])
        return None


def ping_indexnow(post_url):
    try:
        payload = {
            "host":    "blog.tikprofitpro.shop",
            "key":     INDEXNOW_KEY,
            "urlList": [post_url],
        }
        res = requests.post("https://api.indexnow.org/indexnow",
                            json=payload, timeout=10)
        print("  IndexNow: " + str(res.status_code))
    except Exception as e:
        print("  IndexNow error: " + str(e))


def already_posted_today():
    import httpx
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def get_token():
        key_id, key_secret = GHOST_ADMIN_API_KEY.split(":")
        iat = int(time.time())
        return jwt.encode(
            {"iat": iat, "exp": iat + 300, "aud": "/admin/"},
            bytes.fromhex(key_secret), algorithm="HS256",
            headers={"kid": key_id}
        )

    try:
        headers = {"Authorization": "Ghost " + get_token()}
        res = httpx.get(
            GHOST_URL + "/ghost/api/admin/posts/?limit=5&fields=published_at",
            headers=headers, timeout=10
        )
        posts = res.json().get("posts", [])
        for p in posts:
            if p.get("published_at", "").startswith(today):
                return True
    except Exception as e:
        print("  already_posted_today check failed: " + str(e))
    return False


def run():
    print("=" * 60)
    print("  TikProfitPro - Daily Blog Autopilot")
    print("  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 60)

    if already_posted_today():
        print("Already posted today - skipping to prevent duplicates.")
        return

    day_index = datetime.now(timezone.utc).timetuple().tm_yday
    keyword   = KEYWORDS[day_index % len(KEYWORDS)]
    print("\nKeyword: " + keyword)

    print("\nFetching trending products from Supabase...")
    products = fetch_trending_products(limit=5)
    if not products:
        print("No trending products found. Aborting.")
        return
    print(str(len(products)) + " products fetched")
    for p in products:
        print("  [" + str(p["virality_score"]) + "%] " + str(p["product_name"])[:50])

    print("\nGenerating blog post with Groq...")
    content = generate_blog_post(keyword, products)
    word_count = len(content.split())
    print("Generated: " + str(word_count) + " words")

    lines = content.strip().split("\n")
    title = keyword.title()
    for line in lines:
        clean = line.replace("# ", "").replace("#", "").strip()
        if clean and len(clean) > 10:
            title = clean
            break

    print("\nFetching image...")
    image_url = fetch_unsplash_image(keyword)
    print("Image ready")

    print("\nPublishing to Ghost...")
    print("  Title: " + title)
    post_url = publish_to_ghost(title, content, image_url)

    if post_url:
        print("Published: " + str(post_url))
        print("\nPinging IndexNow...")
        if not str(post_url).startswith("http"):
            post_url = GHOST_URL + "/" + str(post_url)
        ping_indexnow(post_url)
    else:
        print("Publishing failed")
        return

    print("\n" + "=" * 60)
    print("  AUTOPILOT COMPLETE")
    print("  Keyword:  " + keyword)
    print("  Words:    " + str(word_count))
    print("  URL:      " + str(post_url))
    print("=" * 60)


if __name__ == "__main__":
    run()
