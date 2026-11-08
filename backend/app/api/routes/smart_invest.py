"""Aporte inteligente: an AI split of the day's contribution.

Unlike Carteira IA, the model here sees the user's real positions on purpose —
splitting new money across what is already held is the feature. Three opt-ins
shape the run: free-text notes, categories as a menu rather than a quota, and
new assets (market-verified in the job, see ``smart_invest.verify_new_assets``).
The run is a
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
from app.market.fx import load_table
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
    #: Free-text instructions for this run ("não quero PETR4"). Optional —
    #: blank means the prompt is exactly what it was without the field.
    notes: str | None = Field(default=None, max_length=1000)
    #: True keeps the original rule (spread across every selected category);
    #: False makes the selection a menu the model may pick one or more from.
    require_all_categories: bool = True
    #: Lets the model propose assets the user does not hold yet.
    allow_new_assets: bool = False


ADVISOR_INTRO = """Você é o consultor de aportes do GumbInvest. Aqui você VÊ a carteira REAL \
do usuário: as posições que ele já possui, com preço médio, preço atual, alocação e fundamentos. \
O usuário informa quanto dinheiro novo tem para investir HOJE e em quais categorias aceita \
investir. Escreva todos os textos da resposta (estratégia, justificativas) em português \
brasileiro, mesmo quando as fontes pesquisadas estiverem em inglês."""

TASK_HELD_ONLY = """Sua tarefa é distribuir esse aporte entre os ativos QUE ELE JÁ POSSUI nas \
categorias selecionadas — nunca proponha ativos novos."""

TASK_ALLOW_NEW = """Sua tarefa é distribuir esse aporte nas categorias selecionadas, \
priorizando os ativos QUE ELE JÁ POSSUI. Você também PODE sugerir ativos novos (que ele ainda \
não tem), mas não é obrigado a sugerir nenhum: só quando um ativo novo fizer claramente mais \
sentido para esta carteira e para o momento atual do que reforçar as posições existentes \
(diversificação que falta, setor ausente, oportunidade evidente). Na dúvida, fique com o que \
ele já possui. Regras para ativos novos:
- Somente tickers reais e negociados hoje, dentro das categorias selecionadas: B3 (ex.: \
PETR4, HGLG11), bolsa americana (ex.: AAPL, VNQ) ou o símbolo da criptomoeda (ex.: BTC). \
Nunca renda fixa nem Tesouro novos.
- No máximo 3 ativos novos, marcados com "novo": true na alocação.
- A justificativa deve dizer por que o ativo novo e não reforçar o que ele já tem.
Tickers que não existirem no mercado ou estiverem fora das categorias serão descartados."""

CATEGORIES_REQUIRED = """- O usuário escolheu as categorias de propósito: em geral, distribua \
o aporte entre TODAS as categorias selecionadas — o equilíbrio entre elas faz parte da decisão. \
Deixar uma categoria selecionada sem nada só com um motivo forte, explicado explicitamente no \
campo "strategy"."""

CATEGORIES_FREE = """- As categorias selecionadas são PERMITIDAS, não obrigatórias: decida \
você se o aporte vai para uma, algumas ou todas elas. Pese o equilíbrio atual entre as classes \
da carteira, o perfil do usuário e o momento do mundo (juros, câmbio, inflação, ciclo de cada \
mercado). Concentrar tudo numa só categoria é uma resposta válida, e dividir igualmente entre \
elas não é um ponto de partida. Explique a escolha entre categorias no campo "strategy"."""

ADVISOR_PRINCIPLES = """- Dentro de cada categoria o aporte NÃO deve ser dividido igualmente: \
concentre no que faz mais sentido AGORA, comparando preço atual com fundamentos, resultados \
recentes, notícias, dividendos e o cenário macro do bloco fornecido.
- Aporte é a chance de melhorar preço médio e rebalancear sem vender: dê peso a posições \
descontadas com tese intacta e a categorias/ativos abaixo do peso razoável na carteira.
- Não é obrigatório contemplar todos os ativos; um ativo com tese deteriorada deve receber 0 \
(e não aparecer na resposta).
- Se nada estiver atrativo, deixe parte do valor de fora — ficará em caixa; explique na \
estratégia.
- A soma das alocações NÃO PODE ultrapassar o valor do aporte.
- Justificativas específicas e comparativas (por que este ativo e não os outros da lista), \
1 a 3 frases, citando números do contexto ou fatos recentes.
- Se o usuário escrever observações para este aporte, elas prevalecem sobre os princípios \
acima (nunca sobre o teto do valor nem sobre o formato): um ativo que ele recusar recebe 0.

