"""Regression tests for upstream rate-limit classification."""
import asyncio

import pytest
from fastapi import HTTPException

import main
from duckai_transport import DuckAIRateLimit


def test_upstream_rate_limit_is_not_retryable():
    assert main._is_retryable(DuckAIRateLimit("Duck.ai ERR_BN_LIMIT")) is False


def test_chat_returns_429_without_repeating_rate_limited_request(monkeypatch):
    calls = 0

    def rate_limited_chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DuckAIRateLimit("Duck.ai ERR_BN_LIMIT (IP/fingerprint banned)")

    monkeypatch.setattr(main, "duckai_chat", rate_limited_chat)
    monkeypatch.setattr(main, "MAX_RETRIES", 2)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main._chat_with_limits("hello", "gpt-5.6-luna"))

    assert caught.value.status_code == 429
    assert caught.value.detail["type"] == "upstream_rate_limit"
    assert caught.value.detail["code"] == "upstream_rate_limited"
    assert calls == 1
