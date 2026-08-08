"""AI chat about one asset: company data + the owner's position + web search.

The frontend keeps the conversation and sends it whole on every turn; this
endpoint assembles the context server-side (so it is always current and the
API key never reaches the browser) and streams the answer back as SSE.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.core.logging import get_logger
from app.db.models import AiChat
from app.db.session import session_scope
from app.services import claude_code
from app.services.ai_providers import (
    AI_PROVIDERS,
    active_ai,
    api_key_for,
    is_configured,
    unavailable_reason,
)

router = APIRouter(prefix="/ai", tags=["ai"])
logger = get_logger(__name__)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatRequest(BaseModel):
    #: Null asks about the portfolio as a whole rather than one asset.
    ticker: str | None = Field(default=None, min_length=1, max_length=40)
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    #: Continue a saved conversation; null starts (and saves) a new one.
    chat_id: int | None = None


#: Stable instructions — kept byte-identical so the prompt cache holds across
#: turns; the per-asset data goes in a second system block after it.
SYSTEM_BASE = """Você é o analista de investimentos do GumbInvest, um gestor de carteira \
pessoal e privado. Você conversa com o dono da carteira sobre UM ativo específico, com os \
dados da empresa e da posição dele fornecidos abaixo e acesso à busca na web.

Como responder:
- Português brasileiro, direto e quantitativo. Use os números fornecidos e cite-os \
explicitamente (preço médio, resultado, alocação, indicadores).
- Quando a resposta depender de informação recente — notícias, resultados trimestrais, \
fatos relevantes, preço-alvo, Selic/COPOM — pesquise na web antes de responder em vez de \
responder de memória, e mencione brevemente as fontes.
- Seja conciso: parágrafos curtos e listas simples com hífens. Sem tabelas e sem títulos \
markdown; negrito (**assim**) é permitido para destacar números-chave.
- Ao citar trechos de fontes, reescreva-os com caracteres acentuados normais — nunca \
reproduza sequências de escape Unicode (barra invertida + u + código) que apareçam no texto.
- Você pode e deve dar opinião analítica: cenários, comparação com alternativas (CDI, \
índices, pares do setor), riscos e pontos de atenção — sempre fundamentada nos dados.
- Seja honesto sobre incertezas. Você não é um consultor licenciado: a análise é \
educacional e a decisão é do usuário — diga isso apenas quando fizer sentido, sem \
repetir o aviso em toda resposta.
- Valores da posição estão na moeda do ativo; percentuais de alocação referem-se à \
carteira toda em BRL."""

#: Same persona, portfolio-wide scope. A separate constant (not a format string)
#: because each prompt must stay byte-identical across turns for the cache.
SYSTEM_PORTFOLIO = """Você é o analista de investimentos do GumbInvest, um gestor de carteira \
pessoal e privado. Você conversa com o dono da carteira sobre a CARTEIRA COMO UM TODO — \
alocação, diversificação, risco, desempenho e próximos aportes — com o resumo consolidado \
e todas as posições fornecidos abaixo e acesso à busca na web.

