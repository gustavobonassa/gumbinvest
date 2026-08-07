"""One research call per AI provider, each with its native web search.

The chat streams text through one OpenAI-compatible dialect, which is why only
Anthropic gets web search there. The AI wallet never streams model text to the
user — it needs one JSON answer — so every provider can be called through the
API where its search actually lives:

* Anthropic — native SDK, ``web_search`` server tool (as the chat does).
* OpenAI — the Responses API (``/responses``) with the ``web_search`` tool;
  chat-completions has no generic search.
* Gemini — the native ``generateContent`` endpoint with Google-Search
  grounding; Google's OpenAI-compat endpoint (the one the chat uses) does not
  expose grounding. Same API key.
* Grok — chat-completions plus ``search_parameters`` (Live Search).
* Groq — ``browser_search`` tool for the models that support it (gpt-oss);
  ``compound`` models search on their own; plain open models cannot search.

If a provider rejects the search payload (they change these APIs faster than
this file), the call is retried once without search and ``used_search`` comes
back False — degraded, visible, never broken. All failures surface as
:class:`AiResearchError` with a user-readable pt-BR message.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

TIMEOUT = httpx.Timeout(300.0, connect=15.0)


class AiResearchError(Exception):
    """Provider call failed; ``str(exc)`` is safe to show the user (pt-BR)."""


class _SearchRejected(Exception):
    """The provider 400'd a search-enabled payload; retry without."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    used_search: bool


def supports_search(provider_id: str, model: str) -> bool:
    """Whether this provider/model pair has server-side web search at all."""
    if provider_id == "groq":
        lowered = (model or "").lower()
        return "gpt-oss" in lowered or "compound" in lowered
    return True


def call_model(
    provider_id: str,
    entry: dict,
    model: str,
    api_key: str,
    system: str,
    messages: list[dict],
    want_search: bool = True,
) -> ModelReply:
    """One complete (non-streaming) model turn; the reply text is raw.

    ``messages`` are ``{"role": "user"|"assistant", "content": str}`` dicts.
    """
    search = want_search and supports_search(provider_id, model)
    try:
        return _dispatch(provider_id, entry, model, api_key, system, messages, search)
    except _SearchRejected:
        logger.warning("ai research: %s rejected search payload for %s; retrying without", provider_id, model)
        return _dispatch(provider_id, entry, model, api_key, system, messages, False)


def _dispatch(
    provider_id: str,
    entry: dict,
    model: str,
    api_key: str,
    system: str,
    messages: list[dict],
    search: bool,
) -> ModelReply:
    if entry.get("kind") == "anthropic":
        return _call_anthropic(entry, model, api_key, system, messages, search)
    if provider_id == "openai":
        return _call_openai_responses(entry, model, api_key, system, messages, search)
    if provider_id == "gemini":
        return _call_gemini(entry, model, api_key, system, messages, search)
    return _call_chat_completions(provider_id, entry, model, api_key, system, messages, search)


# ---------------------------------------------------------------------------
# Providers


