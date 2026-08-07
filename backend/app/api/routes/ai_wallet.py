"""Carteira IA: virtual wallets generated and managed by an AI model.

Generation and suggestion runs take minutes (web search + per-ticker market
verification), so they execute as background jobs on a worker pool: POST
starts the job and returns at once, GET .../job reports progress, and the run
finishes whether or not the browser is still watching. All state changes go
through :mod:`app.services.ai_wallet`; job runners never touch the request
session — every DB access opens its own ``session_scope``.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession
from app.core.config import settings
from app.core.dates import local_today
from app.core.logging import get_logger
from app.db.models import AiWallet, AiWalletCategory, AiWalletEvent, AiWalletSnapshot, AiWalletSuggestion
from app.db.session import session_scope
from app.market import benchmarks
from app.services import ai_research as research
from app.services import ai_wallet as wallets
from app.services import universe as screener
from app.services.ai_providers import AI_PROVIDERS, active_ai
from app.services.jobs import BackgroundJob, JobConflict, JobRegistry, job_payload

router = APIRouter(prefix="/ai-wallets", tags=["ai-wallets"])
logger = get_logger(__name__)

ZERO = Decimal(0)


class CreateWalletPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: Omitted -> the provider/model currently active in Configurações.
    provider: str | None = Field(default=None, max_length=24)
    model: str | None = Field(default=None, max_length=120)


#: Shared persona for every model turn. The wallet is explicitly virtual and
#: explicitly NOT the user's portfolio — the model never sees that one.
SYSTEM_MANAGER = """Você é o gestor de uma carteira de investimentos VIRTUAL do GumbInvest, \
criada para testar sua capacidade de alocação — não é a carteira real do usuário e você não \
tem acesso a ela. Seu objetivo é maximizar o crescimento do patrimônio no LONGO PRAZO \
(anos, não semanas), com qualidade e diversificação.

Critérios de decisão:
- Fundamentos e resultados recentes das empresas e fundos: lucro, margens, endividamento, \
dividendos, vacância no caso de FIIs.
- Cenário macro: Selic/COPOM, inflação, câmbio e ciclo econômico — use o bloco de dados \
macro fornecido.
- Notícias e fatos relevantes recentes, e onde grandes gestores e investidores estão \
posicionados.
- Diversificação inteligente dentro da categoria: setores e teses diferentes, sem pulverizar.

Regra de formato ABSOLUTA: responda APENAS com JSON válido, sem markdown, sem cercas de \
código, sem nenhum texto antes ou depois do JSON."""

SEARCH_CLAUSE = (
    "Pesquise na web antes de escolher: resultados recentes, notícias, recomendações de "
    "casas de análise e movimentos de grandes investidores."
)

#: What each category may hold, in the model's language.
CATEGORY_HINTS = {
    "ACOES": "ações listadas na B3 (tickers como PETR4, VALE3, WEGE3)",
    "FII": "fundos imobiliários e Fiagros listados na B3 (tickers terminados em 11, como HGLG11)",
    "ETF": "ETFs listados na B3 (como BOVA11, IVVB11) e/ou ETFs internacionais (como VOO, QQQ, SCHD)",
    "STOCKS": "ações listadas nos EUA (tickers como AAPL, MSFT, GOOGL)",
    "REIT": "REITs americanos (tickers como O, PLD, VICI)",
    "CRIPTO": "criptomoedas (símbolos como BTC, ETH, SOL)",
}


def _get_wallet(db, wallet_id: int) -> AiWallet:
    wallet = db.get(AiWallet, wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail="carteira não encontrada")
    return wallet


def _wallet_ai(wallet: AiWallet) -> tuple[dict, str]:
    """(provider entry, api key) for the wallet's pinned provider."""
    entry = AI_PROVIDERS.get(wallet.provider)
    if entry is None:
        raise HTTPException(
            status_code=503,
            detail=f"O provedor '{wallet.provider}' desta carteira não está mais disponível.",
        )
    key = getattr(settings, entry["key_setting"], "")
    if not key:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Informe sua chave da {entry['label']} em Configurações → Sistema para "
                f"usar esta carteira (ela foi criada com {wallet.model})."
            ),
        )
    return entry, key


def _pending_by_category(db, wallet_id: int) -> dict[str, int]:
    rows = db.execute(
        select(AiWalletSuggestion.category, func.count())
        .where(AiWalletSuggestion.wallet_id == wallet_id, AiWalletSuggestion.status == "pending")
        .group_by(AiWalletSuggestion.category)
    ).all()
    return dict(rows)


