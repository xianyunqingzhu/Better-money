# Better Money Windows Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reliable Windows 10/11 64-bit installer whose shortcut starts one verified local Better Money instance without Python and preserves personal data across upgrade and uninstall.

**Architecture:** Build one windowless PyInstaller executable with launcher mode by default and server mode behind an internal command-line flag. The launcher owns instance discovery, dynamic local-port selection, detached server startup, health verification, browser opening, and readable errors; Inno Setup installs the onedir bundle and manages shortcuts, upgrades, and optional personal-data removal.

**Tech Stack:** Python 3.12+, FastAPI/Uvicorn, PyInstaller 6.x on Windows, Inno Setup 6.x, Windows `ctypes`, PowerShell build scripts, pytest, and standard-library process/network primitives.

**Spec:** `docs/superpowers/specs/2026-08-20-windows-productization-design.md`

## Global Constraints

- Execute only after Data Gate A and Core Finance Gate B pass.
- Build and test the Windows artifact on Windows; PyInstaller is not used as a cross-compiler.
- Target Windows 10/11 64-bit only for version 1.0.0.
- The installed executable must not require Python or a first-run dependency download.
- The server binds only `127.0.0.1`.
- Health checks verify Better Money product ID and version, not merely an open port.
- Only one server instance runs per Windows user.
- Installation directory is user-selectable; personal data remains `%LOCALAPPDATA%\BetterMoney`.
- Closing the browser does not stop the service; settings provides explicit exit.
- GitHub publication requires separate user authorization.

---

### Task 1: Product identity, version, and controllable Uvicorn server

**Files:**
- Modify: `app/version.py:1-3`
- Create: `app/server.py`
- Create: `tests/test_server_control.py`
- Modify: `app/main.py:20-40, 821-841`
- Modify: `static/index.html:140-190`
- Modify: `static/app.js:806-905`

**Interfaces:**
- Consumes: `APP_ID = "better-money"`, `APP_VERSION = "1.0.0"`, and `HEALTH_PROTOCOL = 1` created by Data Foundation Task 1.
- Produces: `run_server(host: str, port: int, session_token: str) -> int`.
- Produces: `/api/health` response with `ok`, `app_id`, `version`, and `protocol`; same-origin `/api/runtime` returns control availability and the current ephemeral token; `/api/control/shutdown` requires `X-Better-Money-Token`.

- [ ] **Step 1: Write failing product identity and shutdown authorization tests**

Create `tests/test_server_control.py`:

```python
from unittest.mock import Mock

import pytest


@pytest.fixture
def controlled_client(client):
    from app.main import app
    app.state.session_token = "expected-token"
    app.state.request_shutdown = Mock()
    yield client
    app.state.session_token = ""
    app.state.request_shutdown = None


def test_health_identifies_product(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "app_id": "better-money",
        "version": "1.0.0",
        "protocol": 1,
        "ai_configured": False,
    }


def test_shutdown_rejects_missing_token(controlled_client):
    response = controlled_client.post("/api/control/shutdown")
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
```

Add success with the exact token and rejection with a wrong token.

