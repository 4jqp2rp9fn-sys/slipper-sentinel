"""Discord webhook notifier."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def _webhook() -> str | None:
    return os.environ.get(WEBHOOK_ENV)


def notify(anomaly) -> bool:
    url = _webhook()
    if not url:
        log.warning("No %s set, skipping notification", WEBHOOK_ENV)
        return False

    item = anomaly.item
    title = item.get("title", "(no title)")
    price = item.get("price", 0)
    listing_url = item.get("url", "")
    image = item.get("image") or None
    source = (item.get("source") or "market").capitalize()

    # Color: severity wins; otherwise tint by source.
    if anomaly.score >= 2:
        color = 0xE74C3C
    elif source.lower() == "rakuten":
        color = 0xBF0000
    elif source.lower() == "mercari":
        color = 0xFF5722
    else:
        color = 0xF1C40F

    embed = {
        "title": f"[{source}] {title}"[:240],
        "url": listing_url,
        "description": "\n".join(f"• {r}" for r in anomaly.reasons),
        "color": color,
        "fields": [
            {"name": "Source", "value": source, "inline": True},
            {"name": "Price", "value": f"¥{price:,}", "inline": True},
            {"name": "Score", "value": f"{anomaly.score:.2f}", "inline": True},
            {"name": "Keyword", "value": item.get("keyword", "-"), "inline": True},
        ],
        "footer": {"text": "Multi-market anomaly watcher"},
    }
    if image:
        embed["thumbnail"] = {"url": image}

    payload = {"embeds": [embed]}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code >= 300:
            log.warning("Discord webhook returned %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("Discord notify failed: %s", exc)
        return False
