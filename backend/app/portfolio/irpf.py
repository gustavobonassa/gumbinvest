"""The IRPF worksheet: one year of the ledger, in the shape the form asks for.

This produces a *worksheet to transcribe*, never a filing. Every figure keeps
its link back to the movements it came from, and anything the ledger cannot
answer is listed as a gap rather than filled with a plausible number — a
declaration is exactly the wrong place to guess.

Three things the form wants that no other page here needs:

* **Acquisition cost, not market value.** *Bens e Direitos* is declared at what
  was paid, on 31/12, with the previous 31/12 in the column beside it. Both fall
  out of replaying the ledger to a date, which the engine already does.
* **A payer, by CNPJ.** Income is declared per payer, so a dividend is not one
  line but one line *per company*. See :func:`_cnpj_of`.
* **Which pot each kind of income falls in.** Dividends and FII yields are
  exempt; JCP and fixed-income interest were taxed at source and are declared
  separately. The ledger already keeps the gross figure and the withholding
  apart, which is the whole distinction.

The grupo/código table is **data, not logic** — see :data:`BENS_CODES`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.dates import local_today
from app.core.logging import get_logger
from app.db.models import Asset, AssetUniverse, Transaction
from app.domain.enums import AssetKind, OperationType, PositionEffect
from app.portfolio.engine import build_timeline

logger = get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)
#: Below this a residue is float dust, not money.
DUST = Decimal("0.01")

#: Grupo/código per asset class, as the Receita's *Bens e Direitos* table lists
#: them. **Data, deliberately**: the Receita reshuffled these groups in 2023 and
#: gave criptoativos a group of their own, so the mapping is a table someone can
#: correct in one place — the same reasoning as the importer's classifier. The
#: worksheet shows every code beside the position it was applied to and asks the
#: reader to check it against the year's published layout, because a code that
#: has moved is invisible in a number that still looks reasonable.
#:
#: Keyed by declaration year, newest first; a year with no entry of its own
#: inherits the closest earlier one, so a new layout is one dict away.
BENS_CODES: dict[int, dict[str, tuple[str, str]]] = {
    2024: {
        AssetKind.STOCK.value: ("03", "01"),
        AssetKind.STOCK_INTL.value: ("03", "01"),
        AssetKind.FII.value: ("07", "03"),
        AssetKind.REIT.value: ("07", "03"),
        AssetKind.ETF.value: ("07", "04"),
        AssetKind.ETF_INTL.value: ("07", "04"),
        AssetKind.FIXED_INCOME.value: ("04", "02"),
        AssetKind.TREASURY.value: ("04", "02"),
        AssetKind.CRYPTO.value: ("08", "02"),
        AssetKind.STABLECOIN.value: ("08", "03"),
        AssetKind.OPTION.value: ("04", "04"),
        AssetKind.FUTURE.value: ("04", "04"),
    },
}
#: What a class is called on the form, for the reader who is checking the code.
GROUP_LABELS: dict[str, str] = {
    "03": "Participações societárias",
    "04": "Aplicações e investimentos",
    "06": "Depósito à vista e dinheiro em espécie",
    "07": "Fundos",
    "08": "Criptoativos",
    "99": "Outros bens e direitos",
}
#: A hand-kept bank balance is not a paper. Declared as the account it is, and
#: at the balance the *bank* states on 31/12 — see the gap this raises below.
CASH_ACCOUNT_CODE = ("06", "01")
#: Anything with no entry at all. Visible rather than omitted.
FALLBACK_CODE = ("99", "99")

#: Bitcoin has a código of its own; every other coin shares one.
BITCOIN_TICKERS = {"BTC", "WBTC"}
BITCOIN_CODE = ("08", "01")

#: Income the form treats as exempt, per class of payer.
EXEMPT_TYPES = {OperationType.DIVIDEND.value, OperationType.YIELD.value}
#: Income already taxed at source, declared separately and never added to the
#: exempt pot: JCP carries 15 % withheld, fixed income its own table.
EXCLUSIVE_TYPES = {OperationType.JCP.value, OperationType.INTEREST.value}

#: Sales up to this much per month, in ações negociadas à vista, are exempt.
#: Quoted so the worksheet can *show* the test rather than assert its outcome —
#: the threshold is the taxpayer's to apply, and it does not cover FIIs.
EQUITY_EXEMPTION_MONTHLY = Decimal(20_000)


def available_years(service) -> list[int]:
    """Calendar years the ledger has anything to declare for, newest first."""
    first = service.db.scalar(
        select(Transaction.trade_date)
        .where(Transaction.portfolio_id == service.portfolio_id)
        .order_by(Transaction.trade_date)
        .limit(1)
    )
    if first is None:
        return []
    # Only years that have finished: a declaration is of a closed year, and half
    # a year of movements under a "2026" heading reads as a complete one.
    last = local_today().year - 1
    return list(range(last, first.year - 1, -1)) if last >= first.year else []


def codes_for(year: int) -> dict[str, tuple[str, str]]:
    """The grupo/código table in force for a declaration year."""
    applicable = [known for known in sorted(BENS_CODES, reverse=True) if known <= year]
    return BENS_CODES[applicable[0]] if applicable else BENS_CODES[max(BENS_CODES)]


@dataclass(slots=True)
class _Held:
    """One asset's position at the close of a given 31 December."""

    quantity: Decimal
    cost: Decimal


