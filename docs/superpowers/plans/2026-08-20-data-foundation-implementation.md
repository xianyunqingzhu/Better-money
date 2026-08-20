# Better Money Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated, versioned, and recoverable data layer that supports installed Windows paths, legacy migration, automatic backups, and safe restore.

**Architecture:** Introduce a single `AppPaths` resolver used by every persistence component, then route database creation through versioned migrations. Backup and legacy-import services operate through temporary directories and validation before atomically replacing live data; FastAPI exposes narrow endpoints for the browser UI.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, pytest, standard-library `zipfile`, `tempfile`, `pathlib`, and `sqlite3.Connection.backup`.

**Spec:** `docs/superpowers/specs/2026-08-20-windows-productization-design.md`

## Global Constraints

- Target Windows 10/11 64-bit.
- Installed personal data root is exactly `%LOCALAPPDATA%\BetterMoney`.
- Development mode continues to use the repository-local `data` directory.
- API Key is excluded from logs, runtime files, automatic backups, and exported backup archives.
- Restore, legacy migration, schema migration, and initial-balance correction create a safety backup before replacing or changing durable data.
- Existing SQLite records must survive every schema migration.
- Automated tests must never read or write the repository's real `data` directory.

---

### Task 1: Isolated application paths and pytest harness

**Files:**
- Create: `requirements-dev.txt`
- Create: `app/paths.py`
- Create: `app/version.py`
- Create: `tests/conftest.py`
- Create: `tests/test_paths.py`
- Modify: `app/config.py:1-41`
- Modify: `app/db.py:1-82`
- Modify: `app/main.py:1-30, 280-322, 752-759, 832-840`

**Interfaces:**
- Produces: `AppPaths`, `get_paths() -> AppPaths`, `resource_root() -> Path`, and `reset_paths_cache() -> None` from `app.paths`.
- Produces: `APP_ID = "better-money"`, `APP_VERSION = "1.0.0"`, and `HEALTH_PROTOCOL = 1` from `app.version` for backup manifests and later packaging.
- Produces: environment variable `BETTER_MONEY_HOME`, whose value is the parent containing `data`, `backups`, `logs`, and `runtime`.
- Consumes: no earlier plan interfaces.

- [ ] **Step 1: Add development test dependencies**

Create `requirements-dev.txt` with runtime requirements plus pytest:

```text
-r requirements.txt
pytest>=8.0,<9
```

- [ ] **Step 2: Write failing path-resolution tests**

Create `tests/test_paths.py`:

```python
from pathlib import Path

from app.paths import get_paths, reset_paths_cache


def test_environment_home_controls_every_writable_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_MONEY_HOME", str(tmp_path))
    reset_paths_cache()
    paths = get_paths()
    assert paths.root == tmp_path
    assert paths.data_dir == tmp_path / "data"
    assert paths.db_path == tmp_path / "data" / "better_money.db"
    assert paths.config_path == tmp_path / "data" / "config.json"
    assert paths.images_dir == tmp_path / "data" / "images"
    assert paths.backups_dir == tmp_path / "backups"
    assert paths.logs_dir == tmp_path / "logs"
    assert paths.runtime_dir == tmp_path / "runtime"


def test_paths_create_writable_directories(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_MONEY_HOME", str(tmp_path))
    reset_paths_cache()
    paths = get_paths()
    paths.ensure_directories()
    assert all(p.is_dir() for p in (
        paths.data_dir, paths.images_dir, paths.backups_dir,
        paths.logs_dir, paths.runtime_dir,
    ))
```

- [ ] **Step 3: Run the path tests and verify the expected import failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests\test_paths.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.paths'`.

- [ ] **Step 4: Implement the path resolver**

Create `app/paths.py` with these public definitions:

```python
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data_dir: Path
    config_path: Path
    db_path: Path
    images_dir: Path
    backups_dir: Path
    logs_dir: Path
    runtime_dir: Path

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir, self.images_dir, self.backups_dir,
            self.logs_dir, self.runtime_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_paths() -> AppPaths:
    configured = os.environ.get("BETTER_MONEY_HOME", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        installed_layout = True
    elif getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise RuntimeError("Windows LOCALAPPDATA is unavailable")
        root = (Path(local) / "BetterMoney").resolve()
        installed_layout = True
    else:
        root = PROJECT_ROOT
        installed_layout = False
    data = root / "data"
    support = root if installed_layout else data
    return AppPaths(
        root=root,
        data_dir=data,
        config_path=data / "config.json",
        db_path=data / "better_money.db",
        images_dir=data / "images",
        backups_dir=support / "backups",
        logs_dir=support / "logs",
        runtime_dir=support / "runtime",
    )


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else PROJECT_ROOT


def reset_paths_cache() -> None:
    get_paths.cache_clear()
```

