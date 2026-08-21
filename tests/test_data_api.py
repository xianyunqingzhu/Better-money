from __future__ import annotations

import asyncio
from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.backup import inspect_backup
from app.migrations import migrate_database
from app.paths import get_paths


INVALID_BACKUP = {
    "error": "invalid_backup",
    "message": "备份文件无效或已损坏",
}
INVALID_LEGACY_SOURCE = {
    "error": "invalid_legacy_source",
    "message": "所选文件夹不是可迁移的 Better Money 数据",
}
MIGRATION_FAILED = {
    "error": "migration_failed",
    "message": "迁移失败，现有数据未改变",
}
MIGRATION_RECOVERY_INCOMPLETE = {
    "error": "migration_recovery_incomplete",
    "message": "迁移恢复未完成，请退出应用并从安全备份恢复",
}
RESTORE_FAILED = {
    "error": "restore_failed",
    "message": "恢复失败，现有数据未改变",
}
RESTORE_INCOMPLETE = {
    "error": "restore_incomplete",
    "message": "恢复未完成，请退出应用并从安全备份恢复",
}


def _seed_legacy(data_dir: Path, *, api_key: str = "legacy-api-secret") -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(data_dir / "better_money.db")) as connection:
        migrate_database(connection)
        with connection:
            connection.execute(
                "INSERT INTO transactions("
                "date, amount, type, category, note, created_at, updated_at"
                ") VALUES ('2026-08-01', 100, '收入', '工资', "
                "'legacy-income', 'now', 'now')"
            )
            connection.execute(
                "INSERT INTO goals(name, price, saved, created_at) "
                "VALUES ('相机', 8000, 3000, 'now')"
            )
    (data_dir / "config.json").write_text(
        json.dumps(
            {"api_key": api_key, "initial_balance": 500.0},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return data_dir


def _runtime_files() -> set[Path]:
    return {path for path in get_paths().runtime_dir.rglob("*") if path.is_file()}


def test_create_requires_explicit_include_images(client):
    response = client.post("/api/backups/create", json={})

    assert response.status_code == 422


@pytest.mark.parametrize("include_images", [False, True])
def test_create_and_list_backup_with_non_sensitive_manifest(client, include_images):
    secret = "create-response-api-secret"
    get_paths().config_path.write_text(
        json.dumps({"api_key": secret, "theme": "light"}), encoding="utf-8"
    )

    created = client.post(
        "/api/backups/create", json={"include_images": include_images}
    )

    assert created.status_code == 200
    item = created.json()
    assert item["filename"].endswith("-manual.zip")
    assert item["size"] > 0
    assert item["manifest"]["reason"] == "manual"
    assert item["manifest"]["includes_images"] is include_images
    assert set(item) == {"filename", "size", "manifest"}
    assert set(item["manifest"]) == {
        "format_version",
        "created_at",
        "app_version",
        "schema_version",
        "reason",
        "includes_images",
    }
    assert secret not in created.text
    assert "api_key" not in created.text

    listed = client.get("/api/backups")
    assert listed.status_code == 200
    listed_item = next(
        candidate
        for candidate in listed.json()
        if candidate["filename"] == item["filename"]
    )
    assert listed_item == item
    assert set(listed_item) == {"filename", "size", "manifest"}
    assert set(listed_item["manifest"]) == set(item["manifest"])
    assert secret not in listed.text
    assert "api_key" not in listed.text


def test_list_ignores_raw_databases_and_invalid_zip_files(client):
    created = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()
    backups = get_paths().backups_dir
    (backups / "manual-export.db").write_bytes(b"raw sqlite bytes")
    (backups / "broken.zip").write_bytes(b"not a zip")

    response = client.get("/api/backups")

    assert response.status_code == 200
    names = {item["filename"] for item in response.json()}
    assert created["filename"] in names
    assert "manual-export.db" not in names
    assert "broken.zip" not in names


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_list_and_export_reject_linked_backup_files(
    client, app_home, link_kind
):
    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    trusted = get_paths().backups_dir / filename
    outside = app_home / "outside-copy.zip"
    shutil.copy2(trusted, outside)
    linked = get_paths().backups_dir / f"{link_kind}.zip"
    try:
        if link_kind == "symlink":
            os.symlink(outside, linked)
        else:
            os.link(outside, linked)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"{link_kind} unavailable: {exc}")

    listed = client.get("/api/backups")
    exported = client.get(
        "/api/backups/export", params={"filename": linked.name}
    )

    assert linked.name not in {item["filename"] for item in listed.json()}
    assert exported.status_code == 400
    assert exported.json() == INVALID_BACKUP


def test_export_returns_only_an_inspected_backup_without_api_key(client):
    secret = "exported-api-secret"
    get_paths().config_path.write_text(
        json.dumps({"api_key": secret, "theme": "dark"}), encoding="utf-8"
    )
    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]

    response = client.get("/api/backups/export", params={"filename": filename})

    assert response.status_code == 200
    assert f'filename="{filename}"' in response.headers["content-disposition"]
    downloaded = get_paths().runtime_dir / "downloaded.zip"
    downloaded.write_bytes(response.content)
    assert inspect_backup(downloaded).reason == "manual"
    with zipfile.ZipFile(downloaded) as archive:
        config = json.loads(archive.read("data/config.json"))
    assert "api_key" not in config
    assert secret.encode() not in response.content