- [ ] **Step 2: Run the server-control tests and verify they fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_server_control.py -v
```

Expected: health response lacks product fields and shutdown route is 404.

- [ ] **Step 3: Use the stable version module in the health contract**

Confirm `app/version.py` contains:

```python
APP_ID = "better-money"
APP_VERSION = "1.0.0"
HEALTH_PROTOCOL = 1
```

Change health response to include those values and `ai_configured`. Do not return paths, port, session token, API Base, or model names.

- [ ] **Step 4: Implement controlled Uvicorn startup**

`app/server.py` creates `uvicorn.Config` and `uvicorn.Server`, stores a shutdown callback and token on `app.state`, and returns 0 after clean exit:

```python
def run_server(host: str, port: int, session_token: str) -> int:
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    app.state.session_token = session_token
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0
```

Configure separate `startup.log` and `business.log` files under `get_paths().logs_dir`. Apply one logging filter to both files that redacts session tokens, `Authorization` values, API Key fields, and configured API Key values.

- [ ] **Step 5: Implement the protected shutdown endpoint and settings button**

Compare the request header with `secrets.compare_digest`. When valid, schedule `app.state.request_shutdown` after returning HTTP 200 so the browser receives confirmation. Add `/api/runtime`, returning `{"control_available": true, "session_token": "<ephemeral>"}` only when a token exists; the response is not cached and the app does not enable cross-origin requests. The settings button fetches this endpoint immediately before shutdown; never store the token in `config.json`, localStorage, backup, or logs.

If the service is running in developer mode without a control token, return 409 `shutdown_unavailable` and keep the developer process alive.

- [ ] **Step 6: Run server-control and regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_server_control.py tests\test_data_api.py tests\test_goals.py tests\test_summaries_api.py tests\test_ledger.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit product identity and server control**

```powershell
git add app\version.py app\server.py app\main.py static\index.html static\app.js tests\test_server_control.py
git commit -m "feat: add verified health and secure shutdown"
```

### Task 2: Single-instance launcher and dynamic port selection

**Files:**
- Create: `app/launcher.py`
- Create: `windows_entry.py`
- Create: `tests/test_launcher.py`

**Interfaces:**
- Consumes: `run_server`, `APP_ID`, `APP_VERSION`, `HEALTH_PROTOCOL`, and `get_paths().runtime_dir`.
- Produces: `InstanceRecord`, `find_available_port(preferred: int = 8642) -> int`, `probe_instance(record) -> bool`, `launch_or_open() -> int`, and `windows_entry.main(argv=None) -> int`.

- [ ] **Step 1: Write failing pure launcher tests**

Create tests for a free preferred port, occupied preferred port choosing another port, valid health identity, wrong app identity, stale runtime record, and an already-running valid instance opening without a second spawn. Network and process seams are explicit module functions `fetch_health(port: int) -> dict`, `spawn_server(port: int, token: str) -> subprocess.Popen`, and `open_browser(port: int) -> None`, so tests monkeypatch them directly.

```python
def test_wrong_product_on_port_is_not_accepted(monkeypatch):
    monkeypatch.setattr("app.launcher.fetch_health", lambda port: {
        "ok": True,
        "app_id": "another-app",
        "version": "1.0.0",
        "protocol": 1,
    })
    record = InstanceRecord(pid=123, port=8642, token="x", version="1.0.0", started_at="now")
    assert probe_instance(record) is False
```

Use monkeypatched process and browser functions; tests must not spawn real detached processes or open a browser.

- [ ] **Step 2: Run launcher tests and verify the module import failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher.py -v
```

Expected: collection fails because `app.launcher` does not exist.

- [ ] **Step 3: Implement runtime records and health probing**

`InstanceRecord` contains `pid`, `port`, `token`, `version`, and `started_at`. Save it as UTF-8 JSON under `runtime/instance.json` with permissions limited to the current user where Windows supports ACL inheritance. Write to a sibling temporary file and replace atomically.

Probe `http://127.0.0.1:<port>/api/health` with a one-second timeout and accept only exact `app_id`, `version`, and protocol. A stale or malformed record is renamed `instance.stale-<timestamp>.json` before starting.

- [ ] **Step 4: Implement per-user Windows single-instance locking**

Use `CreateMutexW` through `ctypes.windll.kernel32`. Derive the per-user suffix as `sha256(str(get_paths().root).casefold().encode("utf-8")).hexdigest()[:16]` and use name:

```text
Local\BetterMoneyLauncher-<application-home-hash>
```

Do not use a global mutex requiring elevated rights. When another launcher briefly owns the mutex, wait up to five seconds, then re-read the runtime record and open the verified instance.

- [ ] **Step 5: Implement server spawn and readiness wait**

Generate a 32-byte URL-safe token. In frozen mode spawn the following command, passing the token only through the child environment variable `BETTER_MONEY_SESSION_TOKEN` so it does not appear in the command line:

```text
BetterMoney.exe --server --host 127.0.0.1 --port <port>
```

In source mode spawn the current Python interpreter with `windows_entry.py` and the same non-secret flags. On Windows use `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`. Poll health every 200ms for up to 30 seconds. If the child exits because another process captured the selected port, select another free port and retry, for at most three attempts. Only after verified success write the instance record and call `webbrowser.open`.

