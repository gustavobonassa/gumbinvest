"""Corporate actions: declaring that one asset was replaced by another.

Three sources feed the same declaration: the movement-evidence heuristic
(:mod:`app.portfolio.corporate_actions`), the AI scan (a background job that
searches the web for events affecting the portfolio's own tickers and stores
proposals for individual accept/decline), and the manual form. The AI never
applies anything by itself.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.core.logging import get_logger
from app.db.models import (
    Asset,
    AssetSplit,
    AssetSuccession,
    AuditLog,
    SuccessionAiSuggestion,
    Transaction,
)
from app.db.session import session_scope
from app.market.service import MANUAL_SPLIT_SOURCE
from app.portfolio.corporate_actions import suggest_successions
from app.services import ai_research as research
from app.services import corporate_ai
from app.services.ai_providers import active_ai, is_configured, unavailable_reason
from app.services.jobs import BackgroundJob, JobConflict, JobRegistry, job_payload

router = APIRouter(prefix="/corporate-actions", tags=["corporate actions"])
logger = get_logger(__name__)

#: One AI scan per portfolio at a time.
_SCAN_REGISTRY = JobRegistry()
#: ...and one split lookup, which is a different question and may run alongside.
_SPLIT_REGISTRY = JobRegistry()


class SuccessionPayload(BaseModel):
    from_ticker: str
    #: ``None`` marks the asset an artifact: every movement of it is dropped.
    to_ticker: str | None = None
    effective_date: date
    cash_amount: Decimal = Field(default=Decimal(0), ge=0)
    note: str | None = None
    source: str = "manual"


def _serialize(db, row: AssetSuccession) -> dict:
    source = db.get(Asset, row.from_asset_id)
    target = db.get(Asset, row.to_asset_id) if row.to_asset_id else None
    return {
        "id": row.id,
        "from_ticker": source.ticker if source else None,
        "from_name": source.name if source else None,
        "to_ticker": target.ticker if target else None,
        "to_name": target.name if target else None,
        "effective_date": row.effective_date,
        "cash_amount": row.cash_amount,
        "note": row.note,
        "source": row.source,
    }


def _asset(db, ticker: str) -> Asset:
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {ticker} not found")
    return asset


@router.get("", response_model=None, summary="Declared successions and detected candidates")
def list_actions(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    rows = db.scalars(
        select(AssetSuccession)
        .where(AssetSuccession.portfolio_id == portfolio.id)
        .order_by(AssetSuccession.effective_date)
    ).all()
    return {
        "items": [_serialize(db, row) for row in rows],
        "suggestions": suggest_successions(db, portfolio.id),
    }


@router.post("", response_model=None, summary="Declare that an asset was replaced")
def create_action(payload: SuccessionPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    source = _asset(db, payload.from_ticker)
    target = _asset(db, payload.to_ticker) if payload.to_ticker else None
    if target is not None and target.id == source.id:
        raise HTTPException(status_code=422, detail="an asset cannot succeed itself")

    existing = db.scalar(
        select(AssetSuccession).where(
            AssetSuccession.portfolio_id == portfolio.id,
            AssetSuccession.from_asset_id == source.id,
        )
    )
    row = existing or AssetSuccession(portfolio_id=portfolio.id, from_asset_id=source.id)
    row.to_asset_id = target.id if target else None
    row.effective_date = payload.effective_date
    row.cash_amount = payload.cash_amount
    row.note = payload.note
    row.source = payload.source
    db.add(row)
    db.add(
        AuditLog(
            action="portfolio.succession",
            detail={
                "from": source.ticker,
                "to": target.ticker if target else None,
                "date": payload.effective_date.isoformat(),
                "cash": str(payload.cash_amount),
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.delete("/{action_id}", response_model=None, summary="Undo a declared succession")
def delete_action(action_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    row = db.get(AssetSuccession, action_id)
    if row is None or row.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="succession not found")
    db.delete(row)
    db.commit()
    return {"deleted": action_id}


# ---------------------------------------------------------------------------
# AI scan: the configured model searches for events affecting these tickers

SYSTEM_EVENTS = """Você é um pesquisador de eventos corporativos de bolsa (B3 e bolsas \
americanas). Sua tarefa é identificar, com alta confiança, eventos que afetaram os ativos \
listados no contexto: mudança de código de negociação (rename), incorporação/fusão com \
troca de papel, fechamento de capital/OPA com pagamento em dinheiro, e cisões relevantes.

