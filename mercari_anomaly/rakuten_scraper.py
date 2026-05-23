"""Rakuten Ichiba (楽天市場) search scraper.

Fetches search results via plain HTTP + BeautifulSoup and normalizes
listings into the shared schema used across this project:

    id, source, title, price, url, image, created_at, keyword, fetched_at

`id` is a stable identifier derived from the Rakuten item URL
(``<shop>:<itemcode>``) so the same listing across runs maps to the same
row in storage.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SOURCE = "rakuten"
BASE_SEARCH = "https://search.rakuten.co.jp/search/mall/{kw}/"
ITEM_URL_RE = re.compile(r"item\.rakuten\.co\.jp/([^/]+)/([^/?#]+)")

KEYWORDS = [
    "balenciaga",
    "バレンシアガ",
    "vetements",
    "ヴェトモン",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CARD_SELECTORS = [
    "div.searchresultitem",
    "div[class*='searchresultitem']",
]
TITLE_SELECTOR = "h2 a, .title a, a.title-link, a[href*='item.rakuten.co.jp']"
PRICE_SELECTOR = "span.important, .price--OX_YW, [class*='price']"


def _parse_price(text: str) -> int:
    if not text:
        return 0
    digits = re.sub(r"[^\d]", "", text.split("円")[0])
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _item_id(url: str) -> str:
    m = ITEM_URL_RE.search(url)
    if not m:
        return urlparse(url).path.strip("/").replace("/", ":")
    return f"{m.group(1)}:{m.group(2)}"


def _extract_image(card) -> str:
    img = card.select_one("img")
    if not img:
        return ""
    return img.get("src") or img.get("data-src") or ""


def _parse(html: str, keyword: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list = []
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            break
    if not cards:
        log.warning("Rakuten: no cards matched for %r", keyword)
        return []

    now = int(time.time())
    out: list[dict] = []
    seen_ids: set[str] = set()
    for card in cards:
        a = card.select_one(TITLE_SELECTOR)
        if not a:
            continue
        url = (a.get("href") or "").split("?")[0]
        if "item.rakuten.co.jp" not in url:
            continue
        title = a.get_text(strip=True)
        price = _parse_price(
            (card.select_one(PRICE_SELECTOR) or a).get_text(" ", strip=True)
        )
        item_id = _item_id(url)
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        out.append({
            "id": f"{SOURCE}:{item_id}",
            "source": SOURCE,
            "title": title or item_id,
            "price": price,
            "url": url,
            "image": _extract_image(card),
            "created_at": now,
            "keyword": keyword,
            "fetched_at": now,
        })
    return out


def fetch_keyword(keyword: str, *, retries: int = 3) -> list[dict]:
    url = BASE_SEARCH.format(kw=quote(keyword))
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                items = _parse(resp.text, keyword)
                log.info("rakuten keyword=%r parsed=%d", keyword, len(items))
                return items
            log.warning(
                "Rakuten HTTP %s for %r (attempt %d/%d)",
                resp.status_code, keyword, attempt, retries,
            )
        except requests.RequestException as exc:
            log.warning("Rakuten request error for %r: %s", keyword, exc)
        time.sleep(1.5 * attempt)
    return []


def fetch_all(keywords: Iterable[str] = KEYWORDS) -> list[dict]:
    seen: dict[str, dict] = {}
    for kw in keywords:
        for item in fetch_keyword(kw):
            seen.setdefault(item["id"], item)
        time.sleep(1.0)
    log.info("Rakuten total unique listings: %d", len(seen))
    return list(seen.values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for it in fetch_all()[:3]:
        print(it)
