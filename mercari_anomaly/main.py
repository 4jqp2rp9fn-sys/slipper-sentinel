"""Entry point: fetch from all marketplace sources → store → analyze → notify."""
from __future__ import annotations

import logging
import time

from dotenv import load_dotenv
load_dotenv()

from . import scraper as mercari_scraper
from . import rakuten_scraper
from . import storage, analyzer, notifier

log = logging.getLogger(__name__)

# Registry of marketplace sources. Each entry exposes a `fetch_all()`
# function returning items in the shared schema. Add new marketplaces here.
SOURCES = [
    ("mercari", mercari_scraper.fetch_all),
    ("rakuten", rakuten_scraper.fetch_all),
]


def collect_items() -> list[dict]:
    all_items: list[dict] = []
    for name, fetch in SOURCES:
        try:
            items = fetch()
            log.info("source=%s fetched=%d", name, len(items))
            all_items.extend(items)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Source %s failed: %s", name, exc)
    return all_items


def run() -> None:
    storage.init_db()
    items = collect_items()
    if not items:
        log.warning("No items fetched this run.")
        return

    previous_prices: dict[str, int | None] = {}
    fresh_items: list[dict] = []
    for item in items:
        is_new, prev_price = storage.upsert_listing(item)
        previous_prices[item["id"]] = prev_price
        if is_new or (prev_price is not None and prev_price != item["price"]):
            fresh_items.append(item)

    log.info("Analyzing %d fresh/changed items out of %d total", len(fresh_items), len(items))
    anomalies = analyzer.analyze(fresh_items, previous_prices)

    sent = 0
    for a in anomalies:
        if storage.is_notified(a.item["id"]):
            continue
        if notifier.notify(a):
            storage.mark_notified(a.item["id"])
            sent += 1
            time.sleep(0.5)
    log.info("Run done: %d anomalies, %d notifications sent", len(anomalies), sent)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