def _wallet_summary_payload(db, wallet: AiWallet) -> dict:
    valuation = wallets.value_wallet(db, wallet)
    pending = _pending_by_category(db, wallet.id)
    entry = AI_PROVIDERS.get(wallet.provider)
    return {
        "id": wallet.id,
        "name": wallet.name,
        "provider": wallet.provider,
        "provider_label": entry["label"] if entry else wallet.provider,
        "model": wallet.model,
        "created_at": wallet.created_at,
        "value": valuation["value"],
        "invested": valuation["invested"],
        "return_pct": valuation["return_pct"],
        "categories_active": len(valuation["categories"]),
        "pending_suggestions": sum(pending.values()),
    }


@router.get("", response_model=None, summary="All AI wallets with live valuation")
def list_wallets(db: DbSession) -> list[dict]:
    rows = db.scalars(select(AiWallet).order_by(AiWallet.created_at)).all()
    return [_wallet_summary_payload(db, wallet) for wallet in rows]


@router.post("", response_model=None, summary="Create an AI wallet pinned to a chosen model")
def create_wallet(payload: CreateWalletPayload, db: DbSession) -> dict:
    if payload.provider:
        provider_id = payload.provider
        provider = AI_PROVIDERS.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=422, detail="Provedor de IA desconhecido.")
        model = (payload.model or "").strip() or provider["default_model"]
        api_key = getattr(settings, provider["key_setting"], "")
    else:
        provider_id, provider, model, api_key = active_ai(db)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Informe sua chave da {provider['label']} em Configurações → Sistema "
                "para criar uma carteira IA (Gemini e Groq têm nível gratuito)."
            ),
        )
    wallet = AiWallet(name=payload.name.strip(), provider=provider_id, model=model)
    db.add(wallet)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Já existe uma carteira com esse nome.")
    wallets.log_event(
        db,
        wallet.id,
        "wallet.created",
        provider=provider_id,
        model=model,
        detail={"name": wallet.name},
    )
    db.commit()
    db.refresh(wallet)
    return _wallet_summary_payload(db, wallet)


@router.get("/compare", response_model=None, summary="Profitability of every wallet vs CDI/IBOV")
def compare_wallets(
    db: DbSession, range_: Literal["1m", "3m", "6m", "1y", "max"] = Query("max", alias="range")
) -> dict:
    window_days = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}.get(range_)
    cutoff = local_today() - timedelta(days=window_days) if window_days else None

    all_wallets = db.scalars(select(AiWallet).order_by(AiWallet.created_at)).all()
    snapshots: dict[int, list[AiWalletSnapshot]] = {}
    day_set: set = set()
    for wallet in all_wallets:
        rows = db.scalars(
            select(AiWalletSnapshot)
            .where(AiWalletSnapshot.wallet_id == wallet.id)
            .order_by(AiWalletSnapshot.date)
        ).all()
        snapshots[wallet.id] = rows
        day_set.update(row.date for row in rows if cutoff is None or row.date >= cutoff)

    days = sorted(day_set)
    series = [
        {
            "key": f"w{wallet.id}",
            "wallet_id": wallet.id,
            "label": wallet.name,
            "provider": wallet.provider,
            "model": wallet.model,
            "benchmark": False,
        }
        for wallet in all_wallets
        if snapshots[wallet.id]
    ]

    rows_out: list[dict] = [{"date": day.isoformat()} for day in days]
    for wallet in all_wallets:
        stored = snapshots[wallet.id]
        if not stored:
            continue
        key = f"w{wallet.id}"
        index = 0
        factor = None
        base = None
        for position, day in enumerate(days):
            while index < len(stored) and stored[index].date <= day:
                factor = Decimal(stored[index].return_factor)
                index += 1
            if factor is None:
                continue  # before this wallet existed: no point drawn
            if base is None:
                base = factor
            rows_out[position][key] = float((factor / base - 1) * 100)

    for code in ("CDI", "IBOV"):
        values = benchmarks.series(db, code, days)
        if not values:
            continue
        series.append({"key": code, "label": code, "benchmark": True})
        for position, day in enumerate(days):
            if day in values:
                rows_out[position][code] = float(values[day])

    return {"series": series, "rows": rows_out}


