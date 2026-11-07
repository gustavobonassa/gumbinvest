"""The AI analyst's portfolio tools: the registry, its validation, and the
three ways a model reaches it (Anthropic tool use, OpenAI function calling,
and the MCP endpoint the Claude Code CLI talks to).

Nothing here calls a real model: the provider loops are driven by fakes that
ask for a tool and then answer, which is exactly the round trip under test.
All data is synthetic.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes import ai as ai_route
from app.db.models import Asset, Transaction
from app.main import app
from app.services import ai_tools


#: Real B3 wordings — the app re-derives op_type/effect from them on startup.
_LABELS = {"BUY": "Compra", "DIVIDEND": "Dividendo", "JCP": "Juros Sobre Capital Próprio",
           "YIELD": "Rendimento"}


def _tx(portfolio, asset, op_type, effect, day, *, quantity=0, price=0, amount=0, key):
    return Transaction(
        portfolio_id=portfolio.id, asset_id=asset.id, broker_id=None, import_batch_id=None,
        trade_date=day, direction="CREDIT", op_type=op_type, effect=effect,
        quantity=Decimal(quantity), unit_price=Decimal(price),
        gross_amount=Decimal(amount), fees=Decimal(0), taxes=Decimal(0),
        net_amount=Decimal(amount), currency="BRL", fx_rate=None,
        raw_movement=_LABELS[op_type], raw_product=f"{asset.ticker} - {asset.name}",
        raw_institution="i", source_line=None,
        dedup_key=key, occurrence=0,
    )


@pytest.fixture
def seeded(db, portfolio):
    """Two holdings, a sale and a few payments across two years."""
    xpto = Asset(ticker="XPTO3", name="Xpto SA", kind="STOCK", currency="BRL")
    fund = Asset(ticker="FUND11", name="FII FUNDO TESTE IMOBILIARIO", kind="FII", currency="BRL")
    db.add_all([xpto, fund])
    db.commit()
    db.add_all([
        _tx(portfolio, xpto, "BUY", "ACQUIRE", date(2023, 1, 10), quantity=100, price=10,
            amount=1000, key="b1"),
        _tx(portfolio, fund, "BUY", "ACQUIRE", date(2023, 2, 10), quantity=10, price=100,
            amount=1000, key="b2"),
        _tx(portfolio, xpto, "DIVIDEND", "CASH_IN", date(2023, 6, 1), amount=50, key="d1"),
        _tx(portfolio, xpto, "JCP", "CASH_IN", date(2024, 6, 1), amount=30, key="d2"),
        _tx(portfolio, fund, "YIELD", "CASH_IN", date(2024, 7, 15), amount=8, key="d3"),
    ])
    db.commit()
    return portfolio


def _call(portfolio, name, **args):
    text, is_error = ai_tools.run_tool(portfolio.id, name, args)
    assert not is_error, text
    return json.loads(text)


# ---------------------------------------------------------------------------
# The tools


def test_positions_are_sorted_and_filterable(seeded):
    result = _call(seeded, "consultar_posicoes")
    assert {p["ticker"] for p in result["posicoes"]} == {"FUND11", "XPTO3"}
    only_fii = _call(seeded, "consultar_posicoes", classe="FII")
    assert [p["ticker"] for p in only_fii["posicoes"]] == ["FUND11"]
    assert only_fii["posicoes"][0]["preco_medio"] == 100


def test_income_answers_the_ledger_questions(seeded):
    """"How much JCP did XPTO3 pay me in 2024?" — the question the old prompt couldn't answer."""
    by_year = _call(seeded, "consultar_proventos", agrupar_por="ano")
    assert by_year["grupos"] == [{"chave": "2023", "valor": 50}, {"chave": "2024", "valor": 38}]
    assert by_year["total"] == 88

    xpto_2024 = _call(seeded, "consultar_proventos", ticker="xpto3", desde="2024-01-01",
                      ate="2024-12-31", agrupar_por="tipo")
    assert xpto_2024["grupos"] == [{"chave": "JCP", "valor": 30}]
    fund = _call(seeded, "consultar_proventos", ticker="FUND11", agrupar_por="tipo")
    assert fund["grupos"] == [{"chave": "Rendimento", "valor": 8}]  # not "YIELD"
    assert xpto_2024["pagamentos"] == 1


def test_transactions_filter_and_limit(seeded):
    buys = _call(seeded, "consultar_transacoes", tipos=["BUY"])
    assert {t["ticker"] for t in buys["transacoes"]} == {"XPTO3", "FUND11"}
    assert buys["mais_resultados"] is False

    newest = _call(seeded, "consultar_transacoes", limite=1)
    assert newest["transacoes"][0]["data"] == "2024-07-15"  # newest first
    assert newest["mais_resultados"] is True