Create `app/version.py` at the same time:

```python
APP_ID = "better-money"
APP_VERSION = "1.0.0"
HEALTH_PROTOCOL = 1
```

- [ ] **Step 5: Route config, database, uploads, images, backup export, and static files through `AppPaths`**

Replace module-level writable constants with `get_paths()` calls. Keep `load_config()` and `save_config()` signatures unchanged. Use `resource_root() / "static"` for FastAPI static mounting and `index.html`. At application lifespan start, call `get_paths().ensure_directories()` before database initialization.

The relevant replacements are:

```python
paths = get_paths()
paths.config_path
paths.db_path
paths.images_dir
paths.backups_dir
resource_root() / "static"
```

- [ ] **Step 6: Add a session-wide temporary application home fixture**

Create `tests/conftest.py` with a fresh application home for every test. Import the FastAPI app only inside the `client` fixture, after the path cache is reset:

```python
import pytest
from fastapi.testclient import TestClient

from app.paths import get_paths, reset_paths_cache


@pytest.fixture(autouse=True)
def isolated_application_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_MONEY_HOME", str(tmp_path))
    reset_paths_cache()
    get_paths().ensure_directories()
    yield tmp_path
    reset_paths_cache()


@pytest.fixture
def app_home(isolated_application_home):
    return isolated_application_home


@pytest.fixture
def conn(app_home):
    from app import db
    db.init_db()
    connection = db.get_conn()
    yield connection
    connection.close()


@pytest.fixture
def client(app_home):
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client
```

- [ ] **Step 7: Run focused and existing tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_paths.py -v
.\.venv\Scripts\python.exe -m compileall app
```

Expected: path tests pass and compilation exits with code 0.

- [ ] **Step 8: Commit the isolated path foundation**

```powershell
git add requirements-dev.txt app\paths.py app\version.py app\config.py app\db.py app\main.py tests\conftest.py tests\test_paths.py
git commit -m "refactor: isolate application data paths"
```

### Task 2: Versioned SQLite migrations

**Files:**
- Create: `app/migrations.py`
- Create: `tests/test_migrations.py`
- Modify: `app/db.py:5-78`

**Interfaces:**
- Consumes: `get_paths().db_path` from Task 1.
- Produces: `CURRENT_SCHEMA_VERSION: int`, `migrate_database(conn: sqlite3.Connection) -> None`, and `database_integrity(conn) -> tuple[bool, str]`.

- [ ] **Step 1: Write a failing legacy-database preservation test**

Create a SQLite database with the current legacy schema, insert one transaction, one goal, one summary, and one adjustment, then call `migrate_database`:

```python
from app.db import SCHEMA as LEGACY_SCHEMA
from app.migrations import CURRENT_SCHEMA_VERSION, migrate_database


def test_migration_preserves_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO transactions(date, amount, type, category, created_at, updated_at) "
        "VALUES ('2026-08-01', 20, '支出', '餐饮', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO goals(name, price, saved, created_at) VALUES ('相机', 8000, 3000, 'now')"
    )
    conn.execute(
        "INSERT INTO summaries(period_type, period_start, period_end, content, created_at) "
        "VALUES ('周', '2026-08-01', '2026-08-07', '内容', 'now')"
    )
    conn.execute(
        "INSERT INTO adjustments(date, diff, note, created_at) "
        "VALUES ('2026-08-01', 5, '校准', 'now')"
    )
    conn.commit()
    migrate_database(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0] == 1
    columns = {r[1] for r in conn.execute("PRAGMA table_info(adjustments)")}
    assert "reverses_adjustment_id" in columns
```

- [ ] **Step 2: Run the migration test and verify the import failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_migrations.py::test_migration_preserves_legacy_rows -v
```

Expected: collection fails because `app.migrations` does not exist.

- [ ] **Step 3: Implement ordered, transactional migrations**

Create `app/migrations.py` with `CURRENT_SCHEMA_VERSION = 2`. Version 1 creates the existing base schema for a fresh database. Version 2:

- Adds nullable `reverses_adjustment_id INTEGER REFERENCES adjustments(id)` when absent.
- Creates index `idx_adjustments_reverses`.
- Deduplicates summaries by keeping the highest `id` for equal `(period_type, period_start, period_end)`.
- Creates unique index `uq_summaries_period_range` on those three fields.