def test_export_unicode_filename_uses_ascii_rfc5987_and_cleans_snapshot(
    client,
):
    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    source = get_paths().backups_dir / filename
    unicode_name = "中文备份.zip"
    shutil.copy2(source, get_paths().backups_dir / unicode_name)
    before = _runtime_files()

    response = client.get(
        "/api/backups/export", params={"filename": unicode_name}
    )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    assert "filename=\"" in disposition
    assert "filename*=UTF-8''%E4%B8%AD%E6%96%87%E5%A4%87%E4%BB%BD.zip" in disposition
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "manifest.json" in archive.namelist()
    assert _runtime_files() == before


def test_export_response_construction_failure_cleans_snapshot(client, monkeypatch):
    from app import data_api

    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    before = _runtime_files()

    def fail_response_construction(*args, **kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(
        data_api, "_SnapshotStreamingResponse", fail_response_construction
    )

    with pytest.raises(RuntimeError, match="response construction failed"):
        client.get("/api/backups/export", params={"filename": filename})

    assert _runtime_files() == before


def test_export_response_start_failure_closes_unstarted_snapshot(client):
    from app import data_api

    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    before = _runtime_files()
    response = data_api.export_backup(filename)
    assert _runtime_files() != before

    async def receive():
        return {"type": "http.disconnect"}

    async def fail_start(message):
        assert message["type"] == "http.response.start"
        raise OSError("client disconnected before response start")

    with pytest.raises(Exception):
        asyncio.run(
            response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                fail_start,
            )
        )

    assert _runtime_files() == before


def test_export_body_disconnect_closes_snapshot(client):
    from app import data_api

    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    before = _runtime_files()
    response = data_api.export_backup(filename)

    async def receive():
        return {"type": "http.disconnect"}

    async def disconnect_on_body(message):
        if message["type"] == "http.response.body":
            raise OSError("client disconnected while streaming")

    with pytest.raises(Exception):
        asyncio.run(
            response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                disconnect_on_body,
            )
        )

    assert _runtime_files() == before


