/** SQLite schema v4（app/migrations.py BASE_SCHEMA 的移植，含共享同步与退款配对）。 */

export const SCHEMA_VERSION = 4;

export const BASE_SCHEMA = `
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
    last_synced_at TEXT NOT NULL DEFAULT '',
    refund_of TEXT NOT NULL DEFAULT ''
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
    created_at TEXT NOT NULL,
    reverses_adjustment_id INTEGER REFERENCES adjustments(id)
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
    platform TEXT NOT NULL DEFAULT 'mobile',
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_uuid ON transactions(uuid) WHERE uuid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_goals_uuid ON goals(uuid) WHERE uuid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_line_items_uuid ON line_items(uuid) WHERE uuid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_wins_uuid ON savings_wins(uuid) WHERE uuid <> '';
CREATE INDEX IF NOT EXISTS idx_transactions_deleted ON transactions(deleted_at);
CREATE INDEX IF NOT EXISTS idx_transactions_refund_of ON transactions(refund_of);
CREATE INDEX IF NOT EXISTS idx_goals_deleted ON goals(deleted_at);
CREATE INDEX IF NOT EXISTS idx_savings_wins_deleted ON savings_wins(deleted_at);
CREATE INDEX IF NOT EXISTS idx_adjustments_reverses ON adjustments(reverses_adjustment_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_summaries_period_range
    ON summaries(period_type, period_start, period_end);
`;

/** 同步元数据表 + 索引（v3 迁移专用；不含用户数据表，避免重建被删表）。 */
export const SYNC_SCHEMA = `
CREATE TABLE IF NOT EXISTS sync_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'mobile',
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_uuid ON transactions(uuid) WHERE uuid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_goals_uuid ON goals(uuid) WHERE uuid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_line_items_uuid ON line_items(uuid) WHERE uuid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_wins_uuid ON savings_wins(uuid) WHERE uuid <> '';
CREATE INDEX IF NOT EXISTS idx_transactions_deleted ON transactions(deleted_at);
CREATE INDEX IF NOT EXISTS idx_goals_deleted ON goals(deleted_at);
CREATE INDEX IF NOT EXISTS idx_savings_wins_deleted ON savings_wins(deleted_at);
`;
