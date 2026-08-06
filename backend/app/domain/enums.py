"""Domain vocabulary shared by the importer, the engine and the API.

The B3 "Movimentação" export describes events in Portuguese free text
(`Movimentação` column) plus a direction (`Entrada/Saída`: Credito/Debito).
Those raw strings are mapped onto the stable enums below so the calculation
engine never has to care about broker wording — see
:mod:`app.importer.classifier` for the mapping table.
"""
from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    """Raw direction from the CSV."""

    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class OperationType(StrEnum):
    """Business meaning of a movement (what the user sees / filters on)."""

    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    JCP = "JCP"  # Juros sobre capital próprio
    YIELD = "YIELD"  # Rendimento (FII / Fiagro distributions)
    INTEREST = "INTEREST"  # Fixed income coupon
    AMORTIZATION = "AMORTIZATION"  # Return of capital / principal amortisation
    SPLIT = "SPLIT"  # Desdobro
    REVERSE_SPLIT = "REVERSE_SPLIT"  # Grupamento
    BONUS = "BONUS"  # Bonificação em ativos
    SUBSCRIPTION = "SUBSCRIPTION"  # Subscription rights / receipts lifecycle
    MERGER = "MERGER"  # Incorporação / cisão
    FRACTION = "FRACTION"  # Fractional share adjustments and auctions
    TRANSFER_IN = "TRANSFER_IN"  # Custody transfer between brokers (inbound)
    TRANSFER_OUT = "TRANSFER_OUT"  # Custody transfer between brokers (outbound)
    REDEMPTION = "REDEMPTION"  # Maturity / early redemption of fixed income
    POSITION_UPDATE = "POSITION_UPDATE"  # B3 "Atualização" — see PositionEffect.QTY_SYNC
    #: Paid in kind, not in cash: staking and Simple Earn rewards, airdrops,
    #: rebates. Deliberately *not* in ``INCOME_TYPES`` — the income metrics are
    #: built from cash amounts, and a reward has none. It arrives as free
    #: quantity, which dilutes the average price exactly like a bonus share.
    REWARD = "REWARD"
    #: Realised result of a derivatives position (exchange futures). Settles in
    #: the margin coin without any instrument changing hands.
    DERIVATIVE = "DERIVATIVE"
    FEE = "FEE"
    TAX = "TAX"
    INFO = "INFO"  # Informational only (no financial effect)
    UNKNOWN = "UNKNOWN"


class PositionEffect(StrEnum):
    """How the engine must apply the movement to a position.

    This is the *only* thing the calculation engine looks at, which keeps new
    broker wordings a one-line change in the classifier.
    """

    #: Quantity in, cost basis in (a purchase).
    ACQUIRE = "ACQUIRE"
    #: Quantity out, realises profit against the average price (a disposal).
    DISPOSE = "DISPOSE"
    #: Quantity in at zero cost — splits, bonuses, subscription receipts.
    #: Total cost is preserved, so the average price dilutes automatically.
    QTY_IN_FREE = "QTY_IN_FREE"
    #: Quantity out at cost — fractions removed, receipts converted.
    #: Removes cost proportionally without realising a gain.
    QTY_OUT_FREE = "QTY_OUT_FREE"
    #: Cash in, no quantity change (dividends, JCP, yields, interest).
    CASH_IN = "CASH_IN"
    #: Cash out, no quantity change (fees, taxes).
    CASH_OUT = "CASH_OUT"
    #: Cash in that reduces the cost basis instead of counting as income.
    RETURN_OF_CAPITAL = "RETURN_OF_CAPITAL"
    #: B3 "Atualização": the quantity is either a *delta* (shares credited by a
    #: fund event) or a *restatement* of the whole custody position, and the
    #: export does not say which. The engine compares the quantity with the
    #: position currently held at that broker and applies it only when they
    #: differ. See :func:`app.portfolio.engine.apply_movement`.
    QTY_SYNC = "QTY_SYNC"
    #: Share restructuring (grupamento + desdobramento on the same day): the
    #: credited quantity *is* the resulting position, and the old shares are
    #: consumed without an explicit debit. Cost basis is preserved, so the
    #: average price rescales by the restructuring ratio.
    QTY_RESTATE = "QTY_RESTATE"
    #: A subscription right reached its deadline unexercised and is now worth
    #: nothing. B3 states the *event* but almost never the quantity — 56 of the
    #: 57 rows in the reference export carry zero — so an unquantified row
    #: expires whatever is left of the right, and a quantified one takes only
    #: what it names. Any cost still attached is realised as a loss, because
    #: that is what an expired right is.
    QTY_EXPIRE = "QTY_EXPIRE"
    #: Cash received for quantity that already left the position (fraction
    #: auctions). Booked as a realised result, never as income.
    REALIZE = "REALIZE"
    #: Quantity leaves the venue but is still owned — coins withdrawn to a
    #: wallet. The cost travels with them into ``Position.parked_cost`` instead
    #: of being written off, because an exchange is routinely used as a bridge
    #: (buy, withdraw, deposit back months later, sell) and treating the exit as
    #: a disposal makes the eventual sale invent a gain the size of the
    #: purchase. The quantity really is gone from the portfolio, though: once
    #: coins leave the exchange, nothing here can say what became of them.
    QTY_OUT_PARKED = "QTY_OUT_PARKED"
    #: The matching return: quantity comes back and brings its share of the
    #: parked cost with it. Anything beyond what was parked was acquired
    #: elsewhere and arrives uncosted.
    QTY_IN_PARKED = "QTY_IN_PARKED"
    #: Quantity moved into an exchange's staking or Simple Earn product. It
    #: leaves the *reported balance* and nothing else — the coins are still on
    #: the exchange, still owned, still earning — so unlike a withdrawal it
    #: stays in the position, held aside only so the UI can say it is locked.
    QTY_OUT_STAKED = "QTY_OUT_STAKED"
    #: Redeemed back into the free balance, carrying its cost with it.
    QTY_IN_STAKED = "QTY_IN_STAKED"
    #: Moves quantity between brokers without touching the portfolio position
    #: (internal custody transfers, matched credit/debit pairs).
    LEDGER_ONLY = "LEDGER_ONLY"
    #: Recorded for the audit trail but ignored by the engine.
    NONE = "NONE"