def _call_anthropic(
    entry: dict, model: str, api_key: str, system: str, messages: list[dict], search: bool
) -> ModelReply:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=300.0)
    kwargs: dict = {"model": model, "max_tokens": 8_000, "system": system}
    if search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
    convo = list(messages)
    try:
        response = None
        # Server-side web search can pause the turn; re-send to resume.
        for _ in range(4):
            response = client.messages.create(messages=convo, **kwargs)
            if response.stop_reason != "pause_turn":
                break
            convo = convo + [{"role": "assistant", "content": response.content}]
        if response is None:
            raise AiResearchError("Sem resposta da API da Anthropic.")
        if response.stop_reason == "refusal":
            raise AiResearchError(
                "A solicitação foi recusada pelos filtros de segurança do modelo."
            )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return ModelReply(text.strip(), search)
    except anthropic.AuthenticationError as exc:
        raise AiResearchError(
            "Chave da Anthropic inválida. Confira em Configurações → Sistema."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise AiResearchError(
            "Limite de requisições da API atingido. Tente novamente em instantes."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise AiResearchError("Sem conexão com a API da Anthropic.") from exc
    except anthropic.BadRequestError as exc:
        if search:
            raise _SearchRejected() from exc
        raise AiResearchError(f"Erro da API da Anthropic: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AiResearchError(f"Erro da API da Anthropic ({exc.status_code}).") from exc


def _post_json(url: str, headers: dict, payload: dict) -> httpx.Response:
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            return http.post(url, headers=headers, json=payload)
    except httpx.ConnectError as exc:
        raise AiResearchError("Sem conexão com a API do provedor de IA.") from exc
    except httpx.TimeoutException as exc:
        raise AiResearchError(
            "A chamada ao modelo excedeu o tempo limite. Tente novamente."
        ) from exc


def _error_message(label: str, model: str, response: httpx.Response) -> str:
    """The provider's own message on a readable pt-BR prefix (ai.py pattern)."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, list) and body:  # Google wraps errors in a list
            body = body[0]
        detail = (body.get("error") or {}).get("message") or body.get("message", "")
    except Exception:  # noqa: BLE001 — body may not be JSON
        pass
    prefix = {
        401: f"Chave inválida para {label}",
        403: f"Acesso negado pela {label}; verifique créditos/permissões da conta",
        404: f"Modelo '{model}' não existe em {label}",
        429: "Limite de requisições da API atingido",
    }.get(response.status_code, f"Erro da API da {label} ({response.status_code})")
    return f"{prefix}. {detail}".strip()


def _raise_or_retry(label: str, model: str, response: httpx.Response, search: bool) -> None:
    if response.status_code < 400:
        return
    if response.status_code == 400 and search:
        raise _SearchRejected()
    raise AiResearchError(_error_message(label, model, response))


def _call_openai_responses(
    entry: dict, model: str, api_key: str, system: str, messages: list[dict], search: bool
) -> ModelReply:
    payload: dict = {
        "model": model,
        "instructions": system,
        "input": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    if search:
        payload["tools"] = [{"type": "web_search"}]
    response = _post_json(
        f"{entry['base_url']}/responses",
        {"Authorization": f"Bearer {api_key}"},
        payload,
    )
    _raise_or_retry(entry["label"], model, response, search)
    body = response.json()
    text = body.get("output_text")
    if not isinstance(text, str) or not text:
        parts: list[str] = []
        for item in body.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") in ("output_text", "text"):
                    parts.append(content.get("text") or "")
        text = "".join(parts)
    return ModelReply((text or "").strip(), search)


def _call_gemini(
    entry: dict, model: str, api_key: str, system: str, messages: list[dict], search: bool
) -> ModelReply:
    payload: dict = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [
            {
                "role": "user" if m["role"] == "user" else "model",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
        ],
    }
    if search:
        payload["tools"] = [{"google_search": {}}]
    response = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": api_key},
        payload,
    )
    _raise_or_retry(entry["label"], model, response, search)
    body = response.json()
    candidates = body.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    text = "".join(part.get("text") or "" for part in parts)
    return ModelReply(text.strip(), search)


def _call_chat_completions(
    provider_id: str,
    entry: dict,
    model: str,
    api_key: str,
    system: str,
    messages: list[dict],
    search: bool,
) -> ModelReply:
    payload: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    if search:
        if provider_id == "grok":
            payload["search_parameters"] = {"mode": "auto"}
        elif provider_id == "groq" and "gpt-oss" in (model or "").lower():
            payload["tools"] = [{"type": "browser_search"}]
        # groq compound models search on their own — nothing to add.
    response = _post_json(
        f"{entry['base_url']}/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        payload,
    )
    _raise_or_retry(entry["label"], model, response, search)
    body = response.json()
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AiResearchError(f"Resposta inesperada da API da {entry['label']}.") from exc
    return ModelReply(text.strip(), search)


RETRY_JSON = (
    "Sua resposta anterior não era JSON válido. Responda novamente SOMENTE com o JSON no "
    "formato especificado, sem nenhum texto adicional."
)


def call_model_json(
    provider_id: str,
    entry: dict,
    model: str,
    api_key: str,
    system: str,
    messages: list[dict],
    want_search: bool = True,
) -> tuple[dict | None, bool]:
    """One model turn expected to yield JSON, with a single corrective retry.

    Returns ``(data, used_search)``; ``data`` is None when even the retry did
    not produce a parseable object.
    """
    convo = list(messages)
    used_search = False
    for _attempt in range(2):
        reply = call_model(provider_id, entry, model, api_key, system, convo, want_search)
        used_search = used_search or reply.used_search
        data = extract_json(reply.text)
        if data is not None:
            return data, used_search
        convo = convo + [
            {"role": "assistant", "content": reply.text or "(vazio)"},
            {"role": "user", "content": RETRY_JSON},
        ]
        want_search = False  # the correction turn only reformats
    return None, used_search


# ---------------------------------------------------------------------------
# Output parsing

_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def decode_escapes(text: str) -> str:
    """Fix literal ``\\uXXXX`` sequences that leak from web-search quotes."""
    return _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)


def extract_json(text: str | None) -> dict | None:
    """The first balanced JSON object in ``text``, or None.

    Tolerates markdown fences, prose before/after, and trailing junk: it scans
    from the first ``{`` to its matching ``}`` (string-aware) and parses that.
    """
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None
