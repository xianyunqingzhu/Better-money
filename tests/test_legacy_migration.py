from __future__ import annotations

from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import date
import gc
import os
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import weakref
import zipfile

import pytest

from app.db import init_db
from app.migrations import BASE_SCHEMA, CURRENT_SCHEMA_VERSION, migrate_database
from app.paths import get_paths
from app.legacy_migration import (
    LegacyInspection,
    import_legacy,
    inspect_legacy,
)


def _seed_legacy(data_dir: Path, *, api_key: str = "legacy-secret") -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "better_money.db"
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.executescript(BASE_SCHEMA)
        conn.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-01', 100, '收入', '工资', 'legacy-income', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-03', 25, '支出', '餐饮', 'legacy-expense', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO goals(name, price, saved, created_at) "
            "VALUES ('相机', 8000, 3000, 'now')"
        )
        conn.execute(
            "INSERT INTO summaries("
            "period_type, period_start, period_end, content, created_at"
            ") VALUES ('周', '2026-08-01', '2026-08-07', '旧总结', 'now')"
        )
        conn.execute(
            "INSERT INTO adjustments(date, diff, note, created_at) "
            "VALUES ('2026-08-02', 5, '校准', 'now')"
        )
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "api_key": api_key,
                "initial_balance": 500.0,
                "tone": "老师",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return data_dir


def _seed_live(note: str = "original-live") -> None:
    init_db()
    with closing(sqlite3.connect(get_paths().db_path)) as conn, conn:
        conn.execute("DELETE FROM transactions")
        conn.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-20', 9, '支出', '其他', ?, 'now', 'now')",
            (note,),
        )


def _transaction_notes(database: Path) -> list[str]:
    with closing(sqlite3.connect(database)) as conn:
        return [row[0] for row in conn.execute("SELECT note FROM transactions ORDER BY id")]


