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
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.core.logging import get_logger
from app.db.models import Asset, AssetSuccession, AuditLog, SuccessionAiSuggestion
from app.db.session import session_scope
from app.portfolio.corporate_actions import suggest_successions
from app.services import ai_research as research
from app.services import corporate_ai
from app.services.ai_providers import active_ai, is_configured, unavailable_reason
from app.services.jobs import BackgroundJob, JobConflict, JobRegistry, job_payload

router = APIRouter(prefix="/corporate-actions", tags=["corporate actions"])
logger = get_logger(__name__)

#: One AI scan per portfolio at a time.
_SCAN_REGISTRY = JobRegistry()


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
