"""Read-only portfolio tools for the AI analyst.

The analyst used to receive the whole portfolio in its system prompt: every
position, every field, on every turn. That grew with the portfolio — on
Windows it outgrew the command-line limit of the Claude Code subprocess — and
it still could not answer the questions that need the ledger ("how much JCP
did ITSA4 pay me in 2024?"). Now the prompt carries a short orientation and the
model asks for what each question needs through these tools.

One registry, three transports, so every provider sees the same tools with the
same answers:

* Anthropic API — :func:`anthropic_tools` (custom tools beside web search);
* OpenAI-compatible APIs — :func:`openai_tools` (function calling);
* Claude Code (the subscription) — the MCP endpoint in ``api/routes/ai.py``,
  which serves :func:`mcp_tools` and dispatches through :func:`run_tool`.

Every tool reads through :class:`PortfolioService` (or the same tables the
screens read), so a figure the analyst quotes is the figure on the page.
Nothing here writes. Inputs are validated against each tool's schema before it
runs — a model can send anything, and a bad argument must come back as a
readable error the model can correct, never as a traceback.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Callable

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import Asset, AssetFundamentals, Broker, Transaction
from app.db.session import session_scope
from app.domain.enums import AssetKind, OperationType
from app.portfolio.service import PortfolioService

logger = get_logger(__name__)

#: Ceiling on one tool result, in characters. A broad query (every transaction
#: since 2019) must not flood the conversation; the model is told it was cut
#: and can narrow the filter.
MAX_RESULT_CHARS = 24_000

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Asset classes and operation types exactly as the database stores them —
#: derived from the enums, so a filter value the model picks always matches.
KINDS = [kind.value for kind in AssetKind]
OP_TYPES = [op.value for op in OperationType]


# ---------------------------------------------------------------------------
# Helpers


def _num(value) -> float | None:
    """Money and quantities for the model: plain numbers, 2-8 significant places."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return round(value, 8 if abs(value) < 1 else 2)
    return value


