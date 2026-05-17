"""SQLite-backed storage for listing history.

Keeps the schema intentionally tiny so the file stays small and easy to
inspect. We store every listing we've ever seen + the time we first saw it,
and we record each price snapshot so we can detect drops.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "listings.db"


def _ensure_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                title TEXT,
                price INTEGER,
                url TEXT,
                image TEXT,
                keyword TEXT,
                created_at INTEGER,
                first_seen INTEGER,
                last_seen INTEGER,
                last_price INTEGER,
                notified INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT,
                price INTEGER,
                ts INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_listings_keyword ON listings(keyword);
            CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings(first_seen);
            CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id);
            """
        )


def upsert_listing(item: dict) -> tuple[bool, int | None]:
    """Insert/update listing. Returns (is_new, previous_price)."""
    now = int(time.time())
    with connect() as c:
        row = c.execute(
            "SELECT last_price FROM listings WHERE id = ?", (item["id"],)
        ).fetchone()
        if row is None:
            c.execute(
                """INSERT INTO listings
                   (id,title,price,url,image,keyword,created_at,first_seen,last_seen,last_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"],
                    item["title"],
                    item["price"],
                    item["url"],
                    item.get("image", ""),
                    item.get("keyword", ""),
                    item.get("created_at", now),
                    now,
                    now,
                    item["price"],
                ),
            )
            c.execute(
                "INSERT INTO price_history (listing_id, price, ts) VALUES (?,?,?)",
                (item["id"], item["price"], now),
            )
            return True, None

        prev_price = row["last_price"]
        c.execute(
            "UPDATE listings SET last_seen=?, last_price=?, title=? WHERE id=?",
            (now, item["price"], item["title"], item["id"]),
        )
        if prev_price != item["price"]:
            c.execute(
                "INSERT INTO price_history (listing_id, price, ts) VALUES (?,?,?)",
                (item["id"], item["price"], now),
            )
        return False, prev_price


def recent_prices(keyword: str, since_seconds: int = 7 * 24 * 3600) -> list[int]:
    cutoff = int(time.time()) - since_seconds
    with connect() as c:
        rows = c.execute(
            "SELECT price FROM listings WHERE keyword = ? AND first_seen >= ? AND price > 0",
            (keyword, cutoff),
        ).fetchall()
    return [r["price"] for r in rows]


def count_recent_listings(keyword: str, since_seconds: int) -> int:
    cutoff = int(time.time()) - since_seconds
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE keyword = ? AND first_seen >= ?",
            (keyword, cutoff),
        ).fetchone()
    return int(row["n"])


def mark_notified(listing_id: str) -> None:
    with connect() as c:
        c.execute("UPDATE listings SET notified = 1 WHERE id = ?", (listing_id,))


def is_notified(listing_id: str) -> bool:
    with connect() as c:
        row = c.execute(
            "SELECT notified FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
    return bool(row and row["notified"])
