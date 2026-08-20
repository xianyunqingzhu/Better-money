from __future__ import annotations

from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import zipfile

import pytest

from app import backup as backup_module
from app.backup import (
    BackupManifest,
    create_backup,
    ensure_daily_backup,
    inspect_backup,
    restore_backup,
)
from app.db import init_db
from app.migrations import BASE_SCHEMA, CURRENT_SCHEMA_VERSION
from app.paths import get_paths
from app.version import APP_VERSION


def _seed_live_transaction(note: str) -> None:
    init_db()
    with closing(sqlite3.connect(get_paths().db_path)) as conn, conn:
        conn.execute("DELETE FROM transactions")
        conn.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES (?, 20, '支出', '餐饮', ?, 'now', 'now')",
            ("2026-08-20", note),
        )


def _live_notes() -> list[str]:
    with closing(sqlite3.connect(get_paths().db_path)) as conn:
        return [row[0] for row in conn.execute("SELECT note FROM transactions")]


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    manifest_transform=None,
    database_bytes: bytes | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(source) as source_zip:
        members = {
            name: source_zip.read(name)
            for name in source_zip.namelist()
            if name != "data/better_money.db" or database_bytes is None
        }
    if database_bytes is not None:
        members["data/better_money.db"] = database_bytes
    if manifest_transform is not None:
        manifest = json.loads(members["manifest.json"])
        manifest_transform(manifest)
        members["manifest.json"] = json.dumps(manifest).encode("utf-8")
    members.update(extra_members or {})
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, content in members.items():
            output.writestr(name, content)
    return destination