def test_export_streams_validated_private_snapshot_after_source_is_replaced(
    client, monkeypatch
):
    from app import data_api
    from app.backup import create_backup

    with closing(sqlite3.connect(get_paths().db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-01', 1, '支出', '其他', "
            "'trusted-export', 'now', 'now')"
        )
    trusted = create_backup("trusted-export")
    with closing(sqlite3.connect(get_paths().db_path)) as connection, connection:
        connection.execute("UPDATE transactions SET note = 'replacement-export'")
    replacement = create_backup("replacement-export")
    replacement_bytes = replacement.read_bytes()
    real_inspect = data_api.inspect_backup
    swapped = False

    def inspect_then_replace_original(snapshot: Path):
        nonlocal swapped
        manifest = real_inspect(snapshot)
        if snapshot.parent == get_paths().runtime_dir and not swapped:
            trusted.write_bytes(replacement_bytes)
            swapped = True
        return manifest

    monkeypatch.setattr(data_api, "inspect_backup", inspect_then_replace_original)
    before = _runtime_files()

    response = client.get(
        "/api/backups/export", params={"filename": trusted.name}
    )

    assert response.status_code == 200
    downloaded = get_paths().runtime_dir / "trusted-download.zip"
    downloaded.write_bytes(response.content)
    assert inspect_backup(downloaded).reason == "trusted-export"
    assert swapped is True
    downloaded.unlink()
    assert _runtime_files() == before


@pytest.mark.parametrize(
    "filename",
    ["../outside.zip", "..\\outside.zip", "folder/backup.zip"],
)
def test_export_rejects_path_traversal(client, filename):
    response = client.get("/api/backups/export", params={"filename": filename})

    assert response.status_code == 400
    assert response.json() == INVALID_BACKUP


def test_export_rejects_an_invalid_zip_even_when_it_is_inside_backups(client):
    filename = "broken.zip"
    (get_paths().backups_dir / filename).write_bytes(b"not a zip")

    response = client.get("/api/backups/export", params={"filename": filename})

    assert response.status_code == 400
    assert response.json() == INVALID_BACKUP


def test_export_missing_backup_returns_top_level_404(client):
    response = client.get(
        "/api/backups/export", params={"filename": "missing.zip"}
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "backup_not_found",
        "message": "备份文件不存在",
    }


def test_restore_removes_runtime_upload_after_success(client):
    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    archive = get_paths().backups_dir / filename
    before = _runtime_files()

    response = client.post(
        "/api/backups/restore",
        files={"file": ("restore.zip", archive.read_bytes(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert _runtime_files() == before


def test_restore_prerename_retirement_exception_maps_to_preserved_failure(
    client, monkeypatch
):
    from app import rollback_cleanup

    paths = get_paths()
    with closing(sqlite3.connect(paths.db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-20', 1, '支出', '其他', "
            "'retirement-api-desired', 'now', 'now')"
        )
    created = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    archive_bytes = (paths.backups_dir / created).read_bytes()
    with closing(sqlite3.connect(paths.db_path)) as connection, connection:
        connection.execute(
            "UPDATE transactions SET note = 'retirement-api-original'"
        )
    real_lstat = rollback_cleanup.os.lstat

    def fail_retirement_lstat(path, *args, **kwargs):
        candidate = Path(path)
        if (
            candidate.parent == paths.runtime_dir
            and candidate.name.startswith("restore-rollback-")
        ):
            raise OSError("retirement lstat failed")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(rollback_cleanup.os, "lstat", fail_retirement_lstat)
    response = client.post(
        "/api/backups/restore",
        files={"file": ("restore.zip", archive_bytes, "application/zip")},
    )

    assert response.status_code == 500
    assert response.json() == RESTORE_FAILED
    with closing(sqlite3.connect(paths.db_path)) as connection:
        assert connection.execute(
            "SELECT note FROM transactions"
        ).fetchall() == [("retirement-api-original",)]


def test_restore_upload_uses_backup_service_physical_limit(client, monkeypatch):
    from app import backup as backup_module

    monkeypatch.setattr(backup_module, "MAX_PHYSICAL_ARCHIVE_BYTES", 128 * 1024)
    filename = client.post(
        "/api/backups/create", json={"include_images": False}
    ).json()["filename"]
    archive = get_paths().backups_dir / filename
    payload = archive.read_bytes()
    monkeypatch.setattr(
        backup_module, "MAX_PHYSICAL_ARCHIVE_BYTES", len(payload) + 1024
    )

    accepted = client.post(
        "/api/backups/restore",
        files={"file": ("roundtrip.zip", payload, "application/zip")},
    )
    assert accepted.status_code == 200

    monkeypatch.setattr(
        backup_module, "MAX_PHYSICAL_ARCHIVE_BYTES", len(payload) - 1
    )
    rejected = client.post(
        "/api/backups/restore",
        files={"file": ("roundtrip.zip", payload, "application/zip")},
    )
    assert rejected.status_code == 400
    assert rejected.json() == INVALID_BACKUP


@pytest.mark.parametrize(
    ("failure_name", "status", "expected"),
    [
        ("InvalidBackupError", 400, INVALID_BACKUP),
        ("RestoreFailedError", 500, RESTORE_FAILED),
        ("RestoreIncompleteError", 500, RESTORE_INCOMPLETE),
        (None, 500, {"error": "restore_failed", "message": "恢复失败，请稍后重试"}),
    ],
)
def test_restore_service_errors_are_top_level_sanitized_and_remove_upload(
    client, monkeypatch, caplog, failure_name, status, expected
):
    from app import data_api
    from app import backup as backup_module

    failure_type = (
        getattr(backup_module, failure_name) if failure_name else Exception
    )
    failure = failure_type("secret D:/private/restore-rollback-path")

    supplied_paths: list[Path] = []

    def fail_restore(path: Path) -> None:
        supplied_paths.append(path)
        assert path.parent == get_paths().runtime_dir
        assert path.is_file()
        raise failure

    monkeypatch.setattr(data_api, "restore_backup", fail_restore)
    before = _runtime_files()

    response = client.post(
        "/api/backups/restore",
        files={"file": ("restore.zip", b"some bytes", "application/zip")},
    )

    assert response.status_code == status
    assert response.json() == expected
    assert supplied_paths and not supplied_paths[0].exists()
    assert _runtime_files() == before
    assert "secret" not in caplog.text
    assert "D:/private" not in caplog.text


def test_restore_service_runs_in_worker_thread(client, monkeypatch):
    from app import data_api

    worker_names: list[str] = []

    def capture_worker(path: Path) -> None:
        worker_names.append(threading.current_thread().name)

    monkeypatch.setattr(data_api, "restore_backup", capture_worker)

    response = client.post(
        "/api/backups/restore",
        files={"file": ("restore.zip", b"some bytes", "application/zip")},
    )

    assert response.status_code == 200
    assert worker_names == ["AnyIO worker thread"]


def test_restore_close_and_unlink_failures_do_not_mask_success_or_leak_details(
    client, monkeypatch, caplog
):
    from app import data_api

    monkeypatch.setattr(data_api, "restore_backup", lambda path: None)

    async def fail_close(upload):
        raise OSError("close secret D:/private/upload.zip")

    real_unlink = Path.unlink

    def fail_upload_unlink(path, *args, **kwargs):
        if path.name.startswith("better-money-upload-"):
            raise OSError("unlink secret D:/private/upload.zip")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(data_api.UploadFile, "close", fail_close)
    monkeypatch.setattr(data_api.Path, "unlink", fail_upload_unlink)

    response = client.post(
        "/api/backups/restore",
        files={"file": ("restore.zip", b"some bytes", "application/zip")},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "secret" not in caplog.text
    assert "D:/private" not in caplog.text


@pytest.mark.parametrize(
    ("filename", "content"),
    [("restore.db", b"bytes"), ("restore.zip", b"")],
)
def test_restore_rejects_basic_upload_errors(client, filename, content):
    response = client.post(
        "/api/backups/restore",
        files={"file": (filename, content, "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json() == INVALID_BACKUP


def test_select_folder_returns_stable_cancellation(client, monkeypatch):
    from app import data_api

    monkeypatch.setattr(data_api, "choose_directory", lambda title: None)

    response = client.post("/api/migration/select-folder")

    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "path": None}


def test_select_folder_returns_absolute_selected_path(client, monkeypatch, tmp_path):
    from app import data_api

    selected = tmp_path.resolve()
    monkeypatch.setattr(data_api, "choose_directory", lambda title: selected)

    response = client.post("/api/migration/select-folder")

    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "path": str(selected)}


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("/api/migration/inspect", {"source_path": "relative/path"}),
        (
            "/api/migration/import",
            {
                "source_path": "relative/path",
                "initial_balance_date": "2026-08-01",
            },
        ),
    ],
)
def test_migration_requires_absolute_paths(client, endpoint, payload):
    response = client.post(endpoint, json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize("endpoint", ["inspect", "import"])
def test_missing_migration_source_returns_top_level_404(client, app_home, endpoint):
    payload = {"source_path": str((app_home / "missing").resolve())}
    if endpoint == "import":
        payload["initial_balance_date"] = "2026-08-01"

    response = client.post(f"/api/migration/{endpoint}", json=payload)

    assert response.status_code == 404
    assert response.json() == {
        "error": "legacy_source_not_found",
        "message": "所选文件夹不存在",
    }


@pytest.mark.parametrize("endpoint", ["inspect", "import"])
def test_invalid_migration_source_has_stable_top_level_error(
    client, app_home, endpoint
):
    invalid = app_home / "not-legacy"
    invalid.mkdir()
    payload = {"source_path": str(invalid.resolve())}
    if endpoint == "import":
        payload["initial_balance_date"] = "2026-08-01"

    response = client.post(f"/api/migration/{endpoint}", json=payload)

    assert response.status_code == 400
    assert response.json() == INVALID_LEGACY_SOURCE


def test_inspect_returns_explicit_legacy_fields(client, app_home):
    source = _seed_legacy(app_home / "legacy-inspect" / "data")

    response = client.post(
        "/api/migration/inspect", json={"source_path": str(source.resolve())}
    )

    assert response.status_code == 200
    assert response.json() == {
        "source_path": str(source.resolve()),
        "transaction_count": 1,
        "goal_count": 1,
        "summary_count": 0,
        "earliest_transaction_date": "2026-08-01",
        "suggested_initial_balance_date": "2026-08-01",
        "initial_balance": 500.0,
        "calculated_balance": 600.0,
        "cleared_image_path_count": 0,
    }


def test_inspect_unknown_boundary_error_is_stable_and_sanitized(
    client, app_home, monkeypatch, caplog
):
    from app import data_api

    secret = "inspect-boundary-secret"
    private_path = str((app_home / "private-inspect").resolve())

    def fail_resolution(path: Path):
        raise Exception(f"{private_path}: {secret}")

    monkeypatch.setattr(data_api, "_existing_source", fail_resolution)

    response = client.post(
        "/api/migration/inspect",
        json={"source_path": str(app_home.resolve())},
    )

    assert response.status_code == 400
    assert response.json() == INVALID_LEGACY_SOURCE
    assert secret not in caplog.text
    assert private_path not in caplog.text


@pytest.mark.parametrize(
    "initial_balance_date",
    ["2026-8-1", "2026-02-30", "not-a-date", "2026-08-01T00:00:00"],
)
def test_import_requires_canonical_iso_date(
    client, app_home, initial_balance_date
):
    source = _seed_legacy(app_home / "legacy-date" / "data")

    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": initial_balance_date,
        },
    )

    assert response.status_code == 422


def test_import_installs_legacy_data_and_returns_explicit_fields(client, app_home):
    source = _seed_legacy(app_home / "legacy-import" / "data")

    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": "2026-08-01",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "source_path": str(source.resolve()),
        "transaction_count": 1,
        "goal_count": 1,
        "summary_count": 0,
        "earliest_transaction_date": "2026-08-01",
        "suggested_initial_balance_date": "2026-08-01",
        "initial_balance": 500.0,
        "calculated_balance": 600.0,
        "cleared_image_path_count": 0,
    }
    assert "api_key" not in response.text
    assert "legacy-api-secret" not in response.text
    with closing(sqlite3.connect(get_paths().db_path)) as connection:
        assert connection.execute(
            "SELECT note FROM transactions"
        ).fetchall() == [("legacy-income",)]


@pytest.mark.parametrize(
    "failure_type", [RuntimeError, Exception], ids=["runtime", "unexpected"]
)
def test_import_failure_is_sanitized_in_response_and_logs(
    client, app_home, monkeypatch, caplog, failure_type
):
    from app import data_api

    source = _seed_legacy(app_home / "legacy-failure" / "data")
    secret = "migration-api-secret"
    private_path = str((app_home / "private-rollback").resolve())

    def fail_import(source_path: Path, initial_balance_date: str):
        raise failure_type(f"rollback failed at {private_path}: {secret}")

    monkeypatch.setattr(data_api, "import_legacy", fail_import)

    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": "2026-08-01",
        },
    )

    assert response.status_code == 400
    assert response.json() == MIGRATION_FAILED
    assert secret not in caplog.text
    assert private_path not in caplog.text


def test_import_incomplete_recovery_has_distinct_error_that_does_not_claim_unchanged(
    client, app_home, monkeypatch
):
    from app import data_api

    source = _seed_legacy(app_home / "legacy-incomplete" / "data")

    def fail_import(source_path: Path, initial_balance_date: str):
        raise data_api.LegacyRecoveryIncompleteError("private recovery material")

    monkeypatch.setattr(data_api, "import_legacy", fail_import)
    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": "2026-08-01",
        },
    )

    assert response.status_code == 500
    assert response.json() == MIGRATION_RECOVERY_INCOMPLETE
    assert "未改变" not in response.json()["message"]


