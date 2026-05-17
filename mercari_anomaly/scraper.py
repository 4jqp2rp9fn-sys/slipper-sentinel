"""Fetch Mercari Japan listings via headless browser (Playwright).

Mercari's JSON API requires DPoP-signed requests and rejects unauthenticated
clients with 401. Instead we render the public search results page in a real
Chromium browser and parse listing cards from the DOM. This is slower but
robust against API changes and works from GitHub Actions.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable
from urllib.parse import quote

log = logging.getLogger(__name__)

SEARCH_URL = (
    "https://jp.mercari.com/search?keyword={kw}"
    "&status=on_sale&sort=created_time&order=desc"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

EXTRA_HEADERS = {
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

KEYWORDS = [
    "slippers",
    "sandals",
    "クロッグ",
    "サンダル",
    "スリッパ",
]

# Item card selector. Mercari uses <li> wrappers around <a data-testid="thumbnail-link">.
ITEM_SELECTOR = 'a[data-testid="thumbnail-link"]'
ITEM_ID_RE = re.compile(r"/item/(m\d+)")
PRICE_RE = re.compile(r"[\d,]+")


def _parse_price(text: str) -> int:
    if not text:
        return 0
    m = PRICE_RE.search(text.replace("¥", "").replace("￥", ""))
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return 0


def fetch_keyword(page, keyword: str, limit: int = 60) -> list[dict]:
    """Fetch most recent listings for a single keyword using an open Playwright page."""
    url = SEARCH_URL.format(kw=quote(keyword))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector(ITEM_SELECTOR, timeout=15000)
        except Exception:
            log.warning("No item cards rendered for %r", keyword)
            return []
        # Let lazy images settle a moment.
        page.wait_for_timeout(1500)

        anchors = page.query_selector_all(ITEM_SELECTOR)
        now = int(time.time())
        items: list[dict] = []
        for a in anchors[:limit]:
            try:
                href = a.get_attribute("href") or ""
                m = ITEM_ID_RE.search(href)
                if not m:
                    continue
                item_id = m.group(1)

                aria = a.get_attribute("aria-label") or ""
                title = aria.strip()
                img_el = a.query_selector("img")
                image = ""
                if img_el:
                    image = img_el.get_attribute("src") or img_el.get_attribute("data-src") or ""
                    if not title:
                        title = (img_el.get_attribute("alt") or "").strip()

                # Price text usually rendered inside the card.
                price_text = ""
                price_el = a.query_selector('[class*="price"], [data-testid*="price"], .merPrice')
                if price_el:
                    price_text = price_el.inner_text()
                if not price_text:
                    price_text = a.inner_text() or ""
                price = _parse_price(price_text)

                items.append({
                    "id": item_id,
                    "title": title or item_id,
                    "price": price,
                    "url": f"https://jp.mercari.com/item/{item_id}",
                    "image": image,
                    "created_at": now,
                    "keyword": keyword,
                    "fetched_at": now,
                })
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Failed to parse a card: %s", exc)
                continue

        log.info("Fetched %d items for %r", len(items), keyword)
        return items
    except Exception as exc:
        log.warning("Browser fetch failed for %r: %s", keyword, exc)
        return []


def fetch_all(keywords: Iterable[str] = KEYWORDS) -> list[dict]:
    """Open one browser, reuse a single page across all keywords."""
    # Import here so unit tests / non-scrape paths don't require playwright.
    from playwright.sync_api import sync_playwright

    seen: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1366, "height": 900},
            extra_http_headers=EXTRA_HEADERS,
        )
        page = context.new_page()
        try:
            for kw in keywords:
                for item in fetch_keyword(page, kw):
                    seen.setdefault(item["id"], item)
                time.sleep(1.0)  # be polite between keywords
        finally:
            context.close()
            browser.close()
    return list(seen.values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    items = fetch_all()
    print(f"Fetched {len(items)} unique listings")
    for it in items[:3]:
        print(it)