Use `PRAGMA user_version` and a `with conn:` transaction. Before and after migration, run:

```python
row = conn.execute("PRAGMA integrity_check").fetchone()
if not row or row[0] != "ok":
    raise RuntimeError(f"database integrity check failed: {row[0] if row else 'no result'}")
```

- [ ] **Step 4: Make database initialization call the migrator**

Change `db.init_db()` to open `get_paths().db_path` and call `migrate_database(conn)` instead of only calling `executescript(SCHEMA)`. Retain the public `get_conn()` and `now_str()` interfaces.

- [ ] **Step 5: Add idempotence and corrupted-database tests**

Add tests that call `migrate_database` twice and assert version 2 both times, and that pass a non-SQLite file and assert a controlled `sqlite3.DatabaseError` or `RuntimeError` without overwriting the file.

- [ ] **Step 6: Run migration and path tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_migrations.py tests\test_paths.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit database migrations**

```powershell
git add app\migrations.py app\db.py tests\test_migrations.py
git commit -m "feat: add versioned database migrations"
```

### Task 3: Verified backup archives and safe restore

**Files:**
- Modify: `app/backup.py:1-22`
- Create: `tests/test_backup.py`

**Interfaces:**
- Consumes: `AppPaths` from Task 1 and `database_integrity` from Task 2.
- Produces: `BackupManifest`, `create_backup(reason: str, include_images: bool = False) -> Path`, `inspect_backup(archive: Path) -> BackupManifest`, and `restore_backup(archive: Path) -> None`.

- [ ] **Step 1: Write failing archive-content and sanitization tests**

Seed a temporary database and config containing `api_key: "secret"`, create a backup, and inspect the ZIP:

```python
def test_backup_contains_database_manifest_and_sanitized_config(app_home):
    archive = create_backup("manual")
    with zipfile.ZipFile(archive) as zf:
        assert {"manifest.json", "data/better_money.db", "data/config.json"} <= set(zf.namelist())
        config = json.loads(zf.read("data/config.json"))
        manifest = json.loads(zf.read("manifest.json"))
    assert "api_key" not in config
    assert manifest["format_version"] == 1
    assert manifest["reason"] == "manual"
```

- [ ] **Step 2: Run the backup test and verify it fails against the current DB-only copier**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_backup.py::test_backup_contains_database_manifest_and_sanitized_config -v
```

Expected: fail because the current `backup.py` does not produce a ZIP archive or sanitized config.

- [ ] **Step 3: Implement backup creation**

Use `sqlite3.Connection.backup()` to copy the live database to a temporary database. Write `manifest.json`, sanitized `config.json`, and optionally files under `data/images/` to a temporary ZIP in `backups_dir`; validate it with `inspect_backup`, then rename it to:

```text
better-money-YYYYMMDD-HHMMSS-<reason>.zip
```

Define `BackupManifest` as a frozen dataclass with `format_version: int`, `created_at: str`, `app_version: str`, `schema_version: int`, `reason: str`, and `includes_images: bool`. `inspect_backup` returns this type only after validating all six fields and the contained database.

The manifest contains exact keys:

```json
{
  "format_version": 1,
  "created_at": "2026-08-20T12:00:00+08:00",
  "app_version": "1.0.0",
  "schema_version": 2,
  "reason": "manual",
  "includes_images": false
}
```

Sanitize `api_key` by removing the key entirely rather than writing an empty string.

- [ ] **Step 4: Write failing restore rollback tests**

Add one test restoring a valid archive and asserting seeded replacement data is visible, and one test passing a ZIP whose database fails `PRAGMA integrity_check`; the second test must assert the current database bytes remain unchanged.

- [ ] **Step 5: Implement restore through a staging directory**

`restore_backup` must:

1. Call `inspect_backup`.
2. Create `create_backup("pre-restore")`.
3. Extract only declared members into a temporary directory, rejecting absolute paths and `..` path components.
4. Migrate and integrity-check the staged database.
5. Replace live database and sanitized settings with `os.replace` only after validation succeeds.
6. Replace images only when `includes_images` is true.

- [ ] **Step 6: Implement daily retention**

Add `ensure_daily_backup(keep: int = 30) -> Path | None`. It returns `None` when a valid automatic backup already exists for the current local date; otherwise it creates one. Delete only archives whose manifest reason is `automatic`, keeping the newest 30. Manual and pre-operation backups are not removed by automatic retention.

- [ ] **Step 7: Run all backup tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_backup.py -v
```