def _clean(value):
    """Recursively make a result JSON-friendly and compact."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (Decimal, float)):
        return _num(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _ticker(service: PortfolioService, ticker: str) -> Asset | None:
    wanted = ticker.strip().upper()
    return service.db.scalar(select(Asset).where(Asset.ticker == wanted))


# ---------------------------------------------------------------------------
# Tools


def consultar_posicoes(service: PortfolioService, classe: str | None = None,
                       incluir_encerradas: bool = False) -> dict:
    items = sorted(
        service.asset_positions(include_closed=incluir_encerradas),
        key=lambda ap: ap.market_value_base,
        reverse=True,
    )
    total = sum((ap.market_value_base for ap in items), Decimal(0))
    rows = []
    for ap in items:
        if classe and ap.asset.kind != classe:
            continue
        row = ap.to_dict(total)
        rows.append({
            "ticker": row["ticker"],
            "nome": row["name"],
            "classe": row["kind"],
            "moeda": row["currency"],
            "quantidade": row["quantity"],
            "preco_medio": row["average_price"],
            "preco_atual": row["current_price"],
            "valor_de_mercado_brl": row.get("market_value_base"),
            "alocacao_pct": row.get("allocation_pct"),
            "resultado_nao_realizado_pct": row.get("unrealized_pct"),
            "resultado_total": row.get("total_return"),
            "proventos_recebidos": row.get("income"),
            "variacao_dia_pct": row.get("day_change_pct"),
        })
    return {"carteira_total_brl": total, "posicoes": rows}


def consultar_ativo(service: PortfolioService, ticker: str) -> dict:
    items = service.asset_positions(include_closed=True)
    total = sum((ap.market_value_base for ap in items if ap.position.quantity), Decimal(0))
    match = next((ap for ap in items if ap.asset.ticker.upper() == ticker.strip().upper()), None)
    if match is not None:
        asset = match.asset
        position = match.to_dict(total)
        position.pop("transactions", None)  # consultar_transacoes has the ledger
    else:
        asset = _ticker(service, ticker)
        if asset is None:
            return {"erro": f"{ticker.upper()} não está cadastrado no GumbInvest."}
        position = {"possui_o_ativo": False}
    cached = service.db.get(AssetFundamentals, asset.id)
    return {
        "posicao_do_usuario": position,
        "fundamentos": cached.data if cached is not None and cached.data else None,
    }


def consultar_transacoes(service: PortfolioService, ticker: str | None = None,
                         tipos: list[str] | None = None, desde: str | None = None,
                         ate: str | None = None, limite: int = 50) -> dict:
    stmt = (
        select(Transaction, Asset, Broker)
        .join(Asset, Asset.id == Transaction.asset_id)
        .outerjoin(Broker, Broker.id == Transaction.broker_id)
        .where(Transaction.portfolio_id == service.portfolio_id)
    )
    if ticker:
        stmt = stmt.where(Asset.ticker == ticker.strip().upper())
    if tipos:
        stmt = stmt.where(Transaction.op_type.in_(tipos))
    if desde:
        stmt = stmt.where(Transaction.trade_date >= _date(desde))
    if ate:
        stmt = stmt.where(Transaction.trade_date <= _date(ate))
    rows = service.db.execute(
        stmt.order_by(Transaction.trade_date.desc(), Transaction.id.desc()).limit(limite + 1)
    ).all()
    return {
        "transacoes": [
            {
                "data": t.trade_date,
                "ticker": a.ticker,
                "tipo": t.op_type,
                "movimentacao": t.raw_movement,
                "quantidade": t.quantity,
                "preco_unitario": t.unit_price,
                "valor_liquido": t.net_amount,
                "moeda": t.currency or "BRL",
                "corretora": b.canonical_name if b else None,
            }
            for t, a, b in rows[:limite]
        ],
        "mais_resultados": len(rows) > limite,
    }


_INCOME_GROUPS = ("mes", "ano", "ativo", "tipo")

#: The names the Proventos page uses (frontend/src/lib/format.ts), so the model
#: says "Rendimento" rather than echoing the database's "YIELD".
_INCOME_LABELS = {
    "DIVIDEND": "Dividendo",
    "JCP": "JCP",
    "YIELD": "Rendimento",
    "INTEREST": "Juros de renda fixa",
    "AMORTIZATION": "Amortização",
    "REWARD": "Recompensa",
}


def consultar_proventos(service: PortfolioService, ticker: str | None = None,
                        desde: str | None = None, ate: str | None = None,
                        agrupar_por: str = "mes") -> dict:
    assets = service.assets()
    start, end = _date(desde), _date(ate)
    wanted = ticker.strip().upper() if ticker else None
    totals: dict[str, Decimal] = defaultdict(Decimal)
    by_type: dict[str, Decimal] = defaultdict(Decimal)
    count = 0
    for day, asset_id, op_type, amount in service._income_rows():
        asset = assets.get(asset_id)
        symbol = asset.ticker if asset else str(asset_id)
        if wanted and symbol.upper() != wanted:
            continue
        if (start and day < start) or (end and day > end):
            continue
        label = _INCOME_LABELS.get(op_type, op_type)
        key = {
            "mes": day.strftime("%Y-%m"),
            "ano": str(day.year),
            "ativo": symbol,
            "tipo": label,
        }[agrupar_por]
        totals[key] += amount
        by_type[label] += amount
        count += 1
    ordered = (
        sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if agrupar_por in ("ativo", "tipo")
        else sorted(totals.items())
    )
    return {
        "moeda": service.base_currency,
        "observacao": "valores líquidos do imposto retido, na moeda-base, na data do pagamento",
        "pagamentos": count,
        "total": sum(totals.values(), Decimal(0)),
        "por_tipo": dict(by_type),
        "grupos": [{"chave": key, "valor": value} for key, value in ordered],
    }


_ALLOCATION_GROUPS = {"classe": "kind", "ativo": "asset", "corretora": "broker", "moeda": "currency"}


def consultar_alocacao(service: PortfolioService, agrupar_por: str = "classe") -> dict:
    return {"agrupado_por": agrupar_por, "fatias": service.allocation(_ALLOCATION_GROUPS[agrupar_por])}


def consultar_rentabilidade(service: PortfolioService) -> dict:
    return {
        "retornos_mensais": service.monthly_returns()[-36:],
        "resumo_anual": service.annual_report(),
        "observacao": "retornos mensais ajustados pelos aportes (últimos 36 meses)",
    }


def consultar_proventos_anunciados(service: PortfolioService) -> dict:
    from app.portfolio.upcoming import upcoming_dividends

    result = upcoming_dividends(service.db, service)
    return {
        "observacao": "anunciados na B3 e ainda não pagos; total estimado pela posição atual",
        "proventos": result["items"],
        "sem_dados": result["missing"],
    }


def consultar_impostos(service: PortfolioService, ano: int | None = None) -> dict:
    from app.portfolio.monthly_tax import monthly_taxes

    sheet = monthly_taxes(service)
    months = [row for row in sheet["months"] if ano is None or row["period"].startswith(str(ano))]
    return {
        "observacao": (
            "apuração mensal de renda variável (DARF 6015): ações/ETFs/BDRs 15 % com isenção "
            "de vendas de ações até R$ 20 mil/mês, FIIs 20 %, day trade 20 % (informado pelo "
            "usuário); exterior e cripto ficam fora; é uma estimativa a conferir"
        ),
        "meses": [
            {
                "mes": row["period"],
                "situacao": row["status"],
                "vendas_acoes": row["sales"]["acoes"],
                "vendas_outras_comuns": row["sales"]["outras_comuns"],
                "vendas_fii": row["sales"]["fii"],
                "isento": row["exempt"],
                "lucro_isento": row["exempt_gain"],
                "resultados": row["results"],
                "imposto_bruto": row["gross_tax"],
                "irrf_compensado": row["irrf_used"],
                "darf": row["darf"],
                "vencimento": row["due_date"],
                "prejuizo_a_compensar": row["losses_after"],
            }
            for row in months[:24]
        ],
        "avisos": [warning["message"] for warning in sheet["warnings"]],
    }


# ---------------------------------------------------------------------------
# Registry

#: name -> (description, input schema, implementation). Descriptions say *when*
#: to call, not only what comes back — models reach for a tool on that cue.
TOOLS: dict[str, tuple[str, dict, Callable[..., dict]]] = {
    "consultar_posicoes": (
        "Lista as posições da carteira com quantidade, preço médio, preço atual, valor, "
        "alocação, resultado e proventos. Use para perguntas sobre o que o usuário tem, "
        "concentração, maiores/menores posições ou desempenho por ativo.",
        {
            "type": "object",
            "properties": {
                "classe": {"type": "string", "enum": KINDS,
                           "description": "Filtra por classe de ativo (opcional)."},
                "incluir_encerradas": {"type": "boolean",
                                       "description": "Inclui posições já zeradas (padrão: false)."},
            },
            "required": [],
            "additionalProperties": False,
        },
        consultar_posicoes,
    ),
    "consultar_ativo": (
        "Detalha um ativo: a posição do usuário (custo, resultados, proventos) e os "
        "fundamentos em cache (indicadores, dividendos anunciados). Use quando a pergunta "
        "for sobre um ativo específico, mesmo que o usuário não o possua.",
        {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Ex.: ITSA4, HGLG11, AAPL."}},
            "required": ["ticker"],
            "additionalProperties": False,
        },
        consultar_ativo,
    ),
    "consultar_transacoes": (
        "Busca movimentações do extrato (compras, vendas, proventos, desdobramentos...), "
        "mais recentes primeiro. Use para perguntas sobre quando ou a que preço algo foi "
        "comprado/vendido, histórico de um ativo ou operações num período.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Filtra por ativo (opcional)."},
                "tipos": {"type": "array", "items": {"type": "string", "enum": OP_TYPES},
                          "description": "Filtra por tipos de operação (opcional)."},
                "desde": {"type": "string", "description": "Data inicial AAAA-MM-DD (opcional)."},
                "ate": {"type": "string", "description": "Data final AAAA-MM-DD (opcional)."},
                "limite": {"type": "integer", "minimum": 1, "maximum": 200,
                           "description": "Máximo de linhas (padrão 50)."},
            },
            "required": [],
            "additionalProperties": False,
        },
        consultar_transacoes,
    ),
    "consultar_proventos": (
        "Soma os proventos recebidos (dividendos, JCP, rendimentos, juros), líquidos de "
        "imposto, agrupados por mês, ano, ativo ou tipo. Use para qualquer pergunta sobre "
        "quanto o usuário recebeu de proventos, de quem e quando.",
        {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Filtra por ativo (opcional)."},
                "desde": {"type": "string", "description": "Data inicial AAAA-MM-DD (opcional)."},
                "ate": {"type": "string", "description": "Data final AAAA-MM-DD (opcional)."},
                "agrupar_por": {"type": "string", "enum": list(_INCOME_GROUPS),
                                "description": "Agrupamento (padrão: mes)."},
            },
            "required": [],
            "additionalProperties": False,
        },
        consultar_proventos,
    ),
    "consultar_alocacao": (
        "Distribuição da carteira em percentual e valor, por classe, ativo, corretora ou "
        "moeda. Use para perguntas de diversificação e exposição.",
        {
            "type": "object",
            "properties": {
                "agrupar_por": {"type": "string", "enum": list(_ALLOCATION_GROUPS),
                                "description": "Agrupamento (padrão: classe)."},
            },
            "required": [],
            "additionalProperties": False,
        },
        consultar_alocacao,
    ),
    "consultar_rentabilidade": (
        "Retornos mensais da carteira (ajustados pelos aportes, últimos 36 meses) e o "
        "resumo por ano de compras, vendas e proventos. Use para perguntas de desempenho "
        "ao longo do tempo.",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        consultar_rentabilidade,
    ),
    "consultar_impostos": (
        "Apuração mensal do imposto sobre vendas (DARF): vendas do mês, isenção de R$ 20 mil "
        "em ações, resultado por regime, prejuízo a compensar, valor do DARF e vencimento. "
        "Use para perguntas sobre imposto de renda em vendas, day trade, DARF, quanto ainda "
        "dá para vender isento no mês ou prejuízos acumulados.",
        {
            "type": "object",
            "properties": {
                "ano": {"type": "integer", "minimum": 2000, "maximum": 2100,
                        "description": "Filtra por ano (opcional; padrão: últimos 24 meses)."},
            },
            "required": [],
            "additionalProperties": False,
        },
        consultar_impostos,
    ),
    "consultar_proventos_anunciados": (
        "Proventos já anunciados na B3 e ainda não pagos para os ativos da carteira, com "
        "datas e valor estimado. Use para perguntas sobre o que o usuário vai receber.",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        consultar_proventos_anunciados,
    ),
}

#: Short labels for the chat's status line while a tool runs.
STATUS = {
    "consultar_posicoes": "Consultando suas posições…",
    "consultar_ativo": "Consultando o ativo…",
    "consultar_transacoes": "Consultando suas movimentações…",
    "consultar_proventos": "Consultando seus proventos…",
    "consultar_alocacao": "Consultando sua alocação…",
    "consultar_rentabilidade": "Consultando sua rentabilidade…",
    "consultar_proventos_anunciados": "Consultando proventos anunciados…",
    "consultar_impostos": "Consultando seus impostos…",
}


def status_for(name: str) -> str:
    return STATUS.get(name, "Consultando sua carteira…")


def anthropic_tools() -> list[dict]:
    """Custom-tool definitions for the Messages API, in registry order.

    The order is fixed: tools render first in the prompt, and a reordered list
    would invalidate the prompt cache on every turn. ``eager_input_streaming``
    streams inputs as generated — they are tiny here, and :func:`run_tool`
    validates them either way.
    """
    return [
        {"name": name, "description": description, "input_schema": schema,
         "eager_input_streaming": True}
        for name, (description, schema, _impl) in TOOLS.items()
    ]


def openai_tools() -> list[dict]:
    """The same tools in the OpenAI function-calling dialect."""
    return [
        {"type": "function",
         "function": {"name": name, "description": description, "parameters": schema}}
        for name, (description, schema, _impl) in TOOLS.items()
    ]


def mcp_tools() -> list[dict]:
    """The same tools as an MCP ``tools/list`` answer."""
    return [
        {"name": name, "description": description, "inputSchema": schema}
        for name, (description, schema, _impl) in TOOLS.items()
    ]


# ---------------------------------------------------------------------------
# Validation and dispatch


class ToolInputError(ValueError):
    """Arguments that do not match the tool's schema; the message is for the model."""


