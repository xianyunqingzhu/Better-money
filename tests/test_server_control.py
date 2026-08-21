"""Server control: product identity health, runtime token, shutdown authorization."""
from unittest.mock import Mock

import pytest

from app.main import app
from app.version import APP_ID, APP_VERSION, HEALTH_PROTOCOL


@pytest.fixture
def controlled_client(client):
    app.state.session_token = "expected-token"
    app.state.request_shutdown = Mock()
    yield client
    app.state.session_token = ""
    app.state.request_shutdown = None


def test_health_identifies_product(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["app_id"] == APP_ID
    assert body["version"] == APP_VERSION
    assert body["protocol"] == HEALTH_PROTOCOL
    assert "ai_configured" in body
    # no secrets or paths leak through health
    for forbidden in ("token", "session", "api_key", "path", "port"):
        assert forbidden not in body


def test_shutdown_rejects_missing_token(controlled_client):
    response = controlled_client.post("/api/control/shutdown")
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_shutdown_rejects_wrong_token(controlled_client):
    response = controlled_client.post(
        "/api/control/shutdown",
        headers={"X-Better-Money-Token": "wrong-token"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_shutdown_schedules_exit_with_exact_token(controlled_client):
    response = controlled_client.post(
        "/api/control/shutdown",
        headers={"X-Better-Money-Token": "expected-token"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    app.state.request_shutdown.assert_called_once()


def test_runtime_reports_control_when_token_exists(controlled_client):
    response = controlled_client.get("/api/runtime")
    assert response.status_code == 200
    assert response.json() == {
        "control_available": True,
        "session_token": "expected-token",
    }


def test_runtime_control_unavailable_without_token(client):
    response = client.get("/api/runtime")
    assert response.status_code == 200
    assert response.json()["control_available"] is False


def test_shutdown_unavailable_without_control_token(client):
    response = client.post("/api/control/shutdown")
    assert response.status_code == 409
    assert response.json()["error"] == "shutdown_unavailable"


def test_version_module_has_stable_identity():
    assert APP_ID == "better-money"
    assert APP_VERSION == "1.0.0"
    assert HEALTH_PROTOCOL == 1
