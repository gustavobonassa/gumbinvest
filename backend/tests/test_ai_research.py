"""ai_research: each provider gets its own search API, correctly shaped.

No network — ``_post_json`` is replaced and the built request captured. The
Anthropic path is not shaped here (it goes through the SDK and is exercised
daily by the chat); these tests cover the four HTTP dialects and the
retry-without-search degradation.
"""
from __future__ import annotations

import pytest

from app.services import ai_research as research
from app.services.ai_research import AiResearchError, supports_search

OPENAI = {"label": "OpenAI (GPT)", "kind": "openai", "base_url": "https://api.openai.com/v1"}
GEMINI = {
    "label": "Google (Gemini)",
    "kind": "openai",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
}
GROK = {"label": "xAI (Grok)", "kind": "openai", "base_url": "https://api.x.ai/v1"}
GROQ = {"label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1"}

MESSAGES = [{"role": "user", "content": "monte a carteira"}]


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def capture(monkeypatch, responses):
    calls = []

    def fake_post(url, headers, payload):
        calls.append({"url": url, "headers": headers, "payload": payload})
        return responses.pop(0)

    monkeypatch.setattr(research, "_post_json", fake_post)
    return calls


def chat_body(text="ok"):
    return {"choices": [{"message": {"content": text}}]}


def test_openai_uses_responses_api_with_web_search(monkeypatch):
    body = {
        "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [{"type": "output_text", "text": '{"a": 1}'}]},
        ]
    }
    calls = capture(monkeypatch, [FakeResponse(200, body)])
    reply = research.call_model("openai", OPENAI, "gpt-5", "key", "system", MESSAGES)
    assert calls[0]["url"].endswith("/responses")
    assert calls[0]["payload"]["tools"] == [{"type": "web_search"}]
    assert calls[0]["payload"]["instructions"] == "system"
    assert reply.text == '{"a": 1}' and reply.used_search is True


def test_gemini_uses_native_endpoint_with_grounding(monkeypatch):
    body = {"candidates": [{"content": {"parts": [{"text": "resposta"}]}}]}
    calls = capture(monkeypatch, [FakeResponse(200, body)])
    reply = research.call_model("gemini", GEMINI, "gemini-pro-latest", "key", "sys", MESSAGES)
    call = calls[0]
    assert "generativelanguage.googleapis.com/v1beta/models/gemini-pro-latest" in call["url"]
    assert call["headers"] == {"x-goog-api-key": "key"}
    assert call["payload"]["tools"] == [{"google_search": {}}]
    assert call["payload"]["contents"][0]["role"] == "user"
    assert reply.text == "resposta" and reply.used_search is True


def test_grok_adds_live_search_parameters(monkeypatch):
    calls = capture(monkeypatch, [FakeResponse(200, chat_body())])
    reply = research.call_model("grok", GROK, "grok-4", "key", "sys", MESSAGES)
    assert calls[0]["url"].endswith("/chat/completions")
    assert calls[0]["payload"]["search_parameters"] == {"mode": "auto"}
    assert reply.used_search is True


def test_groq_gpt_oss_gets_browser_search(monkeypatch):
    calls = capture(monkeypatch, [FakeResponse(200, chat_body())])
    reply = research.call_model("groq", GROQ, "openai/gpt-oss-120b", "key", "sys", MESSAGES)
    assert calls[0]["payload"]["tools"] == [{"type": "browser_search"}]
    assert reply.used_search is True


def test_groq_llama_is_honestly_searchless(monkeypatch):
    calls = capture(monkeypatch, [FakeResponse(200, chat_body())])
    reply = research.call_model("groq", GROQ, "llama-3.3-70b-versatile", "key", "sys", MESSAGES)
    payload = calls[0]["payload"]
    assert "tools" not in payload and "search_parameters" not in payload
    assert reply.used_search is False


def test_search_rejection_retries_once_without(monkeypatch):
    error = {"error": {"message": "search_parameters is not supported"}}
    calls = capture(monkeypatch, [FakeResponse(400, error), FakeResponse(200, chat_body("sem busca"))])
    reply = research.call_model("grok", GROK, "grok-4", "key", "sys", MESSAGES)
    assert len(calls) == 2
    assert "search_parameters" not in calls[1]["payload"]
    assert reply.text == "sem busca" and reply.used_search is False


def test_auth_error_maps_to_readable_message(monkeypatch):
    capture(monkeypatch, [FakeResponse(401, {"error": {"message": "bad key"}})])
    with pytest.raises(AiResearchError) as excinfo:
        research.call_model("grok", GROK, "grok-4", "key", "sys", MESSAGES)
    assert "Chave inválida" in str(excinfo.value)
    assert "bad key" in str(excinfo.value)


def test_unknown_model_maps_to_404_message(monkeypatch):
    capture(monkeypatch, [FakeResponse(404, {"error": {"message": "no such model"}})])
    with pytest.raises(AiResearchError) as excinfo:
        research.call_model("groq", GROQ, "llama-3.3-70b-versatile", "key", "sys", MESSAGES)
    assert "não existe" in str(excinfo.value)


def test_supports_search_matrix():
    assert supports_search("anthropic", "claude-sonnet-5")
    assert supports_search("openai", "gpt-5")
    assert supports_search("gemini", "gemini-pro-latest")
    assert supports_search("grok", "grok-4")
    assert supports_search("groq", "openai/gpt-oss-120b")
    assert supports_search("groq", "groq/compound-mini")
    assert not supports_search("groq", "llama-3.3-70b-versatile")
