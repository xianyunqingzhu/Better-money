"""SQLite 数据库：建表与基础访问。数据文件：data/better_money.db"""
import sqlite3
from datetime import datetime

from app.paths import get_paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,               -- YYYY-MM-DD
    amount REAL NOT NULL,             -- 恒为正数，方向由 type 决定
    type TEXT NOT NULL,               -- 支出/收入/退款/取现/转账/还款
    category TEXT NOT NULL DEFAULT '其他',
    merchant TEXT DEFAULT '',
    note TEXT DEFAULT '',
    source TEXT DEFAULT '手动',       -- 手动/文字/截图/小票/CSV
    estimated INTEGER DEFAULT 0,      -- 1 = 估算金额，可改
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
    priority INTEGER DEFAULT 100,     -- 越小越优先
    status TEXT DEFAULT '冷静期',     -- 冷静期/进行中/已暂停/已达成/已放弃
    cooldown_until TEXT DEFAULT '',
    expected_date TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    achieved_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type TEXT NOT NULL,        -- 周/月
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    content TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    expired INTEGER DEFAULT 0,        -- 账目修改后置 1，可重新生成
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    diff REAL NOT NULL,               -- 对账差额（正=多出，负=少了）
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


def get_conn() -> sqlite3.Connection:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
