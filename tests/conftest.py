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
