"""Verified ZIP backups and staged restore for application data."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any
import zipfile

from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    database_integrity,
    migrate_database,
)
from app.paths import get_paths
from app.version import APP_VERSION


BACKUP_FORMAT_VERSION = 1
KEEP = 30
_REQUIRED_MEMBERS = {
    "manifest.json",
    "data/better_money.db",
    "data/config.json",
}
_MANIFEST_FIELDS = {
    "format_version",
    "created_at",
    "app_version",
    "schema_version",
    "reason",
    "includes_images",
}
_REASON_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    *(f"COM{number}" for number in "¹²³"),
    *(f"LPT{number}" for number in "¹²³"),
}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')


@dataclass(frozen=True)
class BackupManifest:
    format_version: int
    created_at: str
    app_version: str
    schema_version: int
    reason: str
    includes_images: bool


def _validate_reason(reason: str) -> None:
    if not isinstance(reason, str) or not _REASON_PATTERN.fullmatch(reason):
        raise ValueError("backup reason must be a safe filename component")


def _parse_created_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("backup manifest created_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("backup manifest created_at must include a timezone")
    return parsed


def _manifest_from_dict(value: Any) -> BackupManifest:
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise ValueError("backup manifest must contain exactly the required fields")
    if type(value["format_version"]) is not int:
        raise ValueError("backup manifest format_version is invalid")
    if value["format_version"] != BACKUP_FORMAT_VERSION:
        raise ValueError("backup manifest format_version is unsupported")
    if not isinstance(value["created_at"], str):
        raise ValueError("backup manifest created_at is invalid")
    _parse_created_at(value["created_at"])
    if not isinstance(value["app_version"], str) or not value["app_version"]:
        raise ValueError("backup manifest app_version is invalid")
    if type(value["schema_version"]) is not int:
        raise ValueError("backup manifest schema_version is invalid")
    if not 0 <= value["schema_version"] <= CURRENT_SCHEMA_VERSION:
        raise ValueError("backup manifest schema_version is unsupported")
    _validate_reason(value["reason"])
    if type(value["includes_images"]) is not bool:
        raise ValueError("backup manifest includes_images is invalid")
    return BackupManifest(**value)


def _windows_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.filename
    if not name or "\\" in name or info.is_dir() or info.flag_bits & 0x1:
        raise ValueError("invalid ZIP member form")
    parts = tuple(name.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid ZIP member component")
    for part in parts:
        if part.endswith((".", " ")):
            raise ValueError("Windows strips trailing dots and spaces")
        if any(ord(character) < 32 for character in part):
            raise ValueError("Windows control character in member")
        if _WINDOWS_INVALID_CHARACTERS.intersection(part):
            raise ValueError("Windows-invalid character in member")
        device_stem = part.split(".", 1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError("Windows reserved device name in member")
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == 0o120000:
        raise ValueError("symbolic-link member")
    return parts


def _windows_target_key(parts: tuple[str, ...]) -> str:
    return ntpath.normcase(ntpath.normpath(ntpath.join(*parts)))


def _validated_infos(
    archive: zipfile.ZipFile,
    manifest: BackupManifest | None = None,
) -> dict[str, zipfile.ZipInfo]:
    infos: dict[str, zipfile.ZipInfo] = {}
    target_names: dict[str, str] = {}
    member_parts: dict[str, tuple[str, ...]] = {}
    for info in archive.infolist():
        try:
            parts = _windows_member_parts(info)
        except ValueError as exc:
            raise ValueError(
                f"backup contains unsafe member: {info.filename!r}"
            ) from exc
        if info.filename in infos:
            raise ValueError(f"backup contains duplicate member: {info.filename!r}")
        target_key = _windows_target_key(parts)
        if target_key in target_names:
            raise ValueError(
                "backup contains duplicate Windows target: "
                f"{target_names[target_key]!r} and {info.filename!r}"
            )
        target_names[target_key] = info.filename
        infos[info.filename] = info
        member_parts[info.filename] = parts
    if not _REQUIRED_MEMBERS <= set(infos):
        raise ValueError("backup is missing a required member")
    for name, parts in member_parts.items():
        if name in _REQUIRED_MEMBERS:
            continue
        if len(parts) < 3 or parts[:2] != ("data", "images"):
            raise ValueError(f"backup contains undeclared member: {name!r}")
        if manifest is not None and not manifest.includes_images:
            raise ValueError("backup contains images not declared by its manifest")
    return infos


def _contains_api_key(value: Any) -> bool:
    if isinstance(value, dict):
        return "api_key" in value or any(_contains_api_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_api_key(item) for item in value)
    return False


def _sanitize_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_config(item)
            for key, item in value.items()
            if key != "api_key"
        }
    if isinstance(value, list):
        return [_sanitize_config(item) for item in value]
    return value


def _read_current_config() -> dict[str, Any]:
    config_path = get_paths().config_path
    if not config_path.exists():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("current configuration is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("current configuration must be a JSON object")
    return _sanitize_config(value)


def _database_manifest_version(database_path: Path) -> int:
    try:
        with closing(sqlite3.connect(database_path)) as conn:
            valid, detail = database_integrity(conn)
            if not valid:
                raise ValueError(f"backup database integrity check failed: {detail}")
            return conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise ValueError("backup database is not a valid SQLite database") from exc


def inspect_backup(archive: Path) -> BackupManifest:
    """Validate an archive manifest, allowed members, config, and SQLite payload."""
    archive = Path(archive)
    if not archive.is_file():
        raise ValueError("backup archive does not exist")
    try:
        with zipfile.ZipFile(archive) as zipped:
            infos = _validated_infos(zipped)
            if zipped.testzip() is not None:
                raise ValueError("backup archive has a failed CRC check")
            try:
                manifest_value = json.loads(zipped.read("manifest.json"))
                config_value = json.loads(zipped.read("data/config.json"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("backup contains invalid JSON") from exc
            manifest = _manifest_from_dict(manifest_value)
            _validated_infos(zipped, manifest)
            if not isinstance(config_value, dict):
                raise ValueError("backup config must be a JSON object")
            if _contains_api_key(config_value):
                raise ValueError("backup config contains api_key")
            with tempfile.TemporaryDirectory(prefix="better-money-inspect-") as temporary:
                database_path = Path(temporary) / "better_money.db"
                database_path.write_bytes(zipped.read(infos["data/better_money.db"]))
                database_version = _database_manifest_version(database_path)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValueError("backup archive is not a valid ZIP file") from exc
    if database_version != manifest.schema_version:
        raise ValueError("backup manifest schema_version does not match its database")
    return manifest


def _unique_destination(created_at: datetime, reason: str) -> Path:
    backups_dir = get_paths().backups_dir
    stamp = created_at.strftime("%Y%m%d-%H%M%S")
    destination = backups_dir / f"better-money-{stamp}-{reason}.zip"
    counter = 1
    while destination.exists():
        destination = backups_dir / f"better-money-{stamp}-{counter}-{reason}.zip"
        counter += 1
    return destination


def create_backup(reason: str, include_images: bool = False) -> Path:
    """Create, validate, and atomically publish a sanitized backup archive."""
    _validate_reason(reason)
    if type(include_images) is not bool:
        raise TypeError("include_images must be a bool")
    paths = get_paths()
    paths.ensure_directories()
    if not paths.db_path.is_file():
        raise FileNotFoundError("application database does not exist")

    created_at = datetime.now().astimezone()
    temporary_zip: Path | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="better-money-backup-", dir=paths.runtime_dir
        ) as temporary:
            database_copy = Path(temporary) / "better_money.db"
            with closing(sqlite3.connect(paths.db_path)) as source:
                with closing(sqlite3.connect(database_copy)) as destination:
                    source.backup(destination)
            schema_version = _database_manifest_version(database_copy)
            manifest = BackupManifest(
                format_version=BACKUP_FORMAT_VERSION,
                created_at=created_at.isoformat(timespec="seconds"),
                app_version=APP_VERSION,
                schema_version=schema_version,
                reason=reason,
                includes_images=include_images,
            )
            config = _read_current_config()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".better-money-",
                suffix=".tmp",
                dir=paths.backups_dir,
                delete=False,
            ) as temporary_file:
                temporary_zip = Path(temporary_file.name)
            with zipfile.ZipFile(
                temporary_zip, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(asdict(manifest), ensure_ascii=False, indent=2),
                )
                archive.write(database_copy, "data/better_money.db")
                archive.writestr(
                    "data/config.json",
                    json.dumps(config, ensure_ascii=False, indent=2),
                )
                if include_images:
                    for image in sorted(paths.images_dir.rglob("*")):
                        if image.is_file() and not image.is_symlink():
                            relative = image.relative_to(paths.images_dir).as_posix()
                            archive.write(image, f"data/images/{relative}")
        inspect_backup(temporary_zip)
        destination_path = _unique_destination(created_at, reason)
        os.replace(temporary_zip, destination_path)
        temporary_zip = None
        return destination_path
    finally:
        if temporary_zip is not None:
            temporary_zip.unlink(missing_ok=True)


def _extract_declared_members(archive: Path, staging: Path, manifest: BackupManifest) -> None:
    with zipfile.ZipFile(archive) as zipped:
        infos = _validated_infos(zipped, manifest)
        staging_root = staging.resolve()
        images_root = (staging / "data" / "images").resolve()
        windows_images_root = ntpath.normcase(ntpath.abspath(str(images_root)))
        for name, info in infos.items():
            if name == "manifest.json":
                continue
            parts = _windows_member_parts(info)
            destination = staging.joinpath(*parts)
            resolved = destination.resolve()
            if not resolved.is_relative_to(staging_root):
                raise ValueError(f"backup contains unsafe member: {name!r}")
            if name not in _REQUIRED_MEMBERS:
                windows_destination = ntpath.normcase(ntpath.abspath(str(resolved)))
                try:
                    common = ntpath.commonpath(
                        [windows_images_root, windows_destination]
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"backup image member escapes image directory: {name!r}"
                    ) from exc
                if common != windows_images_root or windows_destination == common:
                    raise ValueError(
                        f"backup image member escapes image directory: {name!r}"
                    )
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as source, resolved.open("wb") as output:
                shutil.copyfileobj(source, output)


def _validate_staged_data(staging: Path, manifest: BackupManifest) -> None:
    database_path = staging / "data" / "better_money.db"
    extracted_schema_version = _database_manifest_version(database_path)
    if extracted_schema_version != manifest.schema_version:
        raise ValueError(
            "extracted database schema does not match the inspected manifest"
        )
    with closing(sqlite3.connect(database_path)) as conn:
        migrate_database(conn)
        valid, detail = database_integrity(conn)
        if not valid:
            raise RuntimeError(f"database integrity check failed: {detail}")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError("staged database did not reach the current schema version")
    config_path = staging / "data" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup config is invalid") from exc
    if not isinstance(config, dict) or _contains_api_key(config):
        raise ValueError("backup config is not sanitized")
    if manifest.includes_images:
        (staging / "data" / "images").mkdir(parents=True, exist_ok=True)


def _replace_live_data(staging: Path, includes_images: bool) -> None:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    rollback = staging / "rollback"
    rollback.mkdir()
    replacements = [
        (staging / "data" / "better_money.db", paths.db_path, rollback / "database"),
        (staging / "data" / "config.json", paths.config_path, rollback / "config"),
    ]
    if includes_images:
        replacements.append(
            (staging / "data" / "images", paths.images_dir, rollback / "images")
        )

    installed: list[Path] = []
    saved_originals: list[tuple[Path, Path]] = []
    try:
        for source, live, saved in replacements:
            if live.exists():
                os.replace(live, saved)
                saved_originals.append((saved, live))
            os.replace(source, live)
            installed.append(live)
    except Exception:
        abandoned = rollback / "abandoned"
        abandoned.mkdir(exist_ok=True)
        for index, live in enumerate(reversed(installed)):
            if live.exists():
                os.replace(live, abandoned / f"installed-{index}")
        for saved, live in reversed(saved_originals):
            if saved.exists():
                os.replace(saved, live)
        raise


def restore_backup(archive: Path) -> None:
    """Restore a verified backup via staging and rollback-capable replacements."""
    archive = Path(archive)
    paths = get_paths()
    paths.ensure_directories()
    with tempfile.TemporaryDirectory(
        prefix="better-money-restore-", dir=paths.runtime_dir
    ) as temporary:
        staging = Path(temporary)
        archive_snapshot = staging / "supplied-backup.zip"
        try:
            with archive.open("rb") as source, archive_snapshot.open("xb") as target:
                shutil.copyfileobj(source, target)
        except OSError as exc:
            raise ValueError("backup archive could not be copied for restore") from exc
        manifest = inspect_backup(archive_snapshot)
        create_backup("pre-restore")
        _extract_declared_members(archive_snapshot, staging, manifest)
        _validate_staged_data(staging, manifest)
        _replace_live_data(staging, manifest.includes_images)


def _valid_automatic_archives() -> list[tuple[datetime, Path]]:
    automatic: list[tuple[datetime, Path]] = []
    for archive in get_paths().backups_dir.glob("*.zip"):
        try:
            manifest = inspect_backup(archive)
        except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError):
            continue
        if manifest.reason == "automatic":
            automatic.append((_parse_created_at(manifest.created_at), archive))
    return automatic


def ensure_daily_backup(keep: int = KEEP) -> Path | None:
    """Create at most one automatic backup today and prune only verified automatic ZIPs."""
    if type(keep) is not int or keep < 1:
        raise ValueError("keep must be a positive integer")
    paths = get_paths()
    paths.ensure_directories()
    today = datetime.now().astimezone().date()
    automatic = _valid_automatic_archives()
    created: Path | None = None
    if not any(created_at.astimezone().date() == today for created_at, _ in automatic):
        created = create_backup("automatic")
        automatic = _valid_automatic_archives()
    automatic.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, archive in automatic[keep:]:
        archive.unlink()
    return created


def backup_database(keep: int = KEEP) -> str | None:
    """Compatibility wrapper for the existing startup hook."""
    archive = ensure_daily_backup(keep)
    return str(archive) if archive is not None else None