def _state_on(timeline: list, day: date) -> dict[int, _Held]:
    """Quantity and cost per asset at the end of ``day``.

    Cost, not value: it is what the form asks for, and it is the one figure that
    does not depend on a price series being complete.
    """
    point = None
    for candidate in timeline:
        if candidate.day > day:
            break
        point = candidate
    if point is None:
        return {}
    return {
        asset_id: _Held(quantity=quantity, cost=point.costs.get(asset_id, ZERO))
        for asset_id, quantity in point.quantities.items()
        if quantity > ZERO
    }


def _realized_on(timeline: list, day: date) -> dict[int, Decimal]:
    """Cumulative realised result per asset at the end of ``day``."""
    point = None
    for candidate in timeline:
        if candidate.day > day:
            break
        point = candidate
    return dict(point.realized_by_asset) if point is not None else {}


def _registry_cnpjs(service) -> dict[str, str]:
    """CNPJ per ticker, as B3 and the CVM publish it."""
    rows = service.db.execute(
        select(AssetUniverse.ticker, AssetUniverse.cnpj).where(AssetUniverse.cnpj.is_not(None))
    ).all()
    return {ticker: cnpj for ticker, cnpj in rows if cnpj}


def _cnpj_of(asset: Asset, registry: dict[str, str]) -> str | None:
    """The payer's CNPJ: the hand-entered one first, then the registry."""
    return asset.cnpj or registry.get(asset.ticker)


def _format_cnpj(digits: str | None) -> str | None:
    if not digits or len(digits) != 14:
        return digits
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def _code_for(asset: Asset, table: dict[str, tuple[str, str]]) -> tuple[str, str]:
    if asset.is_cash_account:
        return CASH_ACCOUNT_CODE
    if asset.ticker.upper() in BITCOIN_TICKERS:
        return BITCOIN_CODE
    return table.get(asset.kind, FALLBACK_CODE)


#: Coins are quoted in dollars and held wherever their custodian is. The two
#: are unrelated, and conflating them declares a wallet as a bem no exterior on
#: the strength of a quote currency.
CRYPTO_KINDS = {AssetKind.CRYPTO.value, AssetKind.STABLECOIN.value}


def _is_crypto(asset: Asset) -> bool:
    return asset.kind in CRYPTO_KINDS


def _is_foreign(asset: Asset, base_currency: str) -> bool:
    """Whether the asset itself sits abroad.

    Currency is the only signal the ledger has, and for a share or a fund it is
    a good one. For a criptoativo it is no signal at all: everything trades
    against the dollar, and where it is *held* is a fact about the custodian
    that nothing here knows. Those are reported as their own gap instead.
    """
    if _is_crypto(asset):
        return False
    return (asset.currency or base_currency).upper() != base_currency.upper()


