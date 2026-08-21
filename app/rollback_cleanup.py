"""Atomically retire validated rollback journals before best-effort deletion."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import uuid

from app.paths import get_paths


_ALLOWED_PREFIXES = frozenset({"restore-rollback-", "legacy-rollback-"})
_RETIRED_PREFIX = ".better-money-retired-cleanup-"


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _linked_or_reparse(status: os.stat_result) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _safe_tree_for_deletion(root: Path, expected: os.stat_result) -> bool:
    """Reject anything that could alias material outside the retired tree."""
    try:
        current = os.lstat(root)
        if (
            current.st_dev,
            current.st_ino,
            current.st_mode,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
        ):
            return False
        stack = [root]
        while stack:
            candidate = stack.pop()
            status = os.lstat(candidate)
            if _linked_or_reparse(status):
                return False
            if stat.S_ISREG(status.st_mode):
                if status.st_nlink > 1:
                    return False
                continue
            if stat.S_ISDIR(status.st_mode):
                with os.scandir(candidate) as entries:
                    stack.extend(Path(entry.path) for entry in entries)
                continue
            return False
    except OSError:
        return False
    return True


def retire_rollback_for_cleanup(rollback: Path, expected_prefix: str) -> bool:
    """Rename one trusted rollback out of the scan namespace, then delete it.

    A failed rename leaves the complete original directory untouched for a later
    startup. Deletion is attempted only for the unique path this call renamed.
    """
    if expected_prefix not in _ALLOWED_PREFIXES:
        raise ValueError("unexpected rollback prefix")
    paths = get_paths()
    rollback = Path(rollback)
    if (
        _path_key(rollback.parent) != _path_key(paths.runtime_dir)
        or not rollback.name.startswith(expected_prefix)
    ):
        raise ValueError("rollback cleanup target is outside the current runtime")
    before = os.lstat(rollback)
    if _linked_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise ValueError("rollback cleanup target is not a trusted directory")
    after = os.lstat(rollback)
    if (before.st_dev, before.st_ino, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    ):
        raise OSError("rollback cleanup target changed before retirement")
    while True:
        retired = paths.runtime_dir / f"{_RETIRED_PREFIX}{uuid.uuid4().hex}"
        if not os.path.lexists(retired):
            break
    try:
        os.replace(rollback, retired)
    except OSError:
        return False
    try:
        safe_to_delete = _safe_tree_for_deletion(retired, before)
    except Exception:
        return True
    if not safe_to_delete:
        return True
    try:
        shutil.rmtree(retired)
    except Exception:
        pass
    return True
