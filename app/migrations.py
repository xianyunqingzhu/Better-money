"""Versioned, in-place SQLite schema migrations."""
from __future__ import annotations

from collections.abc import Callable
import sqlite3
import uuid as _uuid


CURRENT_SCHEMA_VERSION: int = 3


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
    updated_at TEXT NOT NULL,
    uuid TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qty REAL DEFAULT 1,
    price REAL DEFAULT 0,
    uuid TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
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
    achieved_at TEXT DEFAULT '',
    uuid TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT NOT NULL DEFAULT ''
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
    created_at TEXT NOT NULL,
    uuid TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pending_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'desktop',
    created_at TEXT NOT NULL DEFAULT '',
    last_export_at TEXT NOT NULL DEFAULT '',
    last_import_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL DEFAULT '',
    source_device TEXT NOT NULL DEFAULT '',
    exported_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'import',
    result TEXT NOT NULL DEFAULT '',
    conflict_dates TEXT NOT NULL DEFAULT '',
    decisions TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    synced INTEGER NOT NULL DEFAULT 0
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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# 同步元数据表（v3 迁移专用；不含任何用户数据表，避免重建被删表）
SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'desktop',
    created_at TEXT NOT NULL DEFAULT '',
    last_export_at TEXT NOT NULL DEFAULT '',
    last_import_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL DEFAULT '',
    source_device TEXT NOT NULL DEFAULT '',
    exported_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'import',
    result TEXT NOT NULL DEFAULT '',
    conflict_dates TEXT NOT NULL DEFAULT '',
    decisions TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    synced INTEGER NOT NULL DEFAULT 0
);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_to_version_3(conn: sqlite3.Connection) -> None:
    """共享同步层：设备无关 UUID、软删除、最后同步戳与同步元数据表。

    - uuid 用 Python 生成的 32 位 hex 回填，保证迁移结果跨设备唯一。
    - device_id 留空由应用启动时的设备身份登记（app.sync）回填；
      迁移发生在配置加载前，此刻还不知道本机 device_id。
    - last_synced_at 留空表示「从未同步」，首次导出/导入后写入。
    - 注意：这里只创建同步元数据表与索引，绝不重建用户数据表——
      被篡改的旧库缺少表时必须保持缺失，让规范 schema 校验拒绝它。
    """
    # 只执行同步层 DDL（同步表 + 索引）；BASE_SCHEMA 里的用户表语句不能
    # 在此重放，否则会重建被删除的表，破坏完整性守卫。
    for statement in SYNC_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)

    _add_column_if_missing(conn, "transactions", "uuid", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "transactions", "device_id", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "transactions", "deleted_at", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "transactions", "last_synced_at", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "line_items", "uuid", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "line_items", "updated_at", "TEXT NOT NULL DEFAULT ''")
    for column in (
        "uuid", "device_id", "updated_at", "deleted_at", "last_synced_at",
    ):
        _add_column_if_missing(conn, "goals", column, "TEXT NOT NULL DEFAULT ''")
    for column in (
        "uuid", "device_id", "updated_at", "deleted_at", "last_synced_at",
    ):
        _add_column_if_missing(conn, "savings_wins", column, "TEXT NOT NULL DEFAULT ''")

    # 稳定 UUID 回填（旧数据）
    for table in ("transactions", "goals", "line_items", "savings_wins"):
        rows = conn.execute(f"SELECT id FROM {table} WHERE uuid = ''").fetchall()
        for (row_id,) in rows:
            conn.execute(
                f"UPDATE {table} SET uuid = ? WHERE id = ?",
                (_uuid.uuid4().hex, row_id),
            )

    # 时间戳回填：目标/省下的钱此前没有 updated_at；line_items 随所属交易
    now = conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
    conn.execute(
        "UPDATE goals SET updated_at = created_at WHERE updated_at = '' "
        "AND created_at <> ''"
    )
    conn.execute("UPDATE goals SET updated_at = ? WHERE updated_at = ''", (now,))
    conn.execute(
        "UPDATE savings_wins SET updated_at = created_at WHERE updated_at = '' "
        "AND created_at <> ''"
    )
    conn.execute(
        "UPDATE savings_wins SET updated_at = ? WHERE updated_at = ''", (now,))
    conn.execute(
        "UPDATE line_items SET updated_at = COALESCE(("
        "  SELECT updated_at FROM transactions t "
        "  WHERE t.id = line_items.transaction_id), ?) WHERE updated_at = ''",
        (now,),
    )

    # 时间戳归一化：共享包要求 "%Y-%m-%d %H:%M:%S" 格式；历史脏数据统一为现在。
    ts_glob = ("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
               "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]")
    for table, columns in (
        ("transactions", ("created_at", "updated_at")),
        ("goals", ("created_at", "updated_at")),
        ("savings_wins", ("created_at", "updated_at")),
        ("line_items", ("updated_at",)),
    ):
        for column in columns:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} NOT GLOB ?",
                (now, ts_glob),
            )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_uuid "
        "ON transactions(uuid) WHERE uuid <> ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goals_uuid "
        "ON goals(uuid) WHERE uuid <> ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_line_items_uuid "
        "ON line_items(uuid) WHERE uuid <> ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_wins_uuid "
        "ON savings_wins(uuid) WHERE uuid <> ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_deleted "
        "ON transactions(deleted_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_goals_deleted ON goals(deleted_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_savings_wins_deleted "
        "ON savings_wins(deleted_at)")


MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migrate_to_version_1),
    (2, _migrate_to_version_2),
    (3, _migrate_to_version_3),
)


def migrate_database(conn: sqlite3.Connection) -> None:
    """Apply each pending schema version atomically without replacing the database."""
    _require_database_integrity(conn)
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    else:
        conn.execute("SAVEPOINT migrate_database")

    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, migration in MIGRATIONS:
            if current_version < version:
                migration(conn)
                conn.execute(f"PRAGMA user_version = {version}")
        _require_database_integrity(conn)
    except Exception:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_database")
            conn.execute("RELEASE SAVEPOINT migrate_database")
        raise
    else:
        if owns_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT migrate_database")
