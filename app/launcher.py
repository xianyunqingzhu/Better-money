"""Single-instance launcher: runtime record, health probe, detached server spawn.

The launcher owns instance discovery, dynamic local-port selection, detached
server startup, health verification, and browser opening. It is used by
``windows_entry.py`` (default mode) and by the installed shortcut.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.paths import PROJECT_ROOT, get_paths
from app.version import APP_ID, APP_VERSION, HEALTH_PROTOCOL

DEFAULT_PORT = 8642
HEALTH_POLL_SECONDS = 0.2
STARTUP_TIMEOUT_SECONDS = 30
MAX_SPAWN_ATTEMPTS = 3
MUTEX_WAIT_SECONDS = 5


@dataclass(frozen=True)
class InstanceRecord:
    pid: int
    port: int
    token: str
    version: str
    started_at: str


def find_available_port(preferred: int = DEFAULT_PORT) -> int:
    """Return the preferred port when free, otherwise an OS-assigned port."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
        return preferred


def fetch_health(port: int) -> dict:
    """One-second health probe returning the parsed JSON body."""
    response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
    response.raise_for_status()
    return response.json()


def probe_instance(record: InstanceRecord) -> bool:
    """True only when the recorded port serves this exact product version."""
    try:
        health = fetch_health(record.port)
    except Exception:
        return False
    return (
        health.get("ok") is True
        and health.get("app_id") == APP_ID
        and health.get("version") == APP_VERSION
        and health.get("protocol") == HEALTH_PROTOCOL
    )


def open_browser(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}")


def spawn_server(port: int, token: str) -> subprocess.Popen:
    """Start a detached server child; token travels only through the environment."""
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        command = [sys.executable, str(PROJECT_ROOT / "windows_entry.py")]
    command += ["--server", "--host", "127.0.0.1", "--port", str(port)]
    environment = os.environ.copy()
    environment["BETTER_MONEY_SESSION_TOKEN"] = token
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        command,
        env=environment,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _runtime_record_path() -> Path:
    return get_paths().runtime_dir / "instance.json"


def _read_runtime_record() -> InstanceRecord | None:
    path = _runtime_record_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return InstanceRecord(
            pid=int(data["pid"]),
            port=int(data["port"]),
            token=str(data["token"]),
            version=str(data["version"]),
            started_at=str(data["started_at"]),
        )
    except Exception:
        return None


def _write_runtime_record(record: InstanceRecord) -> None:
    path = _runtime_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(asdict(record), ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


def _retire_stale_record() -> None:
    path = _runtime_record_path()
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        path.rename(path.with_name(f"instance.stale-{stamp}.json"))
    except OSError:
        pass


def _mutex_name() -> str:
    root_hash = hashlib.sha256(
        str(get_paths().root).casefold().encode("utf-8")
    ).hexdigest()[:16]
    return f"Local\\BetterMoneyLauncher-{root_hash}"


def _acquire_launcher_mutex(timeout: float = MUTEX_WAIT_SECONDS):
    """Per-user single-instance mutex; returns a handle or None after timeout."""
    if os.name != "nt":
        return object()
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, _mutex_name())
    if not handle:
        return None
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        if result != 0:  # WAIT_OBJECT_0
            kernel32.CloseHandle(handle)
            return None
    return handle


def _release_launcher_mutex(handle) -> None:
    if handle is None or os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


def launch_or_open() -> int:
    """Open the running instance or start one; 0 on success, 1 on failure."""
    paths = get_paths()
    paths.ensure_directories()

    record = _read_runtime_record()
    if record is not None and probe_instance(record):
        open_browser(record.port)
        return 0

    mutex = _acquire_launcher_mutex()
    try:
        # Another launcher may have won the race; re-check before starting.
        record = _read_runtime_record()
        if record is not None and probe_instance(record):
            open_browser(record.port)
            return 0

        _retire_stale_record()

        for attempt in range(1, MAX_SPAWN_ATTEMPTS + 1):
            port = find_available_port(DEFAULT_PORT)
            token = _secrets.token_urlsafe(32)
            child = spawn_server(port, token)
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            healthy = False
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                try:
                    health = fetch_health(port)
                except Exception:
                    time.sleep(HEALTH_POLL_SECONDS)
                    continue
                if (
                    health.get("app_id") == APP_ID
                    and health.get("version") == APP_VERSION
                    and health.get("protocol") == HEALTH_PROTOCOL
                ):
                    healthy = True
                    break
                time.sleep(HEALTH_POLL_SECONDS)
            if healthy:
                _write_runtime_record(InstanceRecord(
                    pid=child.pid,
                    port=port,
                    token=token,
                    version=APP_VERSION,
                    started_at=datetime.now().isoformat(timespec="seconds"),
                ))
                open_browser(port)
                return 0
            # Child exited or never became healthy: try another port.
            if child.poll() is None:
                try:
                    child.terminate()
                except OSError:
                    pass
            if attempt < MAX_SPAWN_ATTEMPTS:
                continue

        _show_startup_error()
        return 1
    finally:
        _release_launcher_mutex(mutex)


def request_shutdown() -> int:
    """Stop the verified running instance through its protected endpoint."""
    record = _read_runtime_record()
    if record is None or not probe_instance(record):
        print("没有正在运行的 Better-money 实例", file=sys.stderr)
        return 0
    try:
        response = httpx.post(
            f"http://127.0.0.1:{record.port}/api/control/shutdown",
            headers={"X-Better-Money-Token": record.token},
            timeout=2.0,
        )
    except Exception as exc:
        print(f"停止请求失败：{exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"停止失败：HTTP {response.status_code}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            fetch_health(record.port)
        except Exception:
            print("服务已停止")
            return 0
        time.sleep(0.2)
    print("服务仍在运行", file=sys.stderr)
    return 1


def _show_startup_error() -> None:
    message = (
        "Better-money 启动失败。\n\n"
        "可能原因：端口被占用、依赖未安装或上次实例未完全退出。\n"
        "请查看日志文件夹中的 startup.log，或重新运行启动脚本。"
    )
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "Better-money", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)