@router.get("/{wallet_id}", response_model=None, summary="Full wallet detail with valuation")
def wallet_detail(wallet_id: int, db: DbSession) -> dict:
    wallet = _get_wallet(db, wallet_id)
    # Deferred buys settle on read: by the next page view after a quote
    # refresh, reservations have become shares.
    if wallets.settle_pending_positions(db, wallet):
        db.commit()
    valuation = wallets.value_wallet(db, wallet)
    pending = _pending_by_category(db, wallet.id)
    entry = AI_PROVIDERS.get(wallet.provider)

    categories = []
    for code, spec in wallets.CATEGORIES.items():
        block = valuation["categories"].get(code)
        if block is None:
            categories.append(
                {
                    "category": code,
                    "label": spec["label"],
                    "active": False,
                    "pending_suggestions": pending.get(code, 0),
                }
            )
        else:
            categories.append(
                {
                    **block,
                    "active": True,
                    "pending_suggestions": pending.get(code, 0),
                }
            )
    return {
        "id": wallet.id,
        "name": wallet.name,
        "provider": wallet.provider,
        "provider_label": entry["label"] if entry else wallet.provider,
        "model": wallet.model,
        "created_at": wallet.created_at,
        "web_search": research.supports_search(wallet.provider, wallet.model),
        "key_configured": bool(entry and getattr(settings, entry["key_setting"], "")),
        "totals": {
            "value": valuation["value"],
            "invested": valuation["invested"],
            "cash": valuation["cash"],
            "return_pct": valuation["return_pct"],
            "unpriced": valuation["unpriced"],
        },
        "categories": categories,
    }


@router.delete("/{wallet_id}", response_model=None, summary="Delete a wallet and its history")
def delete_wallet(wallet_id: int, db: DbSession) -> dict:
    wallet = _get_wallet(db, wallet_id)
    wallets.delete_wallet(db, wallet)
    return {"deleted": True}


@router.get("/{wallet_id}/events", response_model=None, summary="The wallet's audit trail")
def wallet_events(
    wallet_id: int,
    db: DbSession,
    category: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    _get_wallet(db, wallet_id)
    stmt = select(AiWalletEvent).where(AiWalletEvent.wallet_id == wallet_id)
    if category:
        stmt = stmt.where(AiWalletEvent.category == category.upper())
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AiWalletEvent.at.desc(), AiWalletEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": event.id,
                "at": event.at,
                "category": event.category,
                "action": event.action,
                "provider": event.provider,
                "model": event.model,
                "detail": event.detail,
            }
            for event in rows
        ],
    }


def _suggestion_payload(suggestion: AiWalletSuggestion) -> dict:
    return {
        "id": suggestion.id,
        "category": suggestion.category,
        "batch_id": suggestion.batch_id,
        "action": suggestion.action,
        "ticker": suggestion.ticker,
        "name": suggestion.name,
        "amount_brl": suggestion.amount_brl,
        "to_ticker": suggestion.to_ticker,
        "to_category": suggestion.to_category,
        "rationale": suggestion.rationale,
        "status": suggestion.status,
        "detail": suggestion.detail,
        "created_at": suggestion.created_at,
    }


@router.get("/{wallet_id}/suggestions", response_model=None, summary="Suggestions awaiting review")
def wallet_suggestions(
    wallet_id: int, db: DbSession, category: str | None = None, status: str = "pending"
) -> list[dict]:
    _get_wallet(db, wallet_id)
    stmt = select(AiWalletSuggestion).where(AiWalletSuggestion.wallet_id == wallet_id)
    if category:
        stmt = stmt.where(AiWalletSuggestion.category == category.upper())
    if status:
        # "failed" rows ride along with pending ones: the user must SEE what
        # the model proposed and why it was refused, not wonder about a gap.
        statuses = ["pending", "failed"] if status == "pending" else [status]
        stmt = stmt.where(AiWalletSuggestion.status.in_(statuses))
    rows = db.scalars(stmt.order_by(AiWalletSuggestion.id)).all()
    return [_suggestion_payload(row) for row in rows]


@router.post(
    "/{wallet_id}/suggestions/{suggestion_id}/accept",
    response_model=None,
    summary="Apply one suggestion at current market prices",
)
def accept_suggestion(wallet_id: int, suggestion_id: int, db: DbSession) -> dict:
    wallet = _get_wallet(db, wallet_id)
    suggestion = db.get(AiWalletSuggestion, suggestion_id)
    if suggestion is None or suggestion.wallet_id != wallet.id:
        raise HTTPException(status_code=404, detail="sugestão não encontrada")
    if suggestion.status != "pending":
        raise HTTPException(status_code=409, detail="Esta sugestão já foi resolvida.")
    try:
        applied = wallets.apply_suggestion(db, wallet, suggestion)
    except wallets.SuggestionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    return {"applied": wallets._jsonable(applied), "suggestion": _suggestion_payload(suggestion)}