def test_installed_marker_plus_recovery_failure_uses_incomplete_api_error(
    client, app_home, monkeypatch
):
    from app import legacy_migration as migration_module

    source = _seed_legacy(app_home / "legacy-marker-incomplete" / "data")
    paths = get_paths()
    real_write = migration_module._write_journal
    real_replace = migration_module.os.replace

    def fail_installed_marker(rollback, journal):
        if journal["phase"] == "installed":
            raise OSError("installed marker durability failure")
        return real_write(rollback, journal)

    def fail_database_recovery(source_path, destination_path):
        if (
            Path(source_path).name == "original-database"
            and Path(destination_path) == paths.db_path
        ):
            raise OSError("database recovery failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(migration_module, "_write_journal", fail_installed_marker)
    monkeypatch.setattr(migration_module.os, "replace", fail_database_recovery)
    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": "2026-08-01",
        },
    )

    assert response.status_code == 500
    assert response.json() == MIGRATION_RECOVERY_INCOMPLETE
    assert "未改变" not in response.json()["message"]
    rollback = next(paths.runtime_dir.glob("legacy-rollback-*"))
    assert json.loads((rollback / "journal.json").read_text())["phase"] == (
        "recovery-incomplete"
    )


def test_legacy_retirement_failure_maps_to_failed_after_verified_rollback(
    client, app_home, monkeypatch
):
    from app import rollback_cleanup

    source = _seed_legacy(app_home / "legacy-retirement-failure" / "data")
    paths = get_paths()
    with closing(sqlite3.connect(paths.db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-20', 1, '支出', '其他', "
            "'retirement-original', 'now', 'now')"
        )
    real_replace = rollback_cleanup.os.replace

    def fail_legacy_retirement(source_path, destination_path):
        if Path(source_path).name.startswith("legacy-rollback-"):
            raise OSError("atomic retirement failed")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(rollback_cleanup.os, "replace", fail_legacy_retirement)
    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": "2026-08-01",
        },
    )

    assert response.status_code == 400
    assert response.json() == MIGRATION_FAILED
    with closing(sqlite3.connect(paths.db_path)) as connection:
        assert connection.execute(
            "SELECT note FROM transactions ORDER BY id"
        ).fetchall() == [("retirement-original",)]


