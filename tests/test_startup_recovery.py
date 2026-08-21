from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from app import backup as backup_module
from app import legacy_migration as legacy_module
from app.backup import create_backup, restore_backup
from app.db import init_db
from app.migrations import migrate_database
from app.paths import get_paths


class SimulatedProcessTermination(BaseException):
    pass


def _write_live(note: str, config_value: str, image_value: str) -> None:
    paths = get_paths()
    init_db()
    with closing(sqlite3.connect(paths.db_path)) as connection, connection:
        connection.execute("DELETE FROM transactions")
        connection.execute(
            "INSERT INTO transactions("
            "date, amount, type, category, note, created_at, updated_at"
            ") VALUES ('2026-08-20', 1, '支出', '其他', ?, 'now', 'now')",
            (note,),
        )
    paths.config_path.write_text(
        json.dumps({"marker": config_value}), encoding="utf-8"
    )
    paths.images_dir.mkdir(parents=True, exist_ok=True)
    (paths.images_dir / "marker.txt").write_text(image_value, encoding="utf-8")


def _write_legacy_source(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True)
    with closing(sqlite3.connect(data_dir / "better_money.db")) as connection:
        migrate_database(connection)
        with connection:
            connection.execute(
                "INSERT INTO transactions("
                "date, amount, type, category, note, created_at, updated_at"
                ") VALUES ('2026-08-19', 2, '支出', '其他', "
                "'legacy', 'now', 'now')"
            )
    (data_dir / "config.json").write_text(
        json.dumps({"marker": "legacy"}), encoding="utf-8"
    )
    (data_dir / "images").mkdir()
    (data_dir / "images" / "marker.txt").write_text("legacy", encoding="utf-8")
    return data_dir


def _live_manifest() -> dict:
    return backup_module._capture_live_baseline(True)


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {".": None}
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        snapshot[relative] = candidate.read_bytes() if candidate.is_file() else None
    return snapshot


def _interrupted_restore_rollback(app_home, monkeypatch) -> Path:
    _write_live("replacement", "replacement", "replacement")
    archive = create_backup("replacement", include_images=True)
    _write_live("original", "original", "original")
    real_write = backup_module._write_restore_journal

    def terminate_after_database_install(rollback, journal):
        real_write(rollback, journal)
        database = next(item for item in journal["items"] if item["label"] == "database")
        if database["state"] == "installed":
            raise SimulatedProcessTermination()

    monkeypatch.setattr(
        backup_module, "_write_restore_journal", terminate_after_database_install
    )
    with pytest.raises(SimulatedProcessTermination):
        restore_backup(archive)
    monkeypatch.setattr(backup_module, "_write_restore_journal", real_write)
    rollbacks = list(get_paths().runtime_dir.glob("restore-rollback-*"))
    assert len(rollbacks) == 1
    return rollbacks[0]


def _relocate_rollback(rollback: Path, destination: Path) -> Path:
    os_replace = backup_module.os.replace
    os_replace(rollback, destination)
    journal_path = destination / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["rollback_dir"] = str(destination)
    for item in journal["items"]:
        item["saved"] = str(destination / f"original-{item['label']}")
    journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def _capture_live_pair(marker: str) -> tuple[bytes, bytes]:
    _write_live(marker, marker, marker)
    return get_paths().db_path.read_bytes(), get_paths().config_path.read_bytes()


