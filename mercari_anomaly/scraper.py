"""Fetch Mercari Japan listings via the internal search API (no browser).

Mercari's public ``api.mercari.jp/v2/entities:search`` endpoint accepts
anonymous requests as long as each call carries a freshly-signed DPoP JWT
(RFC 9449). We generate an ephemeral ES256 keypair per process, sign one
DPoP token per request, and parse the JSON response.

Schema returned per item:
    id, title, price, url, image, created_at, keyword, fetched_at
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Iterable

import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization

log = logging.getLogger(__name__)

SEARCH_ENDPOINT = "https://api.mercari.jp/v2/entities:search"
ITEM_URL = "https://jp.mercari.com/item/{id}"
DEBUG_DIR = os.environ.get("MERCARI_DEBUG_DIR", "debug")

KEYWORDS = [
    "balenciaga",
    "バレンシアガ",    
    "vetements",
    "ヴェトモン",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/json; charset=utf-8",
    "Origin": "https://jp.mercari.com",
    "Referer": "https://jp.mercari.com/",
    "X-Platform": "web",
}

# Mercari item status / sort enums used by v2 search.
SEARCH_PAYLOAD_TEMPLATE: dict[str, Any] = {
    "userId": "",
    "pageSize": 60,
    "pageToken": "",
    "searchSessionId": "",  # filled per request
    "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
    "thumbnailTypes": [],
    "searchCondition": {
        "keyword": "",  # filled per keyword
        "excludeKeyword": "",
        "sort": "SORT_CREATED_TIME",
        "order": "ORDER_DESC",
        "status": ["STATUS_ON_SALE"],
        "sizeId": [],
        "categoryId": [],
        "brandId": [],
        "sellerId": [],
        "priceMin": 0,
        "priceMax": 0,
        "itemConditionId": [],
        "shippingPayerId": [],
        "shippingFromArea": [],
        "shippingMethod": [],
        "colorId": [],
        "hasCoupon": False,
        "attributes": [],
        "itemTypes": [],
        "skuIds": [],
    },
    "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
    "serviceFrom": "suruga",
    "withItemBrand": True,
    "withItemSize": False,
    "withItemPromotions": True,
    "withItemSizes": True,
    "withShopname": False,
}


# ---------------------------------------------------------------------------
# DPoP signing
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class DPoPSigner:
    """Ephemeral ES256 keypair + per-request DPoP JWT generator."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        nums = self._key.public_key().public_numbers()
        # JWK fields must be 32-byte big-endian, base64url, no padding.
        x = nums.x.to_bytes(32, "big")
        y = nums.y.to_bytes(32, "big")
        self._jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url(x),
            "y": _b64url(y),
        }

    def sign(self, method: str, url: str) -> str:
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        payload = {
            "iat": int(time.time()),
            "jti": str(uuid.uuid4()),
            "htu": url,
            "htm": method.upper(),
            "uuid": str(uuid.uuid4()),
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode())
        ).encode("ascii")

        der_sig = self._key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        # DER → raw (r||s), 32 bytes each.
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        r, s = decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")

        return signing_input.decode("ascii") + "." + _b64url(raw_sig)


_signer = DPoPSigner()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _dump_debug(keyword: str, reason: str, body: str) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in keyword)[:40]
        path = os.path.join(DEBUG_DIR, f"{safe}_{reason}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        log.warning("Dumped debug payload to %s", path)
    except Exception as exc:  # pragma: no cover
        log.warning("Failed to write debug dump: %s", exc)


def _post_search(payload: dict, *, retries: int = 3) -> tuple[int, str]:
    """POST with DPoP signing + exponential backoff. Returns (status, body)."""
    last_status = 0
    last_body = ""
    for attempt in range(1, retries + 1):
        headers = dict(BASE_HEADERS)
        headers["DPoP"] = _signer.sign("POST", SEARCH_ENDPOINT)
        try:
            resp = requests.post(
                SEARCH_ENDPOINT,
                headers=headers,
                data=json.dumps(payload),
                timeout=20,
            )
            last_status = resp.status_code
            last_body = resp.text
            if resp.status_code == 200:
                return last_status, last_body
            log.warning(
                "Mercari API %s on attempt %d/%d", resp.status_code, attempt, retries
            )
            if resp.status_code in (400, 401, 403):
                # Briefer backoff for auth-ish errors; longer for 429/5xx.
                time.sleep(1.5 * attempt)
            else:
                time.sleep(2.0 * attempt)
        except requests.RequestException as exc:
            log.warning("Request error on attempt %d/%d: %s", attempt, retries, exc)
            time.sleep(2.0 * attempt)
    return last_status, last_body


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_items(data: dict, keyword: str) -> list[dict]:
    items_raw = data.get("items") or data.get("data") or []
    now = int(time.time())
    out: list[dict] = []
    for it in items_raw:
        item_id = it.get("id") or it.get("itemId") or ""
        if not item_id:
            continue
        try:
            price = int(it.get("price") or 0)
        except (TypeError, ValueError):
            price = 0

        thumbs = it.get("thumbnails") or []
        image = ""
        if thumbs and isinstance(thumbs, list):
            image = thumbs[0] if isinstance(thumbs[0], str) else thumbs[0].get("url", "")
        if not image:
            image = it.get("thumbnail") or ""

        created = it.get("created") or it.get("createdAt") or now
        try:
            created = int(created)
        except (TypeError, ValueError):
            created = now

        out.append({
            "id": f"mercari:{item_id}",
            "source": "mercari",
            "title": (it.get("name") or it.get("title") or "").strip() or item_id,
            "price": price,
            "url": ITEM_URL.format(id=item_id),
            "image": image,
            "created_at": created,
            "keyword": keyword,
            "fetched_at": now,
        })
    return out


def fetch_keyword(keyword: str, limit: int = 60) -> list[dict]:
    payload = json.loads(json.dumps(SEARCH_PAYLOAD_TEMPLATE))  # deep copy
    payload["pageSize"] = limit
    payload["searchSessionId"] = uuid.uuid4().hex
    payload["searchCondition"]["keyword"] = keyword

    status, body = _post_search(payload)
    if status != 200:
        log.warning("keyword=%r status=%d body[:200]=%r", keyword, status, body[:200])
        if body:
            _dump_debug(keyword, f"http_{status}", body)
        return []

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("keyword=%r non-JSON response", keyword)
        _dump_debug(keyword, "non_json", body)
        return []

    items = _parse_items(data, keyword)
    log.info("keyword=%r status=%d parsed=%d", keyword, status, len(items))
    if not items:
        _dump_debug(keyword, "empty_parse", body[:20000])
    return items


def fetch_all(keywords: Iterable[str] = KEYWORDS) -> list[dict]:
    """Fetch each keyword sequentially and deduplicate by item id."""
    seen: dict[str, dict] = {}
    for kw in keywords:
        for item in fetch_keyword(kw):
            seen.setdefault(item["id"], item)
        time.sleep(1.0)  # polite spacing between keywords
    log.info("Total unique listings: %d", len(seen))
    return list(seen.values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    items = fetch_all()
    print(f"Fetched {len(items)} unique listings")
    for it in items[:3]:
        print(it)