Regra de formato ABSOLUTA: responda APENAS com JSON válido, sem markdown, sem cercas de \
código, sem nenhum texto antes ou depois do JSON."""


def _system_prompt(require_all_categories: bool, allow_new_assets: bool) -> str:
    """The advisor's rules for this run — the defaults reproduce the original prompt."""
    task = TASK_ALLOW_NEW if allow_new_assets else TASK_HELD_ONLY
    categories = CATEGORIES_REQUIRED if require_all_categories else CATEGORIES_FREE
    return f"{ADVISOR_INTRO} {task}\n\nPrincípios:\n{categories}\n{ADVISOR_PRINCIPLES}"


SEARCH_CLAUSE = (
    "Pesquise na web antes de decidir: resultados recentes, notícias e fatos relevantes "
    "dos ativos listados, e o momento de cada setor."
)
SEARCH_CLAUSE_NEW = (
    " Se for considerar ativos novos, confirme na busca que o ticker existe e é negociado hoje."
)


def _invest_prompt(
    context: list[dict],
    macro: dict,
    amount: Decimal,
    currency: str,
    labels: list[str],
    search: bool,
    *,
    notes: str = "",
    allow_new_assets: bool = False,
) -> str:
    parts = []
    if search:
        parts.append(SEARCH_CLAUSE + (SEARCH_CLAUSE_NEW if allow_new_assets else ""))
    parts.append(f"Cenário macro (fonte: GumbInvest, agora):\n{json.dumps(macro, ensure_ascii=False)}")
    parts.append(
        "Posições atuais do usuário nas categorias selecionadas (fonte: GumbInvest, agora):\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )
    if notes:
        parts.append(f"Observações do usuário para este aporte:\n{notes}")
    allocation = (
        '{"ticker": "XXXX", "amount": 123.45, '
        '"rationale": "por que este ativo merece esta fatia agora"'
    )
    if allow_new_assets:
        allocation += ', "novo": false'
    parts.append(
        f"Aporte de hoje: {float(amount):.2f} {currency}, para distribuir entre as categorias: "
        f"{', '.join(labels)}.\n"
        f"Responda neste formato, com os valores em {currency}:\n"
        '{"strategy": "sua visão geral da decisão em 2 a 4 frases", '
        f'"allocations": [{allocation}}}]}}'
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
    notes = (payload.notes or "").strip()
    require_all = payload.require_all_categories
    # Renda fixa/Tesouro alone have no market a new ticker could come from.
    allow_new = payload.allow_new_assets and any(
        kind in smart_invest.NEW_ASSET_KINDS for kind in kinds
    )
    system = _system_prompt(require_all, allow_new)
    prompt = _invest_prompt(
        context, macro, amount, currency, labels, search,
        notes=notes, allow_new_assets=allow_new,
    )
    usd_brl = load_table(db).latest if allow_new else None
    intro = user_intro(db)
    if intro:
        prompt = f"{intro}\n\n{prompt}"
    status_label = f"Analisando seus ativos com {provider['label']} · {model}…"
    portfolio_id = portfolio.id

    def run(job: BackgroundJob) -> None:
        job.status = status_label
        data, used_search = research.call_model_json(
            provider_id, provider, model, api_key, system,
            [{"role": "user", "content": prompt}],
        )
        if data is None:
            job.error = (
                "O modelo não retornou uma resposta válida — tente novamente ou escolha outro modelo."
            )
            return
        known, aliases = facts, {}
        if allow_new:
            job.status = "Conferindo os ativos novos no mercado…"
            new_facts, aliases = smart_invest.verify_new_assets(
                data, facts, kinds, currency, usd_brl
            )
            known = {**facts, **new_facts}
        items, skipped = smart_invest.normalize_allocations(data, known, amount, aliases)
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
            "allocations": smart_invest.enrich_allocations(items, known),
            "leftover": amount - allocated,
            "skipped": skipped,
            "used_search": used_search,
            "notes": notes or None,
            "require_all_categories": require_all,
            "allow_new_assets": allow_new,
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
