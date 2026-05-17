"""Fetch Mercari Japan listings for a set of keywords.

Mercari exposes a public search API used by their web frontend. We hit it
directly with a User-Agent header. No auth required for browsing.
"""
from __future__ import annotations

import time
import uuid
import logging
from typing import Iterable

import requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.mercari.jp/v2/entities:search"
DPOP_FALLBACK = ""  # Mercari now requires a DPoP header; we send a dummy one and rely on web search fallback.

WEB_SEARCH_URL = "https://api.mercari.jp/search_index/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ja,en;q=0.8",
    "X-Platform": "web",
}

KEYWORDS = [
    "slippers",
    "sandals",
    "クロッグ",
    "サンダル",
    "スリッパ",
]


def _normalize(item: dict, keyword: str) -> dict | None:
    """Normalize a raw Mercari item into our minimal schema."""
    try:
        item_id = item.get("id") or item.get("itemId")
        if not item_id:
            return None
        name = item.get("name") or item.get("title") or ""
        price = item.get("price")
        if isinstance(price, str):
            price = int(price.replace(",", "")) if price.isdigit() else None
        thumbnails = item.get("thumbnails") or []
        image = thumbnails[0] if thumbnails else item.get("thumbnail") or ""
        created = item.get("created") or item.get("created_at") or int(time.time())
        return {
            "id": str(item_id),
            "title": name,
            "price": int(price) if price is not None else 0,
            "url": f"https://jp.mercari.com/item/{item_id}",
            "image": image,
            "created_at": int(created),
            "keyword": keyword,
            "fetched_at": int(time.time()),
        }
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to normalize item: %s", exc)
        return None


def fetch_keyword(keyword: str, limit: int = 60) -> list[dict]:
    """Fetch most recent listings for a single keyword."""
    params = {
        "keyword": keyword,
        "limit": limit,
        "sort": "created_time",
        "order": "desc",
        "status": "on_sale",
    }
    try:
        resp = requests.get(WEB_SEARCH_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("Search failed for %r: %s", keyword, exc)
        return []

    raw_items = data.get("data") or data.get("items") or []
    items = []
    for raw in raw_items:
        norm = _normalize(raw, keyword)
        if norm:
            items.append(norm)
    log.info("Fetched %d items for %r", len(items), keyword)
    return items


def fetch_all(keywords: Iterable[str] = KEYWORDS) -> list[dict]:
    seen: dict[str, dict] = {}
    for kw in keywords:
        for item in fetch_keyword(kw):
            seen.setdefault(item["id"], item)
        time.sleep(1.0)  # be polite
    return list(seen.values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    items = fetch_all()
    print(f"Fetched {len(items)} unique listings")
    for it in items[:3]:
        print(it)
