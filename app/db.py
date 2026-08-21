"""SQLite 数据库：建表与基础访问。数据文件：data/better_money.db"""
from contextlib import contextmanager
import errno
import os
import sqlite3
from datetime import datetime
import threading
import time
from typing import Iterator

from app.migrations import BASE_SCHEMA as SCHEMA
from app.migrations import CURRENT_SCHEMA_VERSION, migrate_database
from app.paths import get_paths


class _InterprocessLedgerLock:
    """Advisory lock shared by every supported Better Money process."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor
        self._mutex = threading.Lock()
        self._locked = True

    @classmethod
    def acquire(cls) -> "_InterprocessLedgerLock":
        paths = get_paths()
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock_path = paths.runtime_dir / "ledger.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {
                            errno.EACCES,
                            errno.EAGAIN,
                            errno.EDEADLK,
                        }:
                            raise
                        time.sleep(0.05)
                        os.lseek(descriptor, 0, os.SEEK_SET)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            return cls(descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        with self._mutex:
            if not self._locked:
                return
            assert self._descriptor is not None
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            self._locked = False
            descriptor = self._descriptor
            self._descriptor = None
            os.close(descriptor)


class LedgerGate:
    """Coordinate ordinary connections with an exclusive migration phase."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_connections = 0
        self._exclusive_owner: int | None = None
        self._exclusive_depth = 0
        self._exclusive_waiters = 0
        self._exclusive_process_lock: _InterprocessLedgerLock | None = None

    def acquire_connection(self) -> None:
        with self._condition:
            identity = threading.get_ident()
            if self._exclusive_owner == identity:
                raise RuntimeError(
                    "exclusive migration owner cannot open an ordinary connection"
                )
            while (
                self._exclusive_owner is not None
                or self._exclusive_waiters
                or self._active_connections
            ):
                self._condition.wait()
            self._active_connections += 1

    def release_connection(self) -> None:
        with self._condition:
            if self._active_connections <= 0:
                raise RuntimeError("ledger connection slot was released twice")
            self._active_connections -= 1
            if self._active_connections == 0:
                self._condition.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        identity = threading.get_ident()
        outermost = False
        with self._condition:
            if self._exclusive_owner == identity:
                self._exclusive_depth += 1
            else:
                self._exclusive_waiters += 1
                try:
                    while (
                        self._exclusive_owner is not None
                        or self._active_connections
                    ):
                        self._condition.wait()
                    self._exclusive_owner = identity
                    self._exclusive_depth = 1
                    outermost = True
                finally:
                    self._exclusive_waiters -= 1
                    self._condition.notify_all()
        if outermost:
            try:
                process_lock = _InterprocessLedgerLock.acquire()
            except BaseException:
                with self._condition:
                    self._exclusive_owner = None
                    self._exclusive_depth = 0
                    self._condition.notify_all()
                raise
            with self._condition:
                self._exclusive_process_lock = process_lock
        try:
            yield
        finally:
            with self._condition:
                if self._exclusive_owner != identity:
                    raise RuntimeError("exclusive migration ownership was lost")
                self._exclusive_depth -= 1
                if self._exclusive_depth == 0:
                    process_lock = self._exclusive_process_lock
                    try:
                        assert process_lock is not None
                        process_lock.release()
                    finally:
                        self._exclusive_process_lock = None
                        self._exclusive_owner = None
                        self._condition.notify_all()


LEDGER_GATE = LedgerGate()


class _LockedConnection(sqlite3.Connection):
    """SQLite connection that releases its ledger slot exactly once."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._close_mutex = threading.Lock()
        self._owns_ledger_slot = False
        self._underlying_closed = False
        self._ledger_gate: LedgerGate | None = None
        self._process_lock: _InterprocessLedgerLock | None = None

    def _close_underlying(self) -> None:
        super().close()

    def close(self) -> None:
        with self._close_mutex:
            if not self._underlying_closed:
                self._close_underlying()
                self._underlying_closed = True
            if self._process_lock is not None:
                self._process_lock.release()
                self._process_lock = None
            if self._owns_ledger_slot:
                self._owns_ledger_slot = False
                assert self._ledger_gate is not None
                self._ledger_gate.release_connection()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
        return bool(result)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            _quarantine_connection(self)


_QUARANTINED_CONNECTIONS: list[_LockedConnection] = []
_QUARANTINE_MUTEX = threading.Lock()


def _quarantine_connection(connection: _LockedConnection) -> None:
    with _QUARANTINE_MUTEX:
        if connection not in _QUARANTINED_CONNECTIONS:
            _QUARANTINED_CONNECTIONS.append(connection)


def retry_quarantined_connections() -> int:
    """Retry failed closes and return the number still quarantined."""
    with _QUARANTINE_MUTEX:
        quarantined = tuple(_QUARANTINED_CONNECTIONS)
    for connection in quarantined:
        try:
            connection.close()
        except Exception:
            continue
        with _QUARANTINE_MUTEX:
            if connection in _QUARANTINED_CONNECTIONS:
                _QUARANTINED_CONNECTIONS.remove(connection)
    with _QUARANTINE_MUTEX:
        return len(_QUARANTINED_CONNECTIONS)


def get_conn() -> sqlite3.Connection:
    paths = get_paths()
    gate = LEDGER_GATE
    gate.acquire_connection()
    process_lock: _InterprocessLedgerLock | None = None
    conn: _LockedConnection | None = None
    try:
        process_lock = _InterprocessLedgerLock.acquire()
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            paths.db_path,
            factory=_LockedConnection,
            check_same_thread=False,
        )
        conn._ledger_gate = gate
        conn._process_lock = process_lock
        process_lock = None
        conn._owns_ledger_slot = True
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except BaseException as creation_error:
        if conn is None:
            if process_lock is not None:
                try:
                    process_lock.release()
                finally:
                    gate.release_connection()
            else:
                gate.release_connection()
            raise
        try:
            conn.close()
        except BaseException:
            _quarantine_connection(conn)
            raise RuntimeError(
                "database connection initialization failed and its handle is "
                "quarantined because close also failed"
            ) from creation_error
        raise


def init_db() -> None:
    paths = get_paths()
    db_existed = paths.db_path.exists()
    conn = get_conn()
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if db_existed and current_version < CURRENT_SCHEMA_VERSION:
            _create_pre_migration_backup(conn, current_version)
        migrate_database(conn)
    finally:
        conn.close()


def _create_pre_migration_backup(conn: sqlite3.Connection, current_version: int) -> None:
    """Create an integrity-checked SQLite copy before an in-place upgrade."""
    paths = get_paths()
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = paths.backups_dir / (
        f"pre-migration-v{current_version}-to-v{CURRENT_SCHEMA_VERSION}-{stamp}.db"
    )
    backup = sqlite3.connect(backup_path)
    try:
        conn.backup(backup)
        row = backup.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(
                f"pre-migration backup integrity check failed: {row[0] if row else 'no result'}"
            )
    finally:
        backup.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
