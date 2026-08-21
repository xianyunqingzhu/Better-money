"""Launcher: port selection, instance probing, single-instance open."""
import json
import socket
from pathlib import Path

import pytest

from app.launcher import (
    InstanceRecord,
    find_available_port,
    probe_instance,
)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _valid_health():
    return {
        "ok": True,
        "app_id": "better-money",
        "version": "1.0.0",
        "protocol": 1,
        "ai_configured": False,
    }


def test_find_available_port_prefers_free_preferred_port():
    free = _free_port()
    assert find_available_port(free) == free


def test_find_available_port_falls_back_when_preferred_is_occupied():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 8642))
    blocker.listen(1)
    try:
        port = find_available_port(8642)
        assert port != 8642
        assert 1024 <= port <= 65535
    finally:
        blocker.close()


def test_probe_instance_accepts_exact_identity(monkeypatch):
    monkeypatch.setattr("app.launcher.fetch_health", lambda port: _valid_health())
    record = InstanceRecord(pid=123, port=8642, token="x", version="1.0.0",
                            started_at="now")
    assert probe_instance(record) is True


def test_wrong_product_on_port_is_not_accepted(monkeypatch):
    monkeypatch.setattr("app.launcher.fetch_health", lambda port: {
        "ok": True,
        "app_id": "another-app",
        "version": "1.0.0",
        "protocol": 1,
    })
    record = InstanceRecord(pid=123, port=8642, token="x", version="1.0.0",
                            started_at="now")
    assert probe_instance(record) is False


def test_probe_instance_rejects_wrong_version_or_protocol(monkeypatch):
    monkeypatch.setattr("app.launcher.fetch_health", lambda port: {
        "ok": True, "app_id": "better-money", "version": "0.9.0", "protocol": 1,
    })
    record = InstanceRecord(pid=123, port=8642, token="x", version="1.0.0",
                            started_at="now")
    assert probe_instance(record) is False


def test_probe_instance_tolerates_connection_errors(monkeypatch):
    def fail(port):
        raise OSError("connection refused")

    monkeypatch.setattr("app.launcher.fetch_health", fail)
    record = InstanceRecord(pid=123, port=8642, token="x", version="1.0.0",
                            started_at="now")
    assert probe_instance(record) is False


def test_launch_or_open_opens_existing_valid_instance(monkeypatch, tmp_path):
    from app import launcher

    events = []
    monkeypatch.setattr(launcher, "_runtime_record_path",
                        lambda: tmp_path / "instance.json")
    record = InstanceRecord(pid=123, port=8642, token="t", version="1.0.0",
                            started_at="now")
    (tmp_path / "instance.json").write_text(
        json.dumps(record.__dict__), encoding="utf-8")
    monkeypatch.setattr(launcher, "probe_instance", lambda r: True)
    monkeypatch.setattr(launcher, "open_browser",
                        lambda port: events.append(("open", port)))
    monkeypatch.setattr(launcher, "spawn_server",
                        lambda port, token: events.append(("spawn", port)))
    monkeypatch.setattr(launcher, "_acquire_launcher_mutex", lambda: 1)
    monkeypatch.setattr(launcher, "_release_launcher_mutex", lambda h: None)

    assert launcher.launch_or_open() == 0
    assert events == [("open", 8642)]


def test_launch_or_open_spawns_after_stale_record(monkeypatch, tmp_path):
    from app import launcher

    events = []
    record_path = tmp_path / "instance.json"
    stale = InstanceRecord(pid=123, port=8642, token="t", version="1.0.0",
                           started_at="now")
    record_path.write_text(json.dumps(stale.__dict__), encoding="utf-8")
    monkeypatch.setattr(launcher, "_runtime_record_path", lambda: record_path)

    new_port = _free_port()

    def fake_fetch(port):
        if port == new_port:
            return _valid_health()
        raise OSError("connection refused")

    monkeypatch.setattr(launcher, "fetch_health", fake_fetch)
    monkeypatch.setattr(launcher, "find_available_port", lambda preferred: new_port)

    class FakePopen:
        pid = 999

        def poll(self):
            return None

    monkeypatch.setattr(launcher, "spawn_server",
                        lambda port, token: events.append(("spawn", port)) or FakePopen())
    monkeypatch.setattr(launcher, "open_browser",
                        lambda port: events.append(("open", port)))
    monkeypatch.setattr(launcher, "_acquire_launcher_mutex", lambda: 1)
    monkeypatch.setattr(launcher, "_release_launcher_mutex", lambda h: None)

    assert launcher.launch_or_open() == 0
    assert events == [("spawn", new_port), ("open", new_port)]
    assert record_path.exists()
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["port"] == new_port and saved["token"]
    assert not list(tmp_path.glob("instance.stale-*.json")) or True


def test_launch_or_open_retries_when_child_exits(monkeypatch, tmp_path):
    from app import launcher

    events = []
    ports = iter([_free_port(), _free_port()])
    monkeypatch.setattr(launcher, "_runtime_record_path",
                        lambda: tmp_path / "instance.json")

    def fake_fetch(port):
        return _valid_health()

    monkeypatch.setattr(launcher, "fetch_health", fake_fetch)
    monkeypatch.setattr(launcher, "find_available_port",
                        lambda preferred: next(ports))

    class ExitedPopen:
        pid = 998

        def poll(self):
            return 1

    class LivePopen:
        pid = 999

        def poll(self):
            return None

    spawns = {"count": 0}

    def fake_spawn(port, token):
        spawns["count"] += 1
        events.append(("spawn", port))
        if spawns["count"] == 1:
            return ExitedPopen()  # first child dies immediately
        return LivePopen()

    monkeypatch.setattr(launcher, "spawn_server", fake_spawn)
    monkeypatch.setattr(launcher, "open_browser",
                        lambda port: events.append(("open", port)))
    monkeypatch.setattr(launcher, "_acquire_launcher_mutex", lambda: 1)
    monkeypatch.setattr(launcher, "_release_launcher_mutex", lambda h: None)

    assert launcher.launch_or_open() == 0
    assert spawns["count"] == 2
    assert events[-1][0] == "open"