class AssetKind(StrEnum):
    """Instrument family, inferred from the ticker / product description."""

    # Domestic and offshore holdings are separate families even where the
    # instrument is the same, because they are not comparable in one bucket: they
    # trade in different currencies, under different tax rules, and the whole
    # point of the allocation chart is to show how much sits abroad. FII/REIT
    # already worked this way; STOCK/STOCK_INTL and ETF/ETF_INTL follow it.
    STOCK = "STOCK"  # B3-listed shares
    STOCK_INTL = "STOCK_INTL"  # Shares listed abroad (incl. ADRs)
    FII = "FII"  # Real estate fund / Fiagro
    REIT = "REIT"  # US real estate trust — the offshore equivalent of an FII
    ETF = "ETF"  # B3-listed ETF
    ETF_INTL = "ETF_INTL"  # ETF listed abroad
    BDR = "BDR"
    #: Coins and tokens held on an exchange. A single offshore family, like
    #: STOCK_INTL: they are priced in dollars on a global market and answer the
    #: same question in the allocation chart ("how much sits outside the B3").
    CRYPTO = "CRYPTO"
    #: Dollar-pegged tokens (USDT, USDC, BUSD). Separated from CRYPTO on purpose:
    #: a balance of stablecoins is dollar cash parked on the exchange, not crypto
    #: exposure, and merging the two makes the allocation chart claim a risk that
    #: is not there.
    STABLECOIN = "STABLECOIN"
    #: Retired: units (TAEE11, ALUP11) are classified as STOCK — they behave like
    #: shares in every way that matters here. Kept so old rows still resolve.
    UNIT = "UNIT"
    SUBSCRIPTION = "SUBSCRIPTION"  # Rights (…1/…2) and receipts (…9/…10/…13)
    FIXED_INCOME = "FIXED_INCOME"  # CDB, LCI, LCA, debentures
    TREASURY = "TREASURY"  # Tesouro Direto
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    OTHER = "OTHER"


class ImportStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: Operations that represent cash income for the "dividends received" metrics.
INCOME_TYPES: frozenset[OperationType] = frozenset(
    {
        OperationType.DIVIDEND,
        OperationType.JCP,
        OperationType.YIELD,
        OperationType.INTEREST,
    }
)

#: Operations shown as "corporate actions" in the UI.
CORPORATE_ACTION_TYPES: frozenset[OperationType] = frozenset(
    {
        OperationType.SPLIT,
        OperationType.REVERSE_SPLIT,
        OperationType.BONUS,
        OperationType.MERGER,
        OperationType.SUBSCRIPTION,
        OperationType.FRACTION,
        OperationType.AMORTIZATION,
    }
)
