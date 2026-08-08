"""The AI chat's provider registry: who the user talks to, with which key.

Every provider except Anthropic speaks the OpenAI chat-completions dialect
(OpenAI itself, Google's compatibility endpoint, xAI, Groq), so one streaming
client covers them all — ``kind`` tells the route which path to take.
Anthropic keeps its native SDK because it carries the extras (server-side web
search, prompt caching) the chat was built on.

Model names are free text on purpose: providers ship new models faster than
this table could chase them. ``default_model`` is only what an empty model
field means.

``key_hint`` is where to get a key, and nothing else: whose plan is free, and
how generously, changes faster than a released build can — a screen that
promises a free tier is a screen that will one day be lying to someone.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AppSetting

#: ``models`` are curated suggestions for the UI dropdown — the field stays
#: free text because vendors ship models faster than this list can chase.
#: The first entry is the provider's default.
AI_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "key_setting": "anthropic_api_key",
        "models": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"],
        "key_hint": "console.anthropic.com",
    },
    # O único provedor sem chave: a credencial é a assinatura Claude já logada no
    # Claude Code da máquina. Por isso ``key_setting`` é None e "configurado"
    # deixa de significar "tem chave" — ver ``is_configured``.
    "claude_code": {
        "label": "Plano Anthropic (assinatura)",
        "kind": "claude_code",
        "key_setting": None,
        # Aliases, não ids: o CLI resolve para o modelo atual de cada família,
        # então isso não envelhece a cada lançamento.
        "models": ["sonnet", "opus", "haiku"],
        "key_hint": "requer o Claude Code instalado e conectado nesta máquina",
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_setting": "openai_api_key",
        "models": ["gpt-5.1", "gpt-5", "gpt-5-mini", "gpt-4o"],
        "key_hint": "platform.openai.com",
    },
    "gemini": {
        "label": "Google (Gemini)",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_setting": "gemini_api_key",
        # Google's own rolling aliases first: "-latest" always resolves to
        # whatever Google currently recommends, so the default never goes
        # stale the way a dated model id does (e.g. gemini-2.5-flash was
        # retired for new callers within months of release). Dated ids stay
        # listed for anyone who wants to pin a specific generation.
        "models": [
            "gemini-flash-latest",
            "gemini-pro-latest",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash-lite",
        ],
        "key_hint": "aistudio.google.com",
    },
    "grok": {
        "label": "xAI (Grok)",
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "key_setting": "grok_api_key",
        "models": ["grok-4", "grok-3", "grok-3-mini"],
        "key_hint": "console.x.ai: exige créditos pagos na conta",
    },
    "groq": {
        "label": "Groq (modelos abertos)",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "key_setting": "groq_api_key",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b"],
        "key_hint": "console.groq.com",
    },
}

for _entry in AI_PROVIDERS.values():
    _entry["default_model"] = _entry["models"][0]


def _stored(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    value = (row.value or {}).get("value") if row is not None else None
    return str(value) if value else None


def api_key_for(entry: dict) -> str:
    """The configured key, or "" for providers that don't use one."""
    key_setting = entry.get("key_setting")
    if not key_setting:
        return ""
    return getattr(settings, key_setting, "")


def is_configured(entry: dict) -> bool:
    """Whether this provider can actually be called right now.

    For every keyed provider that is "a key was saved". The subscription
    provider has no key — it is usable when the local Claude Code is installed
    and logged in through a Claude account, so availability is a property of the
    machine, not of a stored setting.
    """
    if entry.get("kind") == "claude_code":
        from app.services import claude_code

        return claude_code.status().uses_subscription
    return bool(api_key_for(entry))


def unavailable_reason(entry: dict) -> str:
    """Why this provider can't be used, in pt-BR, or "" when it can."""
    if is_configured(entry):
        return ""
    if entry.get("kind") == "claude_code":
        from app.services import claude_code

        state = claude_code.status()
        return state.reason or "Conta Anthropic não conectada."
    return f"Informe sua chave da {entry['label']} em Configurações → Inteligência Artificial."


def active_ai(db: Session) -> tuple[str, dict, str, str]:
    """(provider id, provider entry, model, api key) for this instance.

    UI-saved choices win over env; an unknown stored provider falls back to
    the config default rather than erroring — the chat gate reports a missing
    key, not a broken setting. The key is "" for providers that authenticate
    some other way; callers gate on :func:`is_configured`, not on the key.
    """
    provider_id = _stored(db, "ai_provider") or settings.ai_provider
    provider = AI_PROVIDERS.get(provider_id)
    if provider is None:
        provider_id = "anthropic"
        provider = AI_PROVIDERS[provider_id]
    model = _stored(db, "ai_model") or settings.ai_model or provider["default_model"]
    return provider_id, provider, model, api_key_for(provider)


def providers_public(db: Session) -> dict:
    """What the settings screen needs — labels, defaults, configured flags."""
    provider_id, _, model, _ = active_ai(db)
    return {
        "active_provider": provider_id,
        "active_model": model,
        "providers": [
            {
                "id": key,
                "label": entry["label"],
                "default_model": entry["default_model"],
                "models": entry["models"],
                "key_setting": entry.get("key_setting"),
                "key_hint": entry["key_hint"],
                "key_configured": is_configured(entry),
                "unavailable_reason": unavailable_reason(entry),
            }
            for key, entry in AI_PROVIDERS.items()
        ],
    }
