"""Aporte inteligente: an AI split of the day's contribution.

Unlike Carteira IA, the model here sees the user's real positions on purpose —
splitting new money across what is already held is the feature. The run is a
background job (web search takes minutes; the browser may leave): POST starts
it, GET polls, and the result lingers in the registry for an hour. Nothing is
persisted and nothing is executed — the reply is advice, not trades.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import CurrentPortfolio, DbSession
from app.core.logging import get_logger
from app.db.models import SmartInvestRun
from app.db.session import session_scope
from app.services import ai_research as research
from app.services import ai_wallet, smart_invest
from app.services.ai_providers import active_ai, is_configured, unavailable_reason
from app.services.jobs import BackgroundJob, JobConflict, JobRegistry, job_payload
from app.services.user_profile import user_intro

router = APIRouter(prefix="/smart-invest", tags=["smart invest"])
logger = get_logger(__name__)

#: One aporte analysis per portfolio at a time.
_REGISTRY = JobRegistry()


class InvestPayload(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: Literal["BRL", "USD"] = "BRL"
    kinds: list[str] = Field(min_length=1)


SYSTEM_ADVISOR = """Você é o consultor de aportes do GumbInvest. Aqui você VÊ a carteira REAL \
do usuário: as posições que ele já possui, com preço médio, preço atual, alocação e fundamentos. \
O usuário informa quanto dinheiro novo tem para investir HOJE e em quais categorias aceita \
investir. Sua tarefa é distribuir esse aporte entre os ativos QUE ELE JÁ POSSUI nas categorias \
selecionadas — nunca proponha ativos novos.

Princípios:
- O usuário escolheu as categorias de propósito: em geral, distribua o aporte entre TODAS \
as categorias selecionadas — o equilíbrio entre elas faz parte da decisão. Deixar uma \
categoria selecionada sem nada só com um motivo forte, explicado explicitamente no campo \
"strategy".
- Dentro de cada categoria o aporte NÃO deve ser dividido igualmente: concentre no que faz \
mais sentido AGORA, comparando preço atual com fundamentos, resultados recentes, notícias, \
dividendos e o cenário macro do bloco fornecido.
- Aporte é a chance de melhorar preço médio e rebalancear sem vender: dê peso a posições \
descontadas com tese intacta e a categorias/ativos abaixo do peso razoável na carteira.
- Não é obrigatório contemplar todos os ativos; um ativo com tese deteriorada deve receber 0 \
(e não aparecer na resposta).
- Se nada estiver atrativo, deixe parte do valor de fora — ficará em caixa; explique na \
estratégia.
- A soma das alocações NÃO PODE ultrapassar o valor do aporte.
- Justificativas específicas e comparativas (por que este ativo e não os outros da lista), \
1 a 3 frases, citando números do contexto ou fatos recentes.

