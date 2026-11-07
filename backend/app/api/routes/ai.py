"""AI chat about the portfolio or one asset: a short context, portfolio tools, web search.

The frontend keeps the conversation and sends it whole on every turn; this
endpoint assembles the context server-side (so it is always current and the
API key never reaches the browser) and streams the answer back as SSE. The
detail the model needs comes through the read-only tools in
``app.services.ai_tools`` — over the Messages API, OpenAI-style function
calling, or (for the Claude Code CLI) the MCP endpoint at the end of this file.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.core.logging import get_logger
from app.db.models import AiChat
from app.db.session import session_scope
from app.services import ai_tools, claude_code
from app.services.ai_providers import (
    AI_PROVIDERS,
    active_ai,
    api_key_for,
    is_configured,
    unavailable_reason,
)
from app.services.user_profile import user_intro

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


#: How answers are written — shared by both scopes. The chat renders Markdown
#: (headings, lists, tables), so the model may use it where it helps.
_STYLE = """Como responder:
- Português brasileiro, direto e quantitativo. Cite os números explicitamente \
(preço médio, resultado, alocação, proventos, indicadores).
- Formatação em Markdown quando ajudar a leitura: títulos curtos (###), listas, \
**negrito** para números-chave e tabelas para comparar ativos ou períodos. Respostas \
curtas dispensam títulos.
- Quando a resposta depender de informação recente — notícias, resultados trimestrais, \
fatos relevantes, Selic/COPOM, cenário macro — pesquise na web antes de responder em vez \
de responder de memória, e mencione brevemente as fontes.
- Ao citar trechos de fontes, reescreva-os com caracteres acentuados normais — nunca \
reproduza sequências de escape Unicode (barra invertida + u + código) que apareçam no texto.
- Seja honesto sobre incertezas. Você não é um consultor licenciado: a análise é \
educacional e a decisão é do usuário — diga isso apenas quando fizer sentido, sem \
repetir o aviso em toda resposta."""

#: The portfolio tools, as the model should think of them.
_TOOLS_NOTE = """Dados da carteira: você tem ferramentas que consultam o GumbInvest em tempo \
real — posições, um ativo em detalhe, movimentações, proventos recebidos, alocação, \
rentabilidade, impostos sobre vendas (DARF) e proventos anunciados. O bloco de dados abaixo é só um mapa resumido; \
sempre que a resposta depender de custos, resultados, histórico, proventos ou datas, \
consulte a ferramenta adequada em vez de estimar. Nunca invente números da carteira: se \
um dado não estiver disponível, diga isso."""

#: Stable instructions — kept byte-identical so the prompt cache holds across
#: turns; the per-request data goes in a second system block after it.
SYSTEM_BASE = f"""Você é o analista de investimentos do GumbInvest, um gestor de carteira \
pessoal e privado. Você conversa com o dono da carteira sobre UM ativo específico, com os \
dados da empresa e da posição dele fornecidos abaixo e acesso à busca na web.

{_TOOLS_NOTE}

{_STYLE}
- Você pode e deve dar opinião analítica: cenários, comparação com alternativas (CDI, \
índices, pares do setor), riscos e pontos de atenção — sempre fundamentada nos dados.
- Valores da posição estão na moeda do ativo; percentuais de alocação referem-se à \
carteira toda em BRL."""

#: Same persona, portfolio-wide scope. A separate constant because each prompt
#: must stay byte-identical across turns for the cache.
SYSTEM_PORTFOLIO = f"""Você é o analista de investimentos do GumbInvest, um gestor de carteira \
pessoal e privado. Você conversa com o dono da carteira sobre a CARTEIRA COMO UM TODO — \
alocação, diversificação, risco, desempenho, proventos e próximos aportes — com acesso à \
busca na web.

{_TOOLS_NOTE}

{_STYLE}
- Ao sugerir mudanças — rebalanceamento, aporte, corte de posição — seja específico: \
cite tickers, percentuais e valores em reais, e explique o porquê com os dados.
- Você pode e deve dar opinião analítica: concentração excessiva, sobreposição entre \
posições, comparação com alternativas (CDI, índices), riscos e pontos de atenção.
- Todos os valores consolidados estão na moeda-base da carteira (BRL, salvo indicação)."""

#: For models that cannot call tools (some OpenAI-compatible ones): the old
#: arrangement, everything up front. The HTTP APIs have no command-line limit,
#: so the full portfolio fits; it just cannot reach the ledger.
SYSTEM_PORTFOLIO_NO_TOOLS = f"""Você é o analista de investimentos do GumbInvest, um gestor \
de carteira pessoal e privado. Você conversa com o dono da carteira sobre a CARTEIRA COMO UM \
TODO — alocação, diversificação, risco, desempenho e próximos aportes — com o resumo \
consolidado e todas as posições fornecidos abaixo.

{_STYLE}
- Ao sugerir mudanças, seja específico: cite tickers, percentuais e valores em reais.
- Você não tem acesso ao extrato de movimentações nem ao histórico de proventos por \
pagamento; se a pergunta exigir esses detalhes, diga isso.
- Todos os valores consolidados estão na moeda-base da carteira (BRL, salvo indicação)."""

#: Position fields for the no-tools fallback: enough to reason about
#: allocation, cost, result and income.
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

#: Upper bound on model turns per answer (tool rounds plus web-search resumes):
#: a loop that keeps calling tools is a bug, not diligence.
MAX_TOOL_ROUNDS = 8


def _portfolio_context_full(service) -> str:
    """The whole portfolio, compact — only for models without tool support."""
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


def _portfolio_context(service) -> str:
    """The short map: totals and each open position's weight; the rest is a tool call."""
    return json.dumps(ai_tools.orientation(service), ensure_ascii=False, default=str)


def _asset_context(db, service, ticker: str) -> str:
    """The asset being discussed, in full — one asset is small; the rest is a tool call."""
    from app.db.models import Asset, AssetFundamentals

    items = service.asset_positions(include_closed=True)
    match = next((ap for ap in items if ap.asset.ticker.upper() == ticker.upper()), None)
    total = sum((ap.market_value_base for ap in items), Decimal(0))

    if match is not None:
        asset_id = match.asset.id
        position = match.to_dict(total)
        # The movement ledger is big and rarely needed — consultar_transacoes
        # fetches it when a question does.
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


class _Answer:
    """The streamed answer, as one text across tool rounds.

    A model often writes a line ("vou consultar seus proventos"), calls a tool,
    then continues in a new text block. Concatenated raw, the two would run
    into each other; a paragraph break goes between rounds that both wrote.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []
        self._new_round = False

    def round_started(self) -> None:
        self._new_round = True

    def add(self, text: str) -> str:
        """Record ``text``; returns what to stream (with a separator if needed)."""
        if self._new_round and self.parts and not self.parts[-1].endswith("\n"):
            text = "\n\n" + text
        self._new_round = False
        self.parts.append(text)
        return text

    def text(self) -> str:
        return _decode_escapes("".join(self.parts).strip())


@router.post("/chat", response_model=None, summary="Chat about an asset (SSE stream)")
def ai_chat(
    payload: ChatRequest,
    request: Request,
    db: DbSession,
    portfolio: CurrentPortfolio,
    service: PortfolioSvc,
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
    # is a clean 404 and the generator never touches the request's session
    # (tools open their own — see ai_tools.run_tool).
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
        context_label = "Mapa resumido da carteira do usuário (fonte: GumbInvest, agora):"
    # The owner's name and goals ride in the dynamic block, never in the
    # SYSTEM constants — those must stay byte-identical for the prompt cache.
    intro = user_intro(db)
    context_text = f"{context_label}\n{context_json}"
    if intro:
        context_text = f"{intro}\n\n{context_text}"
    system = [
        {"type": "text", "text": base_prompt},
        {
            "type": "text",
            "text": context_text,
            # Cached so follow-up turns in the same conversation reuse the prefix.
            "cache_control": {"type": "ephemeral"},
        },
    ]
    # The no-tools fallback for OpenAI-compatible models, built while the
    # request's session is still open (the generator must not touch it).
    fallback_system_text: str | None = None
    if provider["kind"] not in ("anthropic", "claude_code"):
        if payload.ticker:
            fallback_system_text = "\n\n".join(block["text"] for block in system)
        else:
            full = f"Dados atuais da carteira completa do usuário (fonte: GumbInvest, agora):\n{_portfolio_context_full(service)}"
            fallback_system_text = SYSTEM_PORTFOLIO_NO_TOOLS + "\n\n" + (
                f"{intro}\n\n{full}" if intro else full
            )
    conversation: list[dict] = [{"role": m.role, "content": m.content} for m in payload.messages]

    def finish(answer: _Answer, failed: bool = False):
        text = answer.text()
        chat_id = (
            _persist_chat(payload.chat_id, portfolio_id, payload.ticker, conversation, text)
            if text and not failed
            else payload.chat_id
        )
        return _sse({"done": True, "chat_id": chat_id})

    def generate_openai_compatible():
        """OpenAI-dialect streaming with function calling — OpenAI, Gemini, Grok, Groq.

        No server-side web search here (that is an Anthropic extra). A model
        that rejects the tools parameter gets one retry without it, on the old
        full-portfolio context.
        """
        import httpx

        def provider_error(response) -> str:
            # The provider's own message names the actual problem (no credits,
            # model gated, key disabled) — pass it on.
            detail = ""
            try:
                body = json.loads(response.read())
                if isinstance(body, list) and body:  # Google wraps errors in a list
                    body = body[0]
                detail = (body.get("error") or {}).get("message") or body.get("message", "")
            except Exception:  # noqa: BLE001 — body may not be JSON
                pass
            prefix = {
                401: f"Chave inválida para {provider['label']}",
                403: f"Acesso negado pela {provider['label']}, verifique créditos/permissões da conta",
                404: f"Modelo '{model}' não existe em {provider['label']}",
                429: "Limite de requisições da API atingido",
            }.get(response.status_code, f"Erro da API da {provider['label']} ({response.status_code})")
            return f"{prefix}. {detail}".strip()

        answer = _Answer()
        system_text = "\n\n".join(block["text"] for block in system)
        messages: list[dict] = [{"role": "system", "content": system_text}, *conversation]
        use_tools = True
        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as http:
                for _round in range(MAX_TOOL_ROUNDS):
                    body: dict = {"model": model, "messages": messages, "stream": True}
                    if use_tools:
                        body["tools"] = ai_tools.openai_tools()
                    calls: dict[int, dict] = {}
                    round_text: list[str] = []
                    finish_reason = None
                    answer.round_started()
                    with http.stream(
                        "POST",
                        f"{provider['base_url']}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=body,
                    ) as response:
                        if response.status_code == 400 and use_tools and _round == 0:
                            # Most likely this model has no function calling.
                            logger.info("ai chat: %s rejected tools, retrying without", model)
                            response.read()
                            use_tools = False
                            messages = [
                                {"role": "system", "content": fallback_system_text or system_text},
                                *conversation,
                            ]
                            continue
                        if response.status_code >= 400:
                            yield _sse({"error": provider_error(response)})
                            return
                        for line in response.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                choice = json.loads(data)["choices"][0]
                            except (KeyError, IndexError, ValueError):
                                continue
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                round_text.append(delta["content"])
                                yield _sse({"status": None})
                                yield _sse({"text": answer.add(delta["content"])})
                            for position, call in enumerate(delta.get("tool_calls") or []):
                                index = call.get("index", position)
                                slot = calls.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                if call.get("id"):
                                    slot["id"] = call["id"]
                                function = call.get("function") or {}
                                if function.get("name"):
                                    slot["name"] = function["name"]
                                    yield _sse({"status": ai_tools.status_for(slot["name"])})
                                if function.get("arguments"):
                                    slot["arguments"] += function["arguments"]
                            finish_reason = choice.get("finish_reason") or finish_reason
                    if not calls:
                        break
                    if finish_reason == "length":
                        yield _sse({"error": "A resposta foi cortada pelo limite de tamanho do modelo."})
                        break
                    ordered = [calls[index] for index in sorted(calls)]
                    for position, call in enumerate(ordered):
                        call["id"] = call["id"] or f"call_{_round}_{position}"
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "".join(round_text) or None,
                            "tool_calls": [
                                {
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["name"],
                                        "arguments": call["arguments"] or "{}",
                                    },
                                }
                                for call in ordered
                            ],
                        }
                    )
                    for call in ordered:
                        try:
                            args = json.loads(call["arguments"] or "{}")
                        except ValueError:
                            result = json.dumps(
                                {"INVALID_JSON": call["arguments"]}, ensure_ascii=False
                            )
                        else:
                            result, _is_error = ai_tools.run_tool(portfolio_id, call["name"], args)
                        messages.append(
                            {"role": "tool", "tool_call_id": call["id"], "content": result}
                        )
                else:
                    logger.warning("ai chat: %s hit the tool-round limit", model)
            yield finish(answer)
        except httpx.ConnectError:
            yield _sse({"error": f"Sem conexão com a API da {provider['label']}."})
        except Exception:  # noqa: BLE001 — the stream must end with a readable error
            logger.exception("ai chat failed (%s)", provider_id)
            yield _sse({"error": "Erro inesperado no chat. Veja os logs do backend."})

    def generate():
        """Anthropic Messages API: web search (server tool) plus the portfolio tools.

        A manual loop rather than the SDK's tool runner: every token, search and
        tool call becomes an SSE event the chat renders as it happens.
        """
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
            *ai_tools.anthropic_tools(),
        ]
        convo = list(conversation)
        answer = _Answer()
        reissued = False
        try:
            for _round in range(MAX_TOOL_ROUNDS):
                answer.round_started()
                try:
                    with client.messages.stream(
                        model=model,
                        max_tokens=64_000,
                        system=system,
                        tools=tools,
                        messages=convo,
                    ) as stream:
                        for event in stream:
                            if event.type == "content_block_start":
                                block = event.content_block
                                block_type = getattr(block, "type", "")
                                if block_type == "server_tool_use":
                                    yield _sse({"status": "Pesquisando na web…"})
                                elif block_type == "tool_use":
                                    yield _sse({"status": ai_tools.status_for(block.name)})
                                elif block_type == "thinking":
                                    yield _sse({"status": "Analisando…"})
                                elif block_type == "text":
                                    yield _sse({"status": None})
                            elif (
                                event.type == "content_block_delta"
                                and event.delta.type == "text_delta"
                            ):
                                yield _sse({"text": answer.add(event.delta.text)})
                        response = stream.get_final_message()
                except ValueError:
                    # A tool input the SDK could not parse at all (inputs stream
                    # unbuffered). There is no tool_use id to answer, so the
                    # request is re-issued once.
                    if reissued:
                        raise
                    reissued = True
                    logger.warning("ai chat: unparseable tool input, re-issuing")
                    continue

                if response.stop_reason == "pause_turn":
                    # Server-side web search paused the turn; re-send to resume.
                    convo = convo + [{"role": "assistant", "content": response.content}]
                    continue
                if response.stop_reason == "refusal":
                    yield _sse(
                        {
                            "error": "A pergunta foi recusada pelos filtros de segurança do "
                            "modelo. Reformule e tente de novo."
                        }
                    )
                    break
                tool_uses = [block for block in response.content if block.type == "tool_use"]
                if response.stop_reason == "max_tokens":
                    if tool_uses:
                        # A cut-off tool input can parse as a valid partial one.
                        logger.warning("ai chat: max_tokens inside a tool call")
                    break
                if response.stop_reason != "tool_use" or not tool_uses:
                    break
                results = []
                for block in tool_uses:
                    text, is_error = ai_tools.run_tool(portfolio_id, block.name, block.input)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": text,
                            "is_error": is_error,
                        }
                    )
                # All results of a round go back in one user message.
                convo = convo + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": results},
                ]
            else:
                logger.warning("ai chat: %s hit the tool-round limit", model)
            yield finish(answer)
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

        As ferramentas da carteira chegam ao CLI como um servidor MCP: o
        endpoint /api/ai/mcp deste mesmo servidor, liberado por um token de uso
        único que vale só para esta resposta.
        """
        answer = _Answer()
        failed = False
        server_port = (request.scope.get("server") or (None, None))[1]
        token = _mcp_open(portfolio_id) if server_port else None
        try:
            system_text = "\n\n".join(block["text"] for block in system)
            mcp = (
                claude_code.McpServer(
                    url=f"http://127.0.0.1:{server_port}/api/ai/mcp", token=token
                )
                if token
                else None
            )
            for kind, value in claude_code.stream_events(
                system_text, conversation, model, search=True, mcp=mcp
            ):
                if kind == "text":
                    yield _sse({"text": answer.add(value)})
                elif kind == "status":
                    yield _sse({"status": value or None})
                    if value:
                        answer.round_started()
                elif kind == "error":
                    failed = True
                    yield _sse({"error": value})
            yield finish(answer, failed)
        except Exception:  # noqa: BLE001 — the stream must end with a readable error
            logger.exception("ai chat failed (claude_code)")
            yield _sse({"error": "Erro inesperado no chat. Veja os logs do backend."})
        finally:
            if token:
                _mcp_close(token)

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


# ---------------------------------------------------------------------------
# MCP endpoint — the portfolio tools for the Claude Code CLI
#
# `claude -p` reaches tools only through MCP. A minimal Streamable-HTTP server
# (JSON responses, no server-initiated stream) is all it needs: initialize,
# tools/list, tools/call. Each chat answer opens a random token bound to its
# portfolio and closes it when the answer ends, so the endpoint serves nothing
# to anyone outside a running chat.

_MCP_TOKENS: dict[str, tuple[int, float]] = {}
_MCP_LOCK = threading.Lock()
#: A token outlives any single answer by a wide margin, then dies regardless.
_MCP_TTL = 15 * 60
_MCP_PROTOCOL = "2025-06-18"


def _mcp_open(portfolio_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _MCP_LOCK:
        for stale in [key for key, (_pid, born) in _MCP_TOKENS.items() if now - born > _MCP_TTL]:
            _MCP_TOKENS.pop(stale, None)
        _MCP_TOKENS[token] = (portfolio_id, now)
    return token


def _mcp_close(token: str) -> None:
    with _MCP_LOCK:
        _MCP_TOKENS.pop(token, None)


def _mcp_portfolio(authorization: str | None) -> int | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    with _MCP_LOCK:
        entry = _MCP_TOKENS.get(token)
    if entry is None or time.monotonic() - entry[1] > _MCP_TTL:
        return None
    return entry[0]


def _mcp_reply(portfolio_id: int, message: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notifications/initialized and friends need no answer
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or _MCP_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "gumbinvest", "version": "1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": ai_tools.mcp_tools()}
    elif method == "tools/call":
        text, is_error = ai_tools.run_tool(
            portfolio_id, str(params.get("name") or ""), params.get("arguments") or {}
        )
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"método não suportado: {method}"},
        }
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


@router.post("/mcp", response_model=None, include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    portfolio_id = _mcp_portfolio(request.headers.get("authorization"))
    if portfolio_id is None:
        return JSONResponse({"error": "token inválido ou expirado"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON inválido"}},
            status_code=400,
        )
    messages = body if isinstance(body, list) else [body]
    # Tools touch the database synchronously; keep the event loop free.
    replies = [
        reply
        for reply in [
            await run_in_threadpool(_mcp_reply, portfolio_id, message)
            for message in messages
            if isinstance(message, dict)
        ]
        if reply is not None
    ]
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(body, list) else replies[0])


@router.get("/mcp", response_model=None, include_in_schema=False)
def mcp_no_stream() -> Response:
    """No server-initiated stream: the spec's answer to GET is 405."""
    return Response(status_code=405)


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
