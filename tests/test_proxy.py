import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep the test suite isolated from external credentials and local databases.
os.environ["IGNPRE_API_KEYS"] = "True"
os.environ.setdefault("DEFAULT_MODEL", "gpt-5.6-luna")

import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture(autouse=True)
def stub_live_catalog(monkeypatch):
    monkeypatch.setattr(main, "fetch_model_catalog", lambda timeout=10.0: (_ for _ in ()).throw(RuntimeError("offline in test")))


def fake_chat(prompt, model, timeout=90.0):
    assert "User: hello" in prompt
    assert "User: earlier question" in prompt
    assert model == "gpt-5.6-luna"
    return "hello from fake upstream"


def fake_stream(prompt, model, timeout=90.0):
    yield "hello "
    yield "world"


def test_root_is_admin_control_plane():
    with TestClient(main.app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Admin sign in" in response.text
        assert "API keys" in response.text
        assert "Request logs" in response.text
        assert "Test workflow" in response.text


def test_admin_model_registry_uses_current_transport():
    with TestClient(main.app) as client:
        response = client.get("/admin/models")
        assert response.status_code == 200
        body = response.json()
        assert "supported_upstream_models" in body
        assert "browser transport" in body["notice"]


def test_health_and_openai_model_list():
    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "ok"
        response = client.get("/v1/models")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["data"]}
        assert "gpt-4o-mini" in ids
        assert "mistral-small-2603" in ids
        assert "claude-haiku-4-5" in ids
        assert "tinfoil/gpt-oss-120b" in ids
        assert "mixtral-8x7b" not in ids


def test_chat_preserves_conversation_history(monkeypatch):
    monkeypatch.setattr(main, "duckai_chat", fake_chat)
    with TestClient(main.app) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "hello"},
            ],
        })
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "hello from fake upstream"
        assert body["choices"][0]["finish_reason"] == "stop"


def test_unknown_model_returns_openai_error():
    with TestClient(main.app) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "definitely-not-a-model",
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "model_not_found"


def test_captcha_challenge_is_reported_without_bypass(monkeypatch):
    monkeypatch.setattr(main, "duckai_chat", lambda prompt, model, timeout=90.0: (_ for _ in ()).throw(RuntimeError("CAPTCHA challenge: verify you are human")))
    with TestClient(main.app) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "captcha_or_challenge"


def test_stream_uses_server_sent_events(monkeypatch):
    monkeypatch.setattr(main, "DDGS", FakeDDGS)
    with TestClient(main.app) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4o-mini",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"object": "chat.completion.chunk"' in response.text
        assert '"finish_reason": "stop"' in response.text
        assert "data: [DONE]" in response.text