@pytest.mark.parametrize("stage", ["boundary", "preinspect"])
def test_import_unknown_boundary_errors_are_migration_failed_and_sanitized(
    client, app_home, monkeypatch, caplog, stage
):
    from app import data_api

    source = _seed_legacy(app_home / f"legacy-{stage}" / "data")
    secret = "import-boundary-secret"
    private_path = str((app_home / "private-import").resolve())

    def fail(*args, **kwargs):
        raise Exception(f"{private_path}: {secret}")

    if stage == "boundary":
        monkeypatch.setattr(data_api, "_existing_source", fail)
    else:
        monkeypatch.setattr(data_api, "inspect_legacy", fail)
    monkeypatch.setattr(
        data_api,
        "import_legacy",
        lambda *args, **kwargs: pytest.fail("import must not run"),
    )

    response = client.post(
        "/api/migration/import",
        json={
            "source_path": str(source.resolve()),
            "initial_balance_date": "2026-08-01",
        },
    )

    assert response.status_code == 400
    assert response.json() == MIGRATION_FAILED
    assert secret not in caplog.text
    assert private_path not in caplog.text


def test_native_functions_are_not_called_by_unrelated_endpoints(client, monkeypatch):
    from app import data_api

    def forbidden(*args, **kwargs):
        raise AssertionError("native function called by unrelated endpoint")

    monkeypatch.setattr(data_api, "choose_directory", forbidden)
    monkeypatch.setattr(data_api, "open_directory", forbidden)

    assert client.get("/api/backups").status_code == 200
    assert client.post(
        "/api/backups/create", json={"include_images": False}
    ).status_code == 200


