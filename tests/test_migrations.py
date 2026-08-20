import sqlite3

import pytest

from app.db import SCHEMA as LEGACY_SCHEMA
from app.migrations import CURRENT_SCHEMA_VERSION, migrate_database


def test_migration_preserves_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO transactions(date, amount, type, category, created_at, updated_at) "
        "VALUES ('2026-08-01', 20, '支出', '餐饮', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO goals(name, price, saved, created_at) VALUES ('相机', 8000, 3000, 'now')"
    )
    conn.execute(
        "INSERT INTO summaries(period_type, period_start, period_end, content, created_at) "
        "VALUES ('周', '2026-08-01', '2026-08-07', '内容', 'now')"
    )
    conn.execute(
        "INSERT INTO adjustments(date, diff, note, created_at) "
        "VALUES ('2026-08-01', 5, '校准', 'now')"
    )
    conn.commit()

    migrate_database(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0] == 1
    columns = {row[1] for row in conn.execute("PRAGMA table_info(adjustments)")}
    assert "reverses_adjustment_id" in columns
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(adjustments)")}
    assert "idx_adjustments_reverses" in indexes
    conn.close()


def test_migration_keeps_newest_summary_for_duplicate_periods(tmp_path):
    conn = sqlite3.connect(tmp_path / "duplicates.db")
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO summaries(period_type, period_start, period_end, content, created_at) "
        "VALUES ('月', '2026-08-01', '2026-08-31', '旧内容', 'now')"
    )
    conn.execute(
        "INSERT INTO summaries(period_type, period_start, period_end, content, created_at) "
        "VALUES ('月', '2026-08-01', '2026-08-31', '新内容', 'now')"
    )
    conn.commit()

    migrate_database(conn)

    assert conn.execute("SELECT content FROM summaries").fetchall() == [("新内容",)]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summaries(period_type, period_start, period_end, content, created_at) "
            "VALUES ('月', '2026-08-01', '2026-08-31', '重复', 'now')"
        )
    conn.close()


def test_migration_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")

    migrate_database(conn)
    migrate_database(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    conn.close()


def test_migration_rejects_non_sqlite_files_without_overwriting_them(tmp_path):
    db_path = tmp_path / "not-a-database.db"
    original_contents = b"this is not a SQLite database"
    db_path.write_bytes(original_contents)
    conn = sqlite3.connect(db_path)

    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        migrate_database(conn)

    conn.close()
    assert db_path.read_bytes() == original_contents


def test_init_db_applies_current_schema_migration():
    from app import db

    db.init_db()
    conn = db.get_conn()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()