def _file_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _source_state(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        path.relative_to(directory).as_posix(): (
            path.read_bytes(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _open_wal_legacy(data_dir: Path) -> sqlite3.Connection:
    _seed_legacy(data_dir)
    connection = sqlite3.connect(data_dir / "better_money.db")
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        "INSERT INTO transactions("
        "date, amount, type, category, note, created_at, updated_at"
        ") VALUES ('2026-08-04', 40, '收入', '其他', 'wal-committed', 'now', 'now')"
    )
    connection.commit()
    assert (data_dir / "better_money.db-wal").is_file()
    return connection


@pytest.mark.parametrize("select_project_root", [True, False])
def test_inspect_accepts_project_root_or_data_directory_without_modifying_source(
    tmp_path, select_project_root
):
    project = tmp_path / "old-project"
    data_dir = _seed_legacy(project / "data")
    before = _file_snapshot(project)

    inspection = inspect_legacy(project if select_project_root else data_dir)

    assert isinstance(inspection, LegacyInspection)
    assert inspection.source_dir == data_dir.resolve()
    assert inspection.transaction_count == 2
    assert inspection.goal_count == 1
    assert inspection.summary_count == 1
    assert inspection.earliest_transaction_date == "2026-08-01"
    assert inspection.suggested_initial_balance_date == "2026-08-01"
    assert inspection.initial_balance == 500.0
    assert inspection.calculated_balance == 580.0
    assert inspection.cleared_image_paths == ()
    assert _file_snapshot(project) == before
    with pytest.raises(FrozenInstanceError):
        inspection.transaction_count = 99


@pytest.mark.parametrize("operation", ["inspect", "import"])
def test_legacy_snapshot_preserves_committed_wal_and_all_source_metadata(
    tmp_path, operation
):
    source = tmp_path / "legacy" / "data"
    source_connection = _open_wal_legacy(source)
    try:
        source_before = _source_state(source)
        if operation == "inspect":
            inspection = inspect_legacy(source)
        else:
            _seed_live()
            inspection = import_legacy(source, "2026-08-01")
        source_after = _source_state(source)
    finally:
        source_connection.close()

    assert inspection.transaction_count == 3
    assert inspection.calculated_balance == 620.0
    assert source_after == source_before
    assert "better_money.db-wal" in source_after
    if operation == "import":
        assert _transaction_notes(get_paths().db_path) == [
            "legacy-income",
            "legacy-expense",
            "wal-committed",
        ]


@pytest.mark.parametrize("operation", ["inspect", "import"])
def test_legacy_operations_never_open_the_source_database_with_sqlite(
    tmp_path, monkeypatch, operation
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "legacy" / "data")
    source_database = source / "better_money.db"
    real_connect = migration_module.sqlite3.connect

    def reject_source_database(database, *args, **kwargs):
        rendered = str(database)
        if str(source_database.resolve()) in rendered or source_database.as_uri() in rendered:
            raise AssertionError("SQLite opened the legacy source database")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(migration_module.sqlite3, "connect", reject_source_database)
    if operation == "inspect":
        inspection = inspect_legacy(source)
    else:
        _seed_live()
        inspection = import_legacy(source, "2026-08-01")

    assert inspection.transaction_count == 2
    assert not (source / "better_money.db-shm").exists()


def test_inspect_handles_missing_tables_and_no_transactions(tmp_path):
    data_dir = tmp_path / "sparse-data"
    data_dir.mkdir()
    with closing(sqlite3.connect(data_dir / "better_money.db")):
        pass
    (data_dir / "config.json").write_text(
        json.dumps({"initial_balance": 12.5}), encoding="utf-8"
    )

    inspection = inspect_legacy(data_dir)

    assert inspection.transaction_count == 0
    assert inspection.goal_count == 0
    assert inspection.summary_count == 0
    assert inspection.earliest_transaction_date is None
    assert inspection.suggested_initial_balance_date == date.today().isoformat()
    assert inspection.initial_balance == 12.5
    assert inspection.calculated_balance == 12.5


def test_inspect_prefers_an_existing_initial_balance_date(tmp_path):
    data_dir = _seed_legacy(tmp_path / "legacy" / "data")
    config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    config["initial_balance_date"] = "2026-07-15"
    (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    inspection = inspect_legacy(data_dir)

    assert inspection.suggested_initial_balance_date == "2026-07-15"


def test_inspect_rejects_corrupt_database_without_changing_its_bytes(tmp_path):
    data_dir = tmp_path / "corrupt-data"
    data_dir.mkdir()
    database = data_dir / "better_money.db"
    original = b"not a sqlite database"
    database.write_bytes(original)

    with pytest.raises((ValueError, RuntimeError, sqlite3.DatabaseError)):
        inspect_legacy(data_dir)

    assert database.read_bytes() == original


@pytest.mark.parametrize("source_form", ["data", "root", "alias"])
def test_inspect_rejects_the_live_data_directory_and_resolved_aliases(source_form):
    _seed_live()
    paths = get_paths()
    source = {
        "data": paths.data_dir,
        "root": paths.root,
        "alias": paths.data_dir / ".." / "data",
    }[source_form]

    with pytest.raises(ValueError, match="live"):
        inspect_legacy(source)


def test_inspect_rejects_an_uninitialized_live_data_directory():
    paths = get_paths()

    with pytest.raises(ValueError, match="live"):
        inspect_legacy(paths.data_dir)


def test_inspect_rejects_a_hardlink_to_the_live_database(tmp_path):
    _seed_live()
    source = tmp_path / "hardlinked-legacy"
    source.mkdir()
    os.link(get_paths().db_path, source / "better_money.db")

    with pytest.raises(ValueError, match="live|identity|link"):
        inspect_legacy(source)


def test_inspect_rejects_a_symlinked_database_before_sqlite_opens_it(tmp_path):
    _seed_live()
    source = tmp_path / "symlinked-legacy"
    source.mkdir()
    try:
        (source / "better_money.db").symlink_to(get_paths().db_path)
    except OSError as exc:
        pytest.skip(f"database symlink is unavailable: {exc}")

    with pytest.raises(ValueError, match="link|reparse|live"):
        inspect_legacy(source)


@pytest.mark.parametrize("live_container", ["backups", "runtime"])
def test_inspect_rejects_a_source_nested_in_any_live_writable_directory(
    live_container,
):
    paths = get_paths()
    parent = {
        "backups": paths.backups_dir,
        "runtime": paths.runtime_dir,
    }[live_container]
    source = _seed_legacy(parent / "nested-legacy")

    with pytest.raises(ValueError, match="live"):
        inspect_legacy(source)


def test_import_preserves_source_and_installs_data_after_safety_backup(tmp_path):
    _seed_live()
    paths = get_paths()
    paths.config_path.write_text(
        json.dumps({"api_key": "current-secret", "tone": "朋友"}),
        encoding="utf-8",
    )
    source = _seed_legacy(tmp_path / "old-project" / "data")
    source_before = _file_snapshot(source)

    inspection = import_legacy(source.parent, "2026-07-31")

    assert inspection.source_dir == source.resolve()
    assert inspection.calculated_balance == 580.0
    assert _transaction_notes(paths.db_path) == ["legacy-income", "legacy-expense"]
    with closing(sqlite3.connect(paths.db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    imported_config = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert imported_config == {
        "api_key": "legacy-secret",
        "initial_balance": 500.0,
        "tone": "老师",
        "initial_balance_date": "2026-07-31",
    }
    assert _file_snapshot(source) == source_before

    safety_archives = list(paths.backups_dir.glob("*pre-legacy-import*.zip"))
    assert len(safety_archives) == 1
    with zipfile.ZipFile(safety_archives[0]) as archive:
        backup_database = tmp_path / "pre-import.db"
        backup_database.write_bytes(archive.read("data/better_money.db"))
        backup_config = json.loads(archive.read("data/config.json"))
    assert _transaction_notes(backup_database) == ["original-live"]
    assert "api_key" not in backup_config
    assert backup_config["tone"] == "朋友"


def test_import_rewrites_internal_images_and_clears_external_or_traversing_paths(
    tmp_path,
):
    _seed_live()
    paths = get_paths()
    (paths.images_dir / "live-only.txt").write_text("live", encoding="utf-8")
    project = tmp_path / "old-project"
    source = _seed_legacy(project / "data")
    nested = source / "images" / "nested"
    nested.mkdir(parents=True)
    absolute_image = nested / "absolute.png"
    relative_image = nested / "relative.png"
    project_relative_image = nested / "project-relative.png"
    absolute_image.write_bytes(b"absolute")
    relative_image.write_bytes(b"relative")
    project_relative_image.write_bytes(b"project-relative")
    external_image = project / "outside.png"
    external_image.write_bytes(b"outside")
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute(
            "UPDATE summaries SET image_path = ? WHERE content = '旧总结'",
            (str(absolute_image),),
        )
        conn.execute(
            "INSERT INTO summaries("
            "period_type, period_start, period_end, content, image_path, created_at"
            ") VALUES ('月', '2026-08-01', '2026-08-31', 'project relative', "
            "'data/images/nested/project-relative.png', 'now')"
        )
        conn.executemany(
            "INSERT INTO pending_items(raw_text, image_path, created_at) "
            "VALUES (?, ?, 'now')",
            [
                ("relative", "images/nested/relative.png"),
                ("external", str(external_image)),
                ("traversal", "images/../config.json"),
                ("missing", "images/nested/missing.png"),
            ],
        )
    source_before = _file_snapshot(project)

    inspection = import_legacy(project, "2026-08-01")

    with closing(sqlite3.connect(paths.db_path)) as conn:
        summary_paths = dict(conn.execute("SELECT content, image_path FROM summaries"))
        pending_paths = dict(conn.execute("SELECT raw_text, image_path FROM pending_items"))
    assert summary_paths["旧总结"] == str(
        paths.images_dir / "nested" / "absolute.png"
    )
    assert summary_paths["project relative"] == str(
        paths.images_dir / "nested" / "project-relative.png"
    )
    assert pending_paths["relative"] == str(
        paths.images_dir / "nested" / "relative.png"
    )
    assert pending_paths["external"] == ""
    assert pending_paths["traversal"] == ""
    assert pending_paths["missing"] == ""
    assert set(inspection.cleared_image_paths) == {
        str(external_image),
        "images/../config.json",
        "images/nested/missing.png",
    }
    assert (paths.images_dir / "nested" / "absolute.png").read_bytes() == b"absolute"
    assert (paths.images_dir / "nested" / "relative.png").read_bytes() == b"relative"
    assert not (paths.images_dir / "live-only.txt").exists()
    assert _file_snapshot(project) == source_before


def test_import_preserves_raw_legacy_backups_in_a_dedicated_directory(tmp_path):
    _seed_live()
    paths = get_paths()
    source = _seed_legacy(tmp_path / "legacy" / "data")
    legacy_backups = source / "backups"
    (legacy_backups / "nested").mkdir(parents=True)
    (legacy_backups / "old-copy.db").write_bytes(b"raw database recovery material")
    (legacy_backups / "nested" / "readme.txt").write_text(
        "manual recovery", encoding="utf-8"
    )

    import_legacy(source, "2026-08-01")

    preserved_roots = list(paths.backups_dir.glob("legacy-import-*"))
    assert len(preserved_roots) == 1
    assert (preserved_roots[0] / "old-copy.db").read_bytes() == (
        b"raw database recovery material"
    )
    assert (preserved_roots[0] / "nested" / "readme.txt").read_text(
        encoding="utf-8"
    ) == "manual recovery"
    assert not list(paths.backups_dir.glob("old-copy.db"))


def test_import_rolls_back_all_live_replacements_when_installation_fails(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live("rollback-live")
    paths = get_paths()
    paths.config_path.write_text(
        json.dumps({"api_key": "live-key", "tone": "朋友"}), encoding="utf-8"
    )
    (paths.images_dir / "live.png").write_bytes(b"live-image")
    source = _seed_legacy(tmp_path / "legacy" / "data")
    (source / "images").mkdir()
    (source / "images" / "legacy.png").write_bytes(b"legacy-image")
    source_before = _file_snapshot(source)
    live_db_before = paths.db_path.read_bytes()
    live_config_before = paths.config_path.read_bytes()
    live_images_before = _file_snapshot(paths.images_dir)
    real_replace = migration_module.os.replace
    failed = False

    def fail_installing_config(source_path, destination_path):
        nonlocal failed
        if Path(destination_path) == paths.config_path and not failed:
            failed = True
            raise OSError("simulated legacy config install failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(migration_module.os, "replace", fail_installing_config)

    with pytest.raises(OSError, match="simulated legacy"):
        import_legacy(source, "2026-08-01")

    assert paths.db_path.read_bytes() == live_db_before
    assert paths.config_path.read_bytes() == live_config_before
    assert _file_snapshot(paths.images_dir) == live_images_before
    assert _transaction_notes(paths.db_path) == ["rollback-live"]
    assert _file_snapshot(source) == source_before


def test_failed_install_with_failed_recovery_keeps_originals_in_persistent_journal(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live("persistent-rollback-live")
    paths = get_paths()
    paths.config_path.write_text(json.dumps({"tone": "朋友"}), encoding="utf-8")
    (paths.images_dir / "live.png").write_bytes(b"live-image")
    source = _seed_legacy(tmp_path / "legacy" / "data")
    original_db = paths.db_path.read_bytes()
    original_config = paths.config_path.read_bytes()
    original_image = (paths.images_dir / "live.png").read_bytes()
    real_replace = migration_module.os.replace
    installation_failed = False

    def fail_install_and_database_recovery(source_path, destination_path):
        nonlocal installation_failed
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            destination_path == paths.config_path
            and source_path.name == "config.json"
            and not installation_failed
        ):
            installation_failed = True
            raise OSError("simulated install failure")
        if (
            installation_failed
            and destination_path == paths.db_path
            and "legacy-rollback-" in str(source_path)
        ):
            raise OSError("simulated recovery failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        migration_module.os, "replace", fail_install_and_database_recovery
    )

    with pytest.raises(RuntimeError, match="recovery|rollback|journal"):
        import_legacy(source, "2026-08-01")

    rollback_roots = list(paths.runtime_dir.glob("legacy-rollback-*"))
    assert rollback_roots
    assert any((root / "journal.json").is_file() for root in rollback_roots)
    preserved_bytes = [
        candidate.read_bytes()
        for root in rollback_roots
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.name != "journal.json"
    ]
    assert (
        paths.db_path.exists() and paths.db_path.read_bytes() == original_db
    ) or original_db in preserved_bytes
    assert (
        paths.config_path.exists() and paths.config_path.read_bytes() == original_config
        or original_config in preserved_bytes
    )
    assert (
        (paths.images_dir / "live.png").exists()
        and (paths.images_dir / "live.png").read_bytes() == original_image
        or original_image in preserved_bytes
    )


def test_import_rejects_non_iso_initial_balance_date_before_changing_live_data(
    tmp_path,
):
    _seed_live("unchanged-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "legacy" / "data")
    live_before = paths.db_path.read_bytes()

    with pytest.raises(ValueError, match="ISO"):
        import_legacy(source, "2026/08/01")

    assert paths.db_path.read_bytes() == live_before
    assert not list(paths.backups_dir.glob("*pre-legacy-import*.zip"))


@pytest.mark.parametrize("invalid_date", ["2026-8-01", "2026-02-30", "not-a-date"])
def test_import_rejects_noncanonical_transaction_dates_without_changing_live(
    tmp_path, invalid_date
):
    _seed_live("invalid-date-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "invalid-date" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute("UPDATE transactions SET date = ? WHERE id = 1", (invalid_date,))
    live_before = paths.db_path.read_bytes()

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        import_legacy(source, "2026-08-01")

    assert paths.db_path.read_bytes() == live_before


def test_import_succeeds_into_a_completely_blank_application_home(tmp_path):
    paths = get_paths()
    assert not paths.db_path.exists()
    assert not paths.config_path.exists()
    source = _seed_legacy(tmp_path / "blank-import" / "data")

    inspection = import_legacy(source, "2026-08-01")

    assert inspection.transaction_count == 2
    assert _transaction_notes(paths.db_path) == ["legacy-income", "legacy-expense"]
    assert not list(paths.backups_dir.glob("*pre-legacy-import*.zip"))


def test_import_waits_for_open_app_connection_and_safety_backup_captures_its_write(
    tmp_path,
):
    from app import db as db_module

    _seed_live("before-held-write")
    paths = get_paths()
    held = db_module.get_conn()
    held.execute("PRAGMA journal_mode = WAL")
    held.execute("PRAGMA wal_autocheckpoint = 0")
    held.execute(
        "INSERT INTO transactions("
        "date, amount, type, category, note, created_at, updated_at"
        ") VALUES ('2026-08-20', 1, '支出', '其他', 'held-committed', 'now', 'now')"
    )
    held.commit()
    source = _seed_legacy(tmp_path / "concurrent" / "data")
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run_import():
        started.set()
        try:
            import_legacy(source, "2026-08-01")
        except BaseException as exc:  # surfaced in the asserting thread
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run_import)
    worker.start()
    assert started.wait(1)
    assert not finished.wait(0.25)
    held.close()
    worker.join(5)

    assert not worker.is_alive()
    assert errors == []
    safety = list(paths.backups_dir.glob("*pre-legacy-import*.zip"))
    assert len(safety) == 1
    with zipfile.ZipFile(safety[0]) as archive:
        backed_up = tmp_path / "concurrent-safety.db"
        backed_up.write_bytes(archive.read("data/better_money.db"))
    assert "held-committed" in _transaction_notes(backed_up)
    assert _transaction_notes(paths.db_path) == ["legacy-income", "legacy-expense"]
    assert not Path(f"{paths.db_path}-wal").exists()
    assert not Path(f"{paths.db_path}-shm").exists()


def test_get_conn_releases_shared_lock_when_sqlite_connect_fails(monkeypatch):
    from app import db as db_module

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr(db_module.sqlite3, "connect", fail_connect)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        db_module.get_conn()

    acquired = threading.Event()

    def acquire_from_another_thread():
        try:
            with db_module.LEDGER_GATE.exclusive():
                acquired.set()
        except BaseException:
            return

    worker = threading.Thread(target=acquire_from_another_thread)
    worker.start()
    worker.join(2)
    assert acquired.is_set()


def test_snapshot_rejects_source_changed_during_copy(tmp_path, monkeypatch):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "changing" / "data")
    config = source / "config.json"
    real_copy = migration_module._copy_verified_file
    changed = False

    def copy_then_change(source_path, destination_path, expected):
        nonlocal changed
        result = real_copy(source_path, destination_path, expected)
        if Path(source_path).name == "config.json" and not changed:
            config.write_text(json.dumps({"initial_balance": 999}), encoding="utf-8")
            changed = True
        return result

    monkeypatch.setattr(migration_module, "_copy_verified_file", copy_then_change)

    with pytest.raises(RuntimeError, match="close the old application|changed"):
        inspect_legacy(source)


def test_import_rejects_a_future_schema_version_without_changing_live_data(
    tmp_path,
):
    _seed_live("future-version-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "future" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute("PRAGMA user_version = 99")
    live_before = paths.db_path.read_bytes()

    with pytest.raises((ValueError, RuntimeError), match="version|schema"):
        import_legacy(source, "2026-08-01")

    assert paths.db_path.read_bytes() == live_before


@pytest.mark.parametrize(
    "damage_sql",
    [
        "DROP TABLE pending_items",
        "ALTER TABLE pending_items DROP COLUMN image_path",
        "DROP INDEX idx_adjustments_reverses",
        "INSERT INTO line_items(transaction_id, name) VALUES (999999, 'orphan')",
    ],
    ids=["missing-table", "missing-column", "missing-index", "foreign-key"],
)
def test_import_rejects_invalid_current_schema_without_changing_live_data(
    tmp_path, damage_sql
):
    _seed_live("invalid-schema-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "invalid-schema" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(damage_sql)
        conn.commit()
    live_before = paths.db_path.read_bytes()

    with pytest.raises((ValueError, RuntimeError), match="schema|foreign|table|column|index"):
        import_legacy(source, "2026-08-01")

    assert paths.db_path.read_bytes() == live_before


def _create_hot_journal_legacy(data_dir: Path) -> Path:
    _seed_legacy(data_dir)
    script = """
import os
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode = DELETE')
conn.execute('PRAGMA synchronous = FULL')
conn.execute('PRAGMA cache_size = 1')
conn.execute('PRAGMA cache_spill = ON')
conn.execute('BEGIN IMMEDIATE')
for number in range(2000):
    conn.execute(\"INSERT INTO transactions(date, amount, type, category, note, created_at, updated_at) VALUES ('2026-08-09', 1, '支出', '其他', ?, 'now', 'now')\", (f'crash-uncommitted-{number}-' + 'x' * 400,))
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(data_dir / "better_money.db")],
        check=True,
    )
    journal = data_dir / "better_money.db-journal"
    assert journal.is_file() and journal.stat().st_size > 0
    return data_dir


@pytest.mark.parametrize("operation", ["inspect", "import"])
def test_hot_journal_is_recovered_only_in_private_snapshot(tmp_path, operation):
    source = _create_hot_journal_legacy(tmp_path / "hot-journal" / "data")
    before = _source_state(source)

    if operation == "inspect":
        inspection = inspect_legacy(source)
    else:
        _seed_live()
        inspection = import_legacy(source, "2026-08-01")

    assert inspection.transaction_count == 2
    assert _source_state(source) == before
    assert "better_money.db-journal" in before
    if operation == "import":
        assert _transaction_notes(get_paths().db_path) == [
            "legacy-income",
            "legacy-expense",
        ]


def test_import_validates_linked_live_database_before_backup_or_sqlite_open(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live("linked-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "legacy" / "data")
    before = (
        paths.db_path.read_bytes(),
        paths.db_path.stat().st_size,
        paths.db_path.stat().st_mtime_ns,
    )
    real_is_link = migration_module._is_link

    def report_live_database_as_link(path):
        return Path(path) == paths.db_path or real_is_link(Path(path))

    def backup_must_not_run(*args, **kwargs):
        raise AssertionError("backup opened a linked live database")

    monkeypatch.setattr(migration_module, "_is_link", report_live_database_as_link)
    monkeypatch.setattr(migration_module, "create_backup", backup_must_not_run)

    with pytest.raises(ValueError, match="link|reparse"):
        import_legacy(source, "2026-08-01")

    assert (
        paths.db_path.read_bytes(),
        paths.db_path.stat().st_size,
        paths.db_path.stat().st_mtime_ns,
    ) == before


def test_current_schema_rejects_line_items_without_declared_foreign_key(tmp_path):
    _seed_live("schema-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "missing-fk" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            ALTER TABLE line_items RENAME TO old_line_items;
            CREATE TABLE line_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                qty REAL DEFAULT 1,
                price REAL DEFAULT 0
            );
            INSERT INTO line_items SELECT * FROM old_line_items;
            DROP TABLE old_line_items;
            """
        )
        conn.commit()
    live_before = paths.db_path.read_bytes()

    with pytest.raises(RuntimeError, match="schema|foreign"):
        import_legacy(source, "2026-08-01")

    assert paths.db_path.read_bytes() == live_before


def test_current_schema_rejects_same_named_index_on_wrong_table_and_columns(
    tmp_path,
):
    _seed_live("index-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "wrong-index" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            DROP INDEX idx_adjustments_reverses;
            CREATE INDEX idx_adjustments_reverses ON transactions(note);
            """
        )
        conn.commit()
    live_before = paths.db_path.read_bytes()

    with pytest.raises(RuntimeError, match="schema|index"):
        import_legacy(source, "2026-08-01")

    assert paths.db_path.read_bytes() == live_before


def test_current_schema_allows_a_harmless_extra_table(tmp_path):
    _seed_live()
    source = _seed_legacy(tmp_path / "extra-table" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute("CREATE TABLE legacy_notes(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO legacy_notes(note) VALUES ('kept')")
        conn.commit()

    import_legacy(source, "2026-08-01")

    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert conn.execute("SELECT note FROM legacy_notes").fetchone()[0] == "kept"


@pytest.mark.parametrize("invalid_date", [None, ""])
def test_inspect_rejects_null_or_empty_transaction_date(tmp_path, invalid_date):
    source = tmp_path / "nullable-date" / "data"
    source.mkdir(parents=True)
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute("CREATE TABLE transactions(date TEXT, amount REAL, type TEXT)")
        conn.execute(
            "INSERT INTO transactions(date, amount, type) VALUES (?, 1, '支出')",
            (invalid_date,),
        )

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        inspect_legacy(source)


def test_get_conn_context_manager_closes_and_releases_ordinary_slot():
    from app import db as db_module

    with db_module.get_conn() as connection:
        connection.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with db_module.LEDGER_GATE.exclusive():
        pass


def test_get_conn_sql_exception_closes_without_leaking_slot():
    from app import db as db_module

    with pytest.raises(sqlite3.OperationalError):
        with db_module.get_conn() as connection:
            connection.execute("SELECT * FROM table_that_does_not_exist")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with db_module.LEDGER_GATE.exclusive():
        pass


def test_get_conn_can_be_closed_from_another_thread_without_leaking_slot():
    from app import db as db_module

    connection = db_module.get_conn()
    errors: list[BaseException] = []

    def close_connection():
        try:
            connection.close()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=close_connection)
    worker.start()
    worker.join(2)

    assert not worker.is_alive()
    assert errors == []
    with db_module.LEDGER_GATE.exclusive():
        pass


def test_linked_legacy_image_is_skipped_cleared_and_does_not_abort_import(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live()
    source = _seed_legacy(tmp_path / "linked-image" / "data")
    images = source / "images"
    images.mkdir()
    linked = images / "linked.png"
    linked.write_bytes(b"must-not-be-imported")
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute("UPDATE summaries SET image_path = 'images/linked.png'")
    real_is_link = migration_module._is_link

    def report_one_image_as_link(path):
        return Path(path) == linked or real_is_link(Path(path))

    monkeypatch.setattr(migration_module, "_is_link", report_one_image_as_link)

    inspection = import_legacy(source, "2026-08-01")

    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert conn.execute("SELECT image_path FROM summaries").fetchone()[0] == ""
    assert inspection.cleared_image_paths == ("images/linked.png",)
    assert not (get_paths().images_dir / "linked.png").exists()


def test_legacy_images_copy_rejects_concurrent_source_mutation(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "changing-images" / "data")
    images = source / "images"
    images.mkdir()
    image = images / "receipt.png"
    image.write_bytes(b"before")
    real_copy = migration_module._copy_verified_file
    changed = False

    def copy_then_change(source_path, destination_path, expected):
        nonlocal changed
        result = real_copy(source_path, destination_path, expected)
        if Path(source_path) == image and not changed:
            image.write_bytes(b"after")
            changed = True
        return result

    monkeypatch.setattr(migration_module, "_copy_verified_file", copy_then_change)

    with pytest.raises(RuntimeError, match="close the old application|changed"):
        inspect_legacy(source)


def test_atomic_journal_publish_preserves_previous_version_on_replace_failure(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    rollback = tmp_path / "rollback"
    rollback.mkdir()
    migration_module._write_journal(rollback, {"phase": "first"})
    before = (rollback / "journal.json").read_bytes()
    real_replace = migration_module.os.replace

    def fail_journal_replace(source_path, destination_path):
        if Path(destination_path) == rollback / "journal.json":
            raise OSError("simulated atomic publish failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(migration_module.os, "replace", fail_journal_replace)
    with pytest.raises(OSError, match="atomic publish"):
        migration_module._write_journal(rollback, {"phase": "second"})

    assert (rollback / "journal.json").read_bytes() == before


def test_atomic_retire_failure_rolls_back_legacy_import_before_reporting_failure(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module
    from app import rollback_cleanup

    _seed_live()
    source = _seed_legacy(tmp_path / "cleanup" / "data")
    real_replace = rollback_cleanup.os.replace

    def fail_rollback_retire(source_path, destination_path):
        if Path(source_path).name.startswith("legacy-rollback-"):
            raise OSError("simulated cleanup failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(rollback_cleanup.os, "replace", fail_rollback_retire)

    with pytest.raises(RuntimeError, match="retire|cleanup"):
        import_legacy(source, "2026-08-01")

    assert _transaction_notes(get_paths().db_path) == ["original-live"]
    rollback_roots = list(get_paths().runtime_dir.glob("legacy-rollback-*"))
    assert rollback_roots
    journal = json.loads((rollback_roots[0] / "journal.json").read_text())
    assert journal["phase"] == "recovered"


def test_legacy_partial_cleanup_after_retire_is_not_scanned_at_startup(
    tmp_path, monkeypatch
):
    from app import rollback_cleanup
    from app.recovery import recover_interrupted_installs

    _seed_live("legacy-retire-original")
    source = _seed_legacy(tmp_path / "legacy-retire" / "data")
    real_rmtree = rollback_cleanup.shutil.rmtree

    def partially_delete_retired(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.startswith(".better-money-retired-cleanup-"):
            (candidate / "journal.json").unlink(missing_ok=True)
            raise OSError("partial cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(rollback_cleanup.shutil, "rmtree", partially_delete_retired)

    inspection = import_legacy(source, "2026-08-01")
    assert inspection.transaction_count == 2
    desired = _transaction_notes(get_paths().db_path)
    assert not list(get_paths().runtime_dir.glob("legacy-rollback-*"))
    assert list(
        get_paths().runtime_dir.glob(".better-money-retired-cleanup-*")
    )

    recover_interrupted_installs()
    assert _transaction_notes(get_paths().db_path) == desired


def test_installed_marker_failure_rolls_back_legacy_import_before_returning(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live("original-installed-marker")
    source = _seed_legacy(tmp_path / "installed-marker" / "data")
    paths = get_paths()
    original = migration_module._path_manifest(paths.data_dir)
    real_write = migration_module._write_journal

    def fail_installed_marker(rollback, journal):
        if journal["phase"] == "installed":
            raise OSError("installed marker durability failure")
        return real_write(rollback, journal)

    monkeypatch.setattr(migration_module, "_write_journal", fail_installed_marker)

    with pytest.raises(OSError, match="installed marker"):
        migration_module.import_legacy(source, "2026-08-01")

    assert migration_module._path_manifest(paths.data_dir) == original


def test_new_database_install_occurs_only_after_all_live_sidecars_are_saved(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live()
    paths = get_paths()
    source = _seed_legacy(tmp_path / "ordering" / "data")
    sidecars = [
        Path(f"{paths.db_path}{suffix}")
        for suffix in ("-wal", "-shm", "-journal")
    ]
    for sidecar in sidecars:
        sidecar.write_bytes(b"old-sidecar")
    monkeypatch.setattr(migration_module, "create_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        migration_module, "_checkpoint_live_database", lambda paths: None
    )
    real_replace = migration_module.os.replace

    def assert_sidecars_gone_before_database_install(source_path, destination_path):
        if (
            Path(destination_path) == paths.db_path
            and Path(source_path).name == "better_money.db"
            and "legacy-rollback-" not in str(source_path)
        ):
            assert all(not sidecar.exists() for sidecar in sidecars)
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        migration_module.os, "replace", assert_sidecars_gone_before_database_install
    )

    import_legacy(source, "2026-08-01")

    assert all(not sidecar.exists() for sidecar in sidecars)


def test_get_conn_gc_finalization_releases_ordinary_slot():
    from app import db as db_module

    connection = db_module.get_conn()
    reference = weakref.ref(connection)
    del connection
    gc.collect()

    assert reference() is None
    with db_module.LEDGER_GATE.exclusive():
        pass


def test_legacy_backups_copy_rejects_concurrent_source_mutation(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "changing-backups" / "data")
    backups = source / "backups"
    backups.mkdir()
    raw_backup = backups / "old.db"
    raw_backup.write_bytes(b"before")
    real_copy = migration_module._copy_verified_file
    changed = False

    def copy_then_change(source_path, destination_path, expected):
        nonlocal changed
        result = real_copy(source_path, destination_path, expected)
        if Path(source_path) == raw_backup and not changed:
            raw_backup.write_bytes(b"after")
            changed = True
        return result

    monkeypatch.setattr(migration_module, "_copy_verified_file", copy_then_change)

    with pytest.raises(RuntimeError, match="close the old application|changed"):
        import_legacy(source, "2026-08-01")


def test_link_in_legacy_backups_is_rejected(tmp_path, monkeypatch):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "linked-backup" / "data")
    backups = source / "backups"
    backups.mkdir()
    linked = backups / "linked.db"
    linked.write_bytes(b"must-not-copy")
    real_is_link = migration_module._is_link

    def report_backup_as_link(path):
        return Path(path) == linked or real_is_link(Path(path))

    monkeypatch.setattr(migration_module, "_is_link", report_backup_as_link)

    with pytest.raises(ValueError, match="linked|link"):
        import_legacy(source, "2026-08-01")


def test_recovery_cleanup_failure_does_not_mask_original_install_error(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live("recovery-cleanup-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "recovery-cleanup" / "data")
    real_replace = migration_module.os.replace
    failed_install = False

    def fail_config_install(source_path, destination_path):
        nonlocal failed_install
        if Path(destination_path) == paths.config_path and not failed_install:
            failed_install = True
            raise OSError("original install failure")
        if Path(source_path).name.startswith("legacy-rollback-"):
            raise OSError("cleanup must not mask")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(migration_module.os, "replace", fail_config_install)

    with pytest.raises(OSError, match="original install failure"):
        import_legacy(source, "2026-08-01")

    rollback_roots = list(paths.runtime_dir.glob("legacy-rollback-*"))
    assert rollback_roots
    journal = json.loads((rollback_roots[0] / "journal.json").read_text())
    assert journal["phase"] == "recovered"
    assert _transaction_notes(paths.db_path) == ["recovery-cleanup-live"]


@pytest.mark.parametrize(
    "table_suffix",
    [
        "CHECK (length(raw_text) < 1000)",
        "UNIQUE (raw_text)",
        "STRICT",
    ],
    ids=["check", "unique-autoindex", "strict"],
)
def test_current_schema_rejects_noncanonical_pending_items_table_sql(
    tmp_path, table_suffix
):
    source = _seed_legacy(tmp_path / "table-sql" / table_suffix.split()[0] / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            f"""
            ALTER TABLE pending_items RENAME TO old_pending_items;
            CREATE TABLE pending_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at TEXT NOT NULL
                {',' if not table_suffix == 'STRICT' else ''}
                {table_suffix if not table_suffix == 'STRICT' else ''}
            ) {table_suffix if table_suffix == 'STRICT' else ''};
            INSERT INTO pending_items(id, raw_text, image_path, created_at)
            SELECT id, raw_text, image_path, created_at FROM old_pending_items;
            DROP TABLE old_pending_items;
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|table"):
        import_legacy(source, "2026-08-01")


def test_current_schema_rejects_generated_column_hidden_from_table_info(tmp_path):
    source = _seed_legacy(tmp_path / "generated" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            ALTER TABLE pending_items RENAME TO old_pending_items;
            CREATE TABLE pending_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                generated_text TEXT GENERATED ALWAYS AS (raw_text) VIRTUAL
            );
            INSERT INTO pending_items(id, raw_text, image_path, created_at)
            SELECT id, raw_text, image_path, created_at FROM old_pending_items;
            DROP TABLE old_pending_items;
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|table|column"):
        import_legacy(source, "2026-08-01")


def test_current_schema_rejects_noncanonical_trigger(tmp_path):
    source = _seed_legacy(tmp_path / "trigger" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            "CREATE TRIGGER legacy_delete AFTER DELETE ON transactions "
            "BEGIN DELETE FROM line_items WHERE transaction_id = OLD.id; END"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|trigger"):
        import_legacy(source, "2026-08-01")


def test_two_threads_closing_one_connection_release_exactly_one_slot(monkeypatch):
    from app import db as db_module

    connection = db_module.get_conn()
    entered = threading.Event()
    allow_close = threading.Event()
    underlying_calls = 0
    real_underlying = sqlite3.Connection.close

    def controlled_underlying_close():
        nonlocal underlying_calls
        underlying_calls += 1
        entered.set()
        assert allow_close.wait(2)
        real_underlying(connection)

    monkeypatch.setattr(
        connection, "_close_underlying", controlled_underlying_close, raising=False
    )
    errors: list[BaseException] = []

    def close_from_thread():
        try:
            connection.close()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=close_from_thread)
    second = threading.Thread(target=close_from_thread)
    first.start()
    assert entered.wait(1)
    second.start()
    allow_close.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert underlying_calls == 1
    with db_module.LEDGER_GATE.exclusive():
        pass


def test_close_failure_keeps_slot_until_underlying_close_can_be_retried(monkeypatch):
    from app import db as db_module

    connection = db_module.get_conn()
    real_underlying = sqlite3.Connection.close
    monkeypatch.setattr(
        connection,
        "_close_underlying",
        lambda: (_ for _ in ()).throw(OSError("underlying close failed")),
        raising=False,
    )

    with pytest.raises(OSError, match="underlying close failed"):
        connection.close()

    entered_exclusive = threading.Event()

    def try_exclusive():
        with db_module.LEDGER_GATE.exclusive():
            entered_exclusive.set()

    waiter = threading.Thread(target=try_exclusive)
    waiter.start()
    assert not entered_exclusive.wait(0.2)
    monkeypatch.setattr(
        connection, "_close_underlying", lambda: real_underlying(connection)
    )
    connection.close()
    waiter.join(2)

    assert not waiter.is_alive()
    assert entered_exclusive.is_set()


def test_nested_exclusive_and_owner_get_conn_never_deadlock(monkeypatch):
    from app import db as db_module

    gate = db_module.LedgerGate()
    monkeypatch.setattr(db_module, "LEDGER_GATE", gate)
    finished = threading.Event()
    errors: list[BaseException] = []

    def exercise_owner():
        try:
            with gate.exclusive():
                with gate.exclusive():
                    with pytest.raises(RuntimeError, match="exclusive|migration"):
                        db_module.get_conn()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=exercise_owner, daemon=True)
    worker.start()

    assert finished.wait(1), "exclusive owner deadlocked itself"
    assert errors == []


@pytest.mark.parametrize("import_fails", [False, True], ids=["success", "failure"])
def test_outer_staging_cleanup_failure_never_changes_import_outcome(
    tmp_path, monkeypatch, import_fails
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "staging-cleanup" / "data")
    if import_fails:
        with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
            conn.execute("UPDATE transactions SET date = '' WHERE id = 1")
    real_rmtree = migration_module.shutil.rmtree

    def fail_outer_staging_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith("better-money-legacy-"):
            raise OSError("outer staging cleanup failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        migration_module.shutil, "rmtree", fail_outer_staging_cleanup
    )

    if import_fails:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            import_legacy(source, "2026-08-01")
    else:
        inspection = import_legacy(source, "2026-08-01")
        assert inspection.transaction_count == 2

    assert list(get_paths().runtime_dir.glob("better-money-legacy-*"))


def test_image_path_uses_verified_snapshot_after_source_image_is_deleted(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "post-manifest-delete" / "data")
    images = source / "images"
    images.mkdir()
    image = images / "receipt.png"
    image.write_bytes(b"verified-copy")
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute("UPDATE summaries SET image_path = 'images/receipt.png'")
    real_copy_directory = migration_module._copy_stable_directory

    def copy_then_delete_source(source_path, destination_path, *, skip_links):
        result = real_copy_directory(
            source_path, destination_path, skip_links=skip_links
        )
        if Path(source_path) == images:
            image.unlink()
        return result

    monkeypatch.setattr(
        migration_module, "_copy_stable_directory", copy_then_delete_source
    )

    inspection = import_legacy(source, "2026-08-01")

    assert inspection.cleared_image_paths == ()
    imported = get_paths().images_dir / "receipt.png"
    assert imported.read_bytes() == b"verified-copy"
    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert conn.execute("SELECT image_path FROM summaries").fetchone()[0] == str(
            imported
        )


def test_image_copy_detects_file_identity_replacement_during_open(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "identity-swap" / "data")
    images = source / "images"
    images.mkdir()
    image = images / "receipt.png"
    replacement = images / "replacement.tmp"
    image.write_bytes(b"approved")
    replacement.write_bytes(b"different")
    real_open = migration_module.os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == image and not swapped:
            swapped = True
            os.replace(replacement, image)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(migration_module.os, "open", swap_before_open)

    with pytest.raises(RuntimeError, match="changed|close the old application"):
        inspect_legacy(source)


def test_journal_temp_unlink_failure_does_not_mask_publish_error(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    rollback = tmp_path / "journal-unlink"
    rollback.mkdir()
    real_unlink = Path.unlink

    def fail_temp_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(".journal-"):
            raise OSError("temporary unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        migration_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("publish failed")),
    )
    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)

    with pytest.raises(OSError, match="publish failed"):
        migration_module._write_journal(rollback, {"phase": "test"})


def test_manifest_failure_does_not_leave_rollback_directory(tmp_path, monkeypatch):
    from app import legacy_migration as migration_module

    paths = get_paths()
    staging = tmp_path / "manifest-staging"
    (staging / "data" / "images").mkdir(parents=True)
    (staging / "data" / "better_money.db").write_bytes(b"staged")
    (staging / "data" / "config.json").write_text("{}", encoding="utf-8")
    before = set(paths.runtime_dir.glob("legacy-rollback-*"))
    monkeypatch.setattr(
        migration_module,
        "_path_manifest",
        lambda path: (_ for _ in ()).throw(ValueError("manifest failed")),
    )

    with pytest.raises(ValueError, match="manifest failed"):
        migration_module._install_replacements(staging, None)

    assert set(paths.runtime_dir.glob("legacy-rollback-*")) == before


def test_recovery_incomplete_journal_failure_reports_rollback_and_install_error(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    _seed_live("journal-diagnostic-live")
    paths = get_paths()
    source = _seed_legacy(tmp_path / "journal-diagnostic" / "data")
    real_replace = migration_module.os.replace
    real_write_journal = migration_module._write_journal
    installation_failed = False

    def fail_install_and_recovery(source_path, destination_path):
        nonlocal installation_failed
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if destination_path == paths.config_path and not installation_failed:
            installation_failed = True
            raise OSError("primary install error")
        if (
            installation_failed
            and destination_path == paths.db_path
            and "legacy-rollback-" in str(source_path)
        ):
            raise OSError("database recovery error")
        return real_replace(source_path, destination_path)

    def fail_incomplete_journal(rollback, journal):
        if journal.get("phase") == "recovery-incomplete":
            raise OSError("final journal publish error")
        return real_write_journal(rollback, journal)

    monkeypatch.setattr(migration_module.os, "replace", fail_install_and_recovery)
    monkeypatch.setattr(migration_module, "_write_journal", fail_incomplete_journal)

    with pytest.raises(RuntimeError) as raised:
        import_legacy(source, "2026-08-01")

    message = str(raised.value)
    assert "legacy-rollback-" in message
    assert "primary install error" in message


def test_get_conn_initialization_and_close_failure_quarantines_until_retry(
    monkeypatch,
):
    from app import db as db_module

    real_connect = db_module.sqlite3.connect
    created = []

    def broken_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.execute = lambda *execute_args, **execute_kwargs: (
            (_ for _ in ()).throw(sqlite3.OperationalError("pragma failed"))
        )
        connection._close_underlying = lambda: (
            (_ for _ in ()).throw(OSError("initial close failed"))
        )
        created.append(connection)
        return connection

    monkeypatch.setattr(db_module.sqlite3, "connect", broken_connect)

    with pytest.raises(RuntimeError, match="quarantined"):
        db_module.get_conn()

    entered = threading.Event()

    def try_exclusive():
        with db_module.LEDGER_GATE.exclusive():
            entered.set()

    waiter = threading.Thread(target=try_exclusive)
    waiter.start()
    assert not entered.wait(0.2)
    created[0]._close_underlying = lambda: sqlite3.Connection.close(created[0])
    assert db_module.retry_quarantined_connections() == 0
    waiter.join(2)

    assert entered.is_set()
    assert not waiter.is_alive()


def test_image_copy_rejects_symlink_swap_without_reading_target(
    tmp_path, monkeypatch
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(tmp_path / "symlink-swap" / "data")
    images = source / "images"
    images.mkdir()
    image = images / "receipt.png"
    external = tmp_path / "external-secret.png"
    image.write_bytes(b"approved")
    external.write_bytes(b"must-not-copy")
    capability = tmp_path / "symlink-capability"
    try:
        capability.symlink_to(external)
        capability.unlink()
    except OSError as exc:
        pytest.skip(f"image symlink swap is unavailable: {exc}")
    real_open = migration_module.os.open
    swapped = False

    def swap_to_link_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == image and not swapped:
            swapped = True
            image.unlink()
            image.symlink_to(external)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(migration_module.os, "open", swap_to_link_before_open)

    with pytest.raises((ValueError, RuntimeError), match="linked|changed|close"):
        inspect_legacy(source)


def test_v0_schema_with_sql_comments_migrates_to_canonical_current_schema(
    tmp_path,
):
    source = tmp_path / "commented-v0" / "data"
    source.mkdir(parents=True)
    commented_adjustments = """
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- legacy inline comment
    date TEXT NOT NULL,
    diff REAL NOT NULL /* legacy block comment */,
    note TEXT DEFAULT '-- preserved string /* marker */',
    created_at TEXT NOT NULL
);
"""
    canonical_adjustments = """
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    diff REAL NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""
    legacy_schema = BASE_SCHEMA.replace(
        canonical_adjustments,
        commented_adjustments.replace(
            "DEFAULT '-- preserved string /* marker */'", "DEFAULT ''"
        ),
    )
    assert legacy_schema != BASE_SCHEMA
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-01', 1, '收入', '其他', 'commented-v0', 'now', 'now')"
        )
    (source / "config.json").write_text(
        json.dumps({"initial_balance": 0}), encoding="utf-8"
    )

    import_legacy(source, "2026-08-01")

    assert _transaction_notes(get_paths().db_path) == ["commented-v0"]


def test_schema_sql_comment_stripping_preserves_quoted_comment_markers():
    from app import legacy_migration as migration_module

    normalized = migration_module._normalize_schema_sql(
        "CREATE TABLE [--name] (value TEXT DEFAULT '/* keep */', "
        'quoted TEXT DEFAULT "-- keep"); -- remove\n/* remove */'
    )

    assert "[--name]" in normalized
    assert "'/* keep */'" in normalized
    assert '"-- keep"' in normalized
    assert "remove" not in normalized
    assert migration_module._normalize_schema_sql(
        "CREATE TABLE sample(value TEXT/*separator*/NOT NULL)"
    ) == migration_module._normalize_schema_sql(
        "CREATE TABLE sample(value TEXT NOT NULL)"
    )


def test_harmless_extra_table_trigger_and_view_are_allowed(tmp_path):
    source = _seed_legacy(tmp_path / "harmless-auxiliary" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE transactions_archive(id INTEGER PRIMARY KEY, note TEXT);
            CREATE TRIGGER archive_cleanup AFTER DELETE ON transactions_archive
            BEGIN DELETE FROM transactions_archive WHERE id = OLD.id; END;
            CREATE VIEW transactions_archive_view AS
            SELECT id, note FROM transactions_archive;
            """
        )
        conn.commit()

    import_legacy(source, "2026-08-01")

    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        ).fetchone()[0] == "transactions_archive_view"


@pytest.mark.parametrize(
    "auxiliary_sql",
    [
        "CREATE TRIGGER unsafe_extra AFTER INSERT ON extra_notes "
        "BEGIN UPDATE [transactions] SET note = note WHERE id = NEW.id; END",
        "CREATE VIEW unsafe_extra_view AS SELECT id FROM `transactions`",
    ],
    ids=["extra-owner-trigger-references-core", "extra-view-references-core"],
)
def test_extra_auxiliary_objects_referencing_required_tables_are_rejected(
    tmp_path, auxiliary_sql
):
    source = _seed_legacy(tmp_path / "unsafe-auxiliary" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute("CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute(auxiliary_sql)
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|trigger|view|auxiliary"):
        import_legacy(source, "2026-08-01")


def test_image_reference_uses_manifest_casing_under_windows_semantics(tmp_path):
    source = _seed_legacy(tmp_path / "image-case" / "data")
    images = source / "images"
    images.mkdir()
    (images / "Receipt.PNG").write_bytes(b"case-preserved")
    with closing(sqlite3.connect(source / "better_money.db")) as conn, conn:
        conn.execute("UPDATE summaries SET image_path = 'images/receipt.png'")

    inspection = import_legacy(source, "2026-08-01")

    expected = get_paths().images_dir / "Receipt.PNG"
    assert expected.read_bytes() == b"case-preserved"
    assert inspection.cleared_image_paths == ()
    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert conn.execute("SELECT image_path FROM summaries").fetchone()[0] == str(
            expected
        )


def test_windows_colliding_image_manifest_paths_are_rejected():
    from app import legacy_migration as migration_module

    with pytest.raises(ValueError, match="Windows|collision"):
        migration_module._windows_image_manifest(
            frozenset({"Receipt.PNG", "receipt.png"})
        )


@pytest.mark.parametrize(
    "auxiliary_sql",
    [
        "CREATE VIEW extra_v AS SELECT id FROM 'transactions'",
        "CREATE TRIGGER extra_t AFTER INSERT ON extra_notes "
        "BEGIN UPDATE 'transactions' SET note = note WHERE id = NEW.id; END",
    ],
    ids=["single-quoted-view-table", "single-quoted-trigger-update"],
)
def test_single_quoted_required_table_references_are_rejected(
    tmp_path, auxiliary_sql
):
    source = _seed_legacy(tmp_path / "single-quoted-core" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            "CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, "
            "transactions TEXT, note TEXT)"
        )
        conn.execute(auxiliary_sql)
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger|view"):
        import_legacy(source, "2026-08-01")


@pytest.mark.parametrize(
    "select_expression",
    [
        "'transactions' AS label",
        "id AS transactions",
        "transactions",
        "id IS DISTINCT FROM transactions",
    ],
    ids=["string-literal", "alias", "column-name", "is-distinct-from"],
)
def test_harmless_required_table_words_outside_table_positions_are_allowed(
    tmp_path, select_expression
):
    source = _seed_legacy(tmp_path / "harmless-core-word" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            "CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, transactions TEXT)"
        )
        conn.execute(
            f"CREATE VIEW extra_v AS SELECT {select_expression} FROM extra_notes"
        )
        conn.commit()

    import_legacy(source, "2026-08-01")

    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'extra_v'"
        ).fetchone()[0] == "extra_v"


@pytest.mark.parametrize(
    "auxiliary_sql",
    [
        "CREATE VIEW grouped_from AS SELECT id FROM (transactions)",
        "CREATE VIEW grouped_join AS SELECT e.id FROM extra_notes AS e "
        "JOIN (transactions) AS t ON t.id = e.id",
        "CREATE TRIGGER grouped_trigger AFTER INSERT ON extra_notes "
        "BEGIN SELECT id FROM (transactions); END",
        "CREATE VIEW nested_group AS SELECT id FROM ((transactions))",
        "CREATE VIEW single_group AS SELECT id FROM ('transactions')",
        'CREATE VIEW double_group AS SELECT id FROM (main."transactions")',
        "CREATE VIEW backtick_group AS SELECT id FROM (main.`transactions`)",
        "CREATE VIEW bracket_group AS SELECT id FROM (main.[transactions])",
        "CREATE VIEW derived_group AS "
        "SELECT id FROM (SELECT id FROM transactions) AS nested",
        "CREATE VIEW comma_group AS SELECT transactions.id "
        "FROM (transactions, extra_notes)",
        "CREATE VIEW join_chain_group AS SELECT transactions.id "
        "FROM (transactions JOIN extra_notes "
        "ON transactions.id = extra_notes.id)",
        "CREATE TRIGGER comma_group_trigger AFTER INSERT ON extra_notes "
        "BEGIN SELECT transactions.id FROM (transactions, extra_notes); END",
        "CREATE TRIGGER join_group_trigger AFTER INSERT ON extra_notes "
        "BEGIN SELECT transactions.id FROM (transactions JOIN extra_notes "
        "ON transactions.id = extra_notes.id); END",
        'CREATE VIEW nested_multi_group AS SELECT transactions.id '
        'FROM ((main."transactions", main.[extra_notes]))',
        "CREATE VIEW not_indexed_group AS "
        "SELECT id FROM (transactions NOT INDEXED)",
        "CREATE VIEW indexed_group AS SELECT id "
        "FROM (summaries INDEXED BY uq_summaries_period_range)",
        "CREATE VIEW bare_alias_indexed_group AS SELECT summary.id "
        "FROM (summaries summary INDEXED BY uq_summaries_period_range)",
        "CREATE VIEW as_alias_indexed_group AS SELECT summary.id "
        "FROM (summaries AS summary INDEXED BY uq_summaries_period_range)",
        'CREATE VIEW keyword_alias_group AS SELECT "join".id '
        'FROM (transactions AS "join")',
    ],
    ids=[
        "view-from-group",
        "view-join-group",
        "extra-owner-trigger-from-group",
        "nested-group",
        "single-quoted-group",
        "double-quoted-schema-group",
        "backtick-schema-group",
        "bracket-schema-group",
        "derived-required-table",
        "comma-table-source-list",
        "join-chain",
        "extra-owner-trigger-comma-list",
        "extra-owner-trigger-join-chain",
        "nested-quoted-schema-list",
        "not-indexed-suffix",
        "indexed-by-suffix",
        "bare-alias-indexed-by",
        "as-alias-indexed-by",
        "quoted-keyword-alias",
    ],
)
def test_parenthesized_required_table_dependencies_are_rejected(
    tmp_path, auxiliary_sql
):
    source = _seed_legacy(tmp_path / "parenthesized-core" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute("CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute(auxiliary_sql)
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger|view"):
        import_legacy(source, "2026-08-01")


def test_parenthesized_harmless_table_sources_are_allowed(tmp_path):
    source = _seed_legacy(tmp_path / "parenthesized-extra" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            "CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, transactions TEXT)"
        )
        conn.executescript(
            """
            CREATE VIEW grouped_extra AS SELECT id FROM (extra_notes);
            CREATE VIEW derived_extra AS
                SELECT id FROM (SELECT id FROM extra_notes) AS nested;
            CREATE VIEW aliased_extra AS
                SELECT 'transactions' AS label
                FROM (extra_notes) AS transactions;
            CREATE VIEW function_extra AS
                SELECT value FROM json_each('["transactions"]');
            """
        )
        conn.commit()

    import_legacy(source, "2026-08-01")

    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        assert {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            )
        } >= {
            "grouped_extra", "derived_extra", "aliased_extra", "function_extra"
        }


@pytest.mark.parametrize(
    "event_clause",
    [
        "AFTER INSERT",
        "AFTER UPDATE OF note",
        "AFTER DELETE",
        "AFTER UPDATE OF rowid",
        "AFTER UPDATE OF oid",
        "AFTER UPDATE OF _rowid_",
    ],
    ids=[
        "insert",
        "update-of",
        "delete",
        "update-of-rowid",
        "update-of-oid",
        "update-of-underscore-rowid",
    ],
)
def test_extra_owner_trigger_events_referencing_required_table_are_rejected(
    tmp_path, event_clause
):
    source = _seed_legacy(tmp_path / "trigger-events" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute("CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute(
            f"CREATE TRIGGER unsafe_event {event_clause} ON extra_notes "
            "BEGIN SELECT id FROM transactions; END"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger|view"):
        import_legacy(source, "2026-08-01")


def test_indirect_extra_view_access_to_required_table_is_rejected(tmp_path):
    source = _seed_legacy(tmp_path / "indirect-view" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE VIEW required_proxy AS SELECT id FROM transactions;
            CREATE VIEW indirect_required AS SELECT id FROM required_proxy;
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger|view"):
        import_legacy(source, "2026-08-01")


class _TrackingAuthorizerConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authorizer_callbacks = []
        self.was_closed = False

    def set_authorizer(self, callback):
        self.authorizer_callbacks.append(callback)
        return super().set_authorizer(callback)

    def close(self):
        self.was_closed = True
        return super().close()


def test_auxiliary_compilation_preserves_restrictive_caller_authorizer_and_data():
    from app import legacy_migration as migration_module

    with closing(
        sqlite3.connect(":memory:", factory=_TrackingAuthorizerConnection)
    ) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE extra_notes(id INTEGER PRIMARY KEY, note TEXT);
            CREATE TABLE extra_audit(event TEXT);
            INSERT INTO extra_notes VALUES (1, 'unchanged');
            CREATE TRIGGER audit_insert AFTER INSERT ON extra_notes
            BEGIN INSERT INTO extra_audit VALUES ('insert'); END;
            CREATE TRIGGER audit_update AFTER UPDATE OF note ON extra_notes
            BEGIN INSERT INTO extra_audit VALUES ('update'); END;
            CREATE TRIGGER audit_delete AFTER DELETE ON extra_notes
            BEGIN INSERT INTO extra_audit VALUES ('delete'); END;
            CREATE VIEW harmless_expression AS
            SELECT id IS DISTINCT FROM note AS differs FROM extra_notes;
            """
        )
        conn.commit()
        caller_events = []

        def restrictive_authorizer(action, table, column, database, source):
            caller_events.append((action, table, column, database, source))
            if action == sqlite3.SQLITE_READ and table == "extra_notes":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(restrictive_authorizer)

        migration_module._validate_current_schema(conn)

        with pytest.raises(sqlite3.DatabaseError, match="authorized|prohibited"):
            conn.execute("SELECT * FROM extra_notes").fetchall()
        assert caller_events[-1][1] == "extra_notes"
        conn.set_authorizer(None)
        assert conn.execute("SELECT * FROM extra_notes").fetchall() == [
            (1, "unchanged")
        ]
        assert conn.execute("SELECT * FROM extra_audit").fetchall() == []


@pytest.mark.parametrize(
    "view_sql",
    [
        "CREATE VIEW unsafe_extra AS SELECT id FROM transactions",
        "CREATE VIEW broken_extra AS SELECT id FROM missing_extra_table",
    ],
    ids=["required-rejection", "prepare-error"],
)
def test_auxiliary_compilation_preserves_caller_authorizer_after_error(view_sql):
    from app import legacy_migration as migration_module

    with closing(
        sqlite3.connect(":memory:", factory=_TrackingAuthorizerConnection)
    ) as conn:
        migrate_database(conn)
        conn.execute(view_sql)
        caller_events = []

        def permissive_authorizer(action, table, column, database, source):
            caller_events.append((action, table, column, database, source))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(permissive_authorizer)

        with pytest.raises(RuntimeError, match="schema|auxiliary|view"):
            migration_module._validate_current_schema(conn)

        events_before_select = len(caller_events)
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        assert len(caller_events) > events_before_select


def test_temp_auxiliary_shadow_does_not_hide_unsafe_main_view():
    from app import legacy_migration as migration_module

    with closing(sqlite3.connect(":memory:")) as conn:
        migrate_database(conn)
        conn.execute("CREATE VIEW unsafe_extra AS SELECT id FROM transactions")
        conn.execute("CREATE TEMP VIEW unsafe_extra AS SELECT 1 AS id")

        with pytest.raises(RuntimeError, match="schema|auxiliary|view"):
            migration_module._validate_current_schema(conn)


@pytest.mark.parametrize(
    "view_sql",
    [
        "CREATE VIEW harmless_extra AS SELECT 1 AS id",
        "CREATE VIEW unsafe_extra AS SELECT id FROM transactions",
        "CREATE VIEW broken_extra AS SELECT id FROM missing_extra_table",
    ],
    ids=["success", "required-rejection", "prepare-error"],
)
def test_private_auxiliary_clone_clears_authorizer_and_closes(
    monkeypatch, view_sql
):
    from app import legacy_migration as migration_module

    real_connect = sqlite3.connect
    caller = real_connect(":memory:")
    tracked_connections = []

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", _TrackingAuthorizerConnection)
        tracked = real_connect(*args, **kwargs)
        tracked_connections.append(tracked)
        return tracked

    try:
        migrate_database(caller)
        caller.execute(view_sql)
        monkeypatch.setattr(migration_module.sqlite3, "connect", tracking_connect)

        if "harmless" in view_sql:
            migration_module._validate_current_schema(caller)
        else:
            with pytest.raises(RuntimeError, match="schema|auxiliary|view"):
                migration_module._validate_current_schema(caller)

        compiler_connections = [
            item
            for item in tracked_connections
            if any(callable(callback) for callback in item.authorizer_callbacks)
        ]
        assert compiler_connections
        assert all(
            item.authorizer_callbacks[-1] is None
            for item in compiler_connections
        )
        assert all(item.was_closed for item in compiler_connections)
    finally:
        caller.close()


def test_without_rowid_owner_does_not_gain_hidden_rowid_update_probe(tmp_path):
    source = _seed_legacy(tmp_path / "without-rowid-trigger" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE extra_notes(
                id TEXT PRIMARY KEY,
                note TEXT
            ) WITHOUT ROWID;
            CREATE TRIGGER unreachable_rowid AFTER UPDATE OF rowid ON extra_notes
            BEGIN SELECT id FROM transactions; END;
            """
        )
        conn.commit()

    import_legacy(source, "2026-08-01")


@pytest.mark.parametrize("column_name", ["rowid", "oid", "_rowid_"])
def test_real_rowid_named_column_update_trigger_is_compiled(
    tmp_path, column_name
):
    source = _seed_legacy(tmp_path / "real-rowid-column" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            "CREATE TABLE extra_notes("
            f"id INTEGER PRIMARY KEY, note TEXT, {column_name} TEXT)"
        )
        conn.execute(
            f"CREATE TRIGGER unsafe_alias AFTER UPDATE OF {column_name} "
            "ON extra_notes BEGIN SELECT id FROM transactions; END"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger"):
        import_legacy(source, "2026-08-01")


@pytest.mark.parametrize("rowid_alias", ["rowid", "oid", "_rowid_"])
def test_mixed_case_owner_name_still_gets_hidden_rowid_update_probe(
    tmp_path, rowid_alias
):
    source = _seed_legacy(tmp_path / "mixed-case-rowid-owner" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            "CREATE TABLE Extra_Notes(id INTEGER PRIMARY KEY, note TEXT)"
        )
        conn.execute(
            f"CREATE TRIGGER unsafe_alias AFTER UPDATE OF {rowid_alias} "
            "ON extra_notes BEGIN SELECT id FROM transactions; END"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger"):
        import_legacy(source, "2026-08-01")


def test_quoted_mixed_case_owner_gets_hidden_rowid_update_probe(tmp_path):
    source = _seed_legacy(tmp_path / "quoted-mixed-rowid-owner" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.execute(
            'CREATE TABLE "Quoted_Notes"(id INTEGER PRIMARY KEY, note TEXT)'
        )
        conn.execute(
            'CREATE TRIGGER unsafe_alias AFTER UPDATE OF rowid ON "qUoTeD_nOtEs" '
            "BEGIN SELECT id FROM transactions; END"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger"):
        import_legacy(source, "2026-08-01")


def test_mixed_case_without_rowid_owner_does_not_gain_hidden_alias_probe(
    tmp_path,
):
    source = _seed_legacy(tmp_path / "mixed-without-rowid-owner" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE Extra_Notes(
                id TEXT PRIMARY KEY,
                note TEXT
            ) WITHOUT ROWID;
            CREATE TRIGGER unreachable_alias
            AFTER UPDATE OF rowid ON extra_notes
            BEGIN SELECT id FROM transactions; END;
            """
        )
        conn.commit()

    import_legacy(source, "2026-08-01")


def test_mixed_case_real_rowid_named_columns_remain_real_update_targets(
    tmp_path,
):
    source = _seed_legacy(tmp_path / "mixed-real-rowid-columns" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE Extra_Notes(
                id INTEGER PRIMARY KEY,
                ROWID TEXT,
                Oid TEXT,
                _RoWiD_ TEXT
            );
            CREATE TRIGGER unsafe_real_alias
            AFTER UPDATE OF rowid, oid, _rowid_ ON extra_notes
            BEGIN SELECT id FROM transactions; END;
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger"):
        import_legacy(source, "2026-08-01")


@pytest.mark.parametrize("rowid_alias", ["rowid", "oid", "_rowid_"])
def test_nonascii_distinct_owner_does_not_hide_ascii_rowid_table_probe(
    tmp_path, rowid_alias
):
    source = _seed_legacy(tmp_path / "nonascii-distinct-owner" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE STRASSE(id INTEGER PRIMARY KEY, note TEXT);
            CREATE TABLE "Straße"(
                id TEXT PRIMARY KEY,
                note TEXT
            ) WITHOUT ROWID;
            """
        )
        conn.execute(
            f"CREATE TRIGGER unsafe_ascii_owner AFTER UPDATE OF {rowid_alias} "
            "ON STRASSE BEGIN SELECT id FROM transactions; END"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="schema|auxiliary|trigger"):
        import_legacy(source, "2026-08-01")


def test_nonascii_without_rowid_owner_remains_distinct_from_ascii_table(
    tmp_path,
):
    source = _seed_legacy(tmp_path / "nonascii-without-rowid" / "data")
    with closing(sqlite3.connect(source / "better_money.db")) as conn:
        migrate_database(conn)
        conn.executescript(
            """
            CREATE TABLE STRASSE(id INTEGER PRIMARY KEY, note TEXT);
            CREATE TABLE "Straße"(
                id TEXT PRIMARY KEY,
                note TEXT
            ) WITHOUT ROWID;
            CREATE TRIGGER unreachable_nonascii_owner
            AFTER UPDATE OF rowid ON "Straße"
            BEGIN SELECT id FROM transactions; END;
            """
        )
        conn.commit()

    import_legacy(source, "2026-08-01")


def test_sqlite_identifier_key_folds_ascii_letters_only():
    from app import legacy_migration as migration_module

    assert migration_module._sqlite_identifier_key("Extra_Notes") == "extra_notes"
    assert migration_module._sqlite_identifier_key("Straße") == "straße"
    assert migration_module._sqlite_identifier_key("Straße") != (
        migration_module._sqlite_identifier_key("STRASSE")
    )