def test_asset_detail_and_unknown_ticker(seeded):
    detail = _call(seeded, "consultar_ativo", ticker="XPTO3")
    assert detail["posicao_do_usuario"]["ticker"] == "XPTO3"
    assert "transactions" not in detail["posicao_do_usuario"]
    missing = _call(seeded, "consultar_ativo", ticker="NADA3")
    assert "não está cadastrado" in missing["erro"]


def test_allocation_and_upcoming_run(seeded):
    by_class = _call(seeded, "consultar_alocacao", agrupar_por="classe")
    assert {s["key"] for s in by_class["fatias"]} == {"STOCK", "FII"}
    upcoming = _call(seeded, "consultar_proventos_anunciados")
    assert upcoming["proventos"] == []  # no cached B3 data in a test database


# ---------------------------------------------------------------------------
# Validation and limits


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("consultar_ativo", {}, "obrigatórios ausentes"),
        ("consultar_posicoes", {"classe": "ACAO"}, "deve ser um de"),
        ("consultar_transacoes", {"desde": "01/02/2024"}, "AAAA-MM-DD"),
        ("consultar_transacoes", {"desde": "2024-02-30"}, "não é uma data válida"),
        ("consultar_transacoes", {"limite": 5000}, "entre 1 e 200"),
        ("consultar_transacoes", {"tipos": ["COMPRA"]}, "valores inválidos"),
        ("consultar_posicoes", {"sql": "drop table"}, "desconhecidos"),
        ("consultar_inexistente", {}, "Ferramenta desconhecida"),
    ],
)
def test_bad_arguments_come_back_as_readable_errors(seeded, name, args, message):
    text, is_error = ai_tools.run_tool(seeded.id, name, args)
    assert is_error
    assert message in text


def test_large_results_are_capped(seeded, monkeypatch):
    monkeypatch.setattr(ai_tools, "MAX_RESULT_CHARS", 200)
    text, is_error = ai_tools.run_tool(seeded.id, "consultar_transacoes", {})
    assert not is_error
    assert "resultado truncado" in text
    assert len(text) < 400


def test_three_dialects_expose_the_same_tools_in_the_same_order():
    names = list(ai_tools.TOOLS)
    assert [t["name"] for t in ai_tools.anthropic_tools()] == names
    assert [t["function"]["name"] for t in ai_tools.openai_tools()] == names
    assert [t["name"] for t in ai_tools.mcp_tools()] == names
    # Deterministic: the tool list renders first in the prompt cache key.
    assert ai_tools.anthropic_tools() == ai_tools.anthropic_tools()


# ---------------------------------------------------------------------------
# MCP endpoint (Claude Code)


def _rpc(client, token, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        body["id"] = msg_id
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/ai/mcp", json=body, headers=headers)


def test_mcp_endpoint_speaks_the_protocol(seeded):
    token = ai_route._mcp_open(seeded.id)
    with TestClient(app) as client:
        init = _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18"}).json()
        assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert _rpc(client, token, "notifications/initialized", msg_id=None).status_code == 202

        listed = _rpc(client, token, "tools/list").json()["result"]["tools"]
        assert [t["name"] for t in listed] == list(ai_tools.TOOLS)

        called = _rpc(client, token, "tools/call",
                      {"name": "consultar_proventos", "arguments": {"agrupar_por": "ano"}}).json()
        assert called["result"]["isError"] is False
        assert json.loads(called["result"]["content"][0]["text"])["total"] == 88

        bad = _rpc(client, token, "tools/call", {"name": "consultar_ativo", "arguments": {}})
        assert bad.json()["result"]["isError"] is True

        assert "error" in _rpc(client, token, "resources/list").json()
        assert client.get("/api/ai/mcp").status_code == 405


def test_mcp_endpoint_refuses_without_a_live_token(seeded):
    with TestClient(app) as client:
        assert _rpc(client, None, "tools/list").status_code == 401
        assert _rpc(client, "forged", "tools/list").status_code == 401
        token = ai_route._mcp_open(seeded.id)
        ai_route._mcp_close(token)  # the answer ended
        assert _rpc(client, token, "tools/list").status_code == 401


def test_claude_code_gets_the_tools_by_config_file_not_argv():
    from app.services import claude_code

    argv = claude_code._argv("claude", "sonnet", "s.txt", True, stream=True, mcp_file="m.json")
    assert argv[argv.index("--mcp-config") + 1] == "m.json"
    assert "--strict-mcp-config" in argv  # the user's own MCP servers stay out
    assert argv[argv.index("--allowed-tools") + 1] == "WebSearch,WebFetch,mcp__gumbinvest"

    with claude_code._mcp_config_file(claude_code.McpServer("http://127.0.0.1:1/api/ai/mcp", "t0k")) as path:
        config = json.loads(open(path, encoding="utf-8").read())
    server = config["mcpServers"]["gumbinvest"]
    assert server["headers"]["Authorization"] == "Bearer t0k"


def test_claude_code_status_names_the_portfolio_tool():
    from app.services import claude_code

    event = {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {
        "type": "tool_use", "name": "mcp__gumbinvest__consultar_proventos"}}}
    assert list(claude_code._translate(event)) == [("status", "Consultando seus proventos…")]
    web = {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {
        "type": "server_tool_use", "name": "web_search"}}}
    assert list(claude_code._translate(web)) == [("status", "Pesquisando na web…")]


