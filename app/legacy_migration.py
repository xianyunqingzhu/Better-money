"""Read-only inspection and rollback-safe import of legacy local data."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Any

from app.backup import create_backup
from app.db import LEDGER_GATE
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    database_integrity,
    migrate_database,
)
from app.paths import AppPaths, get_paths
from app.rollback_cleanup import retire_rollback_for_cleanup


class LegacyRecoveryIncompleteError(RuntimeError):
    """A legacy import failed and its original live state is not fully restored."""


@dataclass(frozen=True)
class LegacyInspection:
    source_dir: Path
    transaction_count: int
    goal_count: int
    summary_count: int
    earliest_transaction_date: str | None
    suggested_initial_balance_date: str
    initial_balance: float
    calculated_balance: float
    cleared_image_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FileState:
    digest: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _StagedLegacy:
    database_path: Path
    config: dict[str, Any]
    images_dir: Path
    image_files: dict[str, str]
    legacy_backups: Path | None


_SNAPSHOT_NAMES = (
    "better_money.db",
    "better_money.db-wal",
    "better_money.db-shm",
    "better_money.db-journal",
    "config.json",
)


def _is_link(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        attributes = 0
    return (
        path.is_symlink()
        or (hasattr(path, "is_junction") and path.is_junction())
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _reject_live_overlap(source_dir: Path, paths: AppPaths) -> None:
    for writable in (
        paths.data_dir.resolve(),
        paths.backups_dir.resolve(),
        paths.runtime_dir.resolve(),
    ):
        if _paths_overlap(source_dir, writable):
            raise ValueError("legacy source overlaps a live application directory")


def _database_candidate(directory: Path) -> Path | None:
    database = directory / "better_money.db"
    if not os.path.lexists(database):
        return None
    if _is_link(database):
        raise ValueError("legacy database must not be a link or reparse point")
    if not database.is_file():
        raise ValueError("legacy database is not a regular file")
    return database


def _resolve_source(source: Path) -> Path:
    selected = Path(source).expanduser().resolve()
    paths = get_paths()
    live_data = paths.data_dir.resolve()
    if selected == live_data or (selected / "data").resolve() == live_data:
        raise ValueError("legacy source resolves to the live data directory")
    nested_data = (selected / "data").resolve()
    direct_database = _database_candidate(selected)
    nested_database = _database_candidate(nested_data)
    if direct_database is not None:
        source_dir = selected
        database = direct_database
    elif nested_database is not None:
        source_dir = nested_data
        database = nested_database
    else:
        raise ValueError("legacy source does not contain better_money.db")
    _reject_live_overlap(source_dir, paths)
    if paths.db_path.exists():
        try:
            same_database = os.path.samefile(database, paths.db_path)
        except OSError as exc:
            raise ValueError("legacy database identity could not be verified") from exc
        if same_database:
            raise ValueError("legacy database has the same identity as the live database")
    return source_dir


def _read_config_file(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy config is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("legacy config must be a JSON object")
    return value


def _fingerprint(path: Path) -> _FileState:
    digest = hashlib.sha256()
    with _verified_source_file(path) as (source, opened):
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return _FileState(
        digest.hexdigest(),
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
    )


def _stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _stat_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*_stat_identity(value), value.st_size, value.st_mtime_ns)


def _stat_is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _source_changed() -> RuntimeError:
    return RuntimeError(
        "legacy source changed while copying; close the old application and retry"
    )


@contextmanager
def _verified_source_file(path: Path):
    try:
        before = os.lstat(path)
        if _stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise ValueError(f"legacy source file is linked or not regular: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _source_changed() from exc
    stream = None
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        if (
            _stat_identity(before) != _stat_identity(opened)
            or _stat_identity(before) != _stat_identity(after_open)
            or _stat_is_link_or_reparse(after_open)
        ):
            raise _source_changed()
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        yield stream, opened
        after_read = os.fstat(stream.fileno())
        after_path = os.lstat(path)
        if (
            _stat_snapshot(opened) != _stat_snapshot(after_read)
            or _stat_identity(opened) != _stat_identity(after_path)
            or _stat_is_link_or_reparse(after_path)
        ):
            raise _source_changed()
    except (FileNotFoundError, OSError) as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise _source_changed() from exc
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _copy_verified_file(
    source_path: Path, destination_path: Path, expected: _FileState
) -> None:
    with _verified_source_file(source_path) as (source, opened):
        opened_state = _FileState(
            expected.digest,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
        )
        if opened_state != expected:
            raise _source_changed()
        with destination_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    os.utime(destination_path, ns=(expected.mtime_ns, expected.mtime_ns))


def _capture_snapshot_state(source_dir: Path) -> dict[str, _FileState]:
    state: dict[str, _FileState] = {}
    for name in _SNAPSHOT_NAMES:
        path = source_dir / name
        if not os.path.lexists(path):
            continue
        if _is_link(path) or not path.is_file():
            raise ValueError(f"legacy snapshot member is not a regular file: {name}")
        state[name] = _fingerprint(path)
    if "better_money.db" not in state:
        raise ValueError("legacy source does not contain better_money.db")
    return state


def _copy_stable_snapshot(source_dir: Path, snapshot_dir: Path) -> None:
    baseline = _capture_snapshot_state(source_dir)
    snapshot_dir.mkdir()
    for name in baseline:
        _copy_verified_file(
            source_dir / name, snapshot_dir / name, baseline[name]
        )
        copied = _fingerprint(snapshot_dir / name)
        if (copied.digest, copied.size) != (
            baseline[name].digest,
            baseline[name].size,
        ):
            raise RuntimeError(
                "legacy source changed while copying; close the old application and retry"
            )
    if _capture_snapshot_state(source_dir) != baseline:
        raise RuntimeError(
            "legacy source changed while copying; close the old application and retry"
        )


def _database_version(conn: sqlite3.Connection) -> int:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"legacy database schema version {version} is newer than supported "
            f"version {CURRENT_SCHEMA_VERSION}"
        )
    return version


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


_SQLITE_ASCII_IDENTIFIER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def _sqlite_identifier_key(value: str) -> str:
    return value.translate(_SQLITE_ASCII_IDENTIFIER_TRANSLATION)


def _normalize_schema_sql(value: str | None) -> str | None:
    if value is None:
        return None
    value = _strip_sql_comments(value)
    normalized: list[str] = []
    whitespace_pending = False
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            whitespace_pending = True
            index += 1
            continue
        if (
            whitespace_pending
            and normalized
            and character not in {",", ")", ";"}
        ):
            normalized.append(" ")
        whitespace_pending = False
        if character in {"'", '"', "`"}:
            quote = character
            end = index + 1
            while end < len(value):
                if value[end] == quote:
                    if end + 1 < len(value) and value[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            normalized.append(value[index:end])
            index = end
            continue
        if character == "[":
            end = value.find("]", index + 1)
            end = len(value) if end == -1 else end + 1
            normalized.append(value[index:end])
            index = end
            continue
        normalized.append(character.casefold())
        index += 1
    return "".join(normalized)


def _strip_sql_comments(value: str) -> str:
    stripped: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character in {"'", '"', "`"}:
            quote = character
            end = index + 1
            while end < len(value):
                if value[end] == quote:
                    if end + 1 < len(value) and value[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            stripped.append(value[index:end])
            index = end
            continue
        if character == "[":
            end = index + 1
            while end < len(value):
                if value[end] == "]":
                    if end + 1 < len(value) and value[end + 1] == "]":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            stripped.append(value[index:end])
            index = end
            continue
        if value.startswith("--", index):
            end = value.find("\n", index + 2)
            if end == -1:
                break
            stripped.append("\n")
            index = end + 1
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            if end == -1:
                break
            stripped.append(" ")
            index = end + 2
            continue
        stripped.append(character)
        index += 1
    return "".join(stripped)


def _schema_signature(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = tuple(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    signature: dict[str, Any] = {}
    for table in tables:
        quoted_table = _quoted_identifier(table)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        columns = tuple(
            tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({quoted_table})")
        )
        foreign_keys = tuple(sorted(
            tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({quoted_table})")
        ))
        indexes: dict[str, tuple[Any, ...]] = {}
        for index_row in conn.execute(f"PRAGMA index_list({quoted_table})"):
            _, name, unique, origin, partial = index_row[:5]
            quoted_index = _quoted_identifier(name)
            index_columns = tuple(
                tuple(row)
                for row in conn.execute(f"PRAGMA index_xinfo({quoted_index})")
            )
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
            normalized_sql = _normalize_schema_sql(
                str(sql_row[0]) if sql_row and sql_row[0] else None
            )
            indexes[name] = (
                table,
                int(unique),
                str(origin),
                int(partial),
                index_columns,
                normalized_sql,
            )
        signature[table] = {
            "sql": _normalize_schema_sql(table_sql[0] if table_sql else None),
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }
    auxiliary = tuple(
        (row[0], row[1], row[2], _normalize_schema_sql(row[3]))
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('trigger', 'view') ORDER BY type, name"
        )
    )
    return {"tables": signature, "auxiliary": auxiliary}


def _canonical_schema_signature() -> dict[str, Any]:
    with closing(sqlite3.connect(":memory:")) as canonical:
        migrate_database(canonical)
        return _schema_signature(canonical)


_SQLITE_TABLE_ACCESS_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
    }
)


@contextmanager
def _private_auxiliary_clone(conn: sqlite3.Connection):
    clone = sqlite3.connect(":memory:")
    try:
        serialize = getattr(conn, "serialize", None)
        deserialize = getattr(clone, "deserialize", None)
        if callable(serialize) and callable(deserialize):
            deserialize(serialize())
        else:
            if conn.in_transaction:
                raise RuntimeError(
                    "cannot validate auxiliary schema during an active transaction"
                )
            conn.backup(clone)
        yield clone
    finally:
        clone.close()


def _compiled_auxiliary_references(
    conn: sqlite3.Connection,
    object_name: str,
    statements: tuple[str, ...],
) -> frozenset[str]:
    references: set[str] = set()

    def collect_table_access(
        action: int,
        table: str | None,
        _column: str | None,
        _database: str | None,
        _source: str | None,
    ) -> int:
        if action in _SQLITE_TABLE_ACCESS_ACTIONS and table:
            references.add(table.casefold())
        return sqlite3.SQLITE_OK

    conn.set_authorizer(collect_table_access)
    try:
        for statement in statements:
            conn.execute(statement).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"staged database has invalid auxiliary object: {object_name}"
        ) from exc
    finally:
        conn.set_authorizer(None)
    return frozenset(references)


def _trigger_explain_statements(
    conn: sqlite3.Connection, owner: str
) -> tuple[str, ...]:
    quoted_owner = _quoted_identifier(owner)
    column_rows = tuple(
        conn.execute(f"PRAGMA table_xinfo({quoted_owner})")
    )
    writable_columns = tuple(
        str(row[1]) for row in column_rows if int(row[6]) == 0
    )
    if not writable_columns:
        raise RuntimeError(
            f"staged database trigger owner has no writable columns: {owner}"
        )
    assignments = ", ".join(
        f"{_quoted_identifier(column)} = {_quoted_identifier(column)}"
        for column in writable_columns
    )
    statements = [
        f"EXPLAIN INSERT INTO {quoted_owner} DEFAULT VALUES",
        f"EXPLAIN UPDATE {quoted_owner} SET {assignments} WHERE 0",
    ]
    owner_key = _sqlite_identifier_key(owner)
    table_entry = next(
        (
            row
            for row in conn.execute("PRAGMA main.table_list")
            if _sqlite_identifier_key(str(row[1])) == owner_key
        ),
        None,
    )
    is_ordinary_rowid_table = (
        table_entry is not None
        and str(table_entry[2]) == "table"
        and int(table_entry[4]) == 0
    )
    if is_ordinary_rowid_table:
        real_columns = {str(row[1]).casefold() for row in column_rows}
        for alias in ("rowid", "oid", "_rowid_"):
            if alias.casefold() not in real_columns:
                quoted_alias = _quoted_identifier(alias)
                statements.append(
                    f"EXPLAIN UPDATE {quoted_owner} "
                    f"SET {quoted_alias} = {quoted_alias} WHERE 0"
                )
    statements.append(f"EXPLAIN DELETE FROM {quoted_owner} WHERE 0")
    return tuple(statements)


def _validate_current_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"staged database schema version is {version}, expected {CURRENT_SCHEMA_VERSION}"
        )
    expected = _canonical_schema_signature()
    actual = _schema_signature(conn)
    expected_auxiliary = {
        (item[0], item[1]): item for item in expected["auxiliary"]
    }
    actual_auxiliary = {
        (item[0], item[1]): item for item in actual["auxiliary"]
    }
    for key, expected_item in expected_auxiliary.items():
        if actual_auxiliary.get(key) != expected_item:
            raise RuntimeError(
                "staged database schema is missing or changed a canonical "
                "trigger or view"
            )
    required_tables = frozenset(
        table.casefold() for table in expected["tables"]
    )
    with _private_auxiliary_clone(conn) as auxiliary_conn:
        for key, item in actual_auxiliary.items():
            if key in expected_auxiliary:
                continue
            kind, name, owner, _ = item
            if owner.casefold() in required_tables:
                raise RuntimeError(
                    f"staged database schema has unsafe auxiliary object: {name}"
                )
            if kind == "view":
                statements = (
                    "EXPLAIN SELECT * FROM "
                    f"{_quoted_identifier(name)} LIMIT 0",
                )
            else:
                statements = _trigger_explain_statements(auxiliary_conn, owner)
            if (
                _compiled_auxiliary_references(
                    auxiliary_conn, name, statements
                )
                & required_tables
            ):
                raise RuntimeError(
                    f"staged database schema has unsafe auxiliary object: {name}"
                )
    for table, expected_table in expected["tables"].items():
        if table not in actual["tables"]:
            raise RuntimeError(f"staged database schema is missing table: {table}")
        actual_table = actual["tables"][table]
        if actual_table["sql"] != expected_table["sql"]:
            raise RuntimeError(
                f"staged database schema has noncanonical SQL for table: {table}"
            )
        if actual_table["columns"] != expected_table["columns"]:
            raise RuntimeError(
                f"staged database schema has incompatible columns in table: {table}"
            )
        if actual_table["foreign_keys"] != expected_table["foreign_keys"]:
            raise RuntimeError(
                f"staged database schema has incompatible foreign keys in table: {table}"
            )
        if actual_table["indexes"] != expected_table["indexes"]:
            raise RuntimeError(
                f"staged database schema has incompatible indexes in table: {table}"
            )
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError("staged database failed foreign key validation")


def validate_current_schema(conn: sqlite3.Connection) -> None:
    """Validate a current-version database against the canonical migrated schema.

    This public boundary intentionally delegates to the legacy import validator so
    backup inspection and legacy import cannot drift into separate schema rules.
    """
    _validate_current_schema(conn)


def _materialize_database(snapshot_dir: Path, destination: Path) -> None:
    copied_database = snapshot_dir / "better_money.db"
    try:
        with closing(sqlite3.connect(copied_database)) as source:
            valid, detail = database_integrity(source)
            if not valid:
                raise RuntimeError(f"database integrity check failed: {detail}")
            _database_version(source)
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
    except sqlite3.DatabaseError as exc:
        raise ValueError("legacy database is not a valid SQLite database") from exc
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)


def _capture_directory_state(
    root: Path, *, skip_links: bool
) -> tuple[tuple[str, ...], dict[str, _FileState], tuple[str, ...]]:
    if _is_link(root) or not root.is_dir():
        raise ValueError("legacy data directory must be a regular directory")
    directories: list[str] = []
    files: dict[str, _FileState] = {}
    skipped: list[str] = []

    def visit(directory: Path) -> None:
        for item in sorted(directory.iterdir(), key=lambda value: value.name):
            relative = item.relative_to(root).as_posix()
            if _is_link(item):
                if skip_links:
                    skipped.append(relative)
                    continue
                raise ValueError("legacy data contains a linked file or directory")
            if item.is_dir():
                directories.append(relative)
                visit(item)
            elif item.is_file():
                files[relative] = _fingerprint(item)
            else:
                raise ValueError("legacy data contains an unsupported filesystem entry")

    visit(root)
    return tuple(directories), files, tuple(skipped)


def _copy_stable_directory(
    source: Path, destination: Path, *, skip_links: bool
) -> frozenset[str]:
    baseline = _capture_directory_state(source, skip_links=skip_links)
    directories, files, skipped = baseline
    destination.mkdir(parents=True, exist_ok=False)
    for relative in directories:
        (destination / Path(relative)).mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = destination / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified_file(source / Path(relative), target, files[relative])
    if _capture_directory_state(source, skip_links=skip_links) != baseline:
        raise RuntimeError(
            "legacy source changed while copying; close the old application and retry"
        )
    staged_directories, staged_files, staged_skipped = _capture_directory_state(
        destination, skip_links=False
    )
    expected_content = {
        relative: (state.digest, state.size) for relative, state in files.items()
    }
    staged_content = {
        relative: (state.digest, state.size)
        for relative, state in staged_files.items()
    }
    if (
        staged_directories != directories
        or staged_content != expected_content
        or staged_skipped
    ):
        raise RuntimeError("staged legacy directory copy failed verification")
    return frozenset(files)


def _windows_image_manifest(image_files: frozenset[str]) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative in sorted(image_files):
        windows_path = ntpath.normpath(relative.replace("/", "\\"))
        if (
            windows_path in {"", ".", ".."}
            or ntpath.isabs(windows_path)
            or windows_path.startswith("..\\")
        ):
            raise ValueError("legacy image manifest contains an unsafe path")
        key = ntpath.normcase(windows_path)
        existing = manifest.get(key)
        if existing is not None and existing != relative:
            raise ValueError(
                "legacy image manifest has a Windows path collision: "
                f"{existing!r} and {relative!r}"
            )
        manifest[key] = relative
    return manifest


def _stage_legacy(
    source_dir: Path, staging: Path, *, include_backups: bool
) -> _StagedLegacy:
    snapshot_dir = staging / "source-snapshot"
    _copy_stable_snapshot(source_dir, snapshot_dir)
    staged_data = staging / "data"
    staged_data.mkdir()
    database = staged_data / "better_money.db"
    _materialize_database(snapshot_dir, database)
    copied_config = snapshot_dir / "config.json"
    config = _read_config_file(copied_config) if copied_config.exists() else {}
    (staged_data / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    source_images = source_dir / "images"
    staged_images = staged_data / "images"
    if os.path.lexists(source_images):
        image_files = _windows_image_manifest(
            _copy_stable_directory(
                source_images, staged_images, skip_links=True
            )
        )
    else:
        staged_images.mkdir()
        image_files = {}
    staged_backups: Path | None = None
    source_backups = source_dir / "backups"
    if include_backups and os.path.lexists(source_backups):
        if _paths_overlap(source_backups.resolve(), get_paths().backups_dir.resolve()):
            raise ValueError("legacy backups overlap the live backup directory")
        staged_backups = staging / "legacy-backups"
        _copy_stable_directory(source_backups, staged_backups, skip_links=False)
    return _StagedLegacy(
        database, config, staged_images, image_files, staged_backups
    )


def _initial_balance(config: dict[str, Any]) -> float:
    value = config.get("initial_balance", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("legacy initial_balance must be numeric")
    balance = float(value)
    if not math.isfinite(balance):
        raise ValueError("legacy initial_balance must be finite")
    return balance


def _suggested_initial_balance_date(
    config: dict[str, Any], earliest: str | None
) -> str:
    configured = config.get("initial_balance_date")
    if isinstance(configured, str):
        try:
            parsed = date.fromisoformat(configured)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == configured:
                return configured
    return earliest or date.today().isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return _table_exists(conn, table) and column in {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _sum(conn: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> float:
    row = conn.execute(sql, parameters).fetchone()
    return float(row[0] or 0.0)


def _calculated_balance(conn: sqlite3.Connection, initial_balance: float) -> float:
    transaction_total = 0.0
    if _column_exists(conn, "transactions", "type") and _column_exists(
        conn, "transactions", "amount"
    ):
        transaction_total += _sum(
            conn,
            "SELECT SUM(amount) FROM transactions WHERE type IN ('收入', '退款')",
        )
        transaction_total -= _sum(
            conn,
            "SELECT SUM(amount) FROM transactions "
            "WHERE type IN ('支出', '取现', '转账', '还款')",
        )
    adjustment_total = 0.0
    if _column_exists(conn, "adjustments", "diff"):
        adjustment_total = _sum(conn, "SELECT SUM(diff) FROM adjustments")
    return initial_balance + transaction_total + adjustment_total


def _image_relative_path(
    raw_path: str, source_dir: Path, image_files: dict[str, str]
) -> Path | None:
    old_images = Path(os.path.abspath(source_dir / "images"))
    supplied = Path(raw_path).expanduser()
    if any(part == ".." for part in supplied.parts):
        return None
    if supplied.is_absolute():
        candidate = Path(os.path.abspath(supplied))
    else:
        parts = supplied.parts
        first = parts[0].casefold() if parts else ""
        if first == "images":
            candidate = Path(os.path.abspath(source_dir / supplied))
        elif first == "data":
            candidate = Path(os.path.abspath(source_dir.parent / supplied))
        else:
            candidate = Path(os.path.abspath(old_images / supplied))
    if candidate == old_images or not candidate.is_relative_to(old_images):
        return None
    relative = candidate.relative_to(old_images)
    key = ntpath.normcase(ntpath.normpath(relative.as_posix().replace("/", "\\")))
    actual_relative = image_files.get(key)
    if actual_relative is None:
        return None
    return Path(actual_relative)


def _image_path_rows(conn: sqlite3.Connection):
    for table in ("summaries", "pending_items"):
        if not _column_exists(conn, table, "id") or not _column_exists(
            conn, table, "image_path"
        ):
            continue
        for row_id, image_path in conn.execute(
            f"SELECT id, image_path FROM {table} "
            "WHERE image_path IS NOT NULL AND image_path != ''"
        ):
            yield table, row_id, str(image_path)


def _cleared_image_paths(
    conn: sqlite3.Connection, source_dir: Path, image_files: dict[str, str]
) -> tuple[str, ...]:
    cleared = {
        image_path
        for _, _, image_path in _image_path_rows(conn)
        if _image_relative_path(image_path, source_dir, image_files) is None
    }
    return tuple(sorted(cleared))


def _validated_transaction_dates(conn: sqlite3.Connection) -> str | None:
    if not _column_exists(conn, "transactions", "date"):
        return None
    dates = [row[0] for row in conn.execute("SELECT DISTINCT date FROM transactions")]
    for value in dates:
        if not isinstance(value, str) or not value:
            raise ValueError("legacy transaction date must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("legacy transaction date must use YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError("legacy transaction date must use YYYY-MM-DD")
    return min(dates) if dates else None


def _inspect_staged(
    source_dir: Path, staged: _StagedLegacy
) -> LegacyInspection:
    config = staged.config
    initial_balance = _initial_balance(config)
    try:
        with closing(sqlite3.connect(staged.database_path)) as conn:
            valid, detail = database_integrity(conn)
            if not valid:
                raise RuntimeError(f"database integrity check failed: {detail}")
            version = _database_version(conn)
            if version == CURRENT_SCHEMA_VERSION:
                _validate_current_schema(conn)
            transaction_count = _count(conn, "transactions")
            goal_count = _count(conn, "goals")
            summary_count = _count(conn, "summaries")
            earliest = _validated_transaction_dates(conn)
            balance = _calculated_balance(conn, initial_balance)
            cleared = _cleared_image_paths(conn, source_dir, staged.image_files)
    except sqlite3.DatabaseError as exc:
        raise ValueError("legacy database is not a valid SQLite database") from exc
    return LegacyInspection(
        source_dir=source_dir,
        transaction_count=transaction_count,
        goal_count=goal_count,
        summary_count=summary_count,
        earliest_transaction_date=earliest,
        suggested_initial_balance_date=_suggested_initial_balance_date(config, earliest),
        initial_balance=initial_balance,
        calculated_balance=balance,
        cleared_image_paths=cleared,
    )


def inspect_legacy(source: Path) -> LegacyInspection:
    """Inspect a legacy project or data directory without writing to it."""
    source_dir = _resolve_source(source)
    paths = get_paths()
    _validate_live_targets(paths)
    paths.ensure_directories()
    with tempfile.TemporaryDirectory(
        prefix="better-money-legacy-inspect-", dir=paths.runtime_dir
    ) as temporary:
        staged = _stage_legacy(source_dir, Path(temporary), include_backups=False)
        return _inspect_staged(source_dir, staged)


def _rewrite_image_paths(
    conn: sqlite3.Connection, source_dir: Path, image_files: dict[str, str]
) -> tuple[str, ...]:
    new_images = get_paths().images_dir
    cleared: set[str] = set()
    for table, row_id, image_path in list(_image_path_rows(conn)):
        relative = _image_relative_path(image_path, source_dir, image_files)
        if relative is None:
            replacement = ""
            cleared.add(image_path)
        else:
            replacement = str(new_images / relative)
        conn.execute(
            f"UPDATE {table} SET image_path = ? WHERE id = ?", (replacement, row_id)
        )
    return tuple(sorted(cleared))


def _prepare_staged_database(
    staged: _StagedLegacy, source_dir: Path
) -> tuple[str, ...]:
    with closing(sqlite3.connect(staged.database_path)) as conn:
        _database_version(conn)
        migrate_database(conn)
        with conn:
            cleared = _rewrite_image_paths(conn, source_dir, staged.image_files)
        valid, detail = database_integrity(conn)
        if not valid:
            raise RuntimeError(f"database integrity check failed: {detail}")
        _validate_current_schema(conn)
    return cleared


def _unique_legacy_backup_destination(paths: AppPaths) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = paths.backups_dir / f"legacy-import-{stamp}"
    counter = 1
    while destination.exists():
        destination = paths.backups_dir / f"legacy-import-{stamp}-{counter}"
        counter += 1
    return destination


def _path_manifest(path: Path) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {"kind": "missing"}
    if _is_link(path):
        raise ValueError(f"live replacement target must not be linked: {path}")
    if path.is_file():
        fingerprint = _fingerprint(path)
        return {
            "kind": "file",
            "digest": fingerprint.digest,
            "size": fingerprint.size,
        }
    if path.is_dir():
        files: dict[str, dict[str, Any]] = {}
        for candidate in sorted(path.rglob("*")):
            if _is_link(candidate):
                raise ValueError(f"live replacement tree must not contain links: {path}")
            if candidate.is_file():
                fingerprint = _fingerprint(candidate)
                files[candidate.relative_to(path).as_posix()] = {
                    "digest": fingerprint.digest,
                    "size": fingerprint.size,
                }
        return {"kind": "directory", "files": files}
    raise ValueError(f"unsupported live replacement target: {path}")


def _validate_live_targets(paths: AppPaths) -> None:
    expected_directories = (
        paths.data_dir,
        paths.images_dir,
        paths.backups_dir,
        paths.runtime_dir,
    )
    expected_files = (
        paths.db_path,
        paths.config_path,
        Path(f"{paths.db_path}-wal"),
        Path(f"{paths.db_path}-shm"),
        Path(f"{paths.db_path}-journal"),
    )
    for target in (*expected_directories, *expected_files):
        if os.path.lexists(target) and _is_link(target):
            raise ValueError(f"live replacement target must not be linked: {target}")
    for directory in expected_directories:
        if os.path.lexists(directory) and not directory.is_dir():
            raise ValueError(f"live directory target is not a directory: {directory}")
    for file_path in expected_files:
        if os.path.lexists(file_path) and not file_path.is_file():
            raise ValueError(f"live file target is not a regular file: {file_path}")
    if os.path.lexists(paths.images_dir):
        _path_manifest(paths.images_dir)


def _write_journal(rollback: Path, journal: dict[str, Any]) -> None:
    journal_path = rollback / "journal.json"
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
        os.replace(temporary_path, journal_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass


def _install_replacements(
    staging: Path, staged_legacy_backups: Path | None
) -> None:
    paths = get_paths()
    replacements: list[tuple[str, Path | None, Path]] = [
        ("database-wal", None, Path(f"{paths.db_path}-wal")),
        ("database-shm", None, Path(f"{paths.db_path}-shm")),
        ("database-journal", None, Path(f"{paths.db_path}-journal")),
        ("database", staging / "data" / "better_money.db", paths.db_path),
        ("config", staging / "data" / "config.json", paths.config_path),
        ("images", staging / "data" / "images", paths.images_dir),
    ]
    if staged_legacy_backups is not None:
        replacements.append(
            (
                "legacy-backups",
                staged_legacy_backups,
                _unique_legacy_backup_destination(paths),
            )
        )
    prepared_items: list[dict[str, Any]] = []
    for label, source, live in replacements:
        prepared_items.append(
            {
                "label": label,
                "live": str(live),
                "original": _path_manifest(live),
                "desired": (
                    _path_manifest(source)
                    if source is not None
                    else {"kind": "missing"}
                ),
                "state": "pending",
            }
        )
    rollback = Path(
        tempfile.mkdtemp(prefix="legacy-rollback-", dir=paths.runtime_dir)
    )
    journal: dict[str, Any] = {
        "operation": "legacy-import",
        "phase": "installing",
        "rollback_dir": str(rollback),
        "items": prepared_items,
    }
    for item in journal["items"]:
        item["saved"] = str(rollback / f"original-{item['label']}")
    try:
        _write_journal(rollback, journal)
    except Exception:
        try:
            shutil.rmtree(rollback)
        except Exception:
            pass
        raise
    try:
        for (label, source, live), item in zip(replacements, journal["items"]):
            saved = Path(item["saved"])
            live.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(live):
                os.replace(live, saved)
                item["state"] = "original-saved"
                _write_journal(rollback, journal)
            if source is not None:
                os.replace(source, live)
            item["state"] = "installed"
            _write_journal(rollback, journal)
            if _path_manifest(live) != item["desired"]:
                raise RuntimeError(f"legacy replacement verification failed: {label}")
        journal["phase"] = "installed"
        _write_journal(rollback, journal)
        if not retire_rollback_for_cleanup(rollback, "legacy-rollback-"):
            raise RuntimeError("legacy rollback directory retirement failed")
    except Exception as install_error:
        journal["phase"] = "recovering"
        journal["install_error"] = repr(install_error)
        recovery_errors: list[str] = []
        try:
            _write_journal(rollback, journal)
        except Exception as journal_error:
            recovery_errors.append(f"journal: {journal_error}")
        recovery_order = sorted(
            journal["items"],
            key=lambda item: (
                0 if item["label"] == "database" else
                1 if item["label"].startswith("database-") else
                2
            ),
        )
        for item in recovery_order:
            live = Path(item["live"])
            saved = Path(item["saved"])
            failed = rollback / f"failed-{item['label']}"
            try:
                if os.path.lexists(saved):
                    if os.path.lexists(live):
                        os.replace(live, failed)
                    os.replace(saved, live)
                elif (
                    item["original"]["kind"] == "missing"
                    and os.path.lexists(live)
                ):
                    os.replace(live, failed)
            except Exception as recovery_error:
                recovery_errors.append(
                    f"{item['label']}: {type(recovery_error).__name__}: {recovery_error}"
                )
            try:
                restored = _path_manifest(live) == item["original"]
            except Exception as verification_error:
                restored = False
                recovery_errors.append(
                    f"{item['label']} verification: {verification_error}"
                )
            item["state"] = "restored" if restored else "recovery-incomplete"
            try:
                _write_journal(rollback, journal)
            except Exception as journal_error:
                recovery_errors.append(
                    f"{item['label']} journal: {journal_error}"
                )
        if all(item["state"] == "restored" for item in journal["items"]):
            journal["phase"] = "recovered"
            try:
                _write_journal(rollback, journal)
            except Exception:
                pass
            retired = retire_rollback_for_cleanup(rollback, "legacy-rollback-")
            if not retired:
                install_error.add_note(
                    "verified rollback cleanup remains pending after retirement failure"
                )
            raise
        journal["phase"] = "recovery-incomplete"
        journal["recovery_errors"] = recovery_errors
        try:
            _write_journal(rollback, journal)
        except Exception as journal_error:
            recovery_errors.append(
                f"recovery-incomplete journal: {type(journal_error).__name__}: "
                f"{journal_error}"
            )
        raise LegacyRecoveryIncompleteError(
            "legacy import failed and recovery is incomplete; "
            f"install error: {install_error!r}; originals remain in rollback "
            f"journal directory: {rollback}"
        ) from install_error
def _checkpoint_live_database(paths: AppPaths) -> None:
    if not paths.db_path.exists():
        return
    with closing(sqlite3.connect(paths.db_path, timeout=0)) as conn:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and int(result[0]) != 0:
            raise RuntimeError("live database still has an active connection")


def _validate_iso_date(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("initial_balance_date must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("initial_balance_date must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("initial_balance_date must be an ISO date")
    return value


def import_legacy(source: Path, initial_balance_date: str) -> LegacyInspection:
    """Validate and install a copied legacy dataset with rollback on failure."""
    confirmed_date = _validate_iso_date(initial_balance_date)
    paths = get_paths()
    _validate_live_targets(paths)
    paths.ensure_directories()
    _validate_live_targets(paths)
    source_dir = _resolve_source(source)

    staging = Path(
        tempfile.mkdtemp(prefix="better-money-legacy-", dir=paths.runtime_dir)
    )
    try:
        staged = _stage_legacy(source_dir, staging, include_backups=True)
        inspection = _inspect_staged(source_dir, staged)
        staged.config["initial_balance_date"] = confirmed_date
        (staging / "data" / "config.json").write_text(
            json.dumps(staged.config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cleared = _prepare_staged_database(staged, source_dir)
        with LEDGER_GATE.exclusive():
            _validate_live_targets(paths)
            if paths.db_path.is_file():
                create_backup("pre-legacy-import")
                _checkpoint_live_database(paths)
            _install_replacements(staging, staged.legacy_backups)
        return replace(inspection, cleared_image_paths=cleared)
    finally:
        try:
            shutil.rmtree(staging)
        except Exception:
            pass
