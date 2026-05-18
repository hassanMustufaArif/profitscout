"""
TikProfitPro — Daily Blog Autopilot
====================================
Runs daily via Railway Cron.
Fetches trending products from Supabase,
writes a blog post with GPT-4o,
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

# ── Credentials ────────────────────────────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
GHOST_ADMIN_API_KEY = os.getenv("GHOST_ADMIN_API_KEY")
GHOST_URL           = os.getenv("GHOST_URL", "https://blog.tikprofitpro.shop")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
INDEXNOW_KEY        = os.getenv("INDEXNOW_KEY", "tikprofitpro2026xK9mN3")

# ── Rotating keyword pool ──────────────────────────────────────────────────────
KEYWORDS = [
    "best TikTok Shop products this week USA",
    "trending TikTok Shop products 2026",
    "TikTok Shop product research tool USA",
    "how to find winning products on TikTok Shop",
    "TikTok Shop dropshipping products USA 2026",
    "fastmoss alternative free",
    "kalodata alternative USA",
    "TikTok Shop best selling products",
    "viral TikTok products to sell 2026",
    "TikTok Shop for beginners USA guide",
    "how to find trending products on TikTok",
    "TikTok Shop seller tips USA",
    "best niches TikTok Shop 2026",
    "TikTok Shop beauty products trending",
    "TikTok Shop health products USA",
    "how to research TikTok Shop products",
    "TikTok Shop vs Amazon which is better 2026",
    "most profitable TikTok Shop products",
    "TikTok Shop product research for beginners",
    "TikTok Shop winning product criteria",
]

# ── Internal links pool ────────────────────────────────────────────────────────
INTERNAL_LINKS = [
    ("TikTok Shop product research tool", "https://tikprofitpro.shop"),
    ("find trending TikTok Shop products", "https://tikprofitpro.shop"),
    ("TikTok Shop profit calculator",      "https://tikprofitpro.shop/profit-calculator"),
    ("TikTok Shop supplier finder",        "https://tikprofitpro.shop"),
]





# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Fetch Trending Products from Supabase
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Generate Blog Post with GPT-4o
# ══════════════════════════════════════════════════════════════════════════════

def format_products_for_prompt(products):
    lines = []
    for p in products:
        gmv = p.get("gmv_7d") or 0
        gmv_fmt = f"${gmv/1000:.0f}K" if gmv >= 1000 else f"${gmv:.0f}"
        lines.append(
            f"- {p['product_name']} | Category: {p.get('category','General')} | "
            f"Price: ${p.get('tiktok_price',0)} | 7-Day GMV: {gmv_fmt} | "
            f"Virality: {p.get('virality_score',0)}% | Trend: {p.get('trend_phase','rising').upper()} | "
            f"Link: {p.get('tiktok_product_url','')}"
        )
    return "\n".join(lines)


def generate_blog_post(keyword, products):
    products_text = format_products_for_prompt(products)
    int_link = random.choice(INTERNAL_LINKS)

    prompt = f"""You are an expert TikTok Shop content writer for USA sellers.

Write a detailed, SEO-optimized blog post for the keyword: "{keyword}"

Include these REAL trending products naturally in the content:
{products_text}

REQUIREMENTS:
1. Word count: 1000-1200 words
2. Mention USA or US sellers at least 3 times
3. Include H2 and H3 subheadings
4. Bullet list of trending products with prices
5. 2 CTAs mentioning TikProfitPro at [{int_link[0]}]({int_link[1]})
6. End with Read More section linking to https://tikprofitpro.shop
7. Clean Markdown format
8. Do NOT wrap in ```markdown``` fences

