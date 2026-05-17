"""Fetch Mercari Japan listings via headless browser (Playwright).

Mercari's JSON API requires DPoP-signed requests and rejects unauthenticated
clients with 401. Instead we render the public search results page in a real
Chromium browser and parse listing cards from the DOM.

The DOM is rendered client-side and Mercari changes class names frequently, so
we rely on structural selectors (anchors pointing at ``/item/m\\d+``) instead
of fragile class names. When no cards are found we dump a screenshot and the
raw HTML to ``debug/`` for inspection.
"""
from __future__ import annotations

import logging
import os
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

DEBUG_DIR = os.environ.get("MERCARI_DEBUG_DIR", "debug")

# Structural selector: any anchor pointing at an item page. This is the most
# stable contract Mercari exposes (the URL scheme has been stable for years).
ITEM_ANCHOR_SELECTOR = 'a[href*="/item/m"]'
ITEM_ID_RE = re.compile(r"/item/(m\d+)")
PRICE_RE = re.compile(r"[\d,]{2,}")


def _parse_price(text: str) -> int:
    if not text:
        return 0
    cleaned = text.replace("¥", "").replace("￥", "").replace("円", "")
    m = PRICE_RE.search(cleaned)
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return 0


def _dump_debug(page, keyword: str, reason: str) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_kw = re.sub(r"[^A-Za-z0-9_-]", "_", keyword)[:40]
        prefix = os.path.join(DEBUG_DIR, f"{safe_kw}_{reason}")
        page.screenshot(path=f"{prefix}.png", full_page=True)
        with open(f"{prefix}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log.warning("Wrote debug artifacts to %s.{png,html}", prefix)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to write debug artifacts: %s", exc)


def _autoscroll(page, steps: int = 4, delay_ms: int = 600) -> None:
    """Trigger lazy-loaded cards by scrolling down a few viewports."""
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(delay_ms)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(200)


def _extract_items(page, keyword: str, limit: int) -> list[dict]:
    """Pull listing data out of the page via a single JS evaluation.

    We resolve each ``a[href*="/item/m"]`` to its enclosing card-ish ancestor
    (``li``, ``article``, or a wrapping ``div``) and read the title/price/image
    from inside that subtree. This is resilient to class-name churn.
    """
    raw = page.evaluate(
        """
        (limit) => {
          const anchors = Array.from(document.querySelectorAll('a[href*="/item/m"]'));
          const seen = new Set();
          const out = [];
          for (const a of anchors) {
            const m = a.getAttribute('href')?.match(/\\/item\\/(m\\d+)/);
            if (!m) continue;
            const id = m[1];
            if (seen.has(id)) continue;
            seen.add(id);

            // Walk up to a reasonable card container.
            let card = a;
            for (let i = 0; i < 5 && card.parentElement; i++) {
              card = card.parentElement;
              if (card.tagName === 'LI' || card.tagName === 'ARTICLE') break;
            }

            const img = card.querySelector('img');
            const title =
              a.getAttribute('aria-label') ||
              img?.getAttribute('alt') ||
              card.querySelector('[itemprop="name"]')?.textContent ||
              '';

            // Price: look for a node whose text starts with ¥ or contains digits+円.
            let priceText = '';
            const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
            let n;
            while ((n = walker.nextNode())) {
              const t = (n.nodeValue || '').trim();
              if (!t) continue;
              if (/[¥￥]\\s*[\\d,]{2,}/.test(t) || /[\\d,]{2,}\\s*円/.test(t)) {
                priceText = t;
                break;
              }
            }

            out.push({
              id,
              title: title.trim(),
              price_text: priceText,
              image: img?.getAttribute('src') || img?.getAttribute('data-src') || '',
            });
            if (out.length >= limit) break;
          }
          return out;
        }
        """,
        limit,
    )

    now = int(time.time())
    items: list[dict] = []
    for r in raw:
        items.append({
            "id": r["id"],
            "title": r["title"] or r["id"],
            "price": _parse_price(r.get("price_text", "")),
            "url": f"https://jp.mercari.com/item/{r['id']}",
            "image": r.get("image", ""),
            "created_at": now,
            "keyword": keyword,
            "fetched_at": now,
        })
    return items


def fetch_keyword(page, keyword: str, limit: int = 60) -> list[dict]:
    """Fetch most recent listings for a single keyword using an open Playwright page."""
    url = SEARCH_URL.format(kw=quote(keyword))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Wait for client-rendered item anchors. Fall back to networkidle if the
        # selector never appears (e.g. Mercari shows an empty-state).
        try:
            page.wait_for_selector(ITEM_ANCHOR_SELECTOR, timeout=20000)
        except Exception:
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

        _autoscroll(page)

        anchor_count = page.evaluate(
            f"document.querySelectorAll('{ITEM_ANCHOR_SELECTOR}').length"
        )
        log.info(
            "keyword=%r title=%r url=%s anchors=%d",
            keyword, page.title(), page.url, anchor_count,
        )

        items = _extract_items(page, keyword, limit)
        log.info("Parsed %d items for %r", len(items), keyword)

        if not items:
            _dump_debug(page, keyword, "no_items")
        return items
    except Exception as exc:
        log.warning("Browser fetch failed for %r: %s", keyword, exc)
        try:
            _dump_debug(page, keyword, "exception")
        except Exception:
            pass
        return []


def fetch_all(keywords: Iterable[str] = KEYWORDS) -> list[dict]:
    """Open one browser, reuse a single page across all keywords."""
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    items = fetch_all()
    print(f"Fetched {len(items)} unique listings")
    for it in items[:3]:
        print(it)