@router.post(
    "/{wallet_id}/suggestions/{suggestion_id}/decline",
    response_model=None,
    summary="Decline one suggestion",
)
def decline_suggestion(wallet_id: int, suggestion_id: int, db: DbSession) -> dict:
    wallet = _get_wallet(db, wallet_id)
    suggestion = db.get(AiWalletSuggestion, suggestion_id)
    if suggestion is None or suggestion.wallet_id != wallet.id:
        raise HTTPException(status_code=404, detail="sugestão não encontrada")
    if suggestion.status != "pending":
        raise HTTPException(status_code=409, detail="Esta sugestão já foi resolvida.")
    suggestion.status = "declined"
    suggestion.resolved_at = datetime.now(UTC)
    wallets.log_event(
        db,
        wallet.id,
        "suggestion.declined",
        category=suggestion.category,
        provider=suggestion.provider,
        model=suggestion.model,
        detail={"suggestion_id": suggestion.id, "action": suggestion.action, "ticker": suggestion.ticker},
    )
    db.commit()
    return {"suggestion": _suggestion_payload(suggestion)}


# ---------------------------------------------------------------------------
# Background jobs: generation and suggestions (registry in app/services/jobs)

#: One live job per (wallet, category) — the registry doubles as the lock.
_REGISTRY = JobRegistry()


def _start_job(kind: str, wallet_id: int, category: str, runner) -> BackgroundJob:
    error_message = (
        "Erro inesperado na geração. Veja os logs do backend."
        if kind == "generate"
        else "Erro inesperado ao gerar sugestões. Veja os logs do backend."
    )
    try:
        return _REGISTRY.start(
            (wallet_id, category), kind, runner, error_message=error_message, logger=logger
        )
    except JobConflict:
        raise HTTPException(
            status_code=409, detail="Já existe uma operação da IA em andamento nesta categoria."
        )


def _cleanup_created_assets(created_ids: list[int]) -> None:
    """Drop verification-scratch Asset rows the finished run left unreferenced.

    Without this every generation would permanently add its rejected
    candidates to the scheduled quote refresh — the set must stay "what
    someone actually holds or watches". Own session (the job's are gone) and
    never fatal: a failed sweep costs bytes, not correctness.
    """
    if not created_ids:
        return
    try:
        with session_scope() as db:
            removed = wallets.cleanup_unused_assets(db, created_ids)
        if removed:
            logger.info("ai wallet: removed %s unused candidate assets", removed)
    except Exception:  # noqa: BLE001 — cleanup must never fail a finished job
        logger.exception("ai wallet: candidate asset cleanup failed")


def _model_call_json(call_args: dict, convo: list[dict], want_search: bool) -> tuple[dict | None, bool]:
    """The shared JSON turn (ai_research), fed from this route's call_args."""
    return research.call_model_json(
        call_args["provider_id"],
        call_args["entry"],
        call_args["model"],
        call_args["api_key"],
        call_args["system"],
        convo,
        want_search,
    )


def _phase_a_prompt(category: str, search: bool, prescreened: list[dict] | None = None) -> str:
    hint = CATEGORY_HINTS[category]
    label = wallets.CATEGORIES[category]["label"]
    search_line = f"{SEARCH_CLAUSE}\n" if search else ""
    # With no local universe this block is empty and the prompt is byte-for-byte
    # what it was before the screener existed — asserted in the tests, because
    # a generation that works today must keep working for users who never run
    # the ingest.
    screen_line = ""
    if prescreened:
        screen_line = (
            f"Pré-selecionados pelo screener local do GumbInvest ({len(prescreened)} ativos, "
            "dados baixados das fontes oficiais B3/CVM):\n"
            f"{json.dumps(prescreened, ensure_ascii=False)}\n"
            "Esta lista NÃO é uma recomendação e NÃO é exaustiva: os números vêm do último "
            "balanço publicado e ativos legítimos podem estar de fora. Prefira candidatos "
            "desta lista; se um ativo fora dela for claramente melhor, inclua-o e explique "
            "o porquê em 'why'.\n"
        )
    return (
        f"Monte a lista de CANDIDATOS para a categoria {label} desta carteira virtual: "
        f"{hint}.\n{search_line}{screen_line}"
        "Escolha de 10 a 15 candidatos de qualidade, com teses variadas — a lista será "
        "verificada com dados reais de mercado e você fará a alocação final em seguida.\n"
        "Em 'why', registre os fatos recentes que sustentam o candidato (último resultado, "
        "notícia ou fato relevante, recomendação de analistas) — essas anotações são a sua "
        "memória para a decisão final, então seja específico.\n"
        'Responda com JSON neste formato: {"candidates": [{"ticker": "XXXX", '
        '"name": "Nome", "why": "fatos recentes em 1 a 2 frases"}]}'
    )


