"""Merchant catalog: seed data + live lookups used by the verify step
(so a mandate signed against a stale/cached price gets caught)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.db import cursor

SEED_CATALOG = [
    # sku,            name,                        category,       price_paise, stock, per_item_limit
    ("SKU-BT-001", "Wireless Earbuds Pro",        "electronics", 249900, 40, 3),
    ("SKU-BT-002", "Bluetooth Speaker Mini",       "electronics", 129900, 25, 3),
    ("SKU-KB-001", "Mechanical Keyboard 87-key",   "electronics", 349900, 15, 2),
    ("SKU-BK-001", "Notebook Set (Pack of 3)",     "stationery",   45900, 200, 10),
    ("SKU-BK-002", "Fountain Pen Classic",         "stationery",   89900, 60, 5),
    ("SKU-KT-001", "Ceramic Coffee Mug",           "home",         59900, 100, 6),
    ("SKU-KT-002", "Stainless Steel Water Bottle", "home",         69900, 80, 6),
    ("SKU-SH-001", "Running Shoes — Road",         "apparel",     499900, 30, 2),
    ("SKU-SH-002", "Canvas Sneakers",               "apparel",    259900, 45, 3),
    ("SKU-BG-001", "Laptop Backpack 25L",          "accessories", 189900, 35, 3),
    ("SKU-GC-001", "Gift Card ₹5000",              "gift-cards", 500000, 999, 1),
    ("SKU-GC-002", "Gift Card ₹1000",              "gift-cards", 100000, 999, 5),
]


@dataclass
class CatalogItem:
    sku: str
    name: str
    category: str
    price_paise: int
    stock: int
    per_item_limit: int


def seed_catalog_if_empty() -> None:
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM catalog")
        if cur.fetchone()["n"] > 0:
            return
        cur.executemany(
            "INSERT INTO catalog (sku, name, category, price_paise, stock, per_item_limit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            SEED_CATALOG,
        )


def list_catalog() -> list[CatalogItem]:
    seed_catalog_if_empty()
    with cursor() as cur:
        cur.execute("SELECT * FROM catalog ORDER BY category, name")
        return [CatalogItem(**dict(row)) for row in cur.fetchall()]


def get_item(sku: str) -> Optional[CatalogItem]:
    seed_catalog_if_empty()
    with cursor() as cur:
        cur.execute("SELECT * FROM catalog WHERE sku = ?", (sku,))
        row = cur.fetchone()
        return CatalogItem(**dict(row)) if row else None


def reserve_stock(sku: str, quantity: int) -> bool:
    """
    Atomically decrements stock, but ONLY if enough remains -- the
    WHERE clause and the decrement happen as a single SQL statement,
    so two concurrent calls for the last unit of stock cannot both
    succeed. This is the same style of fix as gateway.reserve_mandate():
    push the race into a single atomic database operation instead of a
    read-then-write in Python, where two readers could both see
    "stock available" before either writes.

    Returns True if reserved, False if insufficient stock remained at
    the moment of the atomic check -- even if an earlier, non-atomic
    policy check (evaluate_policy) had passed on a now-stale read.
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE catalog SET stock = stock - ? WHERE sku = ? AND stock >= ?",
            (quantity, sku, quantity),
        )
        return cur.rowcount == 1


def release_stock(sku: str, quantity: int) -> None:
    """Rolls back a reservation if order creation fails AFTER stock was
    already decremented -- otherwise a failed payment would still
    permanently consume inventory."""
    with cursor() as cur:
        cur.execute("UPDATE catalog SET stock = stock + ? WHERE sku = ?", (quantity, sku))
