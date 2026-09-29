"""SQLite persistence layer.

There is no ORM on purpose: the accounting state is a small, immutable
document graph. Snapshots are stored as canonical JSON blobs and never
updated (see ``save_snapshot``). Factor rows are versioned; a version may be
seeded/extended while open but is frozen once closed (CLI freeze or future
governance workflow) — the engine only ever reads factor *values captured in
the snapshot*, so even an administration error can never rewrite history.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factor_versions (
    version TEXT PRIMARY KEY,
    description TEXT,
    frozen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factors (
    version TEXT NOT NULL REFERENCES factor_versions(version),
    factor_key TEXT NOT NULL,
    name TEXT,
    kg_co2e_per_unit TEXT NOT NULL,
    reference_unit TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (version, factor_key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    prior_snapshot_id TEXT,
    input_summary TEXT NOT NULL,
    factor_versions TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_products (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    process_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_key TEXT NOT NULL,
    unit_kg_co2e TEXT NOT NULL,
    total_kg_co2e TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, product_key)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_prior ON snapshots(prior_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_products_key ON snapshot_products(product_key);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    new_file = not os.path.exists(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if new_file:
        init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),))


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- factor versions -------------------------------------------------------

def create_factor_version(conn: sqlite3.Connection, version: str,
                          description: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO factor_versions(version, description, frozen, created_at) "
        "VALUES (?, ?, 0, ?)", (version, description, utc_now()))


def get_factor_version(conn: sqlite3.Connection, version: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM factor_versions WHERE version = ?", (version,)
    ).fetchone()


def list_factor_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM factor_versions ORDER BY version").fetchall()


def freeze_factor_version(conn: sqlite3.Connection, version: str) -> None:
    conn.execute("UPDATE factor_versions SET frozen = 1 WHERE version = ?",
                 (version,))


def put_factor(conn: sqlite3.Connection, version: str, key: str, name: Optional[str],
               kg_co2e_per_unit: str, reference_unit: str,
               description: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO factors(version, factor_key, name, kg_co2e_per_unit,
                               reference_unit, description)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(version, factor_key) DO UPDATE SET
               name=excluded.name,
               kg_co2e_per_unit=excluded.kg_co2e_per_unit,
               reference_unit=excluded.reference_unit,
               description=excluded.description
        """, (version, key, name, kg_co2e_per_unit, reference_unit, description))


def get_factor(conn: sqlite3.Connection, version: str,
               key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM factors WHERE version = ? AND factor_key = ?",
        (version, key)).fetchone()


def list_factors(conn: sqlite3.Connection, version: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM factors WHERE version = ? ORDER BY factor_key",
        (version,)).fetchall()


# --- snapshots -------------------------------------------------------------

def get_snapshot_by_hash(conn: sqlite3.Connection,
                         content_hash: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots WHERE content_hash = ?",
        (content_hash,)).fetchone()


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,)).fetchone()


def list_snapshots(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT snapshot_id, content_hash, prior_snapshot_id, input_summary, "
        "factor_versions, created_at FROM snapshots ORDER BY created_at DESC, "
        "snapshot_id DESC LIMIT ?", (limit,)).fetchall()


def save_snapshot(conn: sqlite3.Connection, *, snapshot_id: str,
                  content_hash: str, prior_snapshot_id: Optional[str],
                  input_summary: dict[str, Any], factor_versions: dict[str, str],
                  request: dict[str, Any], result: dict[str, Any]) -> None:
    """Insert a snapshot and its per-product rows atomically.

    Rows are INSERT only; no UPDATE path exists in this module, which is what
    makes snapshots immutable at the storage level.
    """
    with tx(conn):
        conn.execute(
            """INSERT INTO snapshots(snapshot_id, content_hash, prior_snapshot_id,
                                     input_summary, factor_versions,
                                     request_json, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, content_hash, prior_snapshot_id,
             json.dumps(input_summary, sort_keys=True, separators=(",", ":")),
             json.dumps(factor_versions, sort_keys=True,
                        separators=(",", ":")),
             json.dumps(request, sort_keys=True, separators=(",", ":")),
             json.dumps(result, sort_keys=True, separators=(",", ":")),
             utc_now()))
        for proc in result["processes"]:
            for p in proc["products"]:
                conn.execute(
                    """INSERT INTO snapshot_products(snapshot_id, process_id,
                           product_id, product_key, unit_kg_co2e, total_kg_co2e)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, proc["process_id"], p["product_id"],
                     p["product_key"], p["unit_kg_co2e"],
                     p["allocated_kg_co2e"]))


def get_product_row(conn: sqlite3.Connection, snapshot_id: str,
                    product_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshot_products WHERE snapshot_id = ? AND product_key = ?",
        (snapshot_id, product_key)).fetchone()
