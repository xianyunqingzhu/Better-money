"""Verified ZIP backups and staged restore for application data."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
from typing import Any
import zipfile

from app.db import LEDGER_GATE
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    database_integrity,
    migrate_database,
)
from app.paths import get_paths
from app.rollback_cleanup import retire_rollback_for_cleanup
from app.version import APP_VERSION


BACKUP_FORMAT_VERSION = 1
KEEP = 30
MAX_ARCHIVE_MEMBERS = 4096
MAX_MEMBER_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
MIN_COMPRESSION_RATIO_BYTES = 1024 * 1024
MAX_AGGREGATE_COMPRESSION_RATIO = 100
MIN_AGGREGATE_EXPANDED_BYTES = 128 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_PHYSICAL_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
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


class InvalidBackupError(ValueError):
    """The supplied archive is not a safe, valid Better Money backup."""


class RestoreFailedError(RuntimeError):
    """Restore failed and the prior live state was fully preserved."""


class RestoreIncompleteError(RuntimeError):
    """Restore recovery was incomplete and persistent recovery material remains."""


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


def _read_exact_at(source, offset: int, size: int) -> bytes:
    source.seek(offset)
    value = source.read(size)
    if len(value) != size:
        raise InvalidBackupError("backup central directory is truncated")
    return value


def _find_eocd(source, archive_size: int) -> tuple[int, bytes, list[Any]]:
    minimum_size = 22
    if archive_size < minimum_size:
        raise InvalidBackupError("backup ZIP end record is missing")
    selected = zipfile._EndRecData(source)
    if selected is None:
        raise InvalidBackupError("backup ZIP end record is missing")
    offset = selected[-1]
    if type(offset) is not int or offset < 0:
        raise InvalidBackupError("backup ZIP end record offset is invalid")
    eocd = _read_exact_at(source, offset, minimum_size)
    comment_size = struct.unpack_from("<H", eocd, 20)[0]
    if offset + minimum_size + comment_size != archive_size:
        raise InvalidBackupError("backup ZIP comment length is invalid")
    return offset, eocd, selected


def _zip64_directory_values(
    source,
    eocd_offset: int,
) -> tuple[int, int, int, int]:
    locator_offset = eocd_offset - 20
    if locator_offset < 0:
        raise InvalidBackupError("backup Zip64 locator is missing")
    locator = _read_exact_at(source, locator_offset, 20)
    signature, record_disk, _reported_record_offset, total_disks = struct.unpack(
        "<4sIQI", locator
    )
    if (
        signature != _ZIP64_LOCATOR_SIGNATURE
        or record_disk != 0
        or total_disks != 1
    ):
        raise InvalidBackupError("backup Zip64 locator is invalid")
    record_offset = locator_offset - 56
    if record_offset < 0:
        raise InvalidBackupError("backup Zip64 end record is missing")
    fixed = _read_exact_at(source, record_offset, 56)
    (
        signature,
        record_size,
        _made_by,
        _required,
        disk_number,
        directory_disk,
        disk_entries,
        total_entries,
        directory_size,
        directory_offset,
    ) = struct.unpack("<4sQ2H2I4Q", fixed)
    if (
        signature != _ZIP64_EOCD_SIGNATURE
        or record_size != 44
        or disk_number != 0
        or directory_disk != 0
        or disk_entries != total_entries
    ):
        raise InvalidBackupError("backup Zip64 end record is invalid")
    return total_entries, directory_size, directory_offset, record_offset


def _preflight_zip_central_directory(archive: Path) -> None:
    """Bound and validate ZIP metadata before ZipFile allocates ZipInfo objects."""
    try:
        archive_size = archive.stat().st_size
        with archive.open("rb") as source:
            eocd_offset, eocd, selected = _find_eocd(source, archive_size)
            (
                signature,
                disk_number,
                directory_disk,
                disk_entries,
                total_entries,
                directory_size,
                directory_offset,
                _comment_size,
            ) = struct.unpack("<4s4H2IH", eocd)
            if signature != _EOCD_SIGNATURE:
                raise InvalidBackupError("backup ZIP end record is invalid")
            raw_uses_zip64 = (
                disk_entries == 0xFFFF
                or total_entries == 0xFFFF
                or directory_size == 0xFFFFFFFF
                or directory_offset == 0xFFFFFFFF
            )
            runtime_uses_zip64 = (
                selected[zipfile._ECD_SIGNATURE] == _ZIP64_EOCD_SIGNATURE
            )
            if raw_uses_zip64 and not runtime_uses_zip64:
                raise InvalidBackupError(
                    "backup Zip64 format is unsupported by this runtime"
                )
            if runtime_uses_zip64:
                (
                    total_entries,
                    directory_size,
                    directory_offset,
                    directory_boundary,
                ) = _zip64_directory_values(source, eocd_offset)
                selected_values = (
                    selected[zipfile._ECD_ENTRIES_TOTAL],
                    selected[zipfile._ECD_SIZE],
                    selected[zipfile._ECD_OFFSET],
                )
                if (
                    total_entries,
                    directory_size,
                    directory_offset,
                ) != selected_values:
                    raise InvalidBackupError(
                        "backup Zip64 metadata changed during preflight"
                    )
            else:
                if (
                    disk_number != 0
                    or directory_disk != 0
                    or disk_entries != total_entries
                ):
                    raise InvalidBackupError("multi-disk backups are unsupported")
                total_entries = selected[zipfile._ECD_ENTRIES_TOTAL]
                directory_size = selected[zipfile._ECD_SIZE]
                directory_offset = selected[zipfile._ECD_OFFSET]
                directory_boundary = eocd_offset
            if total_entries > MAX_ARCHIVE_MEMBERS:
                raise InvalidBackupError(
                    "backup member count exceeds the safety limit"
                )
            if directory_size > MAX_CENTRAL_DIRECTORY_BYTES:
                raise InvalidBackupError(
                    "backup central directory size exceeds the safety limit"
                )
            concat = eocd_offset - directory_size - directory_offset
            if runtime_uses_zip64:
                concat -= 56 + 20
            directory_start = directory_offset + concat
            directory_end = directory_start + directory_size
            if (
                directory_start < 0
                or directory_end != directory_boundary
                or directory_end > archive_size
            ):
                raise InvalidBackupError("backup central directory bounds are invalid")
            position = directory_start
            for _ in range(total_entries):
                if position + 46 > directory_end:
                    raise InvalidBackupError(
                        "backup central directory structure is invalid"
                    )
                header = _read_exact_at(source, position, 46)
                if header[:4] != _CENTRAL_FILE_SIGNATURE:
                    raise InvalidBackupError(
                        "backup central directory structure is invalid"
                    )
                name_size, extra_size, comment_size = struct.unpack_from(
                    "<3H", header, 28
                )
                disk_start = struct.unpack_from("<H", header, 34)[0]
                if disk_start not in {0, 0xFFFF}:
                    raise InvalidBackupError("multi-disk backups are unsupported")
                position += 46 + name_size + extra_size + comment_size
                if position > directory_end:
                    raise InvalidBackupError(
                        "backup central directory structure is invalid"
                    )
            if position != directory_end:
                raise InvalidBackupError(
                    "backup central directory structure is invalid"
                )
    except InvalidBackupError:
        raise
    except (OSError, OverflowError, struct.error) as exc:
        raise InvalidBackupError("backup central directory is invalid") from exc


def _validated_infos(
    archive: zipfile.ZipFile,
    manifest: BackupManifest | None = None,
) -> dict[str, zipfile.ZipInfo]:
    archive_infos = archive.infolist()
    if len(archive_infos) > MAX_ARCHIVE_MEMBERS:
        raise InvalidBackupError("backup member count exceeds the safety limit")
    infos: dict[str, zipfile.ZipInfo] = {}
    target_names: dict[str, str] = {}
    member_parts: dict[str, tuple[str, ...]] = {}
    total_expanded = 0
    total_compressed = 0
    for info in archive_infos:
        if info.file_size < 0 or info.compress_size < 0:
            raise InvalidBackupError("backup member has invalid size metadata")
        if info.file_size > MAX_MEMBER_EXPANDED_BYTES:
            raise InvalidBackupError("backup member expanded size exceeds the limit")
        total_expanded += info.file_size
        total_compressed += info.compress_size
        if total_expanded > MAX_TOTAL_EXPANDED_BYTES:
            raise InvalidBackupError("backup total expanded size exceeds the limit")
        if info.file_size >= MIN_COMPRESSION_RATIO_BYTES:
            if info.compress_size == 0:
                raise InvalidBackupError("backup member compression ratio is unsafe")
            if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise InvalidBackupError("backup member compression ratio is unsafe")
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
    if total_expanded >= MIN_AGGREGATE_EXPANDED_BYTES:
        if total_compressed == 0:
            raise InvalidBackupError("backup aggregate compression ratio is unsafe")
        if total_expanded / total_compressed > MAX_AGGREGATE_COMPRESSION_RATIO:
            raise InvalidBackupError("backup aggregate compression ratio is unsafe")
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


def _read_json_member(
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
    name: str,
) -> Any:
    info = infos[name]
    if info.file_size > MAX_JSON_BYTES:
        raise InvalidBackupError("backup JSON member exceeds the size limit")
    with archive.open(info) as source:
        raw = source.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise InvalidBackupError("backup JSON member exceeds the size limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidBackupError("backup contains invalid JSON") from exc


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
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == CURRENT_SCHEMA_VERSION:
                # Local import avoids a module-import cycle: legacy migration uses
                # create_backup(), while this public validator is the reviewed
                # canonical schema boundary shared by both services.
                from app.legacy_migration import validate_current_schema

                validate_current_schema(conn)
            return version
    except sqlite3.DatabaseError as exc:
        raise ValueError("backup database is not a valid SQLite database") from exc


def inspect_backup(archive: Path) -> BackupManifest:
    """Validate an archive manifest, allowed members, config, and SQLite payload."""
    archive = Path(archive)
    if not archive.is_file():
        raise InvalidBackupError("backup archive does not exist")
    try:
        archive_size = archive.stat().st_size
        if archive_size <= 0:
            raise InvalidBackupError("backup archive is empty")
        if archive_size > MAX_PHYSICAL_ARCHIVE_BYTES:
            raise InvalidBackupError("backup physical archive size exceeds the limit")
        _preflight_zip_central_directory(archive)
        with zipfile.ZipFile(archive) as zipped:
            infos = _validated_infos(zipped)
            if zipped.testzip() is not None:
                raise InvalidBackupError("backup archive has a failed CRC check")
            manifest_value = _read_json_member(zipped, infos, "manifest.json")
            config_value = _read_json_member(zipped, infos, "data/config.json")
            manifest = _manifest_from_dict(manifest_value)
            _validated_infos(zipped, manifest)
            if not isinstance(config_value, dict):
                raise InvalidBackupError("backup config must be a JSON object")
            if _contains_api_key(config_value):
                raise InvalidBackupError("backup config contains api_key")
            with tempfile.TemporaryDirectory(prefix="better-money-inspect-") as temporary:
                database_path = Path(temporary) / "better_money.db"
                with (
                    zipped.open(infos["data/better_money.db"]) as source,
                    database_path.open("xb") as destination,
                ):
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                database_version = _database_manifest_version(database_path)
    except InvalidBackupError:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error,
            zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise InvalidBackupError(str(exc) or "backup archive is not valid") from exc
    if database_version != manifest.schema_version:
        raise InvalidBackupError(
            "backup manifest schema_version does not match its database"
        )
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
            if temporary_zip.stat().st_size > MAX_PHYSICAL_ARCHIVE_BYTES:
                raise InvalidBackupError(
                    "backup physical archive size exceeds the limit"
                )
        inspect_backup(temporary_zip)
        destination_path = _unique_destination(created_at, reason)
        os.replace(temporary_zip, destination_path)
        temporary_zip = None
        return destination_path
    finally:
        if temporary_zip is not None:
            temporary_zip.unlink(missing_ok=True)


def _extract_declared_members(archive: Path, staging: Path, manifest: BackupManifest) -> None:
    _preflight_zip_central_directory(archive)
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
        from app.legacy_migration import validate_current_schema

        validate_current_schema(conn)
    config_path = staging / "data" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup config is invalid") from exc
    if not isinstance(config, dict) or _contains_api_key(config):
        raise ValueError("backup config is not sanitized")
    if manifest.includes_images:
        (staging / "data" / "images").mkdir(parents=True, exist_ok=True)


def _is_link_or_reparse(path: Path, status: os.stat_result | None = None) -> bool:
    try:
        status = status or os.lstat(path)
    except OSError:
        raise
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_manifest(path: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    directories: list[str] = []
    stack = [(path, Path())]
    while stack:
        current, relative_root = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                relative = relative_root / entry.name
                entry_path = Path(entry.path)
                status = entry.stat(follow_symlinks=False)
                if _is_link_or_reparse(entry_path, status):
                    raise ValueError("live image tree contains a link or reparse point")
                if stat.S_ISDIR(status.st_mode):
                    directories.append(relative.as_posix())
                    stack.append((entry_path, relative))
                elif stat.S_ISREG(status.st_mode):
                    if status.st_nlink > 1:
                        raise ValueError("live image file has multiple hard links")
                    files[relative.as_posix()] = {
                        "size": status.st_size,
                        "digest": _file_digest(entry_path),
                    }
                else:
                    raise ValueError("live image tree contains an unsupported target")
    return {
        "kind": "directory",
        "directories": sorted(directories),
        "files": files,
    }


def _path_manifest(path: Path) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {"kind": "missing"}
    status = os.lstat(path)
    if _is_link_or_reparse(path, status):
        raise ValueError("live restore target is a link or reparse point")
    if stat.S_ISREG(status.st_mode):
        if status.st_nlink > 1:
            raise ValueError("live restore target has multiple hard links")
        return {
            "kind": "file",
            "size": status.st_size,
            "digest": _file_digest(path),
        }
    if stat.S_ISDIR(status.st_mode):
        return _directory_manifest(path)
    raise ValueError("live restore target has an unsupported type")


def _validate_live_targets() -> None:
    paths = get_paths()
    file_targets = [
        paths.db_path,
        paths.config_path,
        Path(f"{paths.db_path}-wal"),
        Path(f"{paths.db_path}-shm"),
        Path(f"{paths.db_path}-journal"),
    ]
    for target in file_targets:
        manifest = _path_manifest(target)
        if manifest["kind"] not in {"missing", "file"}:
            raise ValueError("live file target has an unsupported type")
    images = _path_manifest(paths.images_dir)
    if images["kind"] not in {"missing", "directory"}:
        raise ValueError("live images target has an unsupported type")


def _checkpoint_live_database(database_path: Path) -> None:
    if not os.path.lexists(database_path):
        return
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, timeout=0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("ROLLBACK")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0] != 0:
            raise RuntimeError("live database checkpoint is busy")
        valid, detail = database_integrity(connection)
        if not valid:
            raise RuntimeError(f"live database integrity check failed: {detail}")
    finally:
        if connection is not None:
            connection.close()


def _write_restore_journal(rollback: Path, journal: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".journal-",
            suffix=".tmp",
            dir=rollback,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(journal, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, rollback / "journal.json")
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass


def _best_effort_rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except Exception:
        pass


def _replacement_specifications(
    staging: Path | None,
    includes_images: bool,
) -> list[tuple[str, Path | None, Path]]:
    paths = get_paths()
    specifications: list[tuple[str, Path | None, Path]] = [
        ("database-wal", None, Path(f"{paths.db_path}-wal")),
        ("database-shm", None, Path(f"{paths.db_path}-shm")),
        ("database-journal", None, Path(f"{paths.db_path}-journal")),
        (
            "database",
            staging / "data" / "better_money.db" if staging is not None else None,
            paths.db_path,
        ),
        (
            "config",
            staging / "data" / "config.json" if staging is not None else None,
            paths.config_path,
        ),
    ]
    if includes_images:
        specifications.append(
            (
                "images",
                staging / "data" / "images" if staging is not None else None,
                paths.images_dir,
            )
        )
    return specifications


def _capture_live_baseline(includes_images: bool) -> dict[str, dict[str, Any]]:
    return {
        label: _path_manifest(live)
        for label, _, live in _replacement_specifications(None, includes_images)
    }


def _replacement_items(
    staging: Path,
    includes_images: bool,
    baseline: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for label, source, live in _replacement_specifications(staging, includes_images):
        items.append(
            {
                "label": label,
                "source": str(source) if source is not None else None,
                "live": str(live),
                "original": baseline[label],
                "desired": (
                    _path_manifest(source)
                    if source is not None
                    else {"kind": "missing"}
                ),
                "state": "pending",
            }
        )
    return items


def _stable_copy_file(source: Path, destination: Path, expected: dict[str, Any]) -> None:
    before = os.lstat(source)
    if _is_link_or_reparse(source, before) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("restore source changed while preparing rollback")
    if before.st_nlink > 1:
        raise RuntimeError("restore source gained a hard link")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != opened_identity:
            raise RuntimeError("restore source changed while opening rollback copy")
        with os.fdopen(descriptor, "rb", closefd=False) as input_file:
            with destination.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if opened_identity != after_identity:
            raise RuntimeError("restore source changed during rollback copy")
    finally:
        os.close(descriptor)
    if _path_manifest(destination) != expected or _path_manifest(source) != expected:
        raise RuntimeError("restore rollback copy verification failed")


def _stable_copy_path(source: Path, destination: Path, expected: dict[str, Any]) -> None:
    kind = expected["kind"]
    if kind == "file":
        _stable_copy_file(source, destination, expected)
        return
    if kind == "directory":
        shutil.copytree(source, destination, symlinks=True)
        if _path_manifest(destination) != expected or _path_manifest(source) != expected:
            raise RuntimeError("restore rollback directory copy verification failed")
        return
    if kind != "missing":
        raise RuntimeError("restore rollback source has an unsupported type")


def _copy_saved_for_recovery(
    saved: Path,
    live: Path,
    expected: dict[str, Any],
    rollback: Path,
    label: str,
) -> None:
    kind = expected["kind"]
    if kind == "file":
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".restore-{label}-",
            suffix=".tmp",
            dir=live.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with saved.open("rb") as source:
                shutil.copyfileobj(source, temporary, length=1024 * 1024)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            if _path_manifest(temporary_path) != expected:
                raise RuntimeError("restore recovery copy verification failed")
            os.replace(temporary_path, live)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass
        return
    if kind == "directory":
        temporary_path = Path(
            tempfile.mkdtemp(prefix=f".restore-{label}-", dir=live.parent)
        )
        try:
            temporary_path.rmdir()
            shutil.copytree(saved, temporary_path, symlinks=True)
            if _path_manifest(temporary_path) != expected:
                raise RuntimeError("restore recovery directory verification failed")
            if os.path.lexists(live):
                os.replace(live, rollback / f"failed-live-{label}")
            os.replace(temporary_path, live)
        finally:
            _best_effort_rmtree(temporary_path)
        return
    raise RuntimeError("restore recovery source has an unsupported type")


def _recover_restore(
    rollback: Path,
    journal: dict[str, Any],
    install_error: Exception,
) -> None:
    journal["phase"] = "recovering"
    journal["install_error_type"] = type(install_error).__name__
    recovery_errors: list[str] = []
    try:
        _write_restore_journal(rollback, journal)
    except Exception as exc:
        recovery_errors.append(f"journal:{type(exc).__name__}")

    def recovery_priority(item: dict[str, Any]) -> int:
        label = item["label"]
        if label == "database":
            return 0
        if label.startswith("database-"):
            return 1
        return 2

    for item in sorted(journal["items"], key=recovery_priority):
        live = Path(item["live"])
        saved = Path(item["saved"])
        failed = rollback / f"failed-{item['label']}"
        try:
            current = _path_manifest(live)
            if current == item["original"]:
                pass
            elif item["original"]["kind"] == "missing":
                if os.path.lexists(live):
                    os.replace(live, failed)
            elif os.path.lexists(saved):
                if current["kind"] != "missing":
                    try:
                        _stable_copy_path(live, failed, current)
                    except Exception as exc:
                        recovery_errors.append(
                            f"{item['label']}-failed-copy:{type(exc).__name__}"
                        )
                _copy_saved_for_recovery(
                    saved,
                    live,
                    item["original"],
                    rollback,
                    item["label"],
                )
            else:
                raise RuntimeError("restore rollback copy is missing")
        except Exception as exc:
            recovery_errors.append(f"{item['label']}:{type(exc).__name__}")
        try:
            restored = _path_manifest(live) == item["original"]
        except Exception as exc:
            restored = False
            recovery_errors.append(
                f"{item['label']}-verification:{type(exc).__name__}"
            )
        item["state"] = "restored" if restored else "recovery-incomplete"
        try:
            _write_restore_journal(rollback, journal)
        except Exception as exc:
            recovery_errors.append(f"journal:{type(exc).__name__}")

    complete = all(item["state"] == "restored" for item in journal["items"])
    journal["phase"] = "recovered" if complete else "recovery-incomplete"
    journal["recovery_errors"] = recovery_errors
    try:
        _write_restore_journal(rollback, journal)
    except Exception:
        pass
    if complete:
        try:
            retired = retire_rollback_for_cleanup(rollback, "restore-rollback-")
        except Exception:
            retired = False
        if not retired:
            raise RestoreFailedError(
                "restore installation failed; prior data restored; "
                "rollback cleanup remains pending"
            ) from install_error
        raise RestoreFailedError("restore installation failed; prior data restored") from install_error
    raise RestoreIncompleteError(
        f"restore recovery is incomplete; recovery material: {rollback}"
    ) from install_error


def _install_live_data(
    staging: Path,
    includes_images: bool,
    baseline: dict[str, dict[str, Any]],
) -> None:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    rollback = Path(
        tempfile.mkdtemp(prefix="restore-rollback-", dir=paths.runtime_dir)
    )
    try:
        items = _replacement_items(staging, includes_images, baseline)
        journal: dict[str, Any] = {
            "operation": "backup-restore",
            "phase": "preparing",
            "rollback_dir": str(rollback),
            "items": items,
        }
        for item in items:
            item["saved"] = str(rollback / f"original-{item['label']}")
            if item["original"]["kind"] != "missing":
                _stable_copy_path(
                    Path(item["live"]),
                    Path(item["saved"]),
                    item["original"],
                )
                item["state"] = "original-copied"
            else:
                item["state"] = "original-missing"
        if _capture_live_baseline(includes_images) != baseline:
            raise RuntimeError("live data changed while preparing restore")
        journal["phase"] = "prepared"
        _write_restore_journal(rollback, journal)
        if _capture_live_baseline(includes_images) != baseline:
            raise RuntimeError("live data changed before restore mutation")
    except Exception as exc:
        _best_effort_rmtree(rollback)
        raise RestoreFailedError("restore could not prepare rollback journal") from exc

    try:
        for item in journal["items"]:
            source = Path(item["source"]) if item["source"] is not None else None
            live = Path(item["live"])
            saved = Path(item["saved"])
            live.parent.mkdir(parents=True, exist_ok=True)
            if item["label"].startswith("database-"):
                if os.path.lexists(live):
                    os.replace(live, rollback / f"retired-{item['label']}")
            elif item["label"] == "images":
                if os.path.lexists(live):
                    os.replace(live, rollback / "retired-images")
                if source is not None:
                    os.replace(source, live)
            elif source is not None:
                # Direct replacement keeps the old file continuously addressable.
                os.replace(source, live)
            item["state"] = "installed"
            _write_restore_journal(rollback, journal)
            if _path_manifest(live) != item["desired"]:
                raise RuntimeError(f"restore replacement verification failed: {item['label']}")
    except Exception as install_error:
        _recover_restore(rollback, journal, install_error)

    journal["phase"] = "complete"
    try:
        _write_restore_journal(rollback, journal)
    except Exception as marker_error:
        _recover_restore(rollback, journal, marker_error)
    try:
        retired = retire_rollback_for_cleanup(rollback, "restore-rollback-")
    except Exception as retirement_error:
        _recover_restore(rollback, journal, retirement_error)
    if not retired:
        _recover_restore(
            rollback,
            journal,
            OSError("restore rollback directory retirement failed"),
        )


def restore_backup(archive: Path) -> None:
    """Restore a verified archive under the ledger gate with persistent rollback."""
    archive = Path(archive)
    paths = get_paths()
    paths.ensure_directories()
    staging = Path(
        tempfile.mkdtemp(prefix="better-money-restore-", dir=paths.runtime_dir)
    )
    try:
        archive_snapshot = staging / "supplied-backup.zip"
        try:
            with archive.open("rb") as source, archive_snapshot.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            manifest = inspect_backup(archive_snapshot)
            _extract_declared_members(archive_snapshot, staging, manifest)
            _validate_staged_data(staging, manifest)
        except InvalidBackupError:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error,
                zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise InvalidBackupError(
                str(exc) or "backup archive validation failed"
            ) from exc

        with LEDGER_GATE.exclusive():
            try:
                _validate_live_targets()
                database_exists = os.path.lexists(paths.db_path)
                if database_exists:
                    _checkpoint_live_database(paths.db_path)
                    _validate_live_targets()
                baseline = _capture_live_baseline(manifest.includes_images)
                if database_exists:
                    create_backup("pre-restore")
                    _validate_live_targets()
                    if _capture_live_baseline(manifest.includes_images) != baseline:
                        raise RestoreFailedError(
                            "live data changed while creating the safety backup"
                        )
            except (RestoreFailedError, RestoreIncompleteError):
                raise
            except Exception as exc:
                raise RestoreFailedError(
                    "restore preflight failed; live data was not replaced"
                ) from exc
            _install_live_data(staging, manifest.includes_images, baseline)
    finally:
        _best_effort_rmtree(staging)


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