def _write_restore_journal_fixture(
    name: str,
    original: tuple[bytes, bytes],
    desired: tuple[dict, dict],
    *,
    phase: str = "prepared",
) -> Path:
    paths = get_paths()
    rollback = paths.runtime_dir / name
    rollback.mkdir()
    saved_database = rollback / "original-database"
    saved_config = rollback / "original-config"
    saved_database.write_bytes(original[0])
    saved_config.write_bytes(original[1])
    staging = paths.runtime_dir / f"better-money-restore-{name}"
    items = []
    for label, source, live in backup_module._replacement_specifications(None, False):
        saved = rollback / f"original-{label}"
        if label == "database":
            original_manifest = backup_module._path_manifest(saved_database)
            desired_manifest = desired[0]
            source = staging / "data" / "better_money.db"
            state = (
                "installed"
                if phase == "complete"
                else "restored" if phase == "recovered" else "original-copied"
            )
        elif label == "config":
            original_manifest = backup_module._path_manifest(saved_config)
            desired_manifest = desired[1]
            source = staging / "data" / "config.json"
            state = (
                "installed"
                if phase == "complete"
                else "restored" if phase == "recovered" else "original-copied"
            )
        else:
            original_manifest = {"kind": "missing"}
            desired_manifest = {"kind": "missing"}
            state = (
                "installed"
                if phase == "complete"
                else "restored" if phase == "recovered" else "original-missing"
            )
        items.append(
            {
                "label": label,
                "source": str(source) if source is not None else None,
                "live": str(live),
                "original": original_manifest,
                "desired": desired_manifest,
                "state": state,
                "saved": str(saved),
            }
        )
    journal = {
        "operation": "backup-restore",
        "phase": phase,
        "rollback_dir": str(rollback),
        "items": items,
    }
    (rollback / "journal.json").write_text(
        json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rollback


def test_fresh_start_recovers_restore_interrupted_after_database_replacement(
    app_home, monkeypatch
):
    _write_live("replacement", "replacement", "replacement")
    archive = create_backup("replacement", include_images=True)
    _write_live("original", "original", "original")
    original = _live_manifest()
    real_write = backup_module._write_restore_journal

    def terminate_after_database_install(rollback, journal):
        real_write(rollback, journal)
        database = next(item for item in journal["items"] if item["label"] == "database")
        if database["state"] == "installed":
            raise SimulatedProcessTermination()

    monkeypatch.setattr(
        backup_module, "_write_restore_journal", terminate_after_database_install
    )
    with pytest.raises(SimulatedProcessTermination):
        restore_backup(archive)
    monkeypatch.setattr(backup_module, "_write_restore_journal", real_write)

    from app.recovery import recover_interrupted_installs

    recover_interrupted_installs()
    recover_interrupted_installs()

    assert _live_manifest() == original
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))


def test_fresh_start_recovers_legacy_import_interrupted_after_database_replacement(
    app_home, monkeypatch
):
    source = _write_legacy_source(app_home / "legacy" / "data")
    _write_live("original", "original", "original")
    original = _live_manifest()
    real_write = legacy_module._write_journal

    def terminate_after_database_install(rollback, journal):
        real_write(rollback, journal)
        database = next(item for item in journal["items"] if item["label"] == "database")
        if database["state"] == "installed":
            raise SimulatedProcessTermination()

    monkeypatch.setattr(legacy_module, "_write_journal", terminate_after_database_install)
    with pytest.raises(SimulatedProcessTermination):
        legacy_module.import_legacy(source, "2026-08-19")
    monkeypatch.setattr(legacy_module, "_write_journal", real_write)

    from app.recovery import recover_interrupted_installs

    recover_interrupted_installs()
    recover_interrupted_installs()

    assert _live_manifest() == original
    assert not list(get_paths().runtime_dir.glob("legacy-rollback-*"))


