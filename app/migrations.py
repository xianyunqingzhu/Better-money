"""Versioned, in-place SQLite schema migrations."""
from __future__ import annotations

from collections.abc import Callable
import sqlite3


CURRENT_SCHEMA_VERSION: int = 2


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    type TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '其他',
    merchant TEXT DEFAULT '',
    note TEXT DEFAULT '',
    source TEXT DEFAULT '手动',
    estimated INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qty REAL DEFAULT 1,
    price REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    saved REAL DEFAULT 0,
    priority INTEGER DEFAULT 100,
    status TEXT DEFAULT '冷静期',
    cooldown_until TEXT DEFAULT '',
    expected_date TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    achieved_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    content TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    expired INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    diff REAL NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS savings_wins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_name TEXT NOT NULL,
    amount REAL NOT NULL,
    date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def database_integrity(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Return whether SQLite reports the database as structurally sound."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    result = row[0] if row else "no result"
    return result == "ok", result


def _require_database_integrity(conn: sqlite3.Connection) -> None:
    is_valid, result = database_integrity(conn)
    if not is_valid:
        raise RuntimeError(f"database integrity check failed: {result}")


def _migrate_to_version_1(conn: sqlite3.Connection) -> None:
    for statement in BASE_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def _migrate_to_version_2(conn: sqlite3.Connection) -> None:
    adjustment_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(adjustments)")
    }
    if "reverses_adjustment_id" not in adjustment_columns:
        conn.execute(
            "ALTER TABLE adjustments ADD COLUMN "
            "reverses_adjustment_id INTEGER REFERENCES adjustments(id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_adjustments_reverses "
        "ON adjustments(reverses_adjustment_id)"
    )
    conn.execute(
        "DELETE FROM summaries WHERE id NOT IN ("
        "SELECT MAX(id) FROM summaries "
        "GROUP BY period_type, period_start, period_end"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_summaries_period_range "
        "ON summaries(period_type, period_start, period_end)"
    )


MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migrate_to_version_1),
    (2, _migrate_to_version_2),
)


def migrate_database(conn: sqlite3.Connection) -> None:
    """Apply each pending schema version atomically without replacing the database."""
    _require_database_integrity(conn)
    with conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, migration in MIGRATIONS:
            if current_version < version:
                migration(conn)
                conn.execute(f"PRAGMA user_version = {version}")
        _require_database_integrity(conn)