def _quantity_text(quantity: Decimal) -> str:
    """Quantities read as a person would write them, not as Decimal stores them."""
    trimmed = quantity.normalize()
    text = format(trimmed, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _discriminacao(asset: Asset, held: _Held, cnpj: str | None, foreign: bool) -> str:
    """The free-text line the form wants, written out so it can be pasted.

    Deliberately verbose in the way the Receita's own examples are: quantity,
    what it is, who issued it, and where it is held. Where the ledger does not
    know the last of those — a coin's custodian — the line says so rather than
    asserting a location, because the sentence is transcribed as written.
    """
    parts = [f"{_quantity_text(held.quantity)} {asset.ticker}"]
    if asset.name and asset.name.upper() != asset.ticker.upper():
        parts.append(f"- {asset.name}")
    if cnpj:
        parts.append(f"- CNPJ {_format_cnpj(cnpj)}")
    if foreign:
        parts.append("- ativo no exterior")
    elif _is_crypto(asset):
        parts.append("- custodiado em [informe a corretora/carteira]")
    return " ".join(parts)


def worksheet(service, year: int) -> dict:
    """Everything the declaration asks of this portfolio for ``year``."""
    base_currency = service.base_currency
    assets = service.assets()
    registry = _registry_cnpjs(service)
    table = codes_for(year)
    timeline = build_timeline(service.base_movements(), service.successions())

    closing = date(year, 12, 31)
    opening = date(year - 1, 12, 31)
    now = _state_on(timeline, closing)
    before = _state_on(timeline, opening)

    bens = _bens_e_direitos(assets, registry, table, base_currency, now, before, service, closing)
    income = _income(service, assets, registry, base_currency, year)
    sales = _sales(service, assets, base_currency, year, timeline, opening, closing)
    return {
        "year": year,
        "closing": closing,
        "opening": opening,
        "base_currency": base_currency,
        "bens": bens,
        "groups": GROUP_LABELS,
        "isentos": income["isentos"],
        "exclusiva": income["exclusiva"],
        "exterior": income["exterior"],
        "sales": sales,
        "gaps": _gaps(service, assets, bens, income, base_currency, year),
    }


def _bens_e_direitos(
    assets: dict[int, Asset],
    registry: dict[str, str],
    table: dict[str, tuple[str, str]],
    base_currency: str,
    now: dict[int, _Held],
    before: dict[int, _Held],
    service,
    closing: date,
) -> list[dict]:
    """One row per position held at either 31/12.

    A position sold during the year keeps its row with a closing cost of zero:
    the form compares the two columns, and a line that simply disappears is what
    an unexplained drop in património looks like to the Receita.
    """
    rows: list[dict] = []
    for asset_id in set(now) | set(before):
        asset = assets.get(asset_id)
        if asset is None:
            continue
        held = now.get(asset_id, _Held(ZERO, ZERO))
        previously = before.get(asset_id, _Held(ZERO, ZERO))
        if abs(held.cost) < DUST and abs(previously.cost) < DUST:
            continue
        cnpj = _cnpj_of(asset, registry)
        foreign = _is_foreign(asset, base_currency)
        grupo, codigo = _code_for(asset, table)
        # A CDB's accrued value is quoted beside its cost, never instead of it:
        # which of the two belongs on the form is a judgement the informe
        # settles, and the worksheet should not make it silently.
        accrued = service.accrued_value_on(asset_id, closing) if held.quantity > ZERO else None
        rows.append(
            {
                "ticker": asset.ticker,
                "name": asset.name,
                "kind": asset.kind,
                "grupo": grupo,
                "codigo": codigo,
                "cnpj": _format_cnpj(cnpj),
                "country": None if not foreign else (asset.currency or "").upper(),
                "is_foreign": foreign,
                "quantity": held.quantity,
                "cost": held.cost,
                "cost_previous": previously.cost,
                "accrued_value": accrued,
                "discriminacao": _discriminacao(asset, held, cnpj, foreign),
            }
        )
    rows.sort(key=lambda row: (row["grupo"], row["codigo"], row["ticker"]))
    return rows


def _income(
    service, assets: dict[int, Asset], registry: dict[str, str], base_currency: str, year: int
) -> dict[str, list[dict]]:
    """Income for the year, split into the pots the form keeps apart.

    Foreign income is pulled out of both: a dividend from a US company is not
    exempt here, and filing it under *Rendimentos Isentos* because a Brazilian
    dividend would be is the kind of mistake that survives every check.
    """
    gross: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for day, asset_id, op_type, amount in service._income_rows():
        if day.year == year:
            gross[(asset_id, op_type)] += amount
    withheld: dict[int, Decimal] = defaultdict(Decimal)
    for day, asset_id, op_type, amount in service._income_cost_rows():
        if day.year == year and op_type == OperationType.TAX.value:
            withheld[asset_id] += amount

    isentos: list[dict] = []
    exclusiva: list[dict] = []
    exterior: list[dict] = []
    for (asset_id, op_type), amount in gross.items():
        asset = assets.get(asset_id)
        if asset is None or abs(amount) < DUST:
            continue
        row = {
            "ticker": asset.ticker,
            "name": asset.name,
            "kind": asset.kind,
            "cnpj": _format_cnpj(_cnpj_of(asset, registry)),
            "op_type": op_type,
            "gross": amount,
            "withheld": ZERO,
            "net": amount,
        }
        if _is_foreign(asset, base_currency):
            exterior.append(row)
        elif op_type in EXEMPT_TYPES:
            isentos.append(row)
        elif op_type in EXCLUSIVE_TYPES:
            exclusiva.append(row)
        else:
            isentos.append(row)

    # Withholding is reported as its own movement naming no payment type, so it
    # is attached back to the asset's taxed lines — in proportion to what each
    # paid, because that is how it was taken in the first place. Splitting it
    # evenly would move tax from a large dividend onto a small one.
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for row in exclusiva + exterior:
        by_asset[row["ticker"]].append(row)
    for asset_id, tax in withheld.items():
        asset = assets.get(asset_id)
        if asset is None or abs(tax) < DUST:
            continue
        targets = by_asset.get(asset.ticker)
        if not targets:
            continue
        total = sum((row["gross"] for row in targets), ZERO)
        for row in targets:
            share = (row["gross"] / total) if total else (ONE / Decimal(len(targets)))
            row["withheld"] += tax * share
            row["net"] = row["gross"] - row["withheld"]

    for bucket in (isentos, exclusiva, exterior):
        bucket.sort(key=lambda row: -row["gross"])
    return {"isentos": isentos, "exclusiva": exclusiva, "exterior": exterior}


def _sales(
    service,
    assets: dict[int, Asset],
    base_currency: str,
    year: int,
    timeline: list,
    opening: date,
    closing: date,
) -> dict:
    """Disposals per month, and the year's realised result per class.

    The monthly figure exists so the R$ 20.000 test can be *shown* rather than
    decided: the worksheet reports what was sold and leaves the conclusion to
    the person filing, because the threshold covers ações à vista and not FIIs,
    and only the taxpayer knows what else went through elsewhere.
    """
    rows = service.db.execute(
        select(Transaction.trade_date, Transaction.asset_id, Transaction.gross_amount, Transaction.fx_rate)
        .where(
            Transaction.portfolio_id == service.portfolio_id,
            Transaction.effect == PositionEffect.DISPOSE.value,
        )
        .order_by(Transaction.trade_date)
    ).all()

    monthly: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for day, asset_id, amount, rate in rows:
        if day.year != year:
            continue
        asset = assets.get(asset_id)
        # Taking money out of a bank account is not a disposal, however the
        # replay has to book it to keep the balance right. Leaving it in put
        # R$ 44 mil of "vendas" in a month that sold nothing.
        if asset is None or asset.is_cash_account:
            continue
        bucket = _sale_bucket(asset, base_currency)
        monthly[f"{day.year}-{day.month:02d}"][bucket] += Decimal(amount or 0) * Decimal(rate or 1)

    started = _realized_on(timeline, opening)
    ended = _realized_on(timeline, closing)
    result: dict[str, Decimal] = defaultdict(Decimal)
    for asset_id in set(started) | set(ended):
        asset = assets.get(asset_id)
        if asset is None or asset.is_cash_account:
            continue
        moved = ended.get(asset_id, ZERO) - started.get(asset_id, ZERO)
        if abs(moved) >= DUST:
            result[_sale_bucket(asset, base_currency)] += moved

    return {
        "exemption_limit": EQUITY_EXEMPTION_MONTHLY,
        "months": [
            {"period": period, **{k: v for k, v in sorted(buckets.items())}}
            for period, buckets in sorted(monthly.items())
        ],
        "result_by_bucket": dict(sorted(result.items())),
    }


def _sale_bucket(asset: Asset, base_currency: str) -> str:
    """Which exemption rule a disposal answers to.

    Four regimes run side by side in this ledger and only one of them is the
    familiar R$ 20.000: FIIs have no exemption at all, crypto has a threshold of
    its own, and anything abroad is a different law entirely.
    """
    # Crypto first: it is quoted in dollars, so testing "foreign" ahead of it
    # sent every coin into the exterior bucket and quietly applied the wrong
    # exemption to it.
    if _is_crypto(asset):
        return "cripto"
    if _is_foreign(asset, base_currency):
        return "exterior"
    if asset.kind in {AssetKind.FII.value}:
        return "fii"
    if asset.kind in {AssetKind.FIXED_INCOME.value, AssetKind.TREASURY.value}:
        return "renda_fixa"
    return "acoes"


def _gaps(
    service, assets: dict[int, Asset], bens: list[dict], income: dict, base_currency: str, year: int
) -> list[dict]:
    """What this worksheet cannot answer, and why.

    The point of the page. A declaration assembled from an incomplete ledger is
    wrong in a way that looks exactly like a complete one, so every hole is
    named here with the position it belongs to.
    """
    gaps: list[dict] = []

    # A coin has no CNPJ to miss — its custodian does, and that is its own gap.
    crypto_tickers = {asset.ticker for asset in assets.values() if _is_crypto(asset)}
    missing_cnpj = sorted(
        {
            row["ticker"]
            for row in bens + income["isentos"] + income["exclusiva"]
            if not row.get("cnpj")
            and not row.get("is_foreign")
            and row["ticker"] not in crypto_tickers
        }
    )
    if missing_cnpj:
        gaps.append(
            {
                "kind": "cnpj",
                "title": "Sem CNPJ do pagador",
                "tickers": missing_cnpj,
                "detail": (
                    "A declaração identifica cada emissor pelo CNPJ. Estes não constam "
                    "do registro da B3/CVM — informe o CNPJ na página do ativo "
                    "(normalmente está no informe de rendimentos da corretora)."
                ),
            }
        )

    uncosted = sorted(
        assets[asset_id].ticker
        for asset_id, position in service.positions().items()
        if asset_id in assets and (position.uncosted_quantity > ZERO or position.uncosted_proceeds > DUST)
    )
    if uncosted:
        gaps.append(
            {
                "kind": "cost",
                "title": "Custo de aquisição desconhecido",
                "tickers": uncosted,
                "detail": (
                    "Estas unidades entraram por depósito externo sem compra correspondente "
                    "no histórico, então não há custo a declarar. O valor precisa vir de "
                    "onde elas foram compradas — informar zero declara um ganho que não é seu."
                ),
            }
        )

    cash = sorted(asset.ticker for asset in assets.values() if asset.is_cash_account)
    if cash:
        gaps.append(
            {
                "kind": "cash",
                "title": "Contas informadas à mão",
                "tickers": cash,
                "detail": (
                    "O saldo aqui é uma projeção pelo CDI, não um extrato. Declare o saldo "
                    "que o banco informa em 31/12; o valor calculado serve só de conferência."
                ),
            }
        )

    held_crypto = sorted({row["ticker"] for row in bens if row["ticker"] in crypto_tickers})
    if held_crypto:
        gaps.append(
            {
                "kind": "cripto",
                "title": "Criptoativos: falta dizer onde estão",
                "tickers": held_crypto,
                "detail": (
                    "A discriminação precisa nomear a custódia — a corretora (com CNPJ, se "
                    "for brasileira) ou a carteira própria. O extrato só diz a quantidade e "
                    "a moeda de cotação, e cotação em dólar não quer dizer bem no exterior."
                ),
            }
        )

    foreign = sorted({row["ticker"] for row in bens if row["is_foreign"]})
    if foreign:
        gaps.append(
            {
                "kind": "exterior",
                "title": "Bens e rendimentos no exterior",
                "tickers": foreign,
                "detail": (
                    "O custo em reais está calculado pela cotação de cada compra, mas a "
                    "apuração anual dos rendimentos no exterior (Lei 14.754/2023) não é "
                    "feita aqui. Confira com o informe da corretora."
                ),
            }
        )

    # JCP is always taxed 15 % at source, so a JCP line with no withholding
    # beside it means the export did not carry the tax — not that none was
    # taken. Whether the amount on file is the gross or what actually landed is
    # exactly what the informe settles, and it changes the figure declared.
    untaxed_jcp = sorted(
        {
            row["ticker"]
            for row in income["exclusiva"]
            if row["op_type"] == OperationType.JCP.value and abs(row["withheld"]) < DUST
        }
    )
    if untaxed_jcp:
        gaps.append(
            {
                "kind": "jcp",
                "title": "JCP sem IRRF informado",
                "tickers": untaxed_jcp,
                "detail": (
                    "O JCP tem 15 % retidos na fonte, e o extrato da B3 não traz a retenção "
                    "em separado. Confira no informe da corretora se o valor acima é o bruto "
                    "ou o líquido antes de declarar."
                ),
            }
        )

    unreported = sorted(
        {
            assets[asset_id].ticker
            for asset_id, position in service.positions().items()
            if asset_id in assets
            for warning in position.warnings
            if "sem valor de resgate" in warning
        }
    )
    if unreported:
        gaps.append(
            {
                "kind": "redemption",
                "title": "Vencimento sem valor de resgate",
                "tickers": unreported,
                "detail": (
                    "O papel venceu e o extrato não trouxe o valor creditado, então o "
                    "rendimento não entra em 'tributação exclusiva'. Lance o resgate "
                    "para que ele apareça."
                ),
            }
        )
    return gaps
