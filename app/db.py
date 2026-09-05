"""
Data layer for the Merchant Agent Gateway.

Deliberately stdlib-only (sqlite3) so the core system has zero external
dependencies and can run/test anywhere Python runs — the FastAPI layer on
top is an adapter, not a load-bearing dependency for correctness.

Schema notes:
- `mandates.mandate_id` has a UNIQUE constraint. That constraint IS the
  atomic-reserve mechanism: two concurrent submissions of the same
  mandate race to INSERT a row, SQLite's own locking guarantees only one
  write wins, and the loser gets an IntegrityError we translate into a
  clean "already claimed" rejection. See gateway.reserve_mandate().
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gateway.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    secret          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalog (
    sku             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    price_paise     INTEGER NOT NULL,
    stock           INTEGER NOT NULL,
    per_item_limit  INTEGER NOT NULL DEFAULT 5
);

-- The atomic reserve table. UNIQUE(mandate_id) is what closes the race
-- condition: only one concurrent writer can ever successfully INSERT a
-- given mandate_id.
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id      TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    sku             TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    claimed_price_paise INTEGER NOT NULL,
    spend_limit_paise INTEGER NOT NULL,
    expires_at      TEXT NOT NULL,
    signature       TEXT NOT NULL,
    reserved_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mandate_id          TEXT NOT NULL,
    agent_id            TEXT,
    sku                 TEXT,
    cart_total_paise    INTEGER,
    verification_result TEXT NOT NULL,   -- pass / fail
    verification_reason TEXT,
    policy_result       TEXT NOT NULL,   -- pass / fail / skipped
    policy_reason       TEXT,
    razorpay_order_id   TEXT,
    razorpay_mode       TEXT,            -- 'live_test_mode' / 'mock' / NULL if no order was created
    final_status        TEXT NOT NULL,   -- accepted / rejected
    timestamp           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """One connection per thread, so concurrent requests/threads don't
    trip over a shared cursor. check_same_thread=False + WAL-ish timeout
    lets sqlite serialize writers instead of erroring immediately."""
    path = str(db_path or DB_PATH)
    key = f"conn::{path}"
    conn = getattr(_local, key, None)
    if conn is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.executescript(_SCHEMA)
        conn.commit()
        setattr(_local, key, conn)
    return conn


@contextmanager
def cursor(db_path: Path | str | None = None):
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reset_db(db_path: Path | str | None = None) -> None:
    """Test helper: wipe all tables. Never called from app code."""
    with cursor(db_path) as cur:
        for table in ("agents", "catalog", "mandates", "audit_log"):
            cur.execute(f"DELETE FROM {table}")


def use_isolated_test_db(path: Path | str) -> None:
    """Test helper: point the module-level DB_PATH at an isolated file
    and drop any cached per-thread connections so the next get_connection()
    call opens a fresh one against it. Only ever called from tests."""
    global DB_PATH
    DB_PATH = Path(path)
    for attr in list(vars(_local).keys()):
        delattr(_local, attr)