Como responder:
- Português brasileiro, direto e quantitativo. Use os números fornecidos e cite-os \
explicitamente (valores, alocações, resultados por posição).
- Ao sugerir mudanças — rebalanceamento, aporte, corte de posição — seja específico: \
cite tickers, percentuais e valores em reais, e explique o porquê com os dados.
- Quando a resposta depender de informação recente — notícias, resultados, Selic/COPOM, \
cenário macro — pesquise na web antes de responder em vez de responder de memória, e \
mencione brevemente as fontes.
- Seja conciso: parágrafos curtos e listas simples com hífens. Sem tabelas e sem títulos \
markdown; negrito (**assim**) é permitido para destacar números-chave.
- Ao citar trechos de fontes, reescreva-os com caracteres acentuados normais — nunca \
reproduza sequências de escape Unicode (barra invertida + u + código) que apareçam no texto.
- Você pode e deve dar opinião analítica: concentração excessiva, sobreposição entre \
posições, comparação com alternativas (CDI, índices), riscos e pontos de atenção.
- Seja honesto sobre incertezas. Você não é um consultor licenciado: a análise é \
educacional e a decisão é do usuário — diga isso apenas quando fizer sentido, sem \
repetir o aviso em toda resposta.
- Todos os valores consolidados estão na moeda-base da carteira (BRL, salvo indicação)."""

#: Position fields worth the tokens: enough to reason about allocation, cost,
#: result and income — not the full ledger the asset chat gets.
_POSITION_KEYS = (
    "ticker",
    "kind",
    "currency",
    "quantity",
    "average_price",
    "current_price",
    "market_value_base",
    "allocation_pct",
    "unrealized_pct",
    "total_return",
    "income",
    "day_change_pct",
)


def _portfolio_context(service) -> str:
    """The whole portfolio, compact: totals, every open position, class mix."""
    overview = service.overview()
    items = sorted(
        service.asset_positions(include_closed=False),
        key=lambda ap: ap.market_value_base,
        reverse=True,
    )
    total = sum((ap.market_value_base for ap in items), Decimal(0))

    by_kind: dict[str, Decimal] = {}
    positions = []
    for ap in items:
        row = ap.to_dict(total)
        positions.append({key: row.get(key) for key in _POSITION_KEYS})
        by_kind[ap.asset.kind] = by_kind.get(ap.asset.kind, Decimal(0)) + ap.market_value_base

    context = {
        "resumo_da_carteira": overview,
        "alocacao_por_classe_pct": {
            kind: float(value / total * 100) if total else 0.0
            for kind, value in sorted(by_kind.items(), key=lambda item: item[1], reverse=True)
        },
        "posicoes_abertas": positions,
    }
    return json.dumps(context, ensure_ascii=False, default=str)


def _asset_context(db, service, ticker: str) -> str:
    """Everything the model gets to know, as one compact JSON block."""
    from app.db.models import Asset, AssetFundamentals

    items = service.asset_positions(include_closed=True)
    match = next((ap for ap in items if ap.asset.ticker.upper() == ticker.upper()), None)
    total = sum((ap.market_value_base for ap in items), Decimal(0))

    if match is not None:
        asset_id = match.asset.id
        position = match.to_dict(total)
        # The movement ledger is big and rarely needed; the aggregates already
        # carried by the position row tell the position's story.
        position.pop("transactions", None)
    else:
        # Watch-only asset: there is a row (search created it) but no position.
        # The chat still works — arguably at its best, "should I buy this?" —
        # it just has to know the user owns none of it.
        asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
        if asset is None:
            raise HTTPException(status_code=404, detail="ativo não encontrado")
        asset_id = asset.id
        position = {
            "possui_o_ativo": False,
            "observacao": "O usuário NÃO possui este ativo na carteira; está avaliando o papel.",
        }

    cached = db.get(AssetFundamentals, asset_id)
    company = cached.data if cached is not None and cached.data else None

    context = {
        "posicao_do_usuario": position,
        "empresa_fundamentos": company,
        "carteira_total_brl": float(total),
    }
    return json.dumps(context, ensure_ascii=False, default=str)


#: Literal "backslash-u-4-hex" sequences in the model's own prose. They appear
#: when it quotes a web result whose snippet arrived JSON-escaped; the quote is
#: copied verbatim, escapes and all, and reaches the user as "pre\\u00e7o".
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_escapes(text: str) -> str:
    return _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _persist_chat(
    chat_id: int | None,
    portfolio_id: int,
    ticker: str | None,
    history: list[dict],
    answer: str,
) -> int | None:
    """Save the finished turn. Own session: the request's may already be gone."""
    try:
        with session_scope() as db:
            chat = db.get(AiChat, chat_id) if chat_id else None
            if chat is None:
                first = next((m["content"] for m in history if m["role"] == "user"), "Conversa")
                chat = AiChat(
                    portfolio_id=portfolio_id,
                    ticker=ticker.upper() if ticker else None,
                    title=first[:157] + "…" if len(first) > 158 else first,
                )
                db.add(chat)
            chat.messages = history + [{"role": "assistant", "content": answer}]
            db.flush()
            return chat.id
    except Exception:  # noqa: BLE001 — losing the save must not kill the stream
        logger.exception("ai chat: persist failed")
        return None


