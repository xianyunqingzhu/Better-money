"""SQLite 数据库：建表与基础访问。数据文件：data/better_money.db"""
import sqlite3
from datetime import datetime

from app.migrations import BASE_SCHEMA as SCHEMA
from app.migrations import CURRENT_SCHEMA_VERSION, migrate_database
from app.paths import get_paths


def get_conn() -> sqlite3.Connection:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    paths = get_paths()
    db_existed = paths.db_path.exists()
    conn = get_conn()
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if db_existed and current_version < CURRENT_SCHEMA_VERSION:
            _create_pre_migration_backup(conn, current_version)
        migrate_database(conn)
    finally:
        conn.close()


def _create_pre_migration_backup(conn: sqlite3.Connection, current_version: int) -> None:
    """Create an integrity-checked SQLite copy before an in-place upgrade."""
    paths = get_paths()
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = paths.backups_dir / (
        f"pre-migration-v{current_version}-to-v{CURRENT_SCHEMA_VERSION}-{stamp}.db"
    )
    backup = sqlite3.connect(backup_path)
    try:
        conn.backup(backup)
        row = backup.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(
                f"pre-migration backup integrity check failed: {row[0] if row else 'no result'}"
            )
    finally:
        backup.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
