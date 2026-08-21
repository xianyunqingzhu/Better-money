"""Bundle smoke: launch the packaged executable, verify health, shut it down.

Skipped unless BETTER_MONEY_BUNDLE_EXE points at the built BetterMoney.exe.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.version import APP_ID, APP_VERSION, HEALTH_PROTOCOL

BUNDLE_EXE = os.environ.get("BETTER_MONEY_BUNDLE_EXE", "").strip()

pytestmark = pytest.mark.skipif(
    not BUNDLE_EXE,
    reason="set BETTER_MONEY_BUNDLE_EXE to run the packaged smoke test",
)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_bundle_serves_verified_health_and_shuts_down(tmp_path):
    import secrets

    port = _free_port()
    token = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    environment["BETTER_MONEY_HOME"] = str(tmp_path)
    environment["BETTER_MONEY_SESSION_TOKEN"] = token
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [BUNDLE_EXE, "--server", "--host", "127.0.0.1", "--port", str(port)],
        env=environment,
        cwd=Path(BUNDLE_EXE).parent,
        creationflags=creationflags,
    )
    try:
        deadline = time.monotonic() + 60
        health = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("bundled server exited during startup")
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{port}/api/health", timeout=1)
                if response.status_code == 200:
                    health = response.json()
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert health is not None, "bundled server did not become healthy"
        assert health["app_id"] == APP_ID
        assert health["version"] == APP_VERSION
        assert health["protocol"] == HEALTH_PROTOCOL

        shutdown = httpx.post(
            f"http://127.0.0.1:{port}/api/control/shutdown",
            headers={"X-Better-Money-Token": token},
            timeout=5,
        )
        assert shutdown.status_code == 200
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