def test_startup_recovery_rejects_untrusted_journal_target_and_preserves_material(
    app_home,
):
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    paths = get_paths()
    outside = app_home / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    rollback = paths.runtime_dir / "restore-rollback-malicious"
    rollback.mkdir()
    journal = {
        "operation": "backup-restore",
        "phase": "prepared",
        "rollback_dir": str(rollback),
        "items": [
            {
                "label": "database",
                "live": str(outside),
                "saved": str(rollback / "original-database"),
                "source": None,
                "original": {"kind": "missing"},
                "desired": {"kind": "missing"},
                "state": "original-missing",
            }
        ],
    }
    (rollback / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(StartupRecoveryError):
        recover_interrupted_installs()

    assert outside.read_text(encoding="utf-8") == "untouched"
    assert (rollback / "journal.json").is_file()


def test_material_preflight_failure_makes_no_live_or_journal_mutation(
    app_home, monkeypatch
):
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    rollback = _interrupted_restore_rollback(app_home, monkeypatch)
    (rollback / "original-config").unlink()
    get_paths().config_path.write_text(
        json.dumps({"marker": "replacement"}), encoding="utf-8"
    )
    before_live = _live_manifest()
    before_database = get_paths().db_path.read_bytes()
    before_config = get_paths().config_path.read_bytes()
    before_material = _tree_bytes(rollback)
    before_journal = (rollback / "journal.json").read_bytes()

    with pytest.raises(StartupRecoveryError):
        recover_interrupted_installs()

    assert _live_manifest() == before_live
    assert get_paths().db_path.read_bytes() == before_database
    assert get_paths().config_path.read_bytes() == before_config
    assert _tree_bytes(rollback) == before_material
    assert (rollback / "journal.json").read_bytes() == before_journal
    assert json.loads(before_journal)["phase"] == "prepared"


def test_all_journals_material_preflight_before_first_recovery_mutation(
    app_home, monkeypatch
):
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    rollback = _interrupted_restore_rollback(app_home, monkeypatch)
    first = _relocate_rollback(
        rollback, get_paths().runtime_dir / "restore-rollback-000-valid"
    )
    second = get_paths().runtime_dir / "restore-rollback-999-invalid"
    shutil.copytree(first, second)
    second_journal_path = second / "journal.json"
    second_journal = json.loads(second_journal_path.read_text(encoding="utf-8"))
    second_journal["rollback_dir"] = str(second)
    for item in second_journal["items"]:
        item["saved"] = str(second / f"original-{item['label']}")
    second_journal_path.write_text(
        json.dumps(second_journal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (second / "original-config").unlink()
    before_live = _live_manifest()
    before_first = _tree_bytes(first)
    before_second = _tree_bytes(second)

    with pytest.raises(StartupRecoveryError):
        recover_interrupted_installs()

    assert _live_manifest() == before_live
    assert _tree_bytes(first) == before_first
    assert _tree_bytes(second) == before_second


def test_overlapping_active_journals_fail_closed_without_ordering_a_winner(
    app_home,
):
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    original_a = _capture_live_pair("conflict-a")
    original_c = _capture_live_pair("conflict-c")
    _capture_live_pair("conflict-b")
    desired_b = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    first = _write_restore_journal_fixture(
        "restore-rollback-000-a", original_a, desired_b
    )
    second = _write_restore_journal_fixture(
        "restore-rollback-999-c", original_c, desired_b
    )
    before_live = _live_manifest()
    before_first = _tree_bytes(first)
    before_second = _tree_bytes(second)

    with pytest.raises(StartupRecoveryError, match="overlap|conflict"):
        recover_interrupted_installs()

    assert _live_manifest() == before_live
    assert _tree_bytes(first) == before_first
    assert _tree_bytes(second) == before_second


def test_verified_terminal_cleanup_can_coexist_with_one_active_journal(app_home):
    from app.recovery import recover_interrupted_installs

    active_original = _capture_live_pair("terminal-active-original")
    terminal_original = _capture_live_pair("terminal-cleanup-original")
    _capture_live_pair("terminal-active-desired")
    desired = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    _write_restore_journal_fixture(
        "restore-rollback-000-terminal",
        terminal_original,
        desired,
        phase="complete",
    )
    _write_restore_journal_fixture(
        "restore-rollback-999-active", active_original, desired
    )

    recover_interrupted_installs()

    assert get_paths().db_path.read_bytes() == active_original[0]
    assert get_paths().config_path.read_bytes() == active_original[1]
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))


def test_invalid_terminal_plus_active_conflict_fails_before_active_mutation(app_home):
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    active_original = _capture_live_pair("invalid-terminal-active-original")
    current_b = _capture_live_pair("invalid-terminal-current")
    desired_d_bytes = _capture_live_pair("invalid-terminal-desired")
    desired_d = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    get_paths().db_path.write_bytes(current_b[0])
    get_paths().config_path.write_bytes(current_b[1])
    current_desired = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    _write_restore_journal_fixture(
        "restore-rollback-000-active", active_original, current_desired
    )
    _write_restore_journal_fixture(
        "restore-rollback-999-terminal",
        current_b,
        desired_d,
        phase="complete",
    )
    before_live = _live_manifest()
    before = {
        path.name: _tree_bytes(path)
        for path in get_paths().runtime_dir.glob("restore-rollback-*")
    }

    with pytest.raises(StartupRecoveryError):
        recover_interrupted_installs()

    assert _live_manifest() == before_live
    assert {
        path.name: _tree_bytes(path)
        for path in get_paths().runtime_dir.glob("restore-rollback-*")
    } == before


def test_terminal_retire_failure_blocks_before_later_active_journal_write(
    app_home, monkeypatch
):
    from app import rollback_cleanup
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    active_original = _capture_live_pair("retire-block-active-original")
    terminal_original = _capture_live_pair("retire-block-terminal-original")
    _capture_live_pair("retire-block-current")
    current = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    terminal = _write_restore_journal_fixture(
        "restore-rollback-000-terminal",
        terminal_original,
        current,
        phase="complete",
    )
    active = _write_restore_journal_fixture(
        "restore-rollback-999-active", active_original, current
    )
    before_live = _live_manifest()
    before_active = _tree_bytes(active)
    real_replace = rollback_cleanup.os.replace

    def fail_terminal_retirement(source_path, destination_path):
        if Path(source_path) == terminal:
            raise OSError("terminal retirement failed")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        rollback_cleanup.os, "replace", fail_terminal_retirement
    )
    with pytest.raises(StartupRecoveryError, match="retire|cleanup"):
        recover_interrupted_installs()

    assert _live_manifest() == before_live
    assert _tree_bytes(active) == before_active
    assert json.loads((active / "journal.json").read_text())["phase"] == "prepared"
    assert terminal.is_dir()

    monkeypatch.setattr(rollback_cleanup.os, "replace", real_replace)
    recover_interrupted_installs()
    assert get_paths().db_path.read_bytes() == active_original[0]
    assert get_paths().config_path.read_bytes() == active_original[1]
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))


