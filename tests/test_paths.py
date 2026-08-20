from pathlib import Path

from fastapi.testclient import TestClient

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


def test_lifespan_releases_database_for_temporary_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_MONEY_HOME", str(tmp_path))
    reset_paths_cache()
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    get_paths().db_path.unlink()
