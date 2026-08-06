"""Maps raw B3 movement labels onto domain operations.

Everything the calculation engine needs to know about a movement is decided
here. Supporting a new broker wording is a one-line addition to ``_RULES``.

Reference for the sample export (3.492 rows, 2020-2026) — every label found in
the wild is covered, and anything unknown degrades gracefully to
``UNKNOWN``/``NONE`` while being reported in the import log.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.enums import Direction, OperationType, PositionEffect
from app.importer.parser import normalize_key


@dataclass(frozen=True, slots=True)
class Classification:
    op_type: OperationType
    effect: PositionEffect
    #: Set when the movement should be reviewed by a human (unknown label,
    #: position restatement, unpaired custody transfer, ...).
    warning: str | None = None


def _pair(
    credit: tuple[OperationType, PositionEffect],
    debit: tuple[OperationType, PositionEffect],
) -> dict[Direction, tuple[OperationType, PositionEffect]]:
    return {Direction.CREDIT: credit, Direction.DEBIT: debit}


OT = OperationType
PE = PositionEffect

# ---------------------------------------------------------------------------
# label (accent-free, lowercase, alphanumeric) -> per-direction behaviour
# ---------------------------------------------------------------------------
_RULES: dict[str, dict[Direction, tuple[OperationType, PositionEffect]]] = {
    # --- trading -----------------------------------------------------------
    # B3 settles equity/FII trades as "Transferência - Liquidação".
    "transferencialiquidacao": _pair((OT.BUY, PE.ACQUIRE), (OT.SELL, PE.DISPOSE)),
    "liquidacao": _pair((OT.BUY, PE.ACQUIRE), (OT.SELL, PE.DISPOSE)),
    "compra": _pair((OT.BUY, PE.ACQUIRE), (OT.BUY, PE.ACQUIRE)),
    "venda": _pair((OT.SELL, PE.DISPOSE), (OT.SELL, PE.DISPOSE)),
    "compravenda": _pair((OT.BUY, PE.ACQUIRE), (OT.SELL, PE.DISPOSE)),
    "aplicacao": _pair((OT.BUY, PE.ACQUIRE), (OT.BUY, PE.ACQUIRE)),
    # Hand-entered bank balances (see app.portfolio.accounts). Given their own
    # labels rather than reusing B3's so the ledger stays honest about where a
    # movement came from, and so the start-up reclassification — which re-derives
    # every op_type from these strings — leaves them exactly as entered.
    "depositoemconta": _pair((OT.BUY, PE.ACQUIRE), (OT.BUY, PE.ACQUIRE)),
    "saqueemconta": _pair((OT.SELL, PE.DISPOSE), (OT.SELL, PE.DISPOSE)),
    # --- income ------------------------------------------------------------
    "dividendo": _pair((OT.DIVIDEND, PE.CASH_IN), (OT.DIVIDEND, PE.CASH_OUT)),
    "dividendos": _pair((OT.DIVIDEND, PE.CASH_IN), (OT.DIVIDEND, PE.CASH_OUT)),
    "jurossobrecapitalproprio": _pair((OT.JCP, PE.CASH_IN), (OT.JCP, PE.CASH_OUT)),
    "jurossobrecapitalpropriotransferido": _pair((OT.JCP, PE.CASH_IN), (OT.JCP, PE.CASH_OUT)),
    "rendimento": _pair((OT.YIELD, PE.CASH_IN), (OT.YIELD, PE.CASH_OUT)),
    "rendimentotransferido": _pair((OT.YIELD, PE.CASH_IN), (OT.YIELD, PE.CASH_OUT)),
    "pagamentodejuros": _pair((OT.INTEREST, PE.CASH_IN), (OT.INTEREST, PE.CASH_OUT)),
    "juros": _pair((OT.INTEREST, PE.CASH_IN), (OT.INTEREST, PE.CASH_OUT)),
    # --- capital returns (reduce the cost basis, not income) ---------------
    "amortizacao": _pair((OT.AMORTIZATION, PE.RETURN_OF_CAPITAL), (OT.AMORTIZATION, PE.CASH_OUT)),
    "restituicaodecapital": _pair(
        (OT.AMORTIZATION, PE.RETURN_OF_CAPITAL), (OT.AMORTIZATION, PE.CASH_OUT)
    ),
    "reducaodecapital": _pair((OT.AMORTIZATION, PE.RETURN_OF_CAPITAL), (OT.AMORTIZATION, PE.CASH_OUT)),
    # --- corporate actions: free quantity in/out ---------------------------
    # Cost basis is preserved, so the average price dilutes/concentrates
    # automatically — which is exactly what a split/reverse split does.
    "desdobro": _pair((OT.SPLIT, PE.QTY_IN_FREE), (OT.SPLIT, PE.QTY_OUT_FREE)),
    "desdobramento": _pair((OT.SPLIT, PE.QTY_IN_FREE), (OT.SPLIT, PE.QTY_OUT_FREE)),
    "grupamento": _pair((OT.REVERSE_SPLIT, PE.QTY_IN_FREE), (OT.REVERSE_SPLIT, PE.QTY_OUT_FREE)),
    "bonificacaoemativos": _pair((OT.BONUS, PE.QTY_IN_FREE), (OT.BONUS, PE.QTY_OUT_FREE)),
    "bonificacao": _pair((OT.BONUS, PE.QTY_IN_FREE), (OT.BONUS, PE.QTY_OUT_FREE)),
    "incorporacao": _pair((OT.MERGER, PE.QTY_IN_FREE), (OT.MERGER, PE.QTY_OUT_FREE)),
    "cisao": _pair((OT.MERGER, PE.QTY_IN_FREE), (OT.MERGER, PE.QTY_OUT_FREE)),
    "fusao": _pair((OT.MERGER, PE.QTY_IN_FREE), (OT.MERGER, PE.QTY_OUT_FREE)),
    "conversaodeativos": _pair((OT.MERGER, PE.QTY_IN_FREE), (OT.MERGER, PE.QTY_OUT_FREE)),
    "fracaoemativos": _pair((OT.FRACTION, PE.QTY_IN_FREE), (OT.FRACTION, PE.QTY_OUT_FREE)),
    # B3 removes the fraction first ("Fração em Ativos") and pays for it weeks
    # later ("Leilão de Fração"). The quantity is already gone by then, so the
    # auction only realises cash — treating it as a disposal would remove the
    # same fraction twice.
    "leilaodefracao": _pair((OT.FRACTION, PE.REALIZE), (OT.FRACTION, PE.QTY_OUT_FREE)),
    # --- subscriptions ------------------------------------------------------
    "direitodesubscricao": _pair((OT.SUBSCRIPTION, PE.QTY_IN_FREE), (OT.SUBSCRIPTION, PE.QTY_OUT_FREE)),
    "direitosdesubscricao": _pair((OT.SUBSCRIPTION, PE.QTY_IN_FREE), (OT.SUBSCRIPTION, PE.QTY_OUT_FREE)),
    "direitosobrasdesubscricao": _pair(
        (OT.SUBSCRIPTION, PE.QTY_IN_FREE), (OT.SUBSCRIPTION, PE.QTY_OUT_FREE)
    ),
    "recibodesubscricao": _pair((OT.SUBSCRIPTION, PE.QTY_IN_FREE), (OT.SUBSCRIPTION, PE.QTY_OUT_FREE)),
    "cessaodedireitos": _pair((OT.SUBSCRIPTION, PE.QTY_IN_FREE), (OT.SUBSCRIPTION, PE.QTY_OUT_FREE)),
    "cessaodedireitossolicitada": _pair(
        (OT.SUBSCRIPTION, PE.QTY_IN_FREE), (OT.SUBSCRIPTION, PE.QTY_OUT_FREE)
    ),
    "solicitacaodesubscricao": _pair((OT.SUBSCRIPTION, PE.NONE), (OT.SUBSCRIPTION, PE.NONE)),
    # Rights that reached the deadline unexercised. The export states the event
    # with an empty quantity, so this cannot be a plain debit — see
    # PositionEffect.QTY_EXPIRE.
    "direitosdesubscricaonaoexercido": _pair(
        (OT.SUBSCRIPTION, PE.QTY_EXPIRE), (OT.SUBSCRIPTION, PE.QTY_EXPIRE)
    ),
    "direitosobrasdesubscricaonaoexercido": _pair(
        (OT.SUBSCRIPTION, PE.QTY_EXPIRE), (OT.SUBSCRIPTION, PE.QTY_EXPIRE)
    ),
    # Exercising a right costs cash -> booked as an acquisition so the money
    # paid stays in the portfolio's invested capital (see docs/ARCHITECTURE.md).
    "direitosdesubscricaoexercido": _pair((OT.SUBSCRIPTION, PE.ACQUIRE), (OT.SUBSCRIPTION, PE.ACQUIRE)),
    "direitosobrasdesubscricaoexercido": _pair(
        (OT.SUBSCRIPTION, PE.ACQUIRE), (OT.SUBSCRIPTION, PE.ACQUIRE)
    ),
    # --- custody transfers between brokers ---------------------------------
    # Paired credit/debit rows are neutralised by the engine; unpaired ones
    # move quantity in/out at zero cost.
    "transferencia": _pair((OT.TRANSFER_IN, PE.QTY_IN_FREE), (OT.TRANSFER_OUT, PE.QTY_OUT_FREE)),
    "transferenciadecustodia": _pair((OT.TRANSFER_IN, PE.QTY_IN_FREE), (OT.TRANSFER_OUT, PE.QTY_OUT_FREE)),
    # --- fixed income lifecycle --------------------------------------------
    "vencimento": _pair((OT.REDEMPTION, PE.DISPOSE), (OT.REDEMPTION, PE.DISPOSE)),
    "vencimentoresgatesaldoemconta": _pair((OT.REDEMPTION, PE.DISPOSE), (OT.REDEMPTION, PE.DISPOSE)),
    "resgate": _pair((OT.REDEMPTION, PE.DISPOSE), (OT.REDEMPTION, PE.DISPOSE)),
    "resgatetotal": _pair((OT.REDEMPTION, PE.DISPOSE), (OT.REDEMPTION, PE.DISPOSE)),
    "resgateparcial": _pair((OT.REDEMPTION, PE.DISPOSE), (OT.REDEMPTION, PE.DISPOSE)),
    # --- costs --------------------------------------------------------------
    "taxa": _pair((OT.FEE, PE.CASH_IN), (OT.FEE, PE.CASH_OUT)),
    "taxadecustodia": _pair((OT.FEE, PE.CASH_IN), (OT.FEE, PE.CASH_OUT)),
    "emolumentos": _pair((OT.FEE, PE.CASH_IN), (OT.FEE, PE.CASH_OUT)),
    "corretagem": _pair((OT.FEE, PE.CASH_IN), (OT.FEE, PE.CASH_OUT)),
    "irrf": _pair((OT.TAX, PE.CASH_IN), (OT.TAX, PE.CASH_OUT)),
    "imposto": _pair((OT.TAX, PE.CASH_IN), (OT.TAX, PE.CASH_OUT)),
    "impostoderenda": _pair((OT.TAX, PE.CASH_IN), (OT.TAX, PE.CASH_OUT)),
    # --- position updates ---------------------------------------------------
    # "Atualização" is overloaded in B3 exports: sometimes it credits shares
    # (fund events, custody migrations), sometimes it merely restates the
    # position already held. The engine decides per row — see PE.QTY_SYNC.
    "atualizacao": _pair((OT.POSITION_UPDATE, PE.QTY_SYNC), (OT.POSITION_UPDATE, PE.QTY_SYNC)),
    "atualizacaodeposicao": _pair((OT.POSITION_UPDATE, PE.QTY_SYNC), (OT.POSITION_UPDATE, PE.QTY_SYNC)),
    # --- informational ------------------------------------------------------
    "saldo": _pair((OT.INFO, PE.NONE), (OT.INFO, PE.NONE)),
    "posicaoemcustodia": _pair((OT.INFO, PE.NONE), (OT.INFO, PE.NONE)),
    # --- US broker statements (see app.importer.pdf.movements) --------------
    # The PDF importers normalise every broker's wording to these labels, so
    # the whole portfolio — B3 and offshore — shares one classification table.
    "buy": _pair((OT.BUY, PE.ACQUIRE), (OT.BUY, PE.ACQUIRE)),
    "sell": _pair((OT.SELL, PE.DISPOSE), (OT.SELL, PE.DISPOSE)),
    "dividend": _pair((OT.DIVIDEND, PE.CASH_IN), (OT.DIVIDEND, PE.CASH_OUT)),
    # Non-resident withholding: a debit takes it, a credit refunds it. Both are
    # booked as tax so the asset's net income stays right either way.
    "dividendtaxwithheld": _pair((OT.TAX, PE.CASH_IN), (OT.TAX, PE.CASH_OUT)),
    "interest": _pair((OT.INTEREST, PE.CASH_IN), (OT.INTEREST, PE.CASH_OUT)),
    "fee": _pair((OT.FEE, PE.CASH_IN), (OT.FEE, PE.CASH_OUT)),
    "adrfee": _pair((OT.FEE, PE.CASH_IN), (OT.FEE, PE.CASH_OUT)),
    # ACATS / "clearing firm conversion": the same shares leaving one custodian
    # and arriving at another. The engine pairs the two sides and neutralises
    # them — see app.portfolio.engine.neutralize_paired_transfers.
    "custodytransfer": _pair((OT.TRANSFER_IN, PE.QTY_IN_FREE), (OT.TRANSFER_OUT, PE.QTY_OUT_FREE)),
    "merger": _pair((OT.MERGER, PE.QTY_IN_FREE), (OT.MERGER, PE.QTY_OUT_FREE)),
    "stocksplit": _pair((OT.SPLIT, PE.QTY_IN_FREE), (OT.SPLIT, PE.QTY_OUT_FREE)),
    # --- crypto exchanges (see app.importer.crypto) -------------------------
    # Exchanges charge the trading fee in a coin, not in cash — Binance takes it
    # out of the asset bought, or out of BNB when the account holds some. The
    # quantity therefore leaves the position carrying its share of the cost,
    # which is what paying a fee in kind is. Booking it as CASH_OUT would leave
    # the coins behind as a phantom holding that was never actually owned.
    "tradingfee": _pair((OT.FEE, PE.QTY_OUT_FREE), (OT.FEE, PE.QTY_OUT_FREE)),
    # Staking and Simple Earn rewards, airdrops, rebates and dust conversions:
    # coins arriving (or leaving) with no cash on either side. Free quantity is
    # the honest treatment — the app already books bonus shares this way, and
    # the average price dilutes without a price feed having to say what the
    # coins were worth at the moment they landed.
    "reward": _pair((OT.REWARD, PE.QTY_IN_FREE), (OT.REWARD, PE.QTY_OUT_FREE)),
    # Coins crossing the exchange's own boundary. Withdrawing to a wallet does
    # not sell anything, so the cost goes with them and waits; bringing them
    # back reclaims it. Accounts that use an exchange as a bridge — buy, with-
    # draw minutes later, deposit again months on, sell — would otherwise have
    # the cost of every purchase written off on the way out and the coins
    # returning free, turning the eventual sale into invented profit.
    "exchangedeposit": _pair((OT.TRANSFER_IN, PE.QTY_IN_PARKED), (OT.TRANSFER_OUT, PE.QTY_OUT_PARKED)),
    "exchangewithdrawal": _pair(
        (OT.TRANSFER_IN, PE.QTY_IN_PARKED), (OT.TRANSFER_OUT, PE.QTY_OUT_PARKED)
    ),
    # Coins moving into or out of Simple Earn / staking. They leave the balance
    # the exchange reports — which is why applying these literally is what
    # reconciles with it — and nothing else: they are still on the exchange,
    # still owned, still earning. So they stay in the position, and the cost
    # goes with them. Treating a subscription as an exit hid an entire holding:
    # every USDT on the reference account had been subscribed and never
    # redeemed, so a balance of 1.453 USDT reported as no position at all.
    "earntransfer": _pair((OT.TRANSFER_IN, PE.QTY_IN_STAKED), (OT.TRANSFER_OUT, PE.QTY_OUT_STAKED)),
    # Futures P&L and funding: settles in the margin coin, moves no instrument.
    "futuresresult": _pair((OT.DERIVATIVE, PE.QTY_IN_FREE), (OT.DERIVATIVE, PE.QTY_OUT_FREE)),
    # --- user corrections ---------------------------------------------------
    # The difference between a computed position and the balance the venue
    # actually shows. Some of what a portfolio holds is genuinely underivable
    # from an export: interest that compounds inside a staking product is paid
    # into the position, not itemised as a movement, so the balance drifts up
    # with nothing on file to explain it. Rather than let the position be
    # quietly wrong, the user states the real balance and the difference is
    # recorded as what it is — free quantity in, or quantity out at cost.
    # See ``POST /api/assets/{ticker}/reconcile``.
    "balanceadjustment": _pair(
        (OT.POSITION_UPDATE, PE.QTY_IN_FREE), (OT.POSITION_UPDATE, PE.QTY_OUT_FREE)
    ),
}

_WARNINGS: dict[str, str] = {}

#: Fallback keywords, applied when the exact label is unknown. Order matters.
_KEYWORD_FALLBACKS: tuple[tuple[str, OperationType, PositionEffect, PositionEffect], ...] = (
    ("dividendo", OT.DIVIDEND, PE.CASH_IN, PE.CASH_OUT),
    ("jurossobrecapital", OT.JCP, PE.CASH_IN, PE.CASH_OUT),
    ("rendimento", OT.YIELD, PE.CASH_IN, PE.CASH_OUT),
    ("juros", OT.INTEREST, PE.CASH_IN, PE.CASH_OUT),
    ("amortizacao", OT.AMORTIZATION, PE.RETURN_OF_CAPITAL, PE.CASH_OUT),
    ("subscricao", OT.SUBSCRIPTION, PE.QTY_IN_FREE, PE.QTY_OUT_FREE),
    ("desdobr", OT.SPLIT, PE.QTY_IN_FREE, PE.QTY_OUT_FREE),
    ("grupamento", OT.REVERSE_SPLIT, PE.QTY_IN_FREE, PE.QTY_OUT_FREE),
    ("bonificacao", OT.BONUS, PE.QTY_IN_FREE, PE.QTY_OUT_FREE),
    ("fracao", OT.FRACTION, PE.QTY_IN_FREE, PE.QTY_OUT_FREE),
    ("transferencia", OT.TRANSFER_IN, PE.QTY_IN_FREE, PE.QTY_OUT_FREE),
    ("liquidacao", OT.BUY, PE.ACQUIRE, PE.DISPOSE),
    ("resgate", OT.REDEMPTION, PE.DISPOSE, PE.DISPOSE),
    ("vencimento", OT.REDEMPTION, PE.DISPOSE, PE.DISPOSE),
    ("compra", OT.BUY, PE.ACQUIRE, PE.ACQUIRE),
    ("venda", OT.SELL, PE.DISPOSE, PE.DISPOSE),
    ("taxa", OT.FEE, PE.CASH_IN, PE.CASH_OUT),
    ("imposto", OT.TAX, PE.CASH_IN, PE.CASH_OUT),
)


def parse_direction(raw: str) -> Direction:
    """``Credito``/``Debito`` (any casing/accent) -> :class:`Direction`."""
    key = normalize_key(raw)
    if key.startswith("deb") or key.startswith("saida"):
        return Direction.DEBIT
    return Direction.CREDIT


def classify(movement: str, direction: Direction, amount: Decimal | None = None) -> Classification:
    """Resolve a raw movement label into an operation type and position effect."""
    key = normalize_key(movement)

    rule = _RULES.get(key)
    if rule is not None:
        op_type, effect = rule[direction]
        # A disposal with no cash attached cannot realise a gain (e.g. a
        # maturity row with "-" as the amount): downgrade to a free exit.
        if effect is PE.DISPOSE and (amount is None or amount == 0):
            effect = PE.QTY_OUT_FREE
        return Classification(op_type=op_type, effect=effect, warning=_WARNINGS.get(key))

    for needle, op_type, credit_effect, debit_effect in _KEYWORD_FALLBACKS:
        if needle in key:
            effect = credit_effect if direction is Direction.CREDIT else debit_effect
            if effect is PE.DISPOSE and (amount is None or amount == 0):
                effect = PE.QTY_OUT_FREE
            return Classification(
                op_type=op_type,
                effect=effect,
                warning=f"unmapped movement '{movement}' matched by keyword '{needle}'",
            )

    return Classification(
        op_type=OT.UNKNOWN,
        effect=PE.NONE,
        warning=f"unknown movement '{movement}' — imported for the audit trail but not applied",
    )


def known_movements() -> list[str]:
    """Every explicitly mapped label (used by the Settings/diagnostics page)."""
    return sorted(_RULES)