def test_recovered_journal_retire_failure_blocks_startup_until_retry(
    app_home, monkeypatch
):
    from app import rollback_cleanup
    from app.recovery import StartupRecoveryError, recover_interrupted_installs

    original = _capture_live_pair("recovered-retire-original")
    _capture_live_pair("recovered-retire-current")
    desired = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    rollback = _write_restore_journal_fixture(
        "restore-rollback-recovery-retire", original, desired
    )
    real_replace = rollback_cleanup.os.replace

    def fail_recovered_retirement(source_path, destination_path):
        if Path(source_path) == rollback:
            raise OSError("recovered retirement failed")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        rollback_cleanup.os, "replace", fail_recovered_retirement
    )
    with pytest.raises(StartupRecoveryError, match="retire|cleanup"):
        recover_interrupted_installs()

    assert get_paths().db_path.read_bytes() == original[0]
    assert get_paths().config_path.read_bytes() == original[1]
    assert json.loads((rollback / "journal.json").read_text())["phase"] == "recovered"

    monkeypatch.setattr(rollback_cleanup.os, "replace", real_replace)
    recover_interrupted_installs()
    assert not rollback.exists()


@pytest.mark.parametrize("phase", ["complete", "recovered"])
def test_restore_terminal_cleanup_does_not_require_obsolete_saved_originals(
    app_home, phase
):
    from app.recovery import recover_interrupted_installs

    original = _capture_live_pair(f"terminal-{phase}-original")
    _capture_live_pair(f"terminal-{phase}-desired")
    desired = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    if phase == "recovered":
        get_paths().db_path.write_bytes(original[0])
        get_paths().config_path.write_bytes(original[1])
    rollback = _write_restore_journal_fixture(
        f"restore-rollback-terminal-{phase}", original, desired, phase=phase
    )
    for saved in rollback.glob("original-*"):
        if saved.is_dir():
            shutil.rmtree(saved)
        else:
            saved.unlink()

    recover_interrupted_installs()

    assert not rollback.exists()
    expected = original if phase == "recovered" else None
    if expected is not None:
        assert get_paths().db_path.read_bytes() == expected[0]
        assert get_paths().config_path.read_bytes() == expected[1]


def test_installed_legacy_terminal_does_not_require_obsolete_saved_originals(
    app_home, monkeypatch
):
    from app.recovery import recover_interrupted_installs

    source = _write_legacy_source(app_home / "terminal-installed-legacy" / "data")
    _write_live("legacy-terminal-original", "original", "original")
    real_retire = legacy_module.retire_rollback_for_cleanup
    monkeypatch.setattr(
        legacy_module, "retire_rollback_for_cleanup", lambda *args: True
    )
    legacy_module.import_legacy(source, "2026-08-19")
    monkeypatch.setattr(
        legacy_module, "retire_rollback_for_cleanup", real_retire
    )
    rollback = next(get_paths().runtime_dir.glob("legacy-rollback-*"))
    assert json.loads((rollback / "journal.json").read_text())["phase"] == "installed"
    for saved in rollback.glob("original-*"):
        if saved.is_dir():
            shutil.rmtree(saved)
        else:
            saved.unlink()

    recover_interrupted_installs()

    assert not rollback.exists()
    with closing(sqlite3.connect(get_paths().db_path)) as connection:
        assert connection.execute(
            "SELECT note FROM transactions ORDER BY id"
        ).fetchall() == [("legacy",)]


def test_unsafe_obsolete_saved_hardlink_is_retired_without_touching_target(
    app_home,
):
    from app.recovery import recover_interrupted_installs

    original = _capture_live_pair("unsafe-obsolete-original")
    _capture_live_pair("unsafe-obsolete-desired")
    desired = (
        backup_module._path_manifest(get_paths().db_path),
        backup_module._path_manifest(get_paths().config_path),
    )
    rollback = _write_restore_journal_fixture(
        "restore-rollback-unsafe-obsolete", original, desired, phase="complete"
    )
    outside = app_home / "outside-obsolete-original"
    outside.write_bytes(b"outside-must-remain")
    saved = rollback / "original-database"
    saved.unlink()
    try:
        os.link(outside, saved)
    except OSError as exc:
        pytest.skip(f"hardlink unavailable: {exc}")

    recover_interrupted_installs()

    assert outside.read_bytes() == b"outside-must-remain"
    assert not list(get_paths().runtime_dir.glob("restore-rollback-*"))
    retired = list(
        get_paths().runtime_dir.glob(".better-money-retired-cleanup-*")
    )
    assert len(retired) == 1
    assert (retired[0] / "original-database").is_file()