def _phase_b_prompt(verified: list[dict], rejected: list[dict]) -> str:
    rejected_line = (
        f"Candidatos REJEITADOS pela verificação (não use): {json.dumps(rejected, ensure_ascii=False)}\n"
        if rejected
        else ""
    )
    return (
        "Dados verificados dos candidatos (fonte: mercado, agora):\n"
        f"{json.dumps(verified, ensure_ascii=False)}\n{rejected_line}"
        "Monte a alocação FINAL da categoria com base nesses números: escolha de 4 a 8 "
        "ativos SOMENTE entre os candidatos verificados, somando no máximo 100% do "
        "orçamento de R$ 10.000 (o que sobrar fica em caixa).\n"
        "Em 'rationale' (2 a 4 frases por ativo): por que ELE e não os outros candidatos — "
        "compare explicitamente os números verificados com os concorrentes diretos —, o que "
        "os resultados recentes e notícias apontam (use suas anotações de 'why'), e o papel "
        "do ativo na estratégia.\n"
        "Em 'strategy' (2 a 4 frases): a estratégia geral da categoria — o cenário macro "
        "considerado, o equilíbrio buscado entre as posições e o que faria você mudá-la. "
        "Essa estratégia será lembrada nas futuras revisões da carteira.\n"
        'Responda com JSON neste formato: {"strategy": "...", "positions": [{"ticker": "XXXX", '
        '"name": "Nome", "allocation_pct": 20, "rationale": "..."}]}'
    )


def _renda_fixa_prompt(search: bool) -> str:
    search_line = f"{SEARCH_CLAUSE}\n" if search else ""
    return (
        "Monte a alocação de RENDA FIXA desta carteira virtual: de 2 a 4 papéis sintéticos "
        "(CDB/LCI/LCA/Tesouro), somando no máximo 100% do orçamento de R$ 10.000.\n"
        f"{search_line}"
        "Indexadores disponíveis: CDI ou SELIC (percent_of_index, ex.: 110), IPCA "
        "(spread_annual, ex.: 6), PRE (fixed_rate_annual, ex.: 12). Use taxas realistas "
        "para o mercado brasileiro atual.\n"
        "Em 'strategy' (2 a 4 frases): a estratégia da alocação — mix de indexadores diante "
        "do cenário de juros/inflação e o que faria você mudá-la. Será lembrada nas revisões.\n"
        'Responda com JSON neste formato: {"strategy": "...", "positions": [{"name": "CDB 110% CDI", '
        '"index_code": "CDI", "percent_of_index": 110, "spread_annual": 0, '
        '"fixed_rate_annual": 0, "allocation_pct": 40, "rationale": "..."}]}'
    )


def _suggest_prompt(category: str, search: bool, prescreened: list[dict] | None = None) -> str:
    label = wallets.CATEGORIES[category]["label"]
    search_line = f"{SEARCH_CLAUSE}\n" if search else ""
    # Without this the model can only propose a *new* holding from memory —
    # the same blind spot the generation path had before the screener.
    screen_line = ""
    if prescreened:
        screen_line = (
            f"Candidatos do screener local do GumbInvest ({len(prescreened)} ativos, dados "
            "oficiais B3/CVM), caso proponha comprar algo novo:\n"
            f"{json.dumps(prescreened, ensure_ascii=False)}\n"
            "Não é recomendação nem lista exaustiva; um ativo de fora é aceitável se for "
            "claramente melhor.\n"
        )
    return (
        f"Avalie a categoria {label} desta carteira virtual (posições, preços atuais e "
        f"resultado estão no contexto) e proponha mudanças, se fizerem sentido.\n"
        f"{search_line}{screen_line}"
        "Ações possíveis:\n"
        "- buy_new: comprar um ativo novo NESTA categoria (amount_brl limitado ao caixa)\n"
        "- increase: reforçar uma posição existente (ticker + amount_brl)\n"
        "- reduce: reduzir uma posição (ticker + amount_brl)\n"
        "- sell_all: zerar uma posição (ticker)\n"
        "- rebalance: mover amount_brl de uma posição (ticker) para: outra posição já "
        "existente em qualquer categoria (to_ticker + to_category), um ativo novo NESTA "
        "categoria (to_ticker), ou o caixa de outra categoria já gerada (apenas "
        "to_category). Ativo novo em OUTRA categoria não é permitido.\n"
        "Mantenha coerência com a estratégia que você definiu para a categoria (campo "
        "'estrategia_da_categoria' no contexto); se os dados exigirem mudá-la, diga isso na "
        "justificativa.\n"
        "Cada sugestão será aceita ou recusada individualmente pelo usuário — proponha "
        "poucas mudanças, de alta convicção, com justificativa nos dados. Se a categoria "
        'está bem posicionada, responda {"suggestions": []}.\n'
        'Formato: {"suggestions": [{"action": "buy_new|increase|reduce|sell_all|rebalance", '
        '"ticker": "XXXX", "amount_brl": 1500, "to_ticker": null, "to_category": null, '
        '"rationale": "..."}]}'
    )


