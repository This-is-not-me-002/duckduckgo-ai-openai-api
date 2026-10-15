import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep the test suite isolated from external credentials and local databases.
os.environ["IGNPRE_API_KEYS"] = "True"
os.environ.setdefault("DEFAULT_MODEL", "gpt-4o-mini")

from fastapi.testclient import TestClient
import main


class FakeDDGS:
    def chat(self, prompt, model):
        assert "User: hello" in prompt
        assert "User: earlier question" in prompt
        return "hello from fake upstream"

    def chat_yield(self, prompt, model):
        yield "hello "
        yield "world"


def test_health_and_openai_model_list():
    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "ok"
        response = client.get("/v1/models")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["data"]}
        assert "gpt-4o-mini" in ids
        assert "mistral-small-3" in ids
        assert "mixtral-8x7b" not in ids


def test_chat_preserves_conversation_history(monkeypatch):
    monkeypatch.setattr(main, "DDGS", FakeDDGS)
    with TestClient(main.app) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4o-mini",
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
    class ChallengeDDGS:
        def chat(self, prompt, model):
            raise RuntimeError("CAPTCHA challenge: verify you are human")

    monkeypatch.setattr(main, "DDGS", ChallengeDDGS)
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
