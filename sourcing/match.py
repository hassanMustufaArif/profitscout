"""
Deterministic photo matching. No LLM.

score = similarity between the TikTok photo and each supplier photo, from:
  1. CLIP image embeddings (open_clip ViT-B-32), cosine similarity  → robust to angle/background
  2. perceptual hashes (pHash + dHash)                               → catches identical listing photos
Final = max(clip, hash) so a re-used factory photo (very common) is always caught.

Then business rules: drop welcome-deal prices, drop prices that leave no margin, drop duplicates,
rank by similarity first, then landed cost, then supplier trust.
"""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image

try:
    import imagehash
except ImportError:  # pragma: no cover
    imagehash = None


@lru_cache(maxsize=1)
def _clip():
    try:
        import open_clip
        import torch

        model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
        model.eval()
        return model, preprocess, torch
    except Exception as e:
        print(f"CLIP unavailable, using perceptual hash only: {e}")
        return None


def _img(b: bytes) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(b)).convert("RGB")
    except Exception:
        return None


def _hash_sim(a: Image.Image, b: Image.Image) -> float:
    if not imagehash:
        return 0.0
    ph = 1 - (imagehash.phash(a) - imagehash.phash(b)) / 64
    dh = 1 - (imagehash.dhash(a) - imagehash.dhash(b)) / 64
    return max(0.0, (ph + dh) / 2)


def similarities(reference: bytes, candidates: list[bytes | None]) -> list[float]:
    ref = _img(reference)
    if ref is None:
        return [0.0] * len(candidates)
    imgs = [_img(c) if c else None for c in candidates]
    hash_scores = [_hash_sim(ref, im) if im else 0.0 for im in imgs]

    clip = _clip()
    clip_scores = [0.0] * len(imgs)
    if clip:
        model, preprocess, torch = clip
        valid = [i for i, im in enumerate(imgs) if im is not None]
        if valid:
            with torch.no_grad():
                batch = torch.stack([preprocess(ref)] + [preprocess(imgs[i]) for i in valid])
                emb = model.encode_image(batch)
                emb = emb / emb.norm(dim=-1, keepdim=True)
                sims = (emb[1:] @ emb[0]).tolist()
            # CLIP cosine for "same product" usually sits 0.85-0.97; unrelated items 0.4-0.75.
            for i, s in zip(valid, sims):
                clip_scores[i] = max(0.0, min(1.0, (s - 0.55) / 0.40))
    return [round(max(h, c), 3) for h, c in zip(hash_scores, clip_scores)]


def top_raw(offers: list[dict], tiktok_price: float, keep: int) -> list[dict]:
    """No photo check: keep the platform's own order (its image search ranks by look-alike),
    drop welcome-deal prices, prices with no margin left, and duplicate listings."""
    seen, out = set(), []
    for o in offers:
        if o.get("welcome_deal") or not o.get("price_min"):
            continue
        if tiktok_price and o["price_min"] >= tiktok_price * 0.7:
            continue
        key = ("id", str(o["product_id"])) if o.get("product_id") else \
              ("name", (o.get("supplier_name") or "").strip().lower(), round(float(o["price_min"] or 0), 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(o, similarity=None))
        if len(out) >= keep:
            break
    return out


def rank(offers: list[dict], scores: list[float], tiktok_price: float, min_sim: float, keep: int,
         max_sim: float | None = None) -> list[dict]:
    """Candidates with min_sim <= similarity (< max_sim when given), usable price, best first, deduped."""
    rows = []
    for o, s in zip(offers, scores):
        o = dict(o, similarity=s)
        if s < min_sim or (max_sim is not None and s >= max_sim) or o.get("welcome_deal"):
            continue
        if tiktok_price and o["price_min"] >= tiktok_price * 0.7:   # no realistic margin left
            continue
        rows.append(o)
    rows.sort(key=lambda o: (-round(o["similarity"], 2), o["price_min"], -(o.get("supplier_years") or 0)))
    # Same listing (or same supplier at the same price) often comes back twice: keep the best one.
    seen, out = set(), []
    for o in rows:
        key = ("id", str(o["product_id"])) if o.get("product_id") else \
              ("name", (o.get("supplier_name") or "").strip().lower(), round(float(o["price_min"] or 0), 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out[:keep]