Regra de formato ABSOLUTA: responda APENAS com JSON válido, sem markdown, sem cercas de \
código, sem nenhum texto antes ou depois do JSON."""

SEARCH_CLAUSE = (
    "Pesquise na web antes de decidir: resultados recentes, notícias e fatos relevantes "
    "dos ativos listados, e o momento de cada setor."
)


def _invest_prompt(
    context: list[dict], macro: dict, amount: Decimal, currency: str, labels: list[str], search: bool
) -> str:
    parts = []
    if search:
        parts.append(SEARCH_CLAUSE)
    parts.append(f"Cenário macro (fonte: GumbInvest, agora):\n{json.dumps(macro, ensure_ascii=False)}")
    parts.append(
        "Posições atuais do usuário nas categorias selecionadas (fonte: GumbInvest, agora):\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )
    parts.append(
        f"Aporte de hoje: {float(amount):.2f} {currency}, para distribuir entre as categorias: "
        f"{', '.join(labels)}.\n"
        f"Responda neste formato, com os valores em {currency}:\n"
        '{"strategy": "sua visão geral da decisão em 2 a 4 frases", '
        '"allocations": [{"ticker": "XXXX", "amount": 123.45, '
        '"rationale": "por que este ativo merece esta fatia agora"}]}'
    )
    return "\n\n".join(parts)


@router.get("/options", response_model=None, summary="Categories the portfolio can receive an aporte in")
def invest_options(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    return {"categories": smart_invest.available_categories(db, portfolio.id)}


@router.get("", response_model=None, summary="Status of the running (or last) aporte analysis")
def invest_job(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    return job_payload(_REGISTRY.current(portfolio.id))


@router.get("/history", response_model=None, summary="Past aporte analyses, newest first")
def invest_history(db: DbSession, portfolio: CurrentPortfolio) -> list[dict]:
    return smart_invest.list_runs(db, portfolio.id)


@router.delete("/history/{run_id}", response_model=None, summary="Delete one past analysis")
def delete_invest_run(run_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    row = db.get(SmartInvestRun, run_id)
    if row is None or row.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="análise não encontrada")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.post("", response_model=None, summary="Start the aporte analysis (background job)")
def start_invest(payload: InvestPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    # Always the pair chosen in Configurações — this tool deliberately has no
    # picker of its own; one global choice drives every one-shot AI feature.
    provider_id, provider, model, api_key = active_ai(db)
    if not is_configured(provider):
        raise HTTPException(
            status_code=503,
            detail=f"{unavailable_reason(provider)} Necessário para o aporte inteligente.",
        )

    kinds = [kind for kind in dict.fromkeys(payload.kinds) if kind in smart_invest.ELIGIBLE_KINDS]
    if not kinds:
        raise HTTPException(status_code=422, detail="Nenhuma categoria válida selecionada.")

    amount = payload.amount.quantize(Decimal("0.01"))
    currency = payload.currency
    context, facts = smart_invest.invest_context(db, portfolio.id, kinds, currency)
    if not context:
        raise HTTPException(
            status_code=409, detail="Você não possui ativos nas categorias selecionadas."
        )
    macro = ai_wallet.macro_context(db)
    labels = [smart_invest.ELIGIBLE_KINDS[kind] for kind in kinds]
    search = research.supports_search(provider_id, model)
    prompt = _invest_prompt(context, macro, amount, currency, labels, search)
    intro = user_intro(db)
    if intro:
        prompt = f"{intro}\n\n{prompt}"
    status_label = f"Analisando seus ativos com {provider['label']} · {model}…"
    portfolio_id = portfolio.id

    def run(job: BackgroundJob) -> None:
        job.status = status_label
        data, used_search = research.call_model_json(
            provider_id, provider, model, api_key, SYSTEM_ADVISOR,
            [{"role": "user", "content": prompt}],
        )
        if data is None:
            job.error = (
                "O modelo não retornou uma resposta válida — tente novamente ou escolha outro modelo."
            )
            return
        items, skipped = smart_invest.normalize_allocations(data, facts, amount)
        allocated = sum((item["amount"] for item in items), Decimal(0))
        strategy = str(data.get("strategy") or "").strip() or None
        if strategy:
            strategy = research.decode_escapes(strategy)
        job.result = {
            "amount": amount,
            "currency": currency,
            "kinds": kinds,
            "categories": labels,
            "strategy": strategy,
            "allocations": smart_invest.enrich_allocations(items, facts),
            "leftover": amount - allocated,
            "skipped": skipped,
            "used_search": used_search,
            "provider": provider_id,
            "provider_label": provider["label"],
            "model": model,
            "generated_at": datetime.now(UTC),
        }
        # Durable copy: the registry forgets the result in an hour, the
        # history keeps it. Own session — the request's is long gone.
        job.status = "Registrando no histórico…"
        with session_scope() as job_db:
            smart_invest.record_run(job_db, portfolio_id, job.result)

    try:
        job = _REGISTRY.start(
            portfolio.id,
            "invest",
            run,
            error_message="Erro inesperado na análise do aporte. Veja os logs do backend.",
            logger=logger,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Já existe uma análise de aporte em andamento.")
    return job_payload(job)
