from __future__ import annotations

from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
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


def _backup_notes(archive: Path, destination: Path) -> list[str]:
    with zipfile.ZipFile(archive) as zipped:
        with zipped.open("data/better_money.db") as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    with closing(sqlite3.connect(destination)) as connection:
        return [row[0] for row in connection.execute("SELECT note FROM transactions")]


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


def _as_zip64(contents: bytes, *, extensible_data: bytes = b"") -> bytes:
    eocd_offset = contents.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    eocd = bytearray(contents[eocd_offset:])
    entries = struct.unpack_from("<H", eocd, 10)[0]
    directory_size = struct.unpack_from("<I", eocd, 12)[0]
    directory_offset = struct.unpack_from("<I", eocd, 16)[0]
    zip64_record = struct.pack(
        "<4sQ2H2I4Q",
        b"PK\x06\x06",
        44 + len(extensible_data),
        45,
        45,
        0,
        0,
        entries,
        entries,
        directory_size,
        directory_offset,
    ) + extensible_data
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd_offset, 1)
    struct.pack_into("<H", eocd, 8, 0xFFFF)
    struct.pack_into("<H", eocd, 10, 0xFFFF)
    struct.pack_into("<I", eocd, 12, 0xFFFFFFFF)
    struct.pack_into("<I", eocd, 16, 0xFFFFFFFF)
    return contents[:eocd_offset] + zip64_record + locator + bytes(eocd)