def test_open_data_folder_calls_replaceable_native_helper(client, monkeypatch):
    from app import data_api

    opened: list[Path] = []
    monkeypatch.setattr(data_api, "open_directory", opened.append)

    response = client.post("/api/system/open-data-folder")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert opened == [get_paths().data_dir]


@pytest.mark.parametrize("failure_type", [RuntimeError, OSError])
def test_open_data_folder_returns_controlled_error(
    client, monkeypatch, failure_type
):
    from app import data_api

    def unavailable(path: Path) -> None:
        raise failure_type("private platform detail")

    monkeypatch.setattr(data_api, "open_directory", unavailable)

    response = client.post("/api/system/open-data-folder")

    assert response.status_code == 501
    assert response.json() == {
        "error": "open_data_folder_unavailable",
        "message": "无法打开数据文件夹",
    }


def test_native_open_directory_is_controlled_on_non_windows(monkeypatch, tmp_path):
    from app import native_dialogs

    monkeypatch.setattr(native_dialogs.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="Windows"):
        native_dialogs.open_directory(tmp_path)


def test_old_raw_database_export_is_removed_but_csv_export_remains(client):
    old = client.get("/api/export/backup.db")
    csv = client.get("/api/export/transactions.csv")

    assert old.status_code == 404
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")


