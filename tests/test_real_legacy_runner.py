from __future__ import annotations

import hashlib
from http.server import HTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import httpx

from app.paths import PROJECT_ROOT
from tests.mock_openai import Handler


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _repository_data_fingerprint() -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for candidate in sorted((PROJECT_ROOT / "data").rglob("*")):
        if candidate.is_file():
            result[candidate.relative_to(PROJECT_ROOT).as_posix()] = (
                candidate.stat().st_size,
                candidate.stat().st_mtime_ns,
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )
    return result


def test_real_six_script_runner_uses_dynamic_isolated_service(app_home):
    before = _repository_data_fingerprint()
    mock_server = HTTPServer(("127.0.0.1", 0), Handler)
    mock_thread = threading.Thread(target=mock_server.serve_forever, daemon=True)
    mock_thread.start()
    app_port = _free_port()
    base_url = f"http://127.0.0.1:{app_port}"
    config = {
        "api_base": f"http://127.0.0.1:{mock_server.server_port}/v1",
        "api_key": "isolated-test-key",
        "model_text": "mock",
        "model_vision": "mock",
        "model_image": "mock",
        "initial_balance": 0.0,
        "monthly_budget": 1500.0,
        "auto_save_ratio": 0.3,
        "tone": "朋友",
        "cooldown_days": 7,
        "image_gen_enabled": True,
    }
    data_dir = app_home / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["BETTER_MONEY_HOME"] = str(app_home)
    environment["BETTER_MONEY_TEST_BASE_URL"] = base_url
    environment["PYTHONUNBUFFERED"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(app_port),
            "--no-access-log",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if server.poll() is not None:
                output = server.stdout.read() if server.stdout else ""
                raise AssertionError(f"isolated app exited during startup:\n{output}")
            try:
                response = httpx.get(base_url + "/api/health", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("isolated app did not become healthy")
            time.sleep(0.05)

        runner_cwd = app_home / "runner-cwd"
        runner_cwd.mkdir()
        try:
            completed = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "tests" / "run_all.py")],
                cwd=runner_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            print("RUNNER TIMED OUT STDOUT:\n", (exc.stdout or "")[-3000:])
            print("RUNNER TIMED OUT STDERR:\n", (exc.stderr or "")[-2000:])
            raise
        print(completed.stdout)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "REGRESSION ALL PASS" in completed.stdout
        assert not (runner_cwd / "data").exists()
        assert not (app_home / "backups").exists()
        assert not (app_home / "data" / "config.json").exists()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        mock_server.shutdown()
        mock_server.server_close()
        mock_thread.join(timeout=5)

    assert _repository_data_fingerprint() == before