If startup fails, show a native `MessageBoxW` containing a short Chinese explanation and the log path; never display the token.

- [ ] **Step 6: Implement the entry-point mode switch**

`windows_entry.py` uses `argparse`. Default mode calls `launch_or_open`. Internal `--server` mode requires host and port, reads a non-empty token from `BETTER_MONEY_SESSION_TOKEN`, and calls `run_server`. Reject hosts other than `127.0.0.1`; reject a missing token before binding a socket.

- [ ] **Step 7: Run launcher and source smoke tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher.py tests\test_server_control.py -v
.\.venv\Scripts\python.exe windows_entry.py
```

Expected: launcher tests pass; source smoke opens one browser page, a second run opens the same port, and only one server process exists. Exit through settings.

- [ ] **Step 8: Commit launcher behavior**

```powershell
git add app\launcher.py windows_entry.py tests\test_launcher.py
git commit -m "feat: add reliable single-instance Windows launcher"
```

### Task 3: PyInstaller onedir bundle

**Files:**
- Create: `build\better-money.spec`
- Create: `build\version_info.txt`
- Create: `build\build_windows.ps1`
- Create: `tests/test_bundle_smoke.py`
- Modify: `.gitignore:1-6`
- Modify: `requirements-dev.txt`

**Interfaces:**
- Consumes: `windows_entry.py`, `resource_root()`, static files, app modules, and `icon.ico`.
- Produces: `dist\BetterMoney\BetterMoney.exe` and its dependency directory.

- [ ] **Step 1: Add build dependency and ignored output paths**

Append to `requirements-dev.txt`:

```text
pyinstaller>=6.0,<7
```

Add to `.gitignore`:

```text
build-output/
dist/
*.spec.tmp
```

Do not ignore the committed `build/better-money.spec`.

- [ ] **Step 2: Write a failing bundle smoke test**

Create a test that accepts `BETTER_MONEY_BUNDLE_EXE`, generates a token, places it in the child-only `BETTER_MONEY_SESSION_TOKEN` environment, starts the executable with `--server` on an assigned free port and temporary `BETTER_MONEY_HOME`, polls health, asserts product identity, calls shutdown with the token, and asserts exit code 0. Skip only when the bundle path environment variable is absent so normal source suites remain fast.

- [ ] **Step 3: Create the PyInstaller spec**

The spec must:

- Use `windows_entry.py` as entry script.
- Set `name='BetterMoney'`.
- Use `console=False`.
- Include `static` as a data directory.
- Include `icon.ico` and Windows version metadata.
- Collect required Uvicorn/OpenAI hidden imports based on PyInstaller analysis warnings.
- Produce onedir output, not onefile.
- Exclude `tests`, repository `data`, `.venv`, docs, and developer scripts.

Use `Path(SPECPATH).parent.parent` to resolve repository assets; do not depend on the caller's working directory.

- [ ] **Step 4: Create a deterministic PowerShell build script**

`build/build_windows.ps1` must stop on errors, verify 64-bit Windows, install locked dev requirements only when `-InstallDependencies` is passed, remove only the resolved `build-output/pyinstaller` and `dist/BetterMoney` targets after confirming they are under the repository, run PyInstaller, and print the final executable path.

Core command:

```powershell
& $pythonExe -m PyInstaller --noconfirm --clean --distpath $distPath --workpath $workPath $specPath
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
```

- [ ] **Step 5: Build and run the bundle smoke test**

```powershell
.\build\build_windows.ps1
$env:BETTER_MONEY_BUNDLE_EXE = (Resolve-Path '.\dist\BetterMoney\BetterMoney.exe')
.\.venv\Scripts\python.exe -m pytest tests\test_bundle_smoke.py -v
Remove-Item Env:BETTER_MONEY_BUNDLE_EXE
```

Expected: packaged health and shutdown pass without using system Python inside the child process.

- [ ] **Step 6: Inspect bundle contents for forbidden user data**

```powershell
Get-ChildItem '.\dist\BetterMoney' -Recurse -File | Where-Object { $_.FullName -match '\\data\\|config\.json|better_money\.db' }
```

Expected: no output.

- [ ] **Step 7: Commit reproducible bundle configuration**

```powershell
git add requirements-dev.txt .gitignore build\better-money.spec build\version_info.txt build\build_windows.ps1 tests\test_bundle_smoke.py
git commit -m "build: add Windows application bundle"
```

### Task 4: Inno Setup installer, custom path, upgrade, and uninstall

**Files:**
- Create: `installer\BetterMoney.iss`
- Create: `build\build_installer.ps1`
- Create: `installer\README.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `dist\BetterMoney` from Task 3 and `APP_VERSION` from Task 1.
- Produces: `release\BetterMoney-Setup-1.0.0.exe`.