def _system_with_context(category: str, macro: dict, summary: dict, extra: dict | None = None) -> str:
    context = {
        "categoria": category,
        "orcamento_da_categoria_brl": 10000,
        "macro": macro,
        "carteira_virtual": summary,
    }
    if extra:
        context.update(extra)
    return f"{SYSTEM_MANAGER}\n\nContexto atual (fonte: GumbInvest, agora):\n{json.dumps(context, ensure_ascii=False)}"


@router.get(
    "/{wallet_id}/categories/{category}/job",
    response_model=None,
    summary="Status of the category's running (or last) AI job",
)
def category_job(wallet_id: int, category: str, db: DbSession) -> dict:
    category = category.upper()
    if category not in wallets.CATEGORIES:
        raise HTTPException(status_code=404, detail="categoria desconhecida")
    _get_wallet(db, wallet_id)
    return job_payload(_REGISTRY.current((wallet_id, category)))


@router.post(
    "/{wallet_id}/categories/{category}/generate",
    response_model=None,
    summary="Start generating the category's allocation (background job)",
)
def generate_category(wallet_id: int, category: str, db: DbSession) -> dict:
    category = category.upper()
    if category not in wallets.CATEGORIES:
        raise HTTPException(status_code=404, detail="categoria desconhecida")
    wallet = _get_wallet(db, wallet_id)
    entry, api_key = _wallet_ai(wallet)
    existing = db.scalar(
        select(AiWalletCategory).where(
            AiWalletCategory.wallet_id == wallet.id, AiWalletCategory.category == category
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="Categoria já gerada, use as sugestões para mudá-la."
        )

    # Everything the job needs, captured as plain values while the request
    # session is still alive — the runner only ever opens its own sessions.
    search_capable = research.supports_search(wallet.provider, wallet.model)
    call_args = {
        "provider_id": wallet.provider,
        "entry": entry,
        "model": wallet.model,
        "api_key": api_key,
        "system": _system_with_context(
            category, wallets.macro_context(db), wallets.wallet_summary(db, wallet)
        ),
    }
    status_label = f"Consultando {entry['label']} · {wallet.model}…"
    captured_wallet_id = wallet.id
    # Read here, on the request's session — the runner opens its own. Returns
    # [] when the universe is off, empty or too thin, and never raises.
    prescreened = screener.category_screen(db, category) if category != "RENDA_FIXA" else []

    def run(job: BackgroundJob) -> None:
        created_ids: list[int] = []
        try:
            execute(job, created_ids)
        finally:
            _cleanup_created_assets(created_ids)

    def execute(job: BackgroundJob, created_ids: list[int]) -> None:
        job.status = status_label
        skipped: list[dict] = []

        if category == "RENDA_FIXA":
            convo = [{"role": "user", "content": _renda_fixa_prompt(search_capable)}]
            data, used_search = _model_call_json(call_args, convo, search_capable)
            items = wallets.normalize_generation(data, category)
        else:
            convo = [{"role": "user", "content": _phase_a_prompt(category, search_capable, prescreened)}]
            data, used_search = _model_call_json(call_args, convo, search_capable)
            candidates = wallets.normalize_candidates(data)
            if not candidates:
                job.error = "O modelo não retornou candidatos válidos, tente novamente ou troque o modelo em Configurações."
                return

            verified: list[dict] = []
            accepted: set[str] = set()
            for index, ticker in enumerate(candidates):
                job.status = f"Verificando {ticker} no mercado…"
                if index:
                    time.sleep(0.8)  # pace Yahoo calls; bursts get rate-limited
                try:
                    with session_scope() as job_db:
                        context, ok = wallets.candidate_context(job_db, category, ticker, created_ids)
                except Exception:  # noqa: BLE001 — one bad ticker must not kill the run
                    logger.exception("ai wallet: candidate %s failed", ticker)
                    context, ok = {"ticker": ticker, "erro": "falha ao verificar"}, False
                if ok:
                    verified.append(context)
                    accepted.add(context["ticker"])
                else:
                    skipped.append({"ticker": ticker, "reason": context.get("erro", "rejeitado")})
            if not verified:
                job.error = "Nenhum candidato passou na verificação de mercado, tente novamente."
                return

            job.status = "Montando a alocação final…"
            convo = convo + [
                {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                {"role": "user", "content": _phase_b_prompt(verified, skipped)},
            ]
            data, search_b = _model_call_json(call_args, convo, False)
            used_search = used_search or search_b
            items = [
                item
                for item in wallets.normalize_generation(data, category)
                if item["ticker"] in accepted
            ]

        if not items:
            job.error = "O modelo não retornou uma alocação válida, tente novamente ou troque o modelo em Configurações."
            return
        strategy = research.decode_escapes(str((data or {}).get("strategy") or "").strip()) or None

        job.status = "Executando as compras virtuais…"
        with session_scope() as job_db:
            live_wallet = job_db.get(AiWallet, captured_wallet_id)
            if live_wallet is None:
                job.error = "A carteira foi excluída durante a geração."
                return
            try:
                job.result = wallets.apply_generation(
                    job_db,
                    live_wallet,
                    category,
                    items,
                    used_search=used_search,
                    skipped=skipped,
                    strategy=strategy,
                )
            except IntegrityError:
                job_db.rollback()
                job.error = "Esta categoria já foi gerada em outra aba."

    return job_payload(_start_job("generate", wallet.id, category, run))


@router.post(
    "/{wallet_id}/categories/{category}/suggest",
    response_model=None,
    summary="Start asking the wallet's model for changes (background job)",
)
def suggest_changes(wallet_id: int, category: str, db: DbSession) -> dict:
    category = category.upper()
    if category not in wallets.CATEGORIES:
        raise HTTPException(status_code=404, detail="categoria desconhecida")
    wallet = _get_wallet(db, wallet_id)
    entry, api_key = _wallet_ai(wallet)
    cat_row = db.scalar(
        select(AiWalletCategory).where(
            AiWalletCategory.wallet_id == wallet.id, AiWalletCategory.category == category
        )
    )
    if cat_row is None:
        raise HTTPException(status_code=409, detail="Gere a carteira desta categoria primeiro.")

    valuation = wallets.value_wallet(db, wallet)
    block = valuation["categories"].get(category, {})
    positions_context = [
        {
            "ticker": item["ticker"],
            "quantidade": wallets._round2(item["quantity"]),
            "custo_brl": wallets._round2(item["cost_brl"]),
            "valor_atual_brl": wallets._round2(item["market_value_brl"]),
            "retorno_pct": wallets._round2(item["pnl_pct"]),
            "peso_pct": wallets._round2(item["weight_pct"]),
            "renda_fixa": item["fi_label"],
        }
        for item in block.get("positions", [])
    ]
    search_capable = research.supports_search(wallet.provider, wallet.model)
    call_args = {
        "provider_id": wallet.provider,
        "entry": entry,
        "model": wallet.model,
        "api_key": api_key,
        "system": _system_with_context(
            category,
            wallets.macro_context(db),
            wallets.wallet_summary(db, wallet),
            extra={
                "categoria_em_analise": {
                    "estrategia_da_categoria": cat_row.thesis,
                    "caixa_brl": wallets._round2(block.get("cash", ZERO)),
                    "posicoes": positions_context,
                }
            },
        ),
    }
    status_label = f"Consultando {entry['label']} · {wallet.model}…"
    captured = {
        "wallet_id": wallet.id,
        "provider": wallet.provider,
        "model": wallet.model,
    }
    # Read on the request's session, like the generation path: [] when the
    # universe is off or too thin, and never raising.
    prescreened = screener.category_screen(db, category)

    def run(job: BackgroundJob) -> None:
        created_ids: list[int] = []
        try:
            execute(job, created_ids)
        finally:
            _cleanup_created_assets(created_ids)

    def execute(job: BackgroundJob, created_ids: list[int]) -> None:
        job.status = status_label
        convo = [{"role": "user", "content": _suggest_prompt(category, search_capable, prescreened)}]
        data, used_search = _model_call_json(call_args, convo, search_capable)
        if data is None:
            job.error = "O modelo não retornou sugestões válidas, tente novamente."
            return
        items = wallets.normalize_suggestions(data, category)

        # Ground brand-new tickers with real data before storing anything:
        # the model confirms (or drops) its own picks against the numbers.
        new_tickers = sorted(
            {
                item["to_ticker"] if item["action"] == "rebalance" else item["ticker"]
                for item in items
                if (
                    (item["action"] == "buy_new" and category != "RENDA_FIXA" and item["ticker"])
                    or (
                        item["action"] == "rebalance"
                        and item["to_ticker"]
                        and (item["to_category"] or category) == category
                    )
                )
            }
        )
        checked: dict[str, bool] = {}
        if new_tickers and items:
            verified = []
            rejected = []
            for index, ticker in enumerate(new_tickers):
                job.status = f"Verificando {ticker} no mercado…"
                if index:
                    time.sleep(0.8)  # pace Yahoo calls; bursts get rate-limited
                try:
                    with session_scope() as job_db:
                        context, ok = wallets.candidate_context(job_db, category, ticker, created_ids)
                except Exception:  # noqa: BLE001
                    logger.exception("ai wallet: candidate %s failed", ticker)
                    context, ok = {"ticker": ticker, "erro": "falha ao verificar"}, False
                checked[ticker] = ok
                (verified if ok else rejected).append(context)
            if verified or rejected:
                job.status = "Revisando as sugestões com os dados…"
                convo = convo + [
                    {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                    {
                        "role": "user",
                        "content": (
                            "Dados verificados dos ativos novos que você propôs:\n"
                            f"{json.dumps(verified, ensure_ascii=False)}\n"
                            + (
                                f"Rejeitados pela verificação (não proponha): {json.dumps(rejected, ensure_ascii=False)}\n"
                                if rejected
                                else ""
                            )
                            + "Confirme ou revise sua lista final de sugestões, no mesmo formato JSON."
                        ),
                    },
                ]
                data, search_b = _model_call_json(call_args, convo, False)
                used_search = used_search or search_b
                items = wallets.normalize_suggestions(data, category)

        job.status = "Registrando as sugestões…"
        batch_id = str(uuid.uuid4())
        with session_scope() as job_db:
            live_wallet = job_db.get(AiWallet, captured["wallet_id"])
            if live_wallet is None:
                job.error = "A carteira foi excluída durante a análise."
                return
            stale = job_db.scalars(
                select(AiWalletSuggestion).where(
                    AiWalletSuggestion.wallet_id == live_wallet.id,
                    AiWalletSuggestion.category == category,
                    AiWalletSuggestion.status == "pending",
                )
            ).all()
            for row in stale:
                row.status = "superseded"
                row.resolved_at = datetime.now(UTC)

            stored = []
            for item in items:
                error = wallets.suggestion_target_error(job_db, live_wallet.id, category, item)
                if error is None and item["action"] == "buy_new" and category != "RENDA_FIXA":
                    if not checked.get(item["ticker"], True):
                        error = "ticker não encontrado no mercado"
                position = (
                    wallets._find_position(job_db, live_wallet.id, category, item["ticker"] or "")
                    if item["action"] != "buy_new"
                    else None
                )
                to_position = None
                if item["action"] == "rebalance" and item["to_ticker"]:
                    to_position = wallets._find_position(
                        job_db,
                        live_wallet.id,
                        item["to_category"] or category,
                        item["to_ticker"],
                    )
                suggestion = AiWalletSuggestion(
                    wallet_id=live_wallet.id,
                    category=category,
                    batch_id=batch_id,
                    action=item["action"],
                    ticker=item["ticker"],
                    name=item["name"],
                    amount_brl=item["amount_brl"],
                    to_ticker=item["to_ticker"],
                    to_category=item["to_category"],
                    to_position_id=to_position.id if to_position else None,
                    payload=wallets._jsonable(item["raw"]),
                    rationale=item["rationale"],
                    status="failed" if error else "pending",
                    detail=error,
                    position_id=position.id if position else None,
                    provider=captured["provider"],
                    model=captured["model"],
                )
                job_db.add(suggestion)
                job_db.flush()
                stored.append(_suggestion_payload(suggestion))
            wallets.log_event(
                job_db,
                live_wallet.id,
                "suggestion.batch",
                category=category,
                provider=captured["provider"],
                model=captured["model"],
                detail={
                    "batch_id": batch_id,
                    "count": len(stored),
                    "superseded": len(stale),
                    "used_search": used_search,
                },
            )
        job.result = {
            "suggestions": wallets._jsonable(stored),
            "used_search": used_search,
            "batch_id": batch_id,
        }

    return job_payload(_start_job("suggest", wallet.id, category, run))