@router.post("/chat", response_model=None, summary="Chat about an asset (SSE stream)")
def ai_chat(
    payload: ChatRequest, db: DbSession, portfolio: CurrentPortfolio, service: PortfolioSvc
) -> StreamingResponse:
    provider_id, provider, model, api_key = active_ai(db)
    if not is_configured(provider):
        raise HTTPException(
            status_code=503,
            detail=f"{unavailable_reason(provider)} Necessário para habilitar o chat.",
        )
    portfolio_id = portfolio.id
    if payload.chat_id is not None:
        existing = db.get(AiChat, payload.chat_id)
        if existing is None or existing.portfolio_id != portfolio_id:
            raise HTTPException(status_code=404, detail="conversa não encontrada")

    # Context and validation happen before streaming starts, so a bad ticker
    # is a clean 404 and the generator never touches the database session.
    if payload.ticker:
        context_json = _asset_context(db, service, payload.ticker)
        base_prompt = SYSTEM_BASE
        context_label = (
            f"Dados atuais de {payload.ticker.upper()} e da posição do usuário "
            f"(fonte: GumbInvest, agora):"
        )
    else:
        context_json = _portfolio_context(service)
        base_prompt = SYSTEM_PORTFOLIO
        context_label = "Dados atuais da carteira completa do usuário (fonte: GumbInvest, agora):"
    system = [
        {"type": "text", "text": base_prompt},
        {
            "type": "text",
            "text": f"{context_label}\n{context_json}",
            # Cached so follow-up turns in the same conversation reuse the prefix.
            "cache_control": {"type": "ephemeral"},
        },
    ]
    conversation: list[dict] = [{"role": m.role, "content": m.content} for m in payload.messages]

    def generate_openai_compatible():
        """OpenAI-dialect streaming — OpenAI, Gemini, Grok, Groq.

        No server-side web search here (that is an Anthropic extra); the
        model answers from the portfolio context alone.
        """
        import httpx

        system_text = "\n\n".join(block["text"] for block in system)
        messages = [{"role": "system", "content": system_text}, *conversation]
        answer_parts: list[str] = []
        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as http:
                with http.stream(
                    "POST",
                    f"{provider['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "messages": messages, "stream": True},
                ) as response:
                    if response.status_code >= 400:
                        # The provider's own message names the actual problem
                        # (no credits, model gated, key disabled) — pass it on
                        # instead of hiding it behind a status code.
                        detail = ""
                        try:
                            body = json.loads(response.read())
                            # Google wraps errors in a single-element list.
                            if isinstance(body, list) and body:
                                body = body[0]
                            detail = (body.get("error") or {}).get("message") or body.get(
                                "message", ""
                            )
                        except Exception:  # noqa: BLE001 — body may not be JSON
                            pass
                        prefix = {
                            401: f"Chave inválida para {provider['label']}",
                            403: f"Acesso negado pela {provider['label']}, verifique créditos/permissões da conta",
                            404: f"Modelo '{model}' não existe em {provider['label']}",
                            429: "Limite de requisições da API atingido",
                        }.get(response.status_code, f"Erro da API da {provider['label']} ({response.status_code})")
                        yield _sse({"error": f"{prefix}. {detail}".strip()})
                        return
                    for line in response.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"].get("content")
                        except (KeyError, IndexError, ValueError):
                            continue
                        if delta:
                            answer_parts.append(delta)
                            yield _sse({"text": delta})
            answer = _decode_escapes("".join(answer_parts).strip())
            chat_id = (
                _persist_chat(payload.chat_id, portfolio_id, payload.ticker, conversation, answer)
                if answer
                else payload.chat_id
            )
            yield _sse({"done": True, "chat_id": chat_id})
        except httpx.ConnectError:
            yield _sse({"error": f"Sem conexão com a API da {provider['label']}."})
        except Exception:  # noqa: BLE001 — the stream must end with a readable error
            logger.exception("ai chat failed (%s)", provider_id)
            yield _sse({"error": "Erro inesperado no chat. Veja os logs do backend."})

    def generate():
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        convo = list(conversation)
        answer_parts: list[str] = []
        try:
            # Server-side web search can pause the turn; re-send to resume.
            for _ in range(4):
                with client.messages.stream(
                    model=model,
                    max_tokens=64_000,
                    system=system,
                    tools=tools,
                    messages=convo,
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            block_type = getattr(event.content_block, "type", "")
                            if block_type == "server_tool_use":
                                yield _sse({"status": "Pesquisando na web…"})
                            elif block_type == "thinking":
                                yield _sse({"status": "Analisando…"})
                            elif block_type == "text":
                                yield _sse({"status": None})
                        elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                            answer_parts.append(event.delta.text)
                            yield _sse({"text": event.delta.text})
                    response = stream.get_final_message()
                if response.stop_reason == "pause_turn":
                    convo = convo + [{"role": "assistant", "content": response.content}]
                    continue
                if response.stop_reason == "refusal":
                    yield _sse({"error": "A pergunta foi recusada pelos filtros de segurança do modelo. Reformule e tente de novo."})
                break
            answer = _decode_escapes("".join(answer_parts).strip())
            chat_id = (
                _persist_chat(payload.chat_id, portfolio_id, payload.ticker, conversation, answer)
                if answer
                else payload.chat_id
            )
            yield _sse({"done": True, "chat_id": chat_id})
        except anthropic.AuthenticationError:
            yield _sse({"error": "Chave da Anthropic inválida, confira em Configurações → Sistema."})
        except anthropic.RateLimitError:
            yield _sse({"error": "Limite de requisições da API atingido, tente novamente em instantes."})
        except anthropic.APIConnectionError:
            yield _sse({"error": "Sem conexão com a API da Anthropic."})
        except anthropic.APIStatusError as exc:
            logger.exception("ai chat: api error %s", exc.status_code)
            yield _sse({"error": f"Erro da API da Anthropic ({exc.status_code})."})
        except Exception:  # noqa: BLE001 — the stream must end with a readable error
            logger.exception("ai chat failed")
            yield _sse({"error": "Erro inesperado no chat. Veja os logs do backend."})

    def generate_claude_code():
        """Assinatura Anthropic, via o Claude Code local.

        O serviço já traduz o NDJSON do CLI para os mesmos eventos que os outros
        ramos emitem, então aqui só falta virar SSE e persistir a conversa.
        """
        answer_parts: list[str] = []
        failed = False
        try:
            system_text = "\n\n".join(block["text"] for block in system)
            for kind, value in claude_code.stream_events(
                system_text, conversation, model, search=True
            ):
                if kind == "text":
                    answer_parts.append(value)
                    yield _sse({"text": value})
                elif kind == "status":
                    yield _sse({"status": value or None})
                elif kind == "error":
                    failed = True
                    yield _sse({"error": value})
            answer = _decode_escapes("".join(answer_parts).strip())
            chat_id = (
                _persist_chat(payload.chat_id, portfolio_id, payload.ticker, conversation, answer)
                if answer and not failed
                else payload.chat_id
            )
            yield _sse({"done": True, "chat_id": chat_id})
        except Exception:  # noqa: BLE001 — the stream must end with a readable error
            logger.exception("ai chat failed (claude_code)")
            yield _sse({"error": "Erro inesperado no chat. Veja os logs do backend."})

    if provider["kind"] == "anthropic":
        stream = generate()
    elif provider["kind"] == "claude_code":
        stream = generate_claude_code()
    else:
        stream = generate_openai_compatible()
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx: don't buffer the stream.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/models", response_model=None, summary="Models available at an AI provider")
def list_provider_models(provider: str) -> dict:
    """The provider's live model catalog, fetched with the user's own key.

    Vendors add and retire models faster than any hardcoded list can track —
    the settings screen's dropdown asks the source instead. Falls back to the
    curated suggestions when there is no key or the call fails, flagged with
    ``live: False`` so the UI can say so.
    """
    entry = AI_PROVIDERS.get(provider)
    if entry is None:
        raise HTTPException(status_code=404, detail="provedor desconhecido")
    api_key = api_key_for(entry)
    fallback = {"models": entry["models"], "live": False}
    # A assinatura não expõe catálogo: o CLI resolve aliases (sonnet/opus/haiku)
    # para o modelo atual de cada família, então a lista curada já é a resposta.
    if not api_key:
        return fallback

    import httpx

    try:
        if entry["kind"] == "anthropic":
            response = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=15,
            )
        else:
            response = httpx.get(
                f"{entry['base_url']}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
        response.raise_for_status()
        ids = [
            # Google prefixes ids with "models/"; the chat endpoint takes both
            # forms, the bare one reads better.
            str(item["id"]).removeprefix("models/")
            for item in response.json().get("data", [])
            if item.get("id")
        ]
        if not ids:
            return fallback
        return {"models": sorted(ids), "live": True}
    except Exception:  # noqa: BLE001 — the dropdown must render regardless
        logger.exception("could not list models for %s", provider)
        return fallback


def _claude_code_payload(*, refresh: bool) -> dict:
    state = claude_code.status(refresh=refresh)
    return {
        "installed": state.installed,
        "logged_in": state.logged_in,
        "uses_subscription": state.uses_subscription,
        "method": state.method,
        "email": state.email,
        "plan": state.plan,
        "reason": state.reason,
        "install_url": claude_code.INSTALL_URL,
    }


@router.get(
    "/claude-code/status",
    response_model=None,
    summary="Whether the local Claude Code is installed and signed in",
)
def claude_code_status(refresh: bool = False) -> dict:
    """Estado da conexão com a assinatura. O polling do botão passa refresh=1."""
    return _claude_code_payload(refresh=refresh)


@router.post(
    "/claude-code/login",
    response_model=None,
    summary="Open the Anthropic sign-in in the user's browser",
)
def claude_code_login() -> dict:
    """Dispara o login e volta na hora — a tela acompanha por ``/status``.

    Não se espera o término aqui de propósito: o usuário ainda vai autorizar no
    navegador, o que leva o tempo que levar, e segurar a requisição até lá só
    renderia um timeout.
    """
    try:
        claude_code.start_login()
    except claude_code.ClaudeCodeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _claude_code_payload(refresh=True)


@router.post(
    "/claude-code/logout",
    response_model=None,
    summary="Sign the local Claude Code out of the Anthropic account",
)
def claude_code_logout() -> dict:
    claude_code.logout()
    return _claude_code_payload(refresh=True)


@router.get("/chats", response_model=None, summary="Saved AI conversations")
def list_chats(db: DbSession, portfolio: CurrentPortfolio) -> list[dict]:
    rows = db.scalars(
        select(AiChat)
        .where(AiChat.portfolio_id == portfolio.id)
        .order_by(AiChat.updated_at.desc())
        .limit(200)
    ).all()
    return [
        {
            "id": row.id,
            "ticker": row.ticker,
            "title": row.title,
            "message_count": len(row.messages or []),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


@router.get("/chats/{chat_id}", response_model=None, summary="One conversation, with messages")
def get_chat(chat_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    chat = db.get(AiChat, chat_id)
    if chat is None or chat.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="conversa não encontrada")
    return {
        "id": chat.id,
        "ticker": chat.ticker,
        "title": chat.title,
        "messages": chat.messages or [],
        "message_count": len(chat.messages or []),
        "created_at": chat.created_at,
        "updated_at": chat.updated_at,
    }


@router.delete("/chats/{chat_id}", response_model=None, summary="Delete a conversation")
def delete_chat(chat_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    chat = db.get(AiChat, chat_id)
    if chat is None or chat.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="conversa não encontrada")
    db.delete(chat)
    db.commit()
    return {"deleted": chat_id}