- [ ] **Step 1: Create installer metadata and file rules**

Set a stable `AppId`, `AppName=Better Money`, `AppVersion=1.0.0`, default directory `{autopf}\Better Money`, `DisableProgramGroupPage=yes`, 64-bit architecture mode, LZMA2 compression, and `PrivilegesRequired=admin`. The installer requests elevation once because the confirmed default is Program Files; the application itself subsequently runs as the normal interactive user and writes only to that user's LocalAppData.

Add `release/*.exe` and `release/SHA256SUMS.txt` to `.gitignore`; release notes remain trackable Markdown files.

Install every file from `dist\BetterMoney` recursively. Create desktop and Start Menu shortcuts targeting `{app}\BetterMoney.exe`; the desktop shortcut is selected by default and can be disabled in installer tasks.

- [ ] **Step 2: Add running-instance shutdown before install and uninstall**

Before replacing files, execute `{app}\BetterMoney.exe --request-shutdown` when an old executable exists, wait for it to exit, and show a retry/cancel prompt when files remain locked. Add `--request-shutdown` handling to the launcher that reads and verifies the current runtime record, sends the token-protected shutdown request, and waits up to ten seconds.

- [ ] **Step 3: Preserve personal data by default**

Do not include `%LOCALAPPDATA%\BetterMoney` in `[UninstallDelete]`. On the uninstall progress page add an unchecked custom checkbox labeled `同时删除我的账单、设置、图片和备份`. Only when checked, resolve `%LOCALAPPDATA%\BetterMoney`, verify its final directory name is exactly `BetterMoney` and its parent is exactly the current user's LocalAppData, then delete that explicit directory after a second confirmation.

- [ ] **Step 4: Create the installer build script**

`build/build_installer.ps1` verifies bundle existence and locates `ISCC.exe` from an explicit `-IsccPath` or standard Inno Setup installation locations. It reads `APP_VERSION` from `app/version.py`, passes it as `/DAppVersion=<value>`, writes to repository `release`, and fails when the output filename/version differs.

- [ ] **Step 5: Build and inspect the installer**

```powershell
.\build\build_installer.ps1
Get-FileHash '.\release\BetterMoney-Setup-1.0.0.exe' -Algorithm SHA256
```

Expected: one installer and one printed SHA-256 digest. If Inno Setup is not installed, record that external prerequisite as the blocking item; do not replace the installer with a ZIP.

- [ ] **Step 6: Commit installer configuration**

```powershell
git add .gitignore installer\BetterMoney.iss installer\README.md build\build_installer.ps1
git commit -m "build: add Windows installer"
```

### Task 5: Installer and clean-machine acceptance

**Files:**
- Create: `tests\windows-install-checklist.md`
- Create: `tests\smoke_installed.ps1`
- Modify: `使用说明.md:42-92, 345-390`

**Interfaces:**
- Consumes: setup executable from Task 4.
- Produces: repeatable Gate C evidence for Windows 10 and Windows 11 64-bit.

- [ ] **Step 1: Write the installed smoke script**

The script accepts `-ExecutablePath` and `-ApplicationHome`, launches the installed app, waits for `runtime/instance.json`, validates health identity, launches it a second time, verifies the server PID is unchanged, requests shutdown, and exits nonzero on any mismatch.

It must not delete application home. Test cleanup is a separate explicit command against the VM's dedicated test user profile.

- [ ] **Step 2: Create an exact manual checklist**

Include checkboxes for:

