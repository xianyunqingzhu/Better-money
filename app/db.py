"""SQLite 数据库：建表与基础访问。数据文件：data/better_money.db"""
import sqlite3
from datetime import datetime

from app.migrations import BASE_SCHEMA as SCHEMA
from app.migrations import migrate_database
from app.paths import get_paths


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
        migrate_database(conn)
    finally:
        conn.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