Regras:
- Considere SOMENTE os tickers listados no contexto, e somente eventos a partir do primeiro \
movimento de cada um.
- Ignore os que já têm evento declarado (campo evento_ja_declarado).
- Pesquise na web e cite a fonte de cada evento. Proponha apenas o que você conseguir \
confirmar — um evento inventado corrompe o histórico do usuário.
- Datas no formato YYYY-MM-DD (data de eficácia na bolsa). Se houve pagamento em dinheiro \
por ação/cota no evento, informe o valor TOTAL aproximado por posição apenas se conhecido; \
caso contrário use 0.

Regra de formato ABSOLUTA: responda APENAS com JSON válido, sem markdown, sem cercas de \
código, sem nenhum texto antes ou depois do JSON."""


SYSTEM_SPLITS = """Você é um pesquisador de eventos societários de bolsa (B3 e bolsas \
americanas). Sua tarefa é listar os DESDOBRAMENTOS, GRUPAMENTOS e BONIFICAÇÕES EM AÇÕES de \
um único papel — eventos que mudam a quantidade de cotas sem entrada de dinheiro.

Regras:
- Responda a razão como "cotas depois por cota antes": desdobramento 1:6 vira 6; \
grupamento 10:1 vira 0.1; bonificação de 5% vira 1.05.
- Use a data EX (o dia em que o preço na bolsa passou a refletir o evento), YYYY-MM-DD.
- Pesquise e cite a fonte de cada evento. Só proponha o que conseguir confirmar: uma razão \
errada distorce todo o histórico de valor do usuário antes daquela data.
- NÃO inclua dividendos, JCP, rendimentos, subscrições nem fusões — apenas eventos de \
quantidade de cotas.
- Se não encontrar nada confiável, devolva uma lista vazia. Uma lista vazia é uma resposta \
melhor que um palpite.

