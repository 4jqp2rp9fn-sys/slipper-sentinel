"""Anomaly detection over recent Mercari listings.

Lightweight, rule-based. Each rule appends a reason + a score; the final
"anomaly score" is just the sum. Cheap to read, cheap to extend.
"""
from __future__ import annotations

import math
import statistics
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from . import storage


# Thresholds (tune freely)
UNDERPRICED_Z = -1.5         # z-score below mean to count as underpriced
PRICE_DROP_PCT = 0.20        # 20% drop vs previous observed price
LISTING_SPIKE_RATIO = 2.0    # recent 1h volume vs 24h hourly avg
TITLE_SIMILARITY = 0.75      # for "same model repeated cheaper"
MIN_SAMPLES = 8              # need this many recent prices for stats


@dataclass
class Anomaly:
    item: dict
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0

    def add(self, reason: str, weight: float = 1.0) -> None:
        self.reasons.append(reason)
        self.score += weight


def _title_similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def analyze(items: list[dict], previous_prices: dict[str, int | None]) -> list[Anomaly]:
    """Return anomalies for the freshly-seen batch of items.

    `previous_prices` maps listing id -> the price we had stored before this
    run (None if it's a brand-new listing).
    """
    anomalies: list[Anomaly] = []

    # Group recent history by keyword for baseline stats
    by_keyword_prices: dict[str, list[int]] = {}
    for it in items:
        kw = it.get("keyword", "")
        if kw not in by_keyword_prices:
            by_keyword_prices[kw] = storage.recent_prices(kw)

    # Listing-frequency spike detection (per keyword)
    spike_keywords: set[str] = set()
    for kw in by_keyword_prices:
        last_hour = storage.count_recent_listings(kw, 3600)
        last_day = storage.count_recent_listings(kw, 24 * 3600)
        hourly_avg = last_day / 24 if last_day else 0
        if hourly_avg >= 1 and last_hour >= hourly_avg * LISTING_SPIKE_RATIO:
            spike_keywords.add(kw)

    for item in items:
        title = item.get("title", "").lower()

        required = [
        "slippers",
        "sandals",
        "サンダル",
        "スリッパ",
    ]

        if not any(word.lower() in title for word in required):
            continue    

        blocked = [
            "キッズ",
            "レディース",
            "スカート",
            "パンツ",
            "ハンガー",
            "女の子",
            "ワンピース",
        ]

        if any(word.lower() in title for word in blocked):
            continue

        small_size = re.search(r'([0-1]?[0-9]|2[0-5](?:\.5)?)\s?cm', title)

        if small_size:
            continue

        a = Anomaly(item=item)
        kw = item.get("keyword", "")
        price = item.get("price", 0) or 0
        prices = [p for p in by_keyword_prices.get(kw, []) if p > 0]

        # 1) Underpriced vs recent baseline
        if price > 0 and len(prices) >= MIN_SAMPLES:
            mean = statistics.mean(prices)
            stdev = statistics.pstdev(prices) or 1.0
            z = (price - mean) / stdev
            if z <= UNDERPRICED_Z:
                a.add(
                    f"Underpriced: ¥{price:,} vs avg ¥{int(mean):,} (z={z:.2f})",
                    weight=min(3.0, abs(z)),
                )

        # 2) Price drop on a known listing
        prev = previous_prices.get(item["id"])
        if prev and price and price < prev:
            drop = (prev - price) / prev
            if drop >= PRICE_DROP_PCT:
                a.add(
                    f"Price drop: ¥{prev:,} → ¥{price:,} (-{drop*100:.0f}%)",
                    weight=1.0 + drop,
                )

        # 3) Listing frequency spike
        if kw in spike_keywords:
            a.add(f"Listing spike for '{kw}' in the last hour", weight=0.5)

        # 4) Same/similar title appearing cheaper than peers in this batch
        cheaper_twins = [
            other for other in items
            if other["id"] != item["id"]
            and other.get("price", 0) > price > 0
            and _title_similar(item["title"], other["title"]) >= TITLE_SIMILARITY
        ]
        if cheaper_twins:
            a.add(
                f"Similar listings exist at higher prices ({len(cheaper_twins)} matches)",
                weight=0.75,
            )

        if a.reasons:
            anomalies.append(a)

    # Highest score first
    anomalies.sort(key=lambda x: x.score, reverse=True)
    return anomalies