def _check(name: str, schema: dict, args: object) -> dict:
    if not isinstance(args, dict):
        raise ToolInputError("os argumentos devem ser um objeto JSON")
    properties = schema.get("properties", {})
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ToolInputError(f"argumentos desconhecidos: {', '.join(unknown)}")
    missing = [key for key in schema.get("required", []) if args.get(key) in (None, "")]
    if missing:
        raise ToolInputError(f"argumentos obrigatórios ausentes: {', '.join(missing)}")
    clean: dict = {}
    for key, value in args.items():
        if value is None:
            continue
        spec = properties[key]
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str) or not value.strip():
                raise ToolInputError(f"'{key}' deve ser um texto")
            if key in ("desde", "ate") and not _DATE.match(value):
                raise ToolInputError(f"'{key}' deve estar no formato AAAA-MM-DD")
            if key in ("desde", "ate"):
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ToolInputError(f"'{key}' não é uma data válida") from exc
            if "enum" in spec and value not in spec["enum"]:
                raise ToolInputError(f"'{key}' deve ser um de: {', '.join(spec['enum'])}")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ToolInputError(f"'{key}' deve ser true ou false")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolInputError(f"'{key}' deve ser um número inteiro")
            low, high = spec.get("minimum"), spec.get("maximum")
            if (low is not None and value < low) or (high is not None and value > high):
                raise ToolInputError(f"'{key}' deve estar entre {low} e {high}")
        elif kind == "array":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ToolInputError(f"'{key}' deve ser uma lista de textos")
            allowed = spec.get("items", {}).get("enum")
            bad = [item for item in value if allowed and item not in allowed]
            if bad:
                raise ToolInputError(f"valores inválidos em '{key}': {', '.join(bad)}")
        clean[key] = value
    return clean