Regra de formato ABSOLUTA: responda APENAS com JSON válido, sem markdown, sem cercas de \
código, sem nenhum texto antes ou depois do JSON."""


def _split_prompt(asset: Asset, first_trade: date | None, known: list[dict]) -> str:
    return (
        f"Papel: {asset.ticker} ({asset.name}), negociado em "
        f"{'B3' if (asset.currency or 'BRL').upper() == 'BRL' else 'bolsa dos EUA'}.\n"
        f"O usuário tem posição desde {first_trade.isoformat() if first_trade else 'data desconhecida'}; "
        "eventos anteriores a essa data não interessam.\n"
        f"Já registrados (não repita): {json.dumps(known, ensure_ascii=False)}\n"
        "Liste os eventos de quantidade de cotas neste formato:\n"
        '{"splits": [{"date": "YYYY-MM-DD", "ratio": 6, '
        '"event_type": "desdobramento|grupamento|bonificacao", '
        '"rationale": "o que aconteceu, em 1 frase", "source": "fonte"}]}\n'
        'Se não houver nenhum, responda {"splits": []}.'
    )


def _scan_prompt(context: list[dict]) -> str:
    return (
        "Ativos do usuário (fonte: GumbInvest, agora):\n"
        f"{json.dumps(context, ensure_ascii=False)}\n"
        "Liste os eventos corporativos encontrados neste formato:\n"
        '{"events": [{"from_ticker": "XXXX", "to_ticker": "YYYY ou null se baixado", '
        '"effective_date": "YYYY-MM-DD", "cash_amount": 0, '
        '"event_type": "rename|merger|delisting|spinoff|other", '
        '"rationale": "o que aconteceu, em 1 a 2 frases", "source": "fonte"}]}\n'
        'Se nenhum evento for encontrado, responda {"events": []}.'
    )


# ---------------------------------------------------------------------------
# Splits. A different animal from a succession: the asset stays the same, only
# the share count changes — and the reason it needs declaring at all is that a
# provider's price history is retro-adjusted for splits while the ledger counts
# the shares that existed on each day. Missing one values every earlier day at a
# fraction of the truth. The provider supplies most of them automatically (see
# app.market.service.sync_splits); this is for the ones it stays silent about.


class SplitPayload(BaseModel):
    ticker: str
    date: date
    #: Shares after per share before: 6 for a 6-for-1, 0.1 for a 1-for-10.
    ratio: Decimal = Field(gt=0)


@router.get("/splits", response_model=None, summary="Declared share splits")
def list_splits(db: DbSession, portfolio: CurrentPortfolio, svc: PortfolioSvc) -> list[dict]:
    """Every split known for assets this portfolio holds, newest first."""
    rows = db.execute(
        select(AssetSplit, Asset)
        .join(Asset, Asset.id == AssetSplit.asset_id)
        .join(Transaction, Transaction.asset_id == Asset.id)
        .where(Transaction.portfolio_id == portfolio.id)
        .group_by(AssetSplit.id, Asset.id)
        .order_by(AssetSplit.date.desc())
    ).all()
    # A declared ratio the traded prices contradict is not applied — and saying
    # so here is the whole point: silently dropping it would be the same sin as
    # silently applying it.
    rejected = svc.rejected_splits()
    return [
        {
            "id": split.id,
            "ticker": asset.ticker,
            "name": asset.name,
            "date": split.date,
            "ratio": split.ratio,
            "source": split.source,
            # Only what a person declared can be withdrawn here; a provider row
            # would simply come back on the next sync.
            "editable": split.source == MANUAL_SPLIT_SOURCE,
            "ignored_reason": rejected.get((asset.id, split.date)),
        }
        for split, asset in rows
    ]


@router.post("/splits", response_model=None, summary="Declare a share split")
def create_split(payload: SplitPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    asset = _asset(db, payload.ticker)
    if payload.ratio == Decimal(1):
        raise HTTPException(status_code=422, detail="a ratio of 1 changes nothing")
    # A split only ever does one thing: restate the closes used to value a
    # holding. Declared for a paper this portfolio never held, it would be
    # stored, do nothing, and not even appear in the list below.
    held = db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.portfolio_id == portfolio.id, Transaction.asset_id == asset.id
        )
    )
    if not held:
        raise HTTPException(
            status_code=409,
            detail=f"{asset.ticker} não tem movimentações nesta carteira, então um desdobramento nele não muda nada.",
        )
    existing = db.scalar(
        select(AssetSplit).where(
            AssetSplit.asset_id == asset.id, AssetSplit.date == payload.date
        )
    )
    row = existing or AssetSplit(asset_id=asset.id, date=payload.date)
    row.ratio = payload.ratio
    row.source = MANUAL_SPLIT_SOURCE
    db.add(row)
    db.add(
        AuditLog(
            action="portfolio.split",
            detail={
                "ticker": asset.ticker,
                "date": payload.date.isoformat(),
                "ratio": str(payload.ratio),
            },
        )
    )
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "ticker": asset.ticker,
        "name": asset.name,
        "date": row.date,
        "ratio": row.ratio,
        "source": row.source,
        "editable": True,
    }


@router.delete("/splits/{split_id}", response_model=None, summary="Remove a declared split")
def delete_split(split_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    row = db.get(AssetSplit, split_id)
    if row is None:
        raise HTTPException(status_code=404, detail="split not found")
    if row.source != MANUAL_SPLIT_SOURCE:
        raise HTTPException(
            status_code=409,
            detail="Este desdobramento veio do provedor de cotações e voltaria na próxima sincronização.",
        )
    db.delete(row)
    db.commit()
    return {"deleted": split_id}


@router.get("/splits/lookup", response_model=None, summary="Status of the AI split lookup")
def split_lookup_status(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    return job_payload(_SPLIT_REGISTRY.current(portfolio.id))


@router.post("/splits/lookup", response_model=None, summary="Ask the AI for a ticker's splits")
def start_split_lookup(ticker: str, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    """Fill the form rather than file a proposal.

    Splits are found in bulk by the provider already; the model is here for the
    ones it does not publish, so the useful shape is "I know the ticker, tell me
    its events" — the user still presses the button that writes the row.
    """
    provider_id, provider, model, api_key = active_ai(db)
    if not is_configured(provider):
        raise HTTPException(
            status_code=503,
            detail=f"{unavailable_reason(provider)} Necessário para buscar desdobramentos com IA.",
        )
    asset = _asset(db, ticker)
    known = [
        {"date": row.date.isoformat(), "ratio": str(row.ratio)}
        for row in db.scalars(
            select(AssetSplit).where(AssetSplit.asset_id == asset.id).order_by(AssetSplit.date)
        ).all()
    ]
    first_trade = db.scalar(
        select(func.min(Transaction.trade_date)).where(
            Transaction.portfolio_id == portfolio.id, Transaction.asset_id == asset.id
        )
    )

    def run(job: BackgroundJob) -> None:
        job.status = f"Pesquisando desdobramentos de {asset.ticker}…"
        data, used_search = research.call_model_json(
            provider_id, provider, model, api_key, SYSTEM_SPLITS, [
                {"role": "user", "content": _split_prompt(asset, first_trade, known)}
            ],
        )
        if data is None:
            job.error = "O modelo não retornou uma resposta válida, tente novamente ou troque o modelo em Configurações."
            return
        job.result = {
            "ticker": asset.ticker,
            "splits": corporate_ai.normalize_splits(data, known_dates={row["date"] for row in known}),
            "used_search": used_search,
        }

    try:
        job = _SPLIT_REGISTRY.start(
            portfolio.id,
            "split-lookup",
            run,
            error_message="Erro inesperado na busca de desdobramentos. Veja os logs do backend.",
            logger=logger,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Já existe uma busca em andamento.")
    return job_payload(job)


@router.get("/ai-scan", response_model=None, summary="Status of the AI corporate-event scan")
def ai_scan_status(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    return job_payload(_SCAN_REGISTRY.current(portfolio.id))


@router.post("/ai-scan", response_model=None, summary="Start the AI corporate-event scan (background job)")
def start_ai_scan(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    provider_id, provider, model, api_key = active_ai(db)
    if not is_configured(provider):
        raise HTTPException(
            status_code=503,
            detail=f"{unavailable_reason(provider)} Necessário para buscar eventos com IA.",
        )
    context, known = corporate_ai.scan_context(db, portfolio.id)
    if not context:
        raise HTTPException(
            status_code=409, detail="Nenhum ativo elegível para a busca de eventos."
        )
    portfolio_id = portfolio.id
    status_label = f"Pesquisando eventos com {provider['label']} · {model}…"

    def run(job: BackgroundJob) -> None:
        job.status = status_label
        data, used_search = research.call_model_json(
            provider_id, provider, model, api_key, SYSTEM_EVENTS, [
                {"role": "user", "content": _scan_prompt(context)}
            ],
        )
        if data is None:
            job.error = "O modelo não retornou uma resposta válida, tente novamente ou troque o modelo em Configurações."
            return
        items = corporate_ai.normalize_events(data, known)
        job.status = "Registrando as sugestões…"
        with session_scope() as job_db:
            stored = corporate_ai.store_suggestions(
                job_db, portfolio_id, items, provider=provider_id, model=model
            )
            job.result = {
                "found": len(items),
                "stored": len(stored),
                "suggestions": [corporate_ai.serialize_suggestion(row) for row in stored],
                "used_search": used_search,
            }

    try:
        job = _SCAN_REGISTRY.start(
            portfolio_id,
            "corporate-scan",
            run,
            error_message="Erro inesperado na busca de eventos. Veja os logs do backend.",
            logger=logger,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Já existe uma busca de eventos em andamento.")
    return job_payload(job)


@router.get("/ai-suggestions", response_model=None, summary="AI event proposals awaiting review")
def ai_suggestions(db: DbSession, portfolio: CurrentPortfolio) -> list[dict]:
    rows = db.scalars(
        select(SuccessionAiSuggestion)
        .where(
            SuccessionAiSuggestion.portfolio_id == portfolio.id,
            SuccessionAiSuggestion.status == "pending",
        )
        .order_by(SuccessionAiSuggestion.effective_date)
    ).all()
    return [corporate_ai.serialize_suggestion(row) for row in rows]


def _get_suggestion(db, portfolio_id: int, suggestion_id: int) -> SuccessionAiSuggestion:
    row = db.get(SuccessionAiSuggestion, suggestion_id)
    if row is None or row.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="sugestão não encontrada")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Esta sugestão já foi resolvida.")
    return row


@router.post(
    "/ai-suggestions/{suggestion_id}/accept",
    response_model=None,
    summary="Apply one AI event proposal as a declared succession",
)
def accept_ai_suggestion(suggestion_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    row = _get_suggestion(db, portfolio.id, suggestion_id)
    try:
        succession = corporate_ai.accept_suggestion(db, portfolio.id, row)
    except corporate_ai.SuggestionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    db.commit()
    db.refresh(succession)
    return {"succession": _serialize(db, succession), "suggestion": corporate_ai.serialize_suggestion(row)}


@router.post(
    "/ai-suggestions/{suggestion_id}/decline",
    response_model=None,
    summary="Decline one AI event proposal",
)
def decline_ai_suggestion(suggestion_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    row = _get_suggestion(db, portfolio.id, suggestion_id)
    row.status = "declined"
    row.resolved_at = datetime.now(UTC)
    db.commit()
    return {"suggestion": corporate_ai.serialize_suggestion(row)}


@router.get("/preview", response_model=None, summary="Positions affected by the declared successions")
def preview(service: PortfolioSvc, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    """What the successions changed, for a sanity check after applying them."""
    touched: set[int] = set()
    for row in db.scalars(
        select(AssetSuccession).where(AssetSuccession.portfolio_id == portfolio.id)
    ).all():
        touched.add(row.from_asset_id)
        if row.to_asset_id:
            touched.add(row.to_asset_id)

    assets = service.assets()
    positions = service.positions()
    return {
        "positions": [
            {
                "ticker": assets[asset_id].ticker,
                "quantity": position.quantity,
                "cost_basis": position.cost_basis,
                "average_price": position.average_price,
                "realized_pnl": position.realized_pnl,
                "returned_capital": position.returned_capital,
                "notes": position.notes,
            }
            for asset_id, position in positions.items()
            if asset_id in touched and asset_id in assets
        ]
    }
