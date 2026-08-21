"""AI provider presets and connection test; no test touches the internet."""
import httpx
import pytest


def _fake_response():
    request = httpx.Request("POST", "http://localhost/v1/chat/completions")
    return httpx.Response(200, request=request)


@pytest.fixture
def fake_openai(monkeypatch):
    state = {"clients": [], "outcome": None}

    class FakeCompletions:
        def __init__(self, outcome):
            self.calls = []
            self._outcome = outcome

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome

    class FakeChat:
        def __init__(self, outcome):
            self.completions = FakeCompletions(outcome)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = FakeChat(state["outcome"])
            state["clients"].append(self)

    monkeypatch.setattr("app.ai.OpenAI", FakeClient)
    return state


@pytest.fixture
def ai_payload():
    return {
        "api_base": "https://api.deepseek.com",
        "api_key": "sk-test-not-real",
        "model": "deepseek-chat",
    }


def test_test_ai_success(client, fake_openai, ai_payload):
    fake_openai["outcome"] = object()
    response = client.post("/api/settings/test-ai", json=ai_payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    fake = fake_openai["clients"][0]
    assert fake.kwargs["base_url"] == "https://api.deepseek.com"
    assert fake.kwargs["api_key"] == "sk-test-not-real"
    call = fake.chat.completions.calls[0]
    assert call["model"] == "deepseek-chat"
    assert call["messages"][0]["content"] == "回复 OK"


@pytest.mark.parametrize("exc_name", [
    "timeout",
    "invalid_url",
    "auth_failure",
])
def test_test_ai_failures_return_503(client, fake_openai, ai_payload, exc_name):
    from openai import APIConnectionError, APITimeoutError, AuthenticationError

    request = httpx.Request("POST", "http://localhost/v1/chat/completions")
    outcomes = {
        "timeout": APITimeoutError(request=request),
        "invalid_url": APIConnectionError(request=request),
        "auth_failure": AuthenticationError(
            "bad key", response=_fake_response(), body=None),
    }
    fake_openai["outcome"] = outcomes[exc_name]
    response = client.post("/api/settings/test-ai", json=ai_payload)
    assert response.status_code == 503
    assert response.json()["error"] == "ai_connection_failed"


def test_test_ai_missing_key_returns_503(client, fake_openai, ai_payload):
    ai_payload["api_key"] = ""
    response = client.post("/api/settings/test-ai", json=ai_payload)
    assert response.status_code == 503
    assert response.json()["error"] == "ai_connection_failed"
    assert fake_openai["clients"] == []


def test_ai_providers_have_expected_bases():
    from app.ai import AI_PROVIDERS

    assert AI_PROVIDERS["OpenAI"] == "https://api.openai.com/v1"
    assert AI_PROVIDERS["DeepSeek"] == "https://api.deepseek.com"
    assert "Qwen" in AI_PROVIDERS
    assert AI_PROVIDERS["自定义"] == ""


def test_ai_provider_is_saved_through_generic_settings(client):
    response = client.post("/api/settings", json={"ai_provider": "DeepSeek"})
    assert response.status_code == 200
    assert client.get("/api/settings").json()["ai_provider"] == "DeepSeek"