def test_lifespan_initializes_database_before_daily_backup(monkeypatch):
    from app import main

    events: list[str] = []
    monkeypatch.setattr(
        main.recovery, "recover_interrupted_installs", lambda: events.append("recovery")
    )
    monkeypatch.setattr(main.db, "init_db", lambda: events.append("init"))
    monkeypatch.setattr(
        main.ledger, "ensure_finance_config",
        lambda conn, raw, save: events.append("finance"),
    )
    monkeypatch.setattr(
        main.backup, "ensure_daily_backup", lambda: events.append("backup")
    )

    with TestClient(main.app):
        pass

    assert events == ["recovery", "init", "finance", "backup"]


def test_lifespan_skips_daily_backup_when_database_initialization_fails(monkeypatch):
    from app import main

    events: list[str] = []
    monkeypatch.setattr(
        main.recovery, "recover_interrupted_installs", lambda: events.append("recovery")
    )

    def fail_init() -> None:
        events.append("init")
        raise RuntimeError("init failed")

    monkeypatch.setattr(main.db, "init_db", fail_init)
    monkeypatch.setattr(
        main.backup, "ensure_daily_backup", lambda: events.append("backup")
    )

    with pytest.raises(RuntimeError, match="init failed"):
        with TestClient(main.app):
            pass

    assert events == ["recovery", "init"]
