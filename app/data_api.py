"""Narrow HTTP API for verified backups and local legacy-data migration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Iterator
from urllib.parse import quote
import zipfile

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import backup as backup_service
from app.backup import (
    BackupManifest,
    InvalidBackupError,
    RestoreFailedError,
    RestoreIncompleteError,
    create_backup,
    inspect_backup,
    restore_backup,
)
from app.legacy_migration import (
    LegacyInspection,
    LegacyRecoveryIncompleteError,
    import_legacy,
    inspect_legacy,
)
from app.native_dialogs import choose_directory, open_directory
from app.paths import get_paths


logger = logging.getLogger(__name__)
router = APIRouter()

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
RESTORE_FAILED_GENERIC = {
    "error": "restore_failed",
    "message": "恢复失败，请稍后重试",
}
_BACKUP_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    sqlite3.Error,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
)
_MIGRATION_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error)


class _ExplicitModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackupManifestResponse(_ExplicitModel):
    format_version: int
    created_at: str
    app_version: str
    schema_version: int
    reason: str
    includes_images: bool


class BackupResponse(_ExplicitModel):
    filename: str
    size: int
    manifest: BackupManifestResponse


class CreateBackupRequest(_ExplicitModel):
    include_images: StrictBool


class OperationResponse(_ExplicitModel):
    ok: bool


class FolderSelectionResponse(_ExplicitModel):
    cancelled: bool
    path: str | None


class LegacySourceRequest(_ExplicitModel):
    source_path: Path

    @field_validator("source_path")
    @classmethod
    def source_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("source_path must be absolute")
        return value


class ImportLegacyRequest(LegacySourceRequest):
    initial_balance_date: str

    @field_validator("initial_balance_date")
    @classmethod
    def date_must_be_canonical_iso(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("initial_balance_date must be an ISO date") from exc
        if parsed.isoformat() != value:
            raise ValueError("initial_balance_date must be canonical ISO")
        return value


class LegacyInspectionResponse(_ExplicitModel):
    source_path: str
    transaction_count: int
    goal_count: int
    summary_count: int
    earliest_transaction_date: str | None
    suggested_initial_balance_date: str
    initial_balance: float
    calculated_balance: float
    cleared_image_path_count: int


@dataclass
class _BackupSnapshot:
    filename: str
    size: int
    manifest: BackupManifest
    path: Path
    handle: Any

    def close(self) -> None:
        try:
            if self.handle is not None:
                self.handle.close()
        except Exception:
            logger.error("Backup snapshot handle cleanup failed")
        finally:
            self.handle = None
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                logger.error("Backup snapshot file cleanup failed")


class _SnapshotStreamingResponse(StreamingResponse):
    """Close the private snapshot across the complete ASGI response lifecycle."""

    def __init__(self, snapshot: _BackupSnapshot, *args, **kwargs) -> None:
        self._snapshot = snapshot
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._snapshot.close()


def _error(status_code: int, content: dict[str, str]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


def _manifest_response(manifest: BackupManifest) -> BackupManifestResponse:
    return BackupManifestResponse(**asdict(manifest))


def _backup_response(path: Path, manifest: BackupManifest) -> BackupResponse:
    return BackupResponse(
        filename=path.name,
        size=path.stat().st_size,
        manifest=_manifest_response(manifest),
    )


def _backup_snapshot_response(snapshot: _BackupSnapshot) -> BackupResponse:
    return BackupResponse(
        filename=snapshot.filename,
        size=snapshot.size,
        manifest=_manifest_response(snapshot.manifest),
    )


def _legacy_response(inspection: LegacyInspection) -> LegacyInspectionResponse:
    return LegacyInspectionResponse(
        source_path=str(inspection.source_dir),
        transaction_count=inspection.transaction_count,
        goal_count=inspection.goal_count,
        summary_count=inspection.summary_count,
        earliest_transaction_date=inspection.earliest_transaction_date,
        suggested_initial_balance_date=inspection.suggested_initial_balance_date,
        initial_balance=inspection.initial_balance,
        calculated_balance=inspection.calculated_balance,
        cleared_image_path_count=len(inspection.cleared_image_paths),
    )


def _safe_backup_path(filename: str) -> Path | JSONResponse:
    if (
        not filename
        or filename != PurePosixPath(filename).name
        or filename != PureWindowsPath(filename).name
        or Path(filename).suffix.lower() != ".zip"
        or any(ord(character) < 32 for character in filename)
    ):
        return _error(400, INVALID_BACKUP)
    backups_dir = get_paths().backups_dir.resolve()
    candidate = backups_dir / filename
    if not os.path.lexists(candidate):
        return _error(
            404,
            {"error": "backup_not_found", "message": "备份文件不存在"},
        )
    return candidate


def _linked_or_reparse(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _same_open_file(before: os.stat_result, opened: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )


def _snapshot_eligible_backup(candidate: Path) -> _BackupSnapshot:
    paths = get_paths()
    paths.ensure_directories()
    descriptor: int | None = None
    snapshot_path: Path | None = None
    snapshot_handle = None
    try:
        before = os.lstat(candidate)
        if (
            _linked_or_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink > 1
        ):
            raise InvalidBackupError("backup path is not an eligible regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if (
            _linked_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink > 1
            or not _same_open_file(before, opened)
        ):
            raise InvalidBackupError("backup file identity changed while opening")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="better-money-export-",
            suffix=".zip",
            dir=paths.runtime_dir,
            delete=False,
        ) as temporary:
            snapshot_path = Path(temporary.name)
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = None
                shutil.copyfileobj(source, temporary, length=1024 * 1024)
                after = os.fstat(source.fileno())
        if not _same_open_file(opened, after):
            raise InvalidBackupError("backup file changed while being copied")
        manifest = inspect_backup(snapshot_path)
        snapshot_handle = snapshot_path.open("rb")
        snapshot = _BackupSnapshot(
            filename=candidate.name,
            size=after.st_size,
            manifest=manifest,
            path=snapshot_path,
            handle=snapshot_handle,
        )
        snapshot_path = None
        snapshot_handle = None
        return snapshot
    except InvalidBackupError:
        raise
    except Exception as exc:
        raise InvalidBackupError("backup file could not be safely snapshotted") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                pass
        if snapshot_handle is not None:
            try:
                snapshot_handle.close()
            except Exception:
                pass
        if snapshot_path is not None:
            try:
                snapshot_path.unlink(missing_ok=True)
            except Exception:
                logger.error("Backup snapshot file cleanup failed")


def _snapshot_iterator(snapshot: _BackupSnapshot) -> Iterator[bytes]:
    try:
        while chunk := snapshot.handle.read(1024 * 1024):
            yield chunk
    finally:
        snapshot.close()


def _content_disposition(filename: str) -> str:
    fallback = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in filename
    )
    if not fallback or fallback.strip("_.") == "":
        fallback = "backup.zip"
    encoded = quote(filename, safe="")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{encoded}"
    )


def _existing_source(source_path: Path) -> Path | JSONResponse:
    source = source_path.resolve()
    if not source.exists():
        return _error(
            404,
            {
                "error": "legacy_source_not_found",
                "message": "所选文件夹不存在",
            },
        )
    return source


@router.get("/api/backups", response_model=list[BackupResponse])
def list_backups() -> list[BackupResponse]:
    paths = get_paths()
    paths.ensure_directories()
    backups: list[BackupResponse] = []
    for archive in paths.backups_dir.iterdir():
        if archive.suffix.lower() != ".zip":
            continue
        snapshot: _BackupSnapshot | None = None
        try:
            snapshot = _snapshot_eligible_backup(archive)
            backups.append(_backup_snapshot_response(snapshot))
        except Exception:
            continue
        finally:
            if snapshot is not None:
                snapshot.close()
    backups.sort(
        key=lambda item: (item.manifest.created_at, item.filename), reverse=True
    )
    return backups


@router.post("/api/backups/create", response_model=BackupResponse)
def create_backup_endpoint(request: CreateBackupRequest):
    try:
        archive = create_backup("manual", include_images=request.include_images)
        return _backup_response(archive, inspect_backup(archive))
    except _BACKUP_ERRORS:
        logger.error("Manual backup creation failed")
        return _error(
            500,
            {"error": "backup_failed", "message": "创建备份失败"},
        )


@router.get("/api/backups/export")
def export_backup(filename: str):
    candidate = _safe_backup_path(filename)
    if isinstance(candidate, JSONResponse):
        return candidate
    try:
        snapshot = _snapshot_eligible_backup(candidate)
    except Exception:
        return _error(400, INVALID_BACKUP)
    try:
        return _SnapshotStreamingResponse(
            snapshot,
            _snapshot_iterator(snapshot),
            media_type="application/zip",
            headers={
                "Content-Disposition": _content_disposition(snapshot.filename)
            },
            background=BackgroundTask(snapshot.close),
        )
    except Exception:
        snapshot.close()
        raise


@router.post("/api/backups/restore", response_model=OperationResponse)
async def restore_backup_endpoint(file: UploadFile = File(...)):
    temporary_path: Path | None = None
    try:
        filename = file.filename or ""
        if Path(filename).suffix.lower() != ".zip":
            raise InvalidBackupError("restore upload must be a ZIP archive")
        paths = get_paths()
        paths.ensure_directories()
        size = 0
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="better-money-upload-",
            suffix=".zip",
            dir=paths.runtime_dir,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > backup_service.MAX_PHYSICAL_ARCHIVE_BYTES:
                    raise InvalidBackupError("restore upload is too large")
                temporary.write(chunk)
        if size == 0:
            raise InvalidBackupError("restore upload is empty")
        await run_in_threadpool(restore_backup, temporary_path)
        return OperationResponse(ok=True)
    except InvalidBackupError:
        return _error(400, INVALID_BACKUP)
    except RestoreIncompleteError:
        logger.error("Backup restore recovery incomplete")
        return _error(500, RESTORE_INCOMPLETE)
    except RestoreFailedError:
        logger.error("Backup restore failed with prior data preserved")
        return _error(500, RESTORE_FAILED)
    except Exception:
        logger.error("Backup restore failed unexpectedly")
        return _error(500, RESTORE_FAILED_GENERIC)
    finally:
        try:
            try:
                await file.close()
            except Exception:
                logger.error("Restore upload close failed")
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    logger.error("Restore upload cleanup failed")


@router.post("/api/migration/select-folder", response_model=FolderSelectionResponse)
def select_migration_folder() -> FolderSelectionResponse:
    selected = choose_directory("选择 Better Money 旧数据文件夹")
    if selected is None:
        return FolderSelectionResponse(cancelled=True, path=None)
    return FolderSelectionResponse(cancelled=False, path=str(selected.resolve()))


@router.post("/api/migration/inspect", response_model=LegacyInspectionResponse)
def inspect_migration(request: LegacySourceRequest):
    try:
        source = _existing_source(request.source_path)
        if isinstance(source, JSONResponse):
            return source
        return _legacy_response(inspect_legacy(source))
    except _MIGRATION_ERRORS:
        return _error(400, INVALID_LEGACY_SOURCE)
    except Exception:
        logger.error("Legacy inspection failed unexpectedly")
        return _error(400, INVALID_LEGACY_SOURCE)


@router.post("/api/migration/import", response_model=LegacyInspectionResponse)
def import_migration(request: ImportLegacyRequest):
    try:
        source = _existing_source(request.source_path)
    except _MIGRATION_ERRORS:
        return _error(400, INVALID_LEGACY_SOURCE)
    except Exception:
        logger.error("Legacy migration source resolution failed unexpectedly")
        return _error(400, MIGRATION_FAILED)
    if isinstance(source, JSONResponse):
        return source
    try:
        inspect_legacy(source)
    except _MIGRATION_ERRORS:
        return _error(400, INVALID_LEGACY_SOURCE)
    except Exception:
        logger.error("Legacy migration inspection failed unexpectedly")
        return _error(400, MIGRATION_FAILED)
    try:
        inspection = import_legacy(source, request.initial_balance_date)
    except LegacyRecoveryIncompleteError:
        logger.error("Legacy migration recovery incomplete")
        return _error(500, MIGRATION_RECOVERY_INCOMPLETE)
    except Exception:
        logger.error("Legacy migration failed")
        return _error(400, MIGRATION_FAILED)
    return _legacy_response(inspection)


@router.post("/api/system/open-data-folder", response_model=OperationResponse)
def open_data_folder():
    try:
        open_directory(get_paths().data_dir)
    except (OSError, RuntimeError):
        return _error(
            501,
            {
                "error": "open_data_folder_unavailable",
                "message": "无法打开数据文件夹",
            },
        )
    return OperationResponse(ok=True)