def _write_valid_archive(
    path: Path,
    database_bytes: bytes,
    *,
    reason: str,
    created_at: datetime,
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> None:
    manifest = {
        "format_version": 1,
        "created_at": created_at.isoformat(),
        "app_version": APP_VERSION,
        "schema_version": schema_version,
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


@pytest.mark.parametrize("mutation", ["empty", "missing-index"])
def test_inspect_rejects_current_version_database_without_canonical_schema(
    app_home, mutation
):
    database = app_home / f"noncanonical-{mutation}.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        if mutation == "missing-index":
            from app.migrations import migrate_database

            migrate_database(connection)
            connection.execute("DROP INDEX idx_adjustments_reverses")
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    archive = app_home / f"noncanonical-{mutation}.zip"
    _write_valid_archive(
        archive,
        database.read_bytes(),
        reason="manual",
        created_at=datetime.now().astimezone(),
    )

    with pytest.raises(backup_module.InvalidBackupError, match="schema|table|index"):
        inspect_backup(archive)


def test_restore_revalidates_extracted_current_database_with_canonical_validator(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    before = get_paths().db_path.read_bytes()
    real_extract = backup_module._extract_declared_members

    def extract_then_remove_required_index(source, staging, manifest):
        real_extract(source, staging, manifest)
        database = staging / "data" / "better_money.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP INDEX idx_adjustments_reverses")

    monkeypatch.setattr(
        backup_module,
        "_extract_declared_members",
        extract_then_remove_required_index,
    )

    with pytest.raises(backup_module.InvalidBackupError, match="schema|index"):
        restore_backup(archive)

    assert get_paths().db_path.read_bytes() == before
    assert _live_notes() == ["current-safe"]


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


def test_inspect_streams_database_member_instead_of_reading_it(app_home, monkeypatch):
    _seed_live_transaction("streamed-inspection")
    archive = create_backup("manual")
    real_read = zipfile.ZipFile.read

    def reject_database_read(zipped, name, *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member_name == "data/better_money.db":
            raise AssertionError("database member must be streamed")
        return real_read(zipped, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_database_read)

    assert inspect_backup(archive).reason == "manual"


def test_inspect_rejects_member_count_from_metadata_without_large_payload(
    app_home, monkeypatch
):
    _seed_live_transaction("member-limit")
    valid = create_backup("manual", include_images=True)
    limited = _rewrite_archive(
        valid,
        app_home / "too-many-members.zip",
        extra_members={"data/images/tiny.txt": b"x"},
    )
    monkeypatch.setattr(backup_module, "MAX_ARCHIVE_MEMBERS", 3)

    with pytest.raises(ValueError, match="member|limit"):
        inspect_backup(limited)


def test_inspect_rejects_excessive_eocd_count_before_zipfile_construction(
    app_home, monkeypatch
):
    archive = app_home / "too-many-empty-members.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(4097):
            zipped.writestr(f"empty-{index}.txt", b"")

    def forbidden_zipfile_construction(*args, **kwargs):
        raise AssertionError("ZipFile parsed an archive rejected by EOCD preflight")

    monkeypatch.setattr(
        backup_module.zipfile, "ZipFile", forbidden_zipfile_construction
    )

    with pytest.raises(ValueError, match="member count"):
        inspect_backup(archive)


def test_inspect_rejects_oversize_central_directory_before_zipfile_construction(
    app_home, monkeypatch
):
    _seed_live_transaction("central-directory-size")
    valid = create_backup("manual")
    contents = bytearray(valid.read_bytes())
    eocd = contents.rfind(b"PK\x05\x06")
    assert eocd >= 0
    struct.pack_into("<I", contents, eocd + 12, 64 * 1024 * 1024)
    archive = app_home / "oversize-central-directory.zip"
    archive.write_bytes(contents)

    def forbidden_zipfile_construction(*args, **kwargs):
        raise AssertionError("ZipFile parsed an oversized central directory")

    monkeypatch.setattr(
        backup_module.zipfile, "ZipFile", forbidden_zipfile_construction
    )

    with pytest.raises(ValueError, match="central directory.*size|size.*central"):
        inspect_backup(archive)


def test_inspect_rejects_malformed_central_directory_before_zipfile_construction(
    app_home, monkeypatch
):
    _seed_live_transaction("central-directory-structure")
    valid = create_backup("manual")
    contents = bytearray(valid.read_bytes())
    eocd = contents.rfind(b"PK\x05\x06")
    assert eocd >= 0
    central_offset = struct.unpack_from("<I", contents, eocd + 16)[0]
    contents[central_offset : central_offset + 4] = b"NOPE"
    archive = app_home / "malformed-central-directory.zip"
    archive.write_bytes(contents)

    def forbidden_zipfile_construction(*args, **kwargs):
        raise AssertionError("ZipFile parsed a malformed central directory")

    monkeypatch.setattr(
        backup_module.zipfile, "ZipFile", forbidden_zipfile_construction
    )

    with pytest.raises(ValueError, match="central directory.*structure"):
        inspect_backup(archive)


def test_inspect_uses_cpython_last_eocd_candidate_before_zipfile_construction(
    app_home, monkeypatch
):
    _seed_live_transaction("comment-eocd-differential")
    valid = create_backup("manual")
    contents = bytearray(valid.read_bytes())
    real_eocd = contents.rfind(b"PK\x05\x06")
    assert real_eocd >= 0
    prefix_size = 18 * 1024 * 1024
    real_directory_offset = struct.unpack_from("<I", contents, real_eocd + 16)[0]
    fake_eocd = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        16_777_217,
        0,
        1,
    )
    struct.pack_into(
        "<I", contents, real_eocd + 16, real_directory_offset + prefix_size
    )
    struct.pack_into("<H", contents, real_eocd + 20, len(fake_eocd))
    archive = app_home / "comment-embedded-fake-eocd.zip"
    with archive.open("wb") as output:
        output.seek(prefix_size - 1)
        output.write(b"\0")
        output.write(contents)
        output.write(fake_eocd)

    with archive.open("rb") as source:
        selected_by_cpython = zipfile._EndRecData(source)
    assert selected_by_cpython is not None
    assert selected_by_cpython[5] == 16_777_217

    def forbidden_zipfile_construction(*args, **kwargs):
        raise AssertionError("ZipFile parsed an EOCD record preflight skipped")

    monkeypatch.setattr(
        backup_module.zipfile, "ZipFile", forbidden_zipfile_construction
    )

    with pytest.raises(ValueError, match="central directory.*size|comment"):
        inspect_backup(archive)


def test_inspect_rejects_zip64_extensible_data_unsupported_by_cpython(
    app_home, monkeypatch
):
    _seed_live_transaction("zip64-extensible-differential")
    valid = create_backup("manual")
    archive = app_home / "unsupported-zip64-extensible.zip"
    archive.write_bytes(_as_zip64(valid.read_bytes(), extensible_data=b"EXTEND!!"))

    with archive.open("rb") as source:
        selected_by_cpython = zipfile._EndRecData(source)
    assert selected_by_cpython is not None
    assert selected_by_cpython[0] == b"PK\x05\x06"
    assert selected_by_cpython[5] == 0xFFFFFFFF

    def forbidden_zipfile_construction(*args, **kwargs):
        raise AssertionError("ZipFile parsed unsupported Zip64 extensible data")

    monkeypatch.setattr(
        backup_module.zipfile, "ZipFile", forbidden_zipfile_construction
    )

    with pytest.raises(ValueError, match="Zip64.*unsupported|Zip64.*invalid"):
        inspect_backup(archive)


def test_inspect_accepts_prefixed_classic_archive(app_home):
    _seed_live_transaction("prefixed-classic")
    valid = create_backup("manual")
    archive = app_home / "prefixed-classic.zip"
    archive.write_bytes(b"BETTER-MONEY-LAUNCHER" + valid.read_bytes())

    assert inspect_backup(archive).reason == "manual"


def test_inspect_accepts_prefixed_zip64_archive(app_home):
    _seed_live_transaction("prefixed-zip64")
    valid = create_backup("manual")
    archive = app_home / "prefixed-zip64.zip"
    archive.write_bytes(
        b"BETTER-MONEY-LAUNCHER" + _as_zip64(valid.read_bytes())
    )

    assert inspect_backup(archive).reason == "manual"


def test_inspect_rejects_inconsistent_central_offset_before_zipfile_construction(
    app_home, monkeypatch
):
    _seed_live_transaction("inconsistent-central-offset")
    valid = create_backup("manual")
    contents = valid.read_bytes()
    eocd_offset = contents.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    archive = app_home / "inconsistent-central-offset.zip"
    archive.write_bytes(
        contents[:eocd_offset] + b"X" * 64 + contents[eocd_offset:]
    )
    with archive.open("rb") as source:
        selected_by_cpython = zipfile._EndRecData(source)
    assert selected_by_cpython is not None
    raw_directory_offset = struct.unpack_from("<I", contents, eocd_offset + 16)[0]
    assert selected_by_cpython[-1] - selected_by_cpython[5] != raw_directory_offset

    def forbidden_zipfile_construction(*args, **kwargs):
        raise AssertionError("ZipFile parsed an inconsistent central offset")

    monkeypatch.setattr(
        backup_module.zipfile, "ZipFile", forbidden_zipfile_construction
    )

    with pytest.raises(ValueError, match="central directory.*structure|offset"):
        inspect_backup(archive)


def test_inspect_accepts_bounded_zip64_central_directory(app_home):
    _seed_live_transaction("zip64-compatible")
    valid = create_backup("manual")
    contents = valid.read_bytes()
    eocd_offset = contents.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    eocd = bytearray(contents[eocd_offset:])
    entries = struct.unpack_from("<H", eocd, 10)[0]
    directory_size = struct.unpack_from("<I", eocd, 12)[0]
    directory_offset = struct.unpack_from("<I", eocd, 16)[0]
    zip64_record = struct.pack(
        "<4sQ2H2I4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        entries,
        entries,
        directory_size,
        directory_offset,
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd_offset, 1)
    struct.pack_into("<H", eocd, 8, 0xFFFF)
    struct.pack_into("<H", eocd, 10, 0xFFFF)
    struct.pack_into("<I", eocd, 12, 0xFFFFFFFF)
    struct.pack_into("<I", eocd, 16, 0xFFFFFFFF)
    archive = app_home / "bounded-zip64.zip"
    archive.write_bytes(
        contents[:eocd_offset] + zip64_record + locator + bytes(eocd)
    )

    assert inspect_backup(archive).reason == "manual"


def test_inspect_rejects_oversize_member_metadata_with_small_fixture(
    app_home, monkeypatch
):
    _seed_live_transaction("member-size")
    valid = create_backup("manual", include_images=True)
    limited = _rewrite_archive(
        valid,
        app_home / "oversize-member.zip",
        extra_members={"data/images/small.bin": b"x" * 65},
    )
    monkeypatch.setattr(backup_module, "MAX_MEMBER_EXPANDED_BYTES", 64)

    with pytest.raises(ValueError, match="expanded|size|limit"):
        inspect_backup(limited)


def test_inspect_rejects_total_expanded_metadata_with_small_fixture(
    app_home, monkeypatch
):
    _seed_live_transaction("total-size")
    archive = create_backup("manual")
    with zipfile.ZipFile(archive) as zipped:
        total = sum(item.file_size for item in zipped.infolist())
        largest = max(item.file_size for item in zipped.infolist())
    monkeypatch.setattr(backup_module, "MAX_MEMBER_EXPANDED_BYTES", largest + 1)
    monkeypatch.setattr(backup_module, "MAX_TOTAL_EXPANDED_BYTES", total - 1)

    with pytest.raises(ValueError, match="total|expanded|limit"):
        inspect_backup(archive)


def test_inspect_enforces_separate_small_json_limit(app_home, monkeypatch):
    _seed_live_transaction("json-limit")
    archive = create_backup("manual")
    monkeypatch.setattr(backup_module, "MAX_JSON_BYTES", 8)

    with pytest.raises(ValueError, match="JSON|size|limit"):
        inspect_backup(archive)


def test_inspect_rejects_small_high_compression_bomb(app_home):
    _seed_live_transaction("compression-ratio")
    valid = create_backup("manual", include_images=True)
    compressed = _rewrite_archive(
        valid,
        app_home / "high-compression.zip",
        extra_members={
            "data/images/highly-compressible.bin": b"0" * (2 * 1024 * 1024)
        },
    )

    with pytest.raises(ValueError, match="compression"):
        inspect_backup(compressed)


def test_inspect_rejects_aggregate_ratio_bomb_made_of_small_members(app_home):
    _seed_live_transaction("aggregate-compression")
    valid = create_backup("manual", include_images=True)
    compressed = _rewrite_archive(
        valid,
        app_home / "aggregate-compression.zip",
        extra_members={
            f"data/images/tiny-{index}.txt": b"0" * 8192
            for index in range(32)
        },
    )

    with pytest.raises(ValueError, match="aggregate|compression"):
        inspect_backup(compressed)


def test_create_backup_with_many_normal_small_images_still_validates(app_home):
    _seed_live_transaction("many-normal-images")
    for index in range(300):
        payload = bytes((index + offset) % 256 for offset in range(512))
        (get_paths().images_dir / f"image-{index}.bin").write_bytes(payload)

    archive = create_backup("many-images", include_images=True)

    assert inspect_backup(archive).reason == "many-images"


def test_one_physical_archive_limit_governs_creation_and_inspection(
    app_home, monkeypatch
):
    _seed_live_transaction("physical-limit")
    monkeypatch.setattr(backup_module, "MAX_PHYSICAL_ARCHIVE_BYTES", 128 * 1024)

    archive = create_backup("within-limit")

    assert 0 < archive.stat().st_size <= backup_module.MAX_PHYSICAL_ARCHIVE_BYTES
    assert inspect_backup(archive).reason == "within-limit"

    monkeypatch.setattr(
        backup_module, "MAX_PHYSICAL_ARCHIVE_BYTES", archive.stat().st_size - 1
    )
    with pytest.raises(backup_module.InvalidBackupError, match="physical|size|large"):
        inspect_backup(archive)

    published_before = set(get_paths().backups_dir.glob("*.zip"))
    monkeypatch.setattr(backup_module, "MAX_PHYSICAL_ARCHIVE_BYTES", 1)
    with pytest.raises(backup_module.InvalidBackupError, match="physical|size|large"):
        create_backup("over-limit")
    assert set(get_paths().backups_dir.glob("*.zip")) == published_before


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


def test_restore_rejects_noncanonical_v1_after_migration_without_live_mutation(
    app_home,
):
    legacy_database = app_home / "missing-goals-v1.db"
    with closing(sqlite3.connect(legacy_database)) as connection, connection:
        connection.executescript(BASE_SCHEMA)
        connection.execute("DROP TABLE goals")
        connection.execute("PRAGMA user_version = 1")
    archive = app_home / "missing-goals-v1.zip"
    _write_valid_archive(
        archive,
        legacy_database.read_bytes(),
        reason="manual",
        created_at=datetime.now().astimezone(),
        schema_version=1,
    )
    _seed_live_transaction("current-safe-v1")
    paths = get_paths()
    paths.config_path.write_text('{"marker":"current"}', encoding="utf-8")
    before_database = paths.db_path.read_bytes()
    before_config = paths.config_path.read_bytes()
    before_backups = set(paths.backups_dir.iterdir())

    with pytest.raises(backup_module.InvalidBackupError, match="schema|table|goals"):
        restore_backup(archive)

    assert paths.db_path.read_bytes() == before_database
    assert paths.config_path.read_bytes() == before_config
    assert set(paths.backups_dir.iterdir()) == before_backups


def test_restore_valid_v1_migrates_to_canonical_current_schema(app_home):
    legacy_database = app_home / "valid-v1.db"
    with closing(sqlite3.connect(legacy_database)) as connection, connection:
        connection.executescript(BASE_SCHEMA)
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-20', 1, '支出', '其他', "
            "'valid-v1', 'now', 'now')"
        )
    archive = app_home / "valid-v1.zip"
    _write_valid_archive(
        archive,
        legacy_database.read_bytes(),
        reason="manual",
        created_at=datetime.now().astimezone(),
        schema_version=1,
    )
    _seed_live_transaction("replaced-live")

    restore_backup(archive)

    assert _live_notes() == ["valid-v1"]
    with closing(sqlite3.connect(get_paths().db_path)) as connection:
        from app.legacy_migration import validate_current_schema

        validate_current_schema(connection)


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

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert get_paths().db_path.read_bytes() == db_before
    assert get_paths().config_path.read_bytes() == config_before
    assert _live_notes() == ["current-safe"]


def test_restore_waits_for_open_app_connection_and_safety_backup_captures_write(
    app_home,
):
    from app import db

    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-before-lock")
    connection = db.get_conn()
    connection.execute(
        "UPDATE transactions SET note = 'committed-while-held'"
    )
    connection.commit()
    errors: list[BaseException] = []

    def run_restore() -> None:
        try:
            restore_backup(archive)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_restore)
    worker.start()
    time.sleep(0.1)
    assert worker.is_alive()
    assert not any(
        inspect_backup(candidate).reason == "pre-restore"
        for candidate in get_paths().backups_dir.glob("*.zip")
    )

    connection.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    safety = next(
        candidate
        for candidate in get_paths().backups_dir.glob("*.zip")
        if inspect_backup(candidate).reason == "pre-restore"
    )
    with zipfile.ZipFile(safety) as zipped:
        safety_db = app_home / "concurrent-safety.db"
        with zipped.open("data/better_money.db") as source, safety_db.open("wb") as target:
            shutil.copyfileobj(source, target)
    with closing(sqlite3.connect(safety_db)) as captured:
        assert captured.execute("SELECT note FROM transactions").fetchone()[0] == (
            "committed-while-held"
        )