Expected: valid restore, corrupt restore rollback, API Key exclusion, image inclusion choice, daily de-duplication, and retention tests all pass.

- [ ] **Step 8: Commit backup and restore services**

```powershell
git add app\backup.py tests\test_backup.py
git commit -m "feat: add verified backup and restore archives"
```

### Task 4: Legacy project migration service

**Files:**
- Create: `app/legacy_migration.py`
- Create: `tests/test_legacy_migration.py`

**Interfaces:**
- Consumes: `create_backup`, `database_integrity`, `migrate_database`, and `AppPaths`.
- Produces: `LegacyInspection`, `inspect_legacy(source: Path) -> LegacyInspection`, and `import_legacy(source: Path, initial_balance_date: str) -> LegacyInspection`.

- [ ] **Step 1: Write failing inspection tests for project-root and data-directory selection**

Create test fixtures containing either `<source>/data/better_money.db` or `<source>/better_money.db`. Assert both resolve to the same legacy data directory and return transaction, goal, and summary counts plus calculated balance.

```python
inspection = inspect_legacy(source)
assert inspection.transaction_count == 2
assert inspection.goal_count == 1
assert inspection.summary_count == 1
assert inspection.suggested_initial_balance_date == "2026-08-01"
```

- [ ] **Step 2: Run the inspection tests and verify the module import failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_legacy_migration.py -v
```

Expected: collection fails because `app.legacy_migration` does not exist.

- [ ] **Step 3: Implement read-only inspection**

Define `LegacyInspection` as a frozen dataclass containing resolved source directory, counts, earliest transaction date, suggested initial-balance date, initial balance, and calculated current balance. Open the source database in SQLite read-only URI mode:

```python
sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
```

Reject a source that resolves to the live application data directory.

- [ ] **Step 4: Write failing import preservation tests**

Seed live data and legacy data. After import, assert legacy data is live, the original legacy bytes are unchanged, and a `pre-legacy-import` backup exists containing the former live data.

- [ ] **Step 5: Implement staged legacy import**

Copy the selected old `data` directory into a temporary staging directory. Migrate and integrity-check its database, merge the selected `initial_balance_date` into staged config, then rewrite `summaries.image_path` and `pending_items.image_path` values that point inside the selected old `data/images` directory so they point to the new `%LOCALAPPDATA%\BetterMoney\data\images` location. Paths outside the selected legacy directory are cleared and reported rather than copied from arbitrary locations. Then call `create_backup("pre-legacy-import")` and atomically replace live database/config/images. Never delete or write to the selected legacy directory.

If the old data directory contains `backups/`, copy those files into `backups/legacy-import-<timestamp>/` without treating old raw `.db` files as new-format ZIP archives. This preserves them for manual recovery while keeping the new backup list format-safe.

Preserve an old API Key during direct local migration because the user selected their own trusted local directory; exported backup archives remain sanitized.

- [ ] **Step 6: Run migration service tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_legacy_migration.py tests\test_backup.py tests\test_migrations.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the legacy migration service**

```powershell
git add app\legacy_migration.py tests\test_legacy_migration.py
git commit -m "feat: add safe legacy data migration"
```

### Task 5: Data-management API endpoints

**Files:**
- Create: `app/data_api.py`
- Create: `app/native_dialogs.py`
- Create: `tests/test_data_api.py`
- Modify: `app/main.py:20-29, 184-215, 821-841`

**Interfaces:**
- Consumes: backup and migration services from Tasks 3 and 4.
- Produces: FastAPI router with `/api/backups`, `/api/backups/create`, `/api/backups/export`, `/api/backups/restore`, `/api/migration/select-folder`, `/api/migration/inspect`, `/api/migration/import`, and `/api/system/open-data-folder`.

- [ ] **Step 1: Write failing API contract tests**

Use `fastapi.testclient.TestClient` against the app and assert:

```python
def test_create_and_list_backup(client):
    created = client.post("/api/backups/create", json={"include_images": False})
    assert created.status_code == 200
    item = created.json()
    assert item["filename"].endswith("-manual.zip")
    listed = client.get("/api/backups")
    assert listed.status_code == 200
    assert item["filename"] in {x["filename"] for x in listed.json()}