def _write_valid_archive(
    path: Path,
    database_bytes: bytes,
    *,
    reason: str,
    created_at: datetime,
) -> None:
    manifest = {
        "format_version": 1,
        "created_at": created_at.isoformat(),
        "app_version": APP_VERSION,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "reason": reason,
        "includes_images": False,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data/better_money.db", database_bytes)
        archive.writestr("data/config.json", "{}")


def test_backup_contains_database_manifest_and_sanitized_config(app_home):
    _seed_live_transaction("archived")
    get_paths().config_path.write_text(
        json.dumps(
            {
                "api_key": "secret",
                "nested": {"api_key": "nested-secret", "kept": True},
                "tone": "朋友",
            }
        ),
        encoding="utf-8",
    )

    archive = create_backup("manual")

    with zipfile.ZipFile(archive) as zipped:
        assert {
            "manifest.json",
            "data/better_money.db",
            "data/config.json",
        } <= set(zipped.namelist())
        config = json.loads(zipped.read("data/config.json"))
        manifest = json.loads(zipped.read("manifest.json"))
        archived_db = app_home / "archived.db"
        archived_db.write_bytes(zipped.read("data/better_money.db"))
    assert "api_key" not in config
    assert "api_key" not in config["nested"]
    assert b"secret" not in archive.read_bytes()
    assert manifest == {
        "format_version": 1,
        "created_at": manifest["created_at"],
        "app_version": APP_VERSION,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "reason": "manual",
        "includes_images": False,
    }
    with closing(sqlite3.connect(archived_db)) as conn:
        assert conn.execute("SELECT note FROM transactions").fetchone()[0] == "archived"


def test_inspect_backup_returns_a_frozen_validated_manifest(app_home):
    _seed_live_transaction("inspectable")
    manifest = inspect_backup(create_backup("manual"))

    assert isinstance(manifest, BackupManifest)
    assert manifest.reason == "manual"
    assert datetime.fromisoformat(manifest.created_at).tzinfo is not None
    with pytest.raises(FrozenInstanceError):
        manifest.reason = "changed"


@pytest.mark.parametrize(
    "missing_field",
    [
        "format_version",
        "created_at",
        "app_version",
        "schema_version",
        "reason",
        "includes_images",
    ],
)
def test_inspect_backup_rejects_any_missing_manifest_field(app_home, missing_field):
    _seed_live_transaction("complete-manifest")
    source = create_backup("manual")
    invalid = app_home / f"missing-{missing_field}.zip"

    def remove_field(manifest):
        manifest.pop(missing_field)

    _rewrite_archive(source, invalid, manifest_transform=remove_field)

    with pytest.raises(ValueError, match="manifest"):
        inspect_backup(invalid)


def test_inspect_backup_rejects_schema_version_that_disagrees_with_database(app_home):
    _seed_live_transaction("schema")
    source = create_backup("manual")
    invalid = app_home / "wrong-schema.zip"

    def change_schema(manifest):
        manifest["schema_version"] = CURRENT_SCHEMA_VERSION - 1

    _rewrite_archive(source, invalid, manifest_transform=change_schema)

    with pytest.raises(ValueError, match="schema"):
        inspect_backup(invalid)


def test_backup_includes_images_only_when_requested(app_home):
    _seed_live_transaction("images")
    nested = get_paths().images_dir / "receipts"
    nested.mkdir(parents=True)
    (nested / "one.txt").write_text("receipt", encoding="utf-8")

    without_images = create_backup("manual-no-images")
    with_images = create_backup("manual-with-images", include_images=True)

    with zipfile.ZipFile(without_images) as archive:
        assert not any(name.startswith("data/images/") for name in archive.namelist())
    with zipfile.ZipFile(with_images) as archive:
        assert archive.read("data/images/receipts/one.txt") == b"receipt"
        assert json.loads(archive.read("manifest.json"))["includes_images"] is True


def test_create_backup_rejects_a_reason_that_could_escape_backup_directory(app_home):
    _seed_live_transaction("unsafe-reason")

    with pytest.raises(ValueError, match="reason"):
        create_backup("../outside")

    assert not (app_home.parent / "outside.zip").exists()


def test_restore_valid_archive_replaces_database_and_config_and_creates_safety_backup(
    app_home,
):
    _seed_live_transaction("from-backup")
    get_paths().config_path.write_text(
        json.dumps({"tone": "老师", "api_key": "must-not-return"}),
        encoding="utf-8",
    )
    archive = create_backup("manual")
    _seed_live_transaction("current")
    get_paths().config_path.write_text(json.dumps({"tone": "朋友"}), encoding="utf-8")

    restore_backup(archive)

    assert _live_notes() == ["from-backup"]
    assert json.loads(get_paths().config_path.read_text(encoding="utf-8")) == {
        "tone": "老师"
    }
    safety_backups = [
        candidate
        for candidate in get_paths().backups_dir.glob("*.zip")
        if inspect_backup(candidate).reason == "pre-restore"
    ]
    assert len(safety_backups) == 1
    with zipfile.ZipFile(safety_backups[0]) as safety_zip:
        safety_db = app_home / "safety.db"
        safety_db.write_bytes(safety_zip.read("data/better_money.db"))
    with closing(sqlite3.connect(safety_db)) as conn:
        assert conn.execute("SELECT note FROM transactions").fetchone()[0] == "current"


def test_restore_rejects_corrupt_database_without_changing_current_database(app_home):
    _seed_live_transaction("archive-source")
    valid = create_backup("manual")
    corrupt = _rewrite_archive(
        valid,
        app_home / "corrupt.zip",
        database_bytes=b"not a sqlite database",
    )
    _seed_live_transaction("current-safe")
    before = get_paths().db_path.read_bytes()

    with pytest.raises((ValueError, sqlite3.DatabaseError, RuntimeError)):
        restore_backup(corrupt)

    assert get_paths().db_path.read_bytes() == before
    assert _live_notes() == ["current-safe"]


def test_restore_rejects_unsafe_zip_member_without_writing_it(app_home):
    _seed_live_transaction("archive-source")
    valid = create_backup("manual")
    malicious = _rewrite_archive(
        valid,
        app_home / "malicious.zip",
        extra_members={"../escape.txt": b"escaped"},
    )
    _seed_live_transaction("current-safe")

    with pytest.raises(ValueError, match="unsafe|member"):
        restore_backup(malicious)

    assert _live_notes() == ["current-safe"]
    assert not (app_home.parent / "escape.txt").exists()


@pytest.mark.parametrize(
    "unsafe_leaf",
    [
        "{drive}:../config.json",
        "receipt.txt:secret",
        "CON.txt",
        "receipt.txt.",
        "receipt.txt ",
    ],
)
def test_inspect_rejects_windows_alias_and_device_image_members(
    app_home, unsafe_leaf
):
    _seed_live_transaction("windows-path")
    valid = create_backup("manual", include_images=True)
    drive = Path(app_home).drive.rstrip(":") or "C"
    malicious = _rewrite_archive(
        valid,
        app_home / "windows-alias.zip",
        extra_members={
            f"data/images/{unsafe_leaf.format(drive=drive)}": b"malicious"
        },
    )

    with pytest.raises(ValueError, match="unsafe|member|Windows"):
        inspect_backup(malicious)


def test_inspect_rejects_members_with_duplicate_windows_targets(app_home):
    _seed_live_transaction("duplicate-target")
    (get_paths().images_dir / "receipt.txt").write_text("trusted", encoding="utf-8")
    valid = create_backup("manual", include_images=True)
    duplicate = _rewrite_archive(
        valid,
        app_home / "duplicate-target.zip",
        extra_members={"data/images/RECEIPT.TXT": b"replacement"},
    )

    with pytest.raises(ValueError, match="duplicate"):
        inspect_backup(duplicate)


def test_restore_uses_the_same_private_archive_snapshot_after_inspection(
    app_home, monkeypatch
):
    _seed_live_transaction("trusted-snapshot")
    trusted = create_backup("trusted")
    _seed_live_transaction("replacement-source")
    replacement = create_backup("replacement")
    supplied_path = app_home / "supplied.zip"
    shutil.copy2(trusted, supplied_path)
    replacement_bytes = replacement.read_bytes()
    _seed_live_transaction("current-safe")
    real_inspect = backup_module.inspect_backup
    swapped = False

    def inspect_then_replace_supplied_path(path):
        nonlocal swapped
        manifest = real_inspect(path)
        if not swapped:
            supplied_path.write_bytes(replacement_bytes)
            swapped = True
        return manifest

    monkeypatch.setattr(backup_module, "inspect_backup", inspect_then_replace_supplied_path)

    restore_backup(supplied_path)

    assert swapped is True
    assert _live_notes() == ["trusted-snapshot"]


def test_restore_rechecks_extracted_database_schema_against_inspected_manifest(
    app_home, monkeypatch
):
    _seed_live_transaction("trusted-schema")
    archive = create_backup("manual")
    legacy_db = app_home / "legacy-v1.db"
    with closing(sqlite3.connect(legacy_db)) as conn, conn:
        conn.executescript(BASE_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
    _seed_live_transaction("current-safe")
    before = get_paths().db_path.read_bytes()
    real_extract = backup_module._extract_declared_members

    def extract_then_substitute_database(source, staging, manifest):
        real_extract(source, staging, manifest)
        shutil.copy2(legacy_db, staging / "data" / "better_money.db")

    monkeypatch.setattr(
        backup_module,
        "_extract_declared_members",
        extract_then_substitute_database,
    )

    with pytest.raises(ValueError, match="schema"):
        restore_backup(archive)

    assert get_paths().db_path.read_bytes() == before
    assert _live_notes() == ["current-safe"]


def test_restore_rolls_back_database_and_config_when_final_replace_fails(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    get_paths().config_path.write_text(json.dumps({"tone": "老师"}), encoding="utf-8")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    get_paths().config_path.write_text(json.dumps({"tone": "朋友"}), encoding="utf-8")
    db_before = get_paths().db_path.read_bytes()
    config_before = get_paths().config_path.read_bytes()
    real_replace = backup_module.os.replace
    failed = False

    def fail_installing_config(source, destination):
        nonlocal failed
        if Path(destination) == get_paths().config_path and not failed:
            failed = True
            raise OSError("simulated config replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", fail_installing_config)

    with pytest.raises(OSError, match="simulated"):
        restore_backup(archive)

    assert get_paths().db_path.read_bytes() == db_before
    assert get_paths().config_path.read_bytes() == config_before
    assert _live_notes() == ["current-safe"]


def test_restore_without_images_leaves_current_images_untouched(app_home):
    _seed_live_transaction("source")
    (get_paths().images_dir / "same.txt").write_text("archived", encoding="utf-8")
    archive = create_backup("manual")
    (get_paths().images_dir / "same.txt").write_text("current", encoding="utf-8")
    (get_paths().images_dir / "current-only.txt").write_text("keep", encoding="utf-8")

    restore_backup(archive)

    assert (get_paths().images_dir / "same.txt").read_text(encoding="utf-8") == "current"
    assert (get_paths().images_dir / "current-only.txt").read_text(encoding="utf-8") == "keep"


def test_restore_with_images_replaces_the_complete_image_directory(app_home):
    _seed_live_transaction("source")
    (get_paths().images_dir / "archived.txt").write_text("archived", encoding="utf-8")
    archive = create_backup("manual", include_images=True)
    (get_paths().images_dir / "archived.txt").write_text("changed", encoding="utf-8")
    (get_paths().images_dir / "current-only.txt").write_text("remove", encoding="utf-8")

    restore_backup(archive)

    assert (get_paths().images_dir / "archived.txt").read_text(encoding="utf-8") == "archived"
    assert not (get_paths().images_dir / "current-only.txt").exists()


def test_ensure_daily_backup_creates_only_one_valid_automatic_archive_per_day(app_home):
    _seed_live_transaction("automatic")

    first = ensure_daily_backup()
    second = ensure_daily_backup()

    assert first is not None
    assert second is None
    automatic = [
        archive
        for archive in get_paths().backups_dir.glob("*.zip")
        if inspect_backup(archive).reason == "automatic"
    ]
    assert automatic == [first]


def test_automatic_retention_only_removes_verified_automatic_zip_archives(app_home):
    _seed_live_transaction("retention")
    database_bytes = get_paths().db_path.read_bytes()
    backups_dir = get_paths().backups_dir
    old_automatic = []
    base = datetime.now().astimezone() - timedelta(days=20)
    for index in range(4):
        archive = backups_dir / f"old-auto-{index}.zip"
        _write_valid_archive(
            archive,
            database_bytes,
            reason="automatic",
            created_at=base + timedelta(days=index),
        )
        old_automatic.append(archive)
    manual = backups_dir / "manual.zip"
    pre_operation = backups_dir / "pre-restore.zip"
    _write_valid_archive(manual, database_bytes, reason="manual", created_at=base)
    _write_valid_archive(
        pre_operation, database_bytes, reason="pre-restore", created_at=base
    )
    raw_pre_migration = backups_dir / "pre-migration-v1-to-v2.db"
    raw_pre_migration.write_bytes(database_bytes)
    invalid_zip = backups_dir / "invalid-automatic.zip"
    invalid_zip.write_bytes(b"not a zip")

    created = ensure_daily_backup(keep=2)

    assert created is not None
    remaining_automatic = [
        archive
        for archive in backups_dir.glob("*.zip")
        if archive != invalid_zip
        and inspect_backup(archive).reason == "automatic"
    ]
    assert len(remaining_automatic) == 2
    assert created in remaining_automatic
    assert old_automatic[-1] in remaining_automatic
    assert manual.exists()
    assert pre_operation.exists()
    assert raw_pre_migration.exists()
    assert invalid_zip.exists()


def test_m7_backup_policy_check_runs_independently_in_isolated_home(app_home):
    _seed_live_transaction("m7-policy")
    script = Path(__file__).with_name("test_m7.py")

    namespace = runpy.run_path(str(script))
    namespace["check_backup_policy"]()

    assert get_paths().root == app_home
