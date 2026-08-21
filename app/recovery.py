"""Fail-closed startup recovery for interrupted data installations."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Callable

from app import backup as backup_module
from app import legacy_migration as legacy_module
from app.db import LEDGER_GATE
from app.paths import AppPaths, get_paths
from app.rollback_cleanup import retire_rollback_for_cleanup


MAX_JOURNAL_BYTES = 1024 * 1024


class StartupRecoveryError(RuntimeError):
    """Startup cannot safely continue while recovery material is unresolved."""


@dataclass(frozen=True)
class _ValidatedJournal:
    rollback: Path
    operation: str
    phase: str
    journal: dict[str, Any]
    manifest: Callable[[Path], dict[str, Any]]
    writer: Callable[[Path, dict[str, Any]], None]


def _is_link_or_reparse(path: Path, status: os.stat_result | None = None) -> bool:
    status = status or os.lstat(path)
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _require_unlinked_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    stack = [path]
    while stack:
        current = stack.pop()
        status = os.lstat(current)
        if _is_link_or_reparse(current, status):
            raise StartupRecoveryError("recovery path contains a link or reparse point")
        if stat.S_ISREG(status.st_mode):
            if status.st_nlink > 1:
                raise StartupRecoveryError("recovery path contains a hard-linked file")
            continue
        if stat.S_ISDIR(status.st_mode):
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
            continue
        raise StartupRecoveryError("recovery path contains an unsupported entry")


def _validate_runtime_layout(paths: AppPaths) -> None:
    for directory in (
        paths.root,
        paths.data_dir,
        paths.backups_dir,
        paths.runtime_dir,
    ):
        if not os.path.lexists(directory):
            continue
        status = os.lstat(directory)
        if _is_link_or_reparse(directory, status) or not stat.S_ISDIR(status.st_mode):
            raise StartupRecoveryError("application recovery directory is not trusted")


def _read_journal(rollback: Path) -> dict[str, Any]:
    journal_path = rollback / "journal.json"
    descriptor: int | None = None
    try:
        before = os.lstat(journal_path)
        if (
            _is_link_or_reparse(journal_path, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink > 1
            or before.st_size > MAX_JOURNAL_BYTES
        ):
            raise StartupRecoveryError("recovery journal is not a trusted regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(journal_path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise StartupRecoveryError("recovery journal changed while opening")
        raw = os.read(descriptor, MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise StartupRecoveryError("recovery journal exceeds the size limit")
        value = json.loads(raw)
    except StartupRecoveryError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StartupRecoveryError("recovery journal is unreadable or invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise StartupRecoveryError("recovery journal must be a JSON object")
    return value


def _validate_manifest(value: Any, operation: str) -> None:
    if not isinstance(value, dict) or value.get("kind") not in {
        "missing",
        "file",
        "directory",
    }:
        raise StartupRecoveryError("recovery journal contains an invalid manifest")
    kind = value["kind"]
    if kind == "missing":
        if set(value) != {"kind"}:
            raise StartupRecoveryError("missing-path manifest has unexpected fields")
        return
    if kind == "file":
        if set(value) != {"kind", "digest", "size"}:
            raise StartupRecoveryError("file manifest has unexpected fields")
        digest = value["digest"]
        size = value["size"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
            or type(size) is not int
            or size < 0
        ):
            raise StartupRecoveryError("file manifest is invalid")
        return
    required = {"kind", "files"}
    if operation == "backup-restore":
        required.add("directories")
    if set(value) != required or not isinstance(value["files"], dict):
        raise StartupRecoveryError("directory manifest has unexpected fields")
    if operation == "backup-restore" and not isinstance(value["directories"], list):
        raise StartupRecoveryError("directory manifest is invalid")
    for relative, file_value in value["files"].items():
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise StartupRecoveryError("directory manifest contains an unsafe path")
        if not isinstance(file_value, dict):
            raise StartupRecoveryError("directory file manifest is invalid")
        _validate_manifest({"kind": "file", **file_value}, operation)


def _expected_live_targets(paths: AppPaths, operation: str) -> dict[str, Path]:
    targets = {
        "database-wal": Path(f"{paths.db_path}-wal"),
        "database-shm": Path(f"{paths.db_path}-shm"),
        "database-journal": Path(f"{paths.db_path}-journal"),
        "database": paths.db_path,
        "config": paths.config_path,
    }
    if operation == "legacy-import":
        targets["images"] = paths.images_dir
    return targets


def _validate_journal(rollback: Path, paths: AppPaths) -> _ValidatedJournal:
    status = os.lstat(rollback)
    if _is_link_or_reparse(rollback, status) or not stat.S_ISDIR(status.st_mode):
        raise StartupRecoveryError("rollback entry is not a trusted directory")
    journal = _read_journal(rollback)
    operation = journal.get("operation")
    if operation == "backup-restore":
        prefix = "restore-rollback-"
        phases = {"prepared", "recovering", "recovery-incomplete", "complete", "recovered"}
        manifest = backup_module._path_manifest
        writer = backup_module._write_restore_journal
        allowed_states = {
            "pending", "original-copied", "original-missing", "installed",
            "restored", "recovery-incomplete",
        }
        required_item_fields = {
            "label", "source", "live", "original", "desired", "state", "saved",
        }
    elif operation == "legacy-import":
        prefix = "legacy-rollback-"
        phases = {"installing", "recovering", "recovery-incomplete", "installed", "recovered"}
        manifest = legacy_module._path_manifest
        writer = legacy_module._write_journal
        allowed_states = {
            "pending", "original-saved", "installed", "restored",
            "recovery-incomplete",
        }
        required_item_fields = {
            "label", "live", "original", "desired", "state", "saved",
        }
    else:
        raise StartupRecoveryError("recovery journal operation is invalid")
    if not rollback.name.startswith(prefix):
        raise StartupRecoveryError("rollback directory label does not match its operation")
    if _path_key(Path(str(journal.get("rollback_dir", "")))) != _path_key(rollback):
        raise StartupRecoveryError("recovery journal rollback directory is invalid")
    phase = journal.get("phase")
    if phase not in phases:
        raise StartupRecoveryError("recovery journal phase is invalid")
    items = journal.get("items")
    if not isinstance(items, list) or not items:
        raise StartupRecoveryError("recovery journal items are invalid")
    expected = _expected_live_targets(paths, operation)
    labels: set[str] = set()
    live_keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise StartupRecoveryError("recovery journal item is invalid")
        if set(item) != required_item_fields:
            raise StartupRecoveryError("recovery journal item fields are invalid")
        label = item.get("label")
        if not isinstance(label, str) or label in labels:
            raise StartupRecoveryError("recovery journal has duplicate labels")
        labels.add(label)
        live = Path(str(item.get("live", "")))
        live_key = _path_key(live)
        if live_key in live_keys:
            raise StartupRecoveryError("recovery journal has duplicate targets")
        live_keys.add(live_key)
        if label == "legacy-backups" and operation == "legacy-import":
            if (
                _path_key(live.parent) != _path_key(paths.backups_dir)
                or not live.name.startswith("legacy-import-")
            ):
                raise StartupRecoveryError("legacy backup target is outside backups")
        elif label == "images" and operation == "backup-restore":
            if live_key != _path_key(paths.images_dir):
                raise StartupRecoveryError("restore image target does not match AppPaths")
        elif label not in expected or live_key != _path_key(expected[label]):
            raise StartupRecoveryError("recovery journal target does not match AppPaths")
        saved = Path(str(item.get("saved", "")))
        if _path_key(saved) != _path_key(rollback / f"original-{label}"):
            raise StartupRecoveryError("recovery journal saved path is invalid")
        _validate_manifest(item.get("original"), operation)
        _validate_manifest(item.get("desired"), operation)
        if item.get("state") not in allowed_states:
            raise StartupRecoveryError("recovery journal item state is invalid")
        if operation == "backup-restore":
            source_value = item["source"]
            if source_value is not None:
                source = Path(str(source_value))
                if (
                    _path_key(source.parents[2]) != _path_key(paths.runtime_dir)
                    or not source.parents[1].name.startswith("better-money-restore-")
                ):
                    raise StartupRecoveryError("restore source path is outside runtime staging")
    required = set(expected)
    if operation == "backup-restore" and "images" in labels:
        expected["images"] = paths.images_dir
        required.add("images")
    if labels != required and labels != required | {"legacy-backups"}:
        raise StartupRecoveryError("recovery journal labels are incomplete or unexpected")
    return _ValidatedJournal(rollback, operation, phase, journal, manifest, writer)


def _copy_saved_file(saved: Path, live: Path, expected: dict[str, Any], manifest) -> None:
    live.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".startup-recovery-", suffix=".tmp", dir=live.parent,
            delete=False,
        ) as destination, saved.open("rb") as source:
            temporary = Path(destination.name)
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        if manifest(temporary) != expected:
            raise StartupRecoveryError("recovery copy does not match its journal")
        os.replace(temporary, live)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _material_manifest(validated: _ValidatedJournal, path: Path) -> dict[str, Any]:
    try:
        _require_unlinked_tree(path)
        return validated.manifest(path)
    except StartupRecoveryError:
        raise
    except Exception as exc:
        raise StartupRecoveryError(
            "recovery material could not be safely inspected"
        ) from exc


def _preflight_materials(validated: _ValidatedJournal) -> None:
    """Validate every live/saved state without writing a journal or target."""
    _require_unlinked_tree(validated.rollback)
    for item in validated.journal["items"]:
        live = Path(item["live"])
        saved = Path(item["saved"])
        original = item["original"]
        desired = item["desired"]
        state = item["state"]
        live_manifest = _material_manifest(validated, live)
        if live_manifest not in (original, desired, {"kind": "missing"}):
            raise StartupRecoveryError(
                "live recovery target does not match an allowed journal state"
            )
        if state == "pending":
            if live_manifest != original:
                raise StartupRecoveryError(
                    "pending recovery item no longer matches its original state"
                )
            if os.path.lexists(saved):
                raise StartupRecoveryError(
                    "pending recovery item unexpectedly has saved material"
                )
            continue
        saved_required = original["kind"] != "missing" and state in {
            "original-copied",
            "original-saved",
            "installed",
        }
        if (
            state == "installed"
            and validated.phase in {"recovering", "recovery-incomplete"}
            and live_manifest == original
        ):
            saved_required = False
        if state == "recovery-incomplete" and live_manifest != original:
            saved_required = original["kind"] != "missing"
        if state == "restored" and live_manifest != original:
            raise StartupRecoveryError(
                "restored recovery item no longer matches its original state"
            )
        if saved_required:
            if not os.path.lexists(saved):
                raise StartupRecoveryError("saved original is missing")
            if _material_manifest(validated, saved) != original:
                raise StartupRecoveryError(
                    "saved original does not match its journal"
                )
        elif os.path.lexists(saved):
            if original["kind"] == "missing":
                raise StartupRecoveryError(
                    "missing original unexpectedly has saved material"
                )
            if _material_manifest(validated, saved) != original:
                raise StartupRecoveryError(
                    "optional saved original does not match its journal"
                )


def _is_terminal(validated: _ValidatedJournal) -> bool:
    return validated.phase in {"complete", "installed", "recovered"}


def _preflight_recovery_set(
    validated_journals: list[_ValidatedJournal],
) -> tuple[list[_ValidatedJournal], list[_ValidatedJournal]]:
    terminal: list[_ValidatedJournal] = []
    active: list[_ValidatedJournal] = []
    active_targets: set[str] = set()
    for validated in validated_journals:
        if _is_terminal(validated):
            expected_key = (
                "original" if validated.phase == "recovered" else "desired"
            )
            if not all(
                _material_manifest(validated, Path(item["live"]))
                == item[expected_key]
                for item in validated.journal["items"]
            ):
                raise StartupRecoveryError(
                    "terminal recovery journal does not match live data"
                )
            terminal.append(validated)
            continue
        targets = {
            _path_key(Path(item["live"])) for item in validated.journal["items"]
        }
        if active_targets & targets:
            raise StartupRecoveryError(
                "active recovery journals have overlapping live targets"
            )
        active_targets.update(targets)
        active.append(validated)
    return terminal, active


def _restore_item(validated: _ValidatedJournal, item: dict[str, Any]) -> None:
    live = Path(item["live"])
    saved = Path(item["saved"])
    original = item["original"]
    current = validated.manifest(live)
    if current == original:
        item["state"] = "restored"
        return
    failed = validated.rollback / f"failed-live-{item['label']}"
    if original["kind"] == "missing":
        if os.path.lexists(failed):
            raise StartupRecoveryError("prior failed-live material blocks recovery")
        if os.path.lexists(live):
            os.replace(live, failed)
    else:
        if not os.path.lexists(saved):
            raise StartupRecoveryError("saved original is missing")
        _require_unlinked_tree(saved)
        if validated.manifest(saved) != original:
            raise StartupRecoveryError("saved original does not match its journal")
        if original["kind"] == "file":
            _copy_saved_file(saved, live, original, validated.manifest)
        else:
            temporary = Path(tempfile.mkdtemp(prefix=".startup-recovery-", dir=live.parent))
            try:
                temporary.rmdir()
                shutil.copytree(saved, temporary, symlinks=True)
                _require_unlinked_tree(temporary)
                if validated.manifest(temporary) != original:
                    raise StartupRecoveryError("recovery directory copy is invalid")
                if os.path.lexists(live):
                    if os.path.lexists(failed):
                        raise StartupRecoveryError("prior failed-live material blocks recovery")
                    os.replace(live, failed)
                os.replace(temporary, live)
            finally:
                if os.path.lexists(temporary):
                    shutil.rmtree(temporary, ignore_errors=True)
    if validated.manifest(live) != original:
        raise StartupRecoveryError("startup recovery verification failed")
    item["state"] = "restored"


def _recover_validated(validated: _ValidatedJournal) -> None:
    terminal_installed = validated.phase in {"complete", "installed"}
    terminal_recovered = validated.phase == "recovered"
    expected_key = "desired" if terminal_installed else "original"
    if terminal_installed or terminal_recovered:
        if not all(
            validated.manifest(Path(item["live"])) == item[expected_key]
            for item in validated.journal["items"]
        ):
            raise StartupRecoveryError("terminal recovery journal does not match live data")
        prefix = (
            "restore-rollback-"
            if validated.operation == "backup-restore"
            else "legacy-rollback-"
        )
        if not retire_rollback_for_cleanup(validated.rollback, prefix):
            raise StartupRecoveryError(
                "terminal recovery journal cleanup retirement failed"
            )
        return
    validated.journal["phase"] = "recovering"
    validated.writer(validated.rollback, validated.journal)
    priority = lambda item: (
        0 if item["label"] == "database" else
        1 if item["label"].startswith("database-") else 2
    )
    try:
        for item in sorted(validated.journal["items"], key=priority):
            _restore_item(validated, item)
            validated.writer(validated.rollback, validated.journal)
        if not all(
            validated.manifest(Path(item["live"])) == item["original"]
            for item in validated.journal["items"]
        ):
            raise StartupRecoveryError("startup recovery final verification failed")
        validated.journal["phase"] = "recovered"
        validated.writer(validated.rollback, validated.journal)
    except Exception as exc:
        validated.journal["phase"] = "recovery-incomplete"
        validated.journal["startup_recovery_error_type"] = type(exc).__name__
        try:
            validated.writer(validated.rollback, validated.journal)
        except Exception:
            pass
        if isinstance(exc, StartupRecoveryError):
            raise
        raise StartupRecoveryError("startup recovery could not restore original data") from exc
    prefix = (
        "restore-rollback-"
        if validated.operation == "backup-restore"
        else "legacy-rollback-"
    )
    if not retire_rollback_for_cleanup(validated.rollback, prefix):
        raise StartupRecoveryError(
            "recovered journal cleanup retirement failed"
        )


def recover_interrupted_installs() -> None:
    """Recover every trusted persistent install journal before database startup."""
    paths = get_paths()
    paths.ensure_directories()
    with LEDGER_GATE.exclusive():
        _validate_runtime_layout(paths)
        candidates: list[Path] = []
        with os.scandir(paths.runtime_dir) as entries:
            for entry in entries:
                if entry.name.startswith(("restore-rollback-", "legacy-rollback-")):
                    candidates.append(Path(entry.path))
        try:
            validated = [
                _validate_journal(candidate, paths)
                for candidate in sorted(candidates, key=lambda path: path.name)
            ]
            terminal, active = _preflight_recovery_set(validated)
            for journal in active:
                _preflight_materials(journal)
            for journal in terminal:
                _recover_validated(journal)
            for journal in active:
                _preflight_materials(journal)
                _recover_validated(journal)
        except StartupRecoveryError:
            raise
        except Exception as exc:
            raise StartupRecoveryError("startup recovery failed closed") from exc