# ---------------------------------------------------------------------------
# Provider loops


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _use_provider(monkeypatch, provider_id):
    from app.services.ai_providers import AI_PROVIDERS

    entry = AI_PROVIDERS[provider_id]
    monkeypatch.setattr(ai_route, "active_ai", lambda db: (provider_id, entry, "test-model", "k"))
    monkeypatch.setattr(ai_route, "is_configured", lambda provider: True)


class _FakeStream:
    """Enough of the SDK's MessageStream for the chat loop."""

    def __init__(self, events, final):
        self._events, self._final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


def test_anthropic_loop_runs_a_tool_round_then_answers(seeded, monkeypatch):
    import anthropic

    _use_provider(monkeypatch, "anthropic")
    tool_block = SimpleNamespace(type="tool_use", id="toolu_1", name="consultar_proventos",
                                 input={"agrupar_por": "ano"})
    rounds = [
        _FakeStream(
            [SimpleNamespace(type="content_block_start", content_block=tool_block)],
            SimpleNamespace(stop_reason="tool_use", content=[tool_block]),
        ),
        _FakeStream(
            [
                SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text")),
                SimpleNamespace(type="content_block_delta",
                                delta=SimpleNamespace(type="text_delta", text="Você recebeu R$ 88.")),
            ],
            SimpleNamespace(stop_reason="end_turn", content=[]),
        ),
    ]
    sent: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = self

        def stream(self, **kwargs):
            sent.append(kwargs)
            return rounds.pop(0)

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    with TestClient(app) as client:
        response = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "Quanto recebi por ano?"}]})
    events = _sse_events(response)

    assert {"status": "Consultando seus proventos…"} in events
    assert "".join(e.get("text", "") for e in events) == "Você recebeu R$ 88."
    assert events[-1]["done"] is True
    # The second request carried the tool result, answered by id.
    tool_names = [t.get("name") for t in sent[0]["tools"]]
    assert tool_names[0] == "web_search" and "consultar_proventos" in tool_names
    result = sent[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "toolu_1" and result["is_error"] is False
    assert json.loads(result["content"])["total"] == 88
    # The prompt is the short map, not the full portfolio.
    assert "Mapa resumido" in sent[0]["system"][1]["text"]


def _openai_chunks(*chunks) -> bytes:
    lines = [f"data: {json.dumps(chunk)}" for chunk in chunks] + ["data: [DONE]"]
    return ("\n\n".join(lines) + "\n\n").encode()


def _patch_httpx(monkeypatch, handler):
    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)


def test_openai_loop_runs_function_calls(seeded, monkeypatch):
    _use_provider(monkeypatch, "openai")
    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            stream = _openai_chunks(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a",
                    "function": {"name": "consultar_proventos", "arguments": "{\"agrupar"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": "_por\": \"ano\"}"}}]}, "finish_reason": "tool_calls"}]},
            )
        else:
            stream = _openai_chunks({"choices": [{"delta": {"content": "Total: R$ 88."}}]})
        return httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})

    _patch_httpx(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "Quanto recebi?"}]})
    events = _sse_events(response)

    assert "".join(e.get("text", "") for e in events) == "Total: R$ 88."
    assert bodies[0]["tools"][0]["type"] == "function"
    tool_message = bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_a"
    assert json.loads(tool_message["content"])["total"] == 88
    assert bodies[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"agrupar_por": "ano"}'


def test_openai_model_without_tools_falls_back_to_full_context(seeded, monkeypatch):
    _use_provider(monkeypatch, "openai")
    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if "tools" in bodies[-1]:
            return httpx.Response(400, json={"error": {"message": "tools are not supported"}})
        return httpx.Response(
            200, content=_openai_chunks({"choices": [{"delta": {"content": "Sem ferramentas."}}]}),
            headers={"content-type": "text/event-stream"},
        )

    _patch_httpx(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "Oi"}]})
    events = _sse_events(response)

    assert "".join(e.get("text", "") for e in events) == "Sem ferramentas."
    assert len(bodies) == 2 and "tools" not in bodies[1]
    fallback_system = bodies[1]["messages"][0]["content"]
    assert "Dados atuais da carteira completa" in fallback_system
    assert "posicoes_abertas" in fallback_system and "preco_medio" not in fallback_system


def test_tax_tool_reports_the_monthly_worksheet(seeded):
    result = _call(seeded, "consultar_impostos", ano=2024)
    assert "DARF 6015" in result["observacao"]
    assert all(row["mes"].startswith("2024") for row in result["meses"])
    text, is_error = ai_tools.run_tool(seeded.id, "consultar_impostos", {"ano": "2024"})
    assert is_error and "inteiro" in text