```

Add tests for invalid restore upload returning 400, missing migration source returning 404, and `open-data-folder` being replaceable by a monkeypatched function so tests never open Explorer.

- [ ] **Step 2: Run the API tests and verify 404 responses**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_data_api.py -v
```

Expected: fail because the routes do not exist.

- [ ] **Step 3: Implement native folder and Explorer helpers**

`app/native_dialogs.py` exposes:

```python
def choose_directory(title: str) -> Path | None:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(title=title, mustexist=True)
        return Path(selected) if selected else None
    finally:
        root.destroy()


def open_directory(path: Path) -> None:
    os.startfile(path)
```

On non-Windows systems, `open_directory` raises a controlled `RuntimeError`; tests monkeypatch both functions.

- [ ] **Step 4: Implement the data router**

Use Pydantic request models with explicit fields. Restore accepts a multipart ZIP upload, saves it to a temporary file under application home, calls `restore_backup`, and always removes the upload in `finally`. Migration inspection accepts a selected absolute path; import requires the same path plus ISO `initial_balance_date`.

Return stable errors:

```json
{"error":"invalid_backup","message":"备份文件无效或已损坏"}
{"error":"invalid_legacy_source","message":"所选文件夹不是可迁移的 Better Money 数据"}
{"error":"migration_failed","message":"迁移失败，现有数据未改变"}
```

- [ ] **Step 5: Include the router and replace the legacy DB-only export**

Include the router in `app.main`. Remove `/api/export/backup.db`; retain `/api/export/transactions.csv`. At lifespan startup call `ensure_daily_backup()` only after `db.init_db()` succeeds.

- [ ] **Step 6: Run data API and service tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_data_api.py tests\test_backup.py tests\test_legacy_migration.py -v
```

Expected: all tests pass without opening native dialogs.

- [ ] **Step 7: Commit data-management APIs**

```powershell
git add app\data_api.py app\native_dialogs.py app\main.py tests\test_data_api.py
git commit -m "feat: expose backup restore and migration APIs"
```

### Task 6: Data-foundation regression gate

**Files:**
- Modify: `README.md:20-90`
- Modify: `使用说明.md:42-92, 295-315, 365-385`
- Modify: `tests/run_all.py:1-40`

**Interfaces:**
- Consumes: all interfaces in this plan.
- Produces: a documented development test command that never uses repository data.

- [ ] **Step 1: Update the legacy script runner to reject real data paths**

At the beginning of `tests/run_all.py`, require `BETTER_MONEY_HOME` and abort when it resolves to the repository root:

```python
configured = os.environ.get("BETTER_MONEY_HOME", "").strip()
if not configured:
    raise SystemExit("BETTER_MONEY_HOME must point to a temporary test directory")
if Path(configured).resolve() == Path(__file__).resolve().parents[1]:
    raise SystemExit("refusing to run tests against repository data")
```

- [ ] **Step 2: Document development and installed data locations**

Update README and 使用说明 to state that source mode uses repository `data`, the future installed build uses `%LOCALAPPDATA%\BetterMoney`, and tests use a temporary `BETTER_MONEY_HOME`. Replace the old claim that `backup.db` is a complete backup with the ZIP archive behavior.

- [ ] **Step 3: Run the complete data-foundation suite**

```powershell
$env:BETTER_MONEY_HOME = Join-Path $env:TEMP 'better-money-plan-a-tests'
.\.venv\Scripts\python.exe -m pytest tests\test_paths.py tests\test_migrations.py tests\test_backup.py tests\test_legacy_migration.py tests\test_data_api.py -v
Remove-Item Env:BETTER_MONEY_HOME
```

Expected: all tests pass and no files under repository `data` change.

- [ ] **Step 4: Verify source-mode startup against a disposable home**

```powershell
$testHome = Join-Path $env:TEMP 'better-money-source-smoke'
$env:BETTER_MONEY_HOME = $testHome
$process = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8765' -PassThru -WindowStyle Hidden
Invoke-RestMethod 'http://127.0.0.1:8765/api/health'
Stop-Process -Id $process.Id
Remove-Item Env:BETTER_MONEY_HOME
```

Expected: health JSON contains `ok: true`; repository data remains unchanged.

- [ ] **Step 5: Commit the data-foundation documentation and guard**

```powershell
git add README.md 使用说明.md tests\run_all.py
git commit -m "docs: document safe data and backup workflows"
```

- [ ] **Step 6: Record Gate A evidence**

Run `git status --short` and confirm no unexpected files. Save the passing pytest command and smoke-test output in the implementation handoff message; do not create a release artifact at this gate.