- Windows 10 64-bit with no Python.
- Windows 11 64-bit with no Python.
- Default installation path.
- D-drive path containing Chinese characters and spaces.
- First click opens only after health success.
- Rapid five-click launch produces one PID.
- Port 8642 occupied by another HTTP service.
- Network disconnected and API Key blank.
- Data migration from an old source folder.
- Upgrade from an earlier installer while data exists.
- Uninstall default retains application home.
- Reinstall discovers retained data.
- Uninstall checked deletion removes only `%LOCALAPPDATA%\BetterMoney`.
- Windows display scaling at 100%, 125%, and 150%.
- SmartScreen/antivirus behavior and exact warning text if shown.

- [ ] **Step 3: Test Windows 11 clean VM**

Install, execute the smoke script, complete every applicable checklist item, export a backup, uninstall retaining data, reinstall, restore the backup, and record installer filename, SHA-256, Windows build number, and result.

- [ ] **Step 4: Test Windows 10 clean VM**

Repeat the same procedure on Windows 10 64-bit. A Windows 11 success cannot substitute for this result.

- [ ] **Step 5: Update user installation and troubleshooting documentation**

Replace `.bat` as the normal Windows path with GitHub Release installer steps. Document custom installation directory, `%LOCALAPPDATA%\BetterMoney`, offline/manual functionality, explicit exit, upgrade, default data retention, optional personal-data deletion, and likely unsigned SmartScreen steps.

- [ ] **Step 6: Run source and packaged regressions after acceptance fixes**

```powershell
.\.venv\Scripts\python.exe -m pytest -v
$env:BETTER_MONEY_BUNDLE_EXE = (Resolve-Path '.\dist\BetterMoney\BetterMoney.exe')
.\.venv\Scripts\python.exe -m pytest tests\test_bundle_smoke.py -v
Remove-Item Env:BETTER_MONEY_BUNDLE_EXE
```

Expected: all tests pass.

- [ ] **Step 7: Commit checklist, smoke script, and docs**

```powershell
git add tests\windows-install-checklist.md tests\smoke_installed.ps1 使用说明.md README.md
git commit -m "docs: add Windows installation verification"
```

### Task 6: Release-candidate build and Gate C

**Files:**
- Create: `release\SHA256SUMS.txt` during the build; do not commit release binaries.
- Create: `release\RELEASE_NOTES-1.0.0.md` during the build; commit release notes only after review.
- Modify: `README.md:20-42`

**Interfaces:**
- Consumes: passing installer and clean-machine evidence.
- Produces: local release candidate ready for user-authorized GitHub publication.

- [ ] **Step 1: Build from a clean Git worktree state**

Verify `git status --short` is empty, run the bundle and installer scripts, and fail if either creates source-tree modifications outside ignored build output.

- [ ] **Step 2: Calculate and write the checksum**

Write exactly one line to `release/SHA256SUMS.txt` using the lowercase SHA-256 digest, two spaces, and filename:

```text
<64-hex-digest>  BetterMoney-Setup-1.0.0.exe
```

Verify it by recalculating with `Get-FileHash`.

- [ ] **Step 3: Write release notes from verified changes**

Include installation instructions, fixed first-click startup, multiple goals, goal deletion, ranged/deletable summaries, dated initial balance, backup/restore, migration, known unsigned SmartScreen behavior, and the exact app-data location. Do not claim automatic update, tray support, cloud sync, macOS packaging, or code signing.

- [ ] **Step 4: Run final verification**

Run the complete pytest suite, packaged smoke test, both VM checklists, checksum verification, and `git diff --check`. Confirm no API Key appears in logs or exported backup fixtures.

- [ ] **Step 5: Commit reviewed text-only release notes**

```powershell
git add README.md release\RELEASE_NOTES-1.0.0.md
git commit -m "docs: prepare Better Money 1.0.0 release notes"
```

Do not add the installer binary or checksum file to Git unless repository policy is changed explicitly.

- [ ] **Step 6: Record Gate C and request publication authorization**

Report installer absolute path, byte size, SHA-256, Windows 10 result, Windows 11 result, full test count, and remaining SmartScreen warning. Ask the user before creating or uploading any GitHub Release.