def run_tool(portfolio_id: int, name: str, args: object) -> tuple[str, bool]:
    """Run one tool call; ``(result text, is_error)``.

    Opens its own database session: the chat streams from a generator that
    outlives the request's session. Never raises — every failure becomes an
    error result the model can read and work around.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return f"Ferramenta desconhecida: {name}", True
    _description, schema, impl = entry
    try:
        clean = _check(name, schema, args)
    except ToolInputError as exc:
        return f"Argumentos inválidos para {name}: {exc}", True
    try:
        with session_scope() as db:
            result = impl(PortfolioService(db, portfolio_id), **clean)
    except Exception:  # noqa: BLE001 — a failed read must not end the conversation
        logger.exception("ai tool %s failed", name)
        return f"A consulta {name} falhou no GumbInvest. Tente outra abordagem.", True
    text = json.dumps(_clean(result), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS]
            + ' … [resultado truncado: refine os filtros (ticker, datas, limite) para ver o restante]'
        )
    return text, False


def orientation(service: PortfolioService) -> dict:
    """The short map the system prompt keeps: totals plus each open position's weight.

    Enough to know what exists and how big it is without a tool call — the
    detail (costs, results, income, ledger) is one call away.
    """
    items = sorted(
        service.asset_positions(include_closed=False),
        key=lambda ap: ap.market_value_base,
        reverse=True,
    )
    total = sum((ap.market_value_base for ap in items), Decimal(0))
    return _clean({
        "resumo_da_carteira": service.overview(),
        "posicoes_abertas": [
            {
                "ticker": ap.asset.ticker,
                "classe": ap.asset.kind,
                "alocacao_pct": (ap.market_value_base / total * 100) if total else 0,
            }
            for ap in items
        ],
    })