Write the full blog post now:"""

    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not set in .env")

    for attempt in range(3):
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
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
            print(f"  🔍 Groq status: {res.status_code}")
            if res.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  ⏳ Rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            res.raise_for_status()
            data = res.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  ❌ Attempt {attempt+1} failed: {e}")
            if attempt == 2:
                raise Exception(f"Blog generation failed: {e}")
            time.sleep(20)

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Fetch Unsplash Image
# ══════════════════════════════════════════════════════════════════════════════

def fetch_unsplash_image(keyword):
    query = keyword.replace("USA","").replace("2026","").strip()
    url = f"https://api.unsplash.com/search/photos?query={query}&per_page=5&orientation=landscape"
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        print(f"  Unsplash status: {res.status_code}")  # ← add this
        results = res.json().get("results", [])
        if results:
            photo = random.choice(results[:3])
            return photo["urls"]["regular"]
        else:
            print(f"  Unsplash response: {res.text[:200]}")  # ← add this
    except Exception as e:
        print(f"⚠️  Unsplash error: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Publish to Ghost
# ══════════════════════════════════════════════════════════════════════════════

def ghost_token():
    key_id, secret = GHOST_ADMIN_API_KEY.split(":")
    iat = int(datetime.now(timezone.utc).timestamp())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    return jwt.encode(payload, bytes.fromhex(secret), algorithm="HS256",
                      headers={"alg": "HS256", "typ": "JWT", "kid": key_id})


def publish_to_ghost(title, content_md, feature_image_url=None):
    token = ghost_token()
    headers = {
        "Authorization": f"Ghost {token}",
        "Content-Type": "application/json",
    }

    # Wrap markdown content in a Mobiledoc card — Ghost handles rendering
    import json
    mobiledoc = json.dumps({
        "version": "0.3.1",
        "markups": [],
        "atoms": [],
        "cards": [["markdown", {"markdown": content_md}]],
        "sections": [[10, 0]]
    })

    post = {
        "title":          title,
        "mobiledoc":      mobiledoc,
        "status":         "published",
        "published_at":   datetime.now(timezone.utc).isoformat(),
    }
    if feature_image_url:
        post["feature_image"] = feature_image_url

    body = {"posts": [post]}
    url  = f"{GHOST_URL}/ghost/api/admin/posts/"
    res  = requests.post(url, headers=headers, json=body, timeout=30)

    if res.status_code in (200, 201):
        post_data = res.json()["posts"][0]
        return post_data.get("url") or post_data.get("slug")
    else:
        print(f"❌ Ghost error {res.status_code}: {res.text[:300]}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Ping IndexNow
# ══════════════════════════════════════════════════════════════════════════════

def ping_indexnow(post_url):
    try:
        payload = {
            "host":    "blog.tikprofitpro.shop",
            "key":     INDEXNOW_KEY,
            "urlList": [post_url],
        }
        res = requests.post("https://api.indexnow.org/indexnow",
                            json=payload, timeout=10)
        print(f"  IndexNow: {res.status_code}")
    except Exception as e:
        print(f"  ⚠️  IndexNow error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AUTOPILOT FUNCTION
# ══════════════════════════════════════════════════════════════════════════════
def already_posted_today():
    """Check if autopilot already ran today"""
    import httpx, time
    
    def get_token():
        key_id, key_secret = GHOST_ADMIN_API_KEY.split(":")
        iat = int(time.time())
        return jwt.encode(
            {"iat": iat, "exp": iat + 300, "aud": "/admin/"},
            bytes.fromhex(key_secret), algorithm="HS256",
            headers={"kid": key_id}
        )
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {"Authorization": f"Ghost {get_token()}"}
    try:
        res = httpx.get(
            f"{GHOST_URL}/ghost/api/admin/posts/?limit=5&fields=published_at",
            headers=headers, timeout=10
        )
        posts = res.json().get("posts", [])
        for p in posts:
            if p.get("published_at", "").startswith(today):
                return True
    except:
        pass
    return False

def run():
    print("=" * 60)
    print(f"  TikProfitPro — Daily Blog Autopilot")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # ── Guard: don't post twice in one day ──────────────────────
    if already_posted_today():
        print("⏭️  Already posted today — skipping to prevent duplicates.")
        return
    

    print("=" * 60)
    print(f"  TikProfitPro — Daily Blog Autopilot")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # ── Pick keyword (rotate by day of year) ──────────────────────────────────
    day_index = datetime.now(timezone.utc).timetuple().tm_yday
    keyword   = KEYWORDS[day_index % len(KEYWORDS)]
    print(f"\n📝 Keyword: {keyword}")

    # ── Fetch trending products ───────────────────────────────────────────────
    print(f"\n📦 Fetching trending products from Supabase...")
    products = fetch_trending_products(limit=5)
    if not products:
        print("❌ No trending products found. Aborting.")
        return
    print(f"✅ {len(products)} products fetched")
    for p in products:
        print(f"   [{p['virality_score']}%] {p['product_name'][:50]}")

    # ── Generate blog post ────────────────────────────────────────────────────
    print(f"\n✍️  Generating blog post with Claude...")
    content = generate_blog_post(keyword, products)
    word_count = len(content.split())
    print(f"✅ Generated: {word_count} words")

    # Extract title from first line — strip markdown fences
    lines = content.strip().split("\n")
    # Skip markdown code fences and empty lines
    for line in lines:
        clean = line.replace("```markdown", "").replace("```", "").replace("# ", "").replace("#", "").strip()
        if clean and len(clean) > 10:
            title = clean
            break
    else:
        title = keyword.title()
    
    # Also strip the markdown fence from content
    content = content.strip()
    if content.startswith("```markdown"):
        content = content[len("```markdown"):].strip()
    if content.endswith("```"):
        content = content[:-3].strip()

    # ── Fetch image ───────────────────────────────────────────────────────────
    # ── Fetch image ───────────────────────
    print(f"\n🖼️  Fetching image from Unsplash...")
    image_url = fetch_unsplash_image(keyword)
    
    # Fallback images if Unsplash fails
    FALLBACK_IMAGES = [
        "https://images.unsplash.com/photo-1611162616475-46b635cb6868?w=1200",
        "https://images.unsplash.com/photo-1556742049-0cfed4f6a45d?w=1200",
        "https://images.unsplash.com/photo-1563013544-824ae1b704d3?w=1200",
        "https://images.unsplash.com/photo-1607082348824-0a96f2a4b9da?w=1200",
        "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=1200",
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
    if not image_url:
        image_url = random.choice(FALLBACK_IMAGES)
        print(f"⚠️  Using fallback image")
    else:
        print(f"✅ Image found")

    # ── Publish to Ghost ──────────────────────────────────────────────────────
    print(f"\n🚀 Publishing to Ghost...")
    print(f"   Title: {title}")
    post_url = publish_to_ghost(title, content, image_url)

    if post_url:
        print(f"✅ Published: {post_url}")
        # ── Ping IndexNow ─────────────────────────────────────────────────────
        print(f"\n📡 Pinging IndexNow...")
        if not post_url.startswith("http"):
            post_url = f"{GHOST_URL}/{post_url}"
        ping_indexnow(post_url)
    else:
        print("❌ Publishing failed")
        return

    print(f"\n{'='*60}")
    print(f"  ✅ AUTOPILOT COMPLETE")
    print(f"  Keyword:  {keyword}")
    print(f"  Words:    {word_count}")
    print(f"  URL:      {post_url}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()

    # Run once — add this to autopilot.py temporarily
# then run: python autopilot.py --batch

LOST_KEYWORDS = [
    "trending TikTok Shop products 2026",
    "TikTok Shop winning products",
    "TikTok Shop for beginners USA guide",
    "TikTok Shop product research tool USA",
    "best TikTok Shop products this week USA",
    "how to find winning products on TikTok Shop",
    "TikTok Shop dropshipping products USA 2026",
    "TikTok Shop best selling products",
]