def test_restore_moves_all_old_sidecars_before_installing_new_database(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current")
    sidecars = [Path(f"{get_paths().db_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    for index, sidecar in enumerate(sidecars):
        sidecar.write_bytes(f"sidecar-{index}".encode())
    monkeypatch.setattr(backup_module, "_checkpoint_live_database", lambda path: None)
    monkeypatch.setattr(
        backup_module,
        "create_backup",
        lambda reason, include_images=False: archive,
    )
    real_replace = backup_module.os.replace

    database_replace_events: list[tuple[bool, bool]] = []

    def assert_sidecars_gone_before_database_install(source, destination):
        if Path(destination) == get_paths().db_path and "restore-rollback-" not in str(source):
            database_replace_events.append(
                (
                    get_paths().db_path.is_file(),
                    all(not os.path.lexists(sidecar) for sidecar in sidecars),
                )
            )
        return real_replace(source, destination)

    monkeypatch.setattr(
        backup_module.os, "replace", assert_sidecars_gone_before_database_install
    )

    restore_backup(archive)

    assert database_replace_events == [(True, True)]
    assert _live_notes() == ["archive-source"]


def test_restore_detects_external_write_after_safety_backup_without_replacing_live(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("baseline-current")
    real_create = backup_module.create_backup
    safety_archives: list[Path] = []

    def create_then_change_live(reason: str, include_images: bool = False):
        created = real_create(reason, include_images=include_images)
        if reason == "pre-restore":
            safety_archives.append(created)
            with closing(sqlite3.connect(get_paths().db_path)) as connection, connection:
                connection.execute(
                    "UPDATE transactions SET note = 'external-after-safety'"
                )
        return created

    monkeypatch.setattr(backup_module, "create_backup", create_then_change_live)

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert _live_notes() == ["external-after-safety"]
    assert len(safety_archives) == 1
    assert _backup_notes(
        safety_archives[0], app_home / "baseline-safety.db"
    ) == ["baseline-current"]
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))


def test_restore_serializes_short_lived_writer_from_another_app_process(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("baseline-current")
    attempted = app_home / "child-attempted"
    committed = app_home / "child-committed"
    child_script = """
from pathlib import Path
import sys
from app import db

attempted = Path(sys.argv[1])
committed = Path(sys.argv[2])
attempted.write_text("attempted", encoding="utf-8")
connection = db.get_conn()
try:
    connection.execute("UPDATE transactions SET note = 'cross-process-commit'")
    connection.commit()
finally:
    connection.close()
committed.write_text("committed", encoding="utf-8")
"""
    real_capture = backup_module._capture_live_baseline
    real_replace = backup_module.os.replace
    capture_count = 0
    child: subprocess.Popen[str] | None = None
    committed_before_database_replace: list[bool] = []

    def wait_for(path: Path, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return path.exists()

    def start_child_after_final_baseline(includes_images: bool):
        nonlocal capture_count, child
        baseline = real_capture(includes_images)
        capture_count += 1
        if capture_count == 4:
            child = subprocess.Popen(
                [sys.executable, "-c", child_script, str(attempted), str(committed)],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert wait_for(attempted, 5)
        return baseline

    def observe_writer_at_database_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path == get_paths().db_path
            and "better-money-restore-" in str(source_path)
        ):
            committed_before_database_replace.append(wait_for(committed, 1))
        return real_replace(source, destination)

    monkeypatch.setattr(
        backup_module, "_capture_live_baseline", start_child_after_final_baseline
    )
    monkeypatch.setattr(backup_module.os, "replace", observe_writer_at_database_replace)

    try:
        restore_backup(archive)
        assert child is not None
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, (stdout, stderr)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()

    assert committed_before_database_replace == [False]
    assert _live_notes() == ["cross-process-commit"]


def test_restore_database_sharing_violation_never_removes_old_database(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    real_replace = backup_module.os.replace
    attempted = False

    def deny_new_database_replace(source, destination):
        nonlocal attempted
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path == get_paths().db_path
            and "better-money-restore-" in str(source_path)
        ):
            attempted = True
            assert get_paths().db_path.is_file()
            raise PermissionError("simulated Windows sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", deny_new_database_replace)

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert attempted is True
    assert get_paths().db_path.is_file()
    assert _live_notes() == ["current-safe"]


def test_restore_install_and_rollback_failure_preserves_recovery_material(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    get_paths().config_path.write_text('{"tone":"current"}', encoding="utf-8")
    real_replace = backup_module.os.replace
    failed_install = False

    def fail_install_and_database_recovery(source, destination):
        nonlocal failed_install
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == get_paths().config_path and not failed_install:
            failed_install = True
            raise OSError("install config failed")
        if (
            failed_install
            and source_path.name.startswith(".restore-database-")
            and destination_path == get_paths().db_path
        ):
            raise OSError("database rollback failed")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", fail_install_and_database_recovery)

    with pytest.raises(backup_module.RestoreIncompleteError) as raised:
        restore_backup(archive)

    rollback_dirs = list(get_paths().runtime_dir.glob("restore-rollback-*"))
    assert len(rollback_dirs) == 1
    assert str(rollback_dirs[0]) in str(raised.value)
    assert (rollback_dirs[0] / "journal.json").is_file()
    assert (rollback_dirs[0] / "original-database").is_file()


def test_restore_final_journal_failure_does_not_downgrade_complete_recovery(
    app_home, monkeypatch
):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    get_paths().config_path.write_text('{"tone":"current"}', encoding="utf-8")
    real_replace = backup_module.os.replace
    real_write_journal = backup_module._write_restore_journal
    failed_install = False

    def fail_config_install(source, destination):
        nonlocal failed_install
        if Path(destination) == get_paths().config_path and not failed_install:
            failed_install = True
            raise OSError("install config failed")
        return real_replace(source, destination)

    def fail_only_final_recovery_journal(rollback, journal):
        if journal["phase"] == "recovered":
            raise OSError("final journal write failed")
        return real_write_journal(rollback, journal)

    monkeypatch.setattr(backup_module.os, "replace", fail_config_install)
    monkeypatch.setattr(
        backup_module, "_write_restore_journal", fail_only_final_recovery_journal
    )

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert _live_notes() == ["current-safe"]


def test_restore_blank_home_skips_safety_backup(app_home):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    get_paths().db_path.unlink()
    get_paths().config_path.unlink(missing_ok=True)

    restore_backup(archive)

    assert _live_notes() == ["archive-source"]
    assert not any(
        inspect_backup(candidate).reason == "pre-restore"
        for candidate in get_paths().backups_dir.glob("*.zip")
    )


def test_restore_cleanup_failure_does_not_mask_success(app_home, monkeypatch):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current")
    real_rmtree = backup_module.shutil.rmtree

    def fail_restore_cleanup(path, *args, **kwargs):
        if "better-money-restore-" in str(path) or "restore-rollback-" in str(path):
            raise OSError("cleanup failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, "rmtree", fail_restore_cleanup)

    restore_backup(archive)

    assert _live_notes() == ["archive-source"]


def test_postrename_safe_tree_exception_is_committed_and_leaves_retired_tree(
    app_home, monkeypatch
):
    from app import rollback_cleanup

    rollback = get_paths().runtime_dir / "restore-rollback-cleanup-contract"
    rollback.mkdir()
    (rollback / "journal.json").write_text("committed", encoding="utf-8")

    def fail_safe_tree(*args, **kwargs):
        raise RuntimeError("safe traversal failed after retirement")

    monkeypatch.setattr(
        rollback_cleanup, "_safe_tree_for_deletion", fail_safe_tree
    )

    assert rollback_cleanup.retire_rollback_for_cleanup(
        rollback, "restore-rollback-"
    )
    assert not os.path.lexists(rollback)
    retired = list(
        get_paths().runtime_dir.glob(".better-money-retired-cleanup-*")
    )
    assert len(retired) == 1
    assert (retired[0] / "journal.json").read_text(encoding="utf-8") == "committed"


def test_restore_postrename_safe_tree_exception_keeps_desired_live_state(
    app_home, monkeypatch
):
    from app import rollback_cleanup

    _seed_live_transaction("postrename-desired")
    archive = create_backup("postrename-cleanup")
    _seed_live_transaction("postrename-original")

    def fail_safe_tree(*args, **kwargs):
        raise RuntimeError("safe traversal failed after retirement")

    monkeypatch.setattr(
        rollback_cleanup, "_safe_tree_for_deletion", fail_safe_tree
    )

    restore_backup(archive)

    assert _live_notes() == ["postrename-desired"]
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))
    assert len(
        list(get_paths().runtime_dir.glob(".better-money-retired-cleanup-*"))
    ) == 1


def test_partial_cleanup_after_atomic_retire_cannot_trigger_startup_rollback(
    app_home, monkeypatch
):
    from app import rollback_cleanup
    from app.recovery import recover_interrupted_installs

    _seed_live_transaction("retired-cleanup-desired")
    archive = create_backup("retired-cleanup")
    _seed_live_transaction("retired-cleanup-original")
    real_rmtree = rollback_cleanup.shutil.rmtree

    def partially_delete_retired(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.startswith(".better-money-retired-cleanup-"):
            (candidate / "journal.json").unlink(missing_ok=True)
            raise OSError("partial cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(rollback_cleanup.shutil, "rmtree", partially_delete_retired)

    restore_backup(archive)
    assert _live_notes() == ["retired-cleanup-desired"]
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))
    assert list(
        get_paths().runtime_dir.glob(".better-money-retired-cleanup-*")
    )

    recover_interrupted_installs()
    assert _live_notes() == ["retired-cleanup-desired"]


def test_atomic_retire_rename_failure_rolls_back_before_reporting_failure(
    app_home, monkeypatch
):
    from app import rollback_cleanup
    from app.recovery import recover_interrupted_installs

    _seed_live_transaction("rename-failure-desired")
    archive = create_backup("rename-failure")
    _seed_live_transaction("rename-failure-original")
    real_replace = rollback_cleanup.os.replace

    def fail_retire_rename(source, destination):
        if Path(source).name.startswith("restore-rollback-"):
            raise OSError("atomic retire rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(rollback_cleanup.os, "replace", fail_retire_rename)
    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    rollback = next(get_paths().runtime_dir.glob("restore-rollback-*"))
    before = {
        path.relative_to(rollback).as_posix(): path.read_bytes()
        for path in rollback.rglob("*")
        if path.is_file()
    }
    assert json.loads(before["journal.json"])["phase"] == "recovered"
    assert _live_notes() == ["rename-failure-original"]

    monkeypatch.setattr(rollback_cleanup.os, "replace", real_replace)
    real_rmtree = rollback_cleanup.shutil.rmtree

    def partially_delete_startup_retired(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.startswith(".better-money-retired-cleanup-"):
            (candidate / "journal.json").unlink(missing_ok=True)
            raise OSError("startup partial cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        rollback_cleanup.shutil, "rmtree", partially_delete_startup_retired
    )
    recover_interrupted_installs()

    assert _live_notes() == ["rename-failure-original"]
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))
    assert list(
        get_paths().runtime_dir.glob(".better-money-retired-cleanup-*")
    )


@pytest.mark.parametrize(
    "failure_mode", ["lstat-error", "identity-change"]
)
def test_prerename_retirement_exception_rolls_back_complete_restore(
    app_home, monkeypatch, failure_mode
):
    from app import rollback_cleanup

    _seed_live_transaction("prerename-desired")
    get_paths().config_path.write_text('{"state":"desired"}', encoding="utf-8")
    (get_paths().images_dir / "state.txt").write_text("desired", encoding="utf-8")
    archive = create_backup("prerename-exception", include_images=True)
    _seed_live_transaction("prerename-original")
    get_paths().config_path.write_text('{"state":"original"}', encoding="utf-8")
    (get_paths().images_dir / "state.txt").write_text("original", encoding="utf-8")
    real_lstat = rollback_cleanup.os.lstat
    rollback_lstat_count = 0

    def fail_before_retirement(path, *args, **kwargs):
        nonlocal rollback_lstat_count
        candidate = Path(path)
        status = real_lstat(path, *args, **kwargs)
        if (
            candidate.parent == get_paths().runtime_dir
            and candidate.name.startswith("restore-rollback-")
        ):
            rollback_lstat_count += 1
            if failure_mode == "lstat-error":
                raise OSError("retirement lstat failed")
            if rollback_lstat_count % 2 == 0:
                values = list(status)
                values[1] += 1
                return os.stat_result(values)
        return status

    monkeypatch.setattr(rollback_cleanup.os, "lstat", fail_before_retirement)

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert _live_notes() == ["prerename-original"]
    assert get_paths().config_path.read_text(encoding="utf-8") == '{"state":"original"}'
    assert (get_paths().images_dir / "state.txt").read_text(encoding="utf-8") == "original"
    rollback = next(get_paths().runtime_dir.glob("restore-rollback-*"))
    assert json.loads((rollback / "journal.json").read_text())["phase"] == "recovered"


def test_prerename_retirement_exception_with_failed_recovery_is_incomplete(
    app_home, monkeypatch
):
    from app import rollback_cleanup

    _seed_live_transaction("prerename-incomplete-desired")
    archive = create_backup("prerename-incomplete")
    _seed_live_transaction("prerename-incomplete-original")
    real_lstat = rollback_cleanup.os.lstat
    real_replace = backup_module.os.replace

    def fail_retirement_lstat(path, *args, **kwargs):
        candidate = Path(path)
        if (
            candidate.parent == get_paths().runtime_dir
            and candidate.name.startswith("restore-rollback-")
        ):
            raise OSError("retirement lstat failed")
        return real_lstat(path, *args, **kwargs)

    def fail_database_recovery(source, destination):
        if (
            Path(source).name.startswith(".restore-database-")
            and Path(destination) == get_paths().db_path
        ):
            raise OSError("database recovery failed")
        return real_replace(source, destination)

    monkeypatch.setattr(rollback_cleanup.os, "lstat", fail_retirement_lstat)
    monkeypatch.setattr(backup_module.os, "replace", fail_database_recovery)

    with pytest.raises(backup_module.RestoreIncompleteError):
        restore_backup(archive)

    rollback = next(get_paths().runtime_dir.glob("restore-rollback-*"))
    journal = json.loads((rollback / "journal.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "recovery-incomplete"
    assert (rollback / "original-database").is_file()


def test_restore_complete_marker_failure_rolls_back_and_does_not_report_success(
    app_home, monkeypatch
):
    _seed_live_transaction("replacement-marker")
    get_paths().config_path.write_text('{"marker":"replacement"}', encoding="utf-8")
    (get_paths().images_dir / "marker.txt").write_text(
        "replacement", encoding="utf-8"
    )
    archive = create_backup("marker-replacement", include_images=True)
    _seed_live_transaction("original-marker")
    get_paths().config_path.write_text('{"marker":"original"}', encoding="utf-8")
    (get_paths().images_dir / "marker.txt").write_text("original", encoding="utf-8")
    original = backup_module._capture_live_baseline(True)
    real_write = backup_module._write_restore_journal

    def fail_complete_marker(rollback, journal):
        if journal["phase"] == "complete":
            raise OSError("complete marker durability failure")
        return real_write(rollback, journal)

    monkeypatch.setattr(backup_module, "_write_restore_journal", fail_complete_marker)

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert backup_module._capture_live_baseline(True) == original


def test_restore_complete_marker_and_recovery_failure_is_incomplete(
    app_home, monkeypatch
):
    _seed_live_transaction("replacement-incomplete-marker")
    archive = create_backup("marker-incomplete")
    _seed_live_transaction("original-incomplete-marker")
    real_write = backup_module._write_restore_journal
    real_replace = backup_module.os.replace

    def fail_complete_marker(rollback, journal):
        if journal["phase"] == "complete":
            raise OSError("complete marker durability failure")
        return real_write(rollback, journal)

    def fail_database_recovery(source, destination):
        if (
            Path(source).name.startswith(".restore-database-")
            and Path(destination) == get_paths().db_path
        ):
            raise OSError("database recovery failure")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_module, "_write_restore_journal", fail_complete_marker)
    monkeypatch.setattr(backup_module.os, "replace", fail_database_recovery)

    with pytest.raises(backup_module.RestoreIncompleteError):
        restore_backup(archive)

    rollback = next(get_paths().runtime_dir.glob("restore-rollback-*"))
    journal = json.loads((rollback / "journal.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "recovery-incomplete"


def test_restore_fails_closed_when_untracked_sqlite_writer_is_busy(app_home):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    external = sqlite3.connect(get_paths().db_path, timeout=0)
    try:
        external.execute("BEGIN IMMEDIATE")
        external.execute("UPDATE transactions SET note = 'uncommitted-external'")

        with pytest.raises(backup_module.RestoreFailedError):
            restore_backup(archive)

        assert _live_notes() == ["current-safe"]
    finally:
        external.rollback()
        external.close()


def test_restore_preflight_rejects_linked_live_config(app_home):
    _seed_live_transaction("archive-source")
    archive = create_backup("manual")
    _seed_live_transaction("current-safe")
    outside = app_home / "outside-config.json"
    outside.write_text('{"api_key":"outside-secret"}', encoding="utf-8")
    get_paths().config_path.unlink(missing_ok=True)
    try:
        get_paths().config_path.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(backup_module.RestoreFailedError):
        restore_backup(archive)

    assert get_paths().config_path.is_symlink()
    assert "outside-secret" in outside.read_text(encoding="utf-8")
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
