"""Shared model for exchange trade exports.

A crypto exchange reports a *swap*, not a purchase: one asset leaves and another
arrives, and which of the two is "the money" depends only on which side of the
pair it sits. ``ETHUSDT`` BUY spends Tether to get Ether; ``USDTBRL`` BUY spends
reais to get Tether; ``NEARBTC`` BUY spends Bitcoin to get NEAR. A B3 or broker
statement never does this — there, cash is always the counterparty and cash is
not tracked.

So :class:`CryptoTrade` is deliberately symmetric: it names both sides and lets
the import service decide which of them becomes a position. That is what makes a
stablecoin balance visible (spending 100 USDT on Ether has to *remove* 100 USDT
from somewhere) and what keeps "capital contributed" honest — swapping one coin
for another is not new money entering the portfolio, and it only reads that way
if the leg that paid for it is missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class CryptoFormatError(ValueError):
    """Raised when a CSV is not a recognisable exchange export."""


#: "0.00308BTC" / "1000.8306BRL" / "324945" / "0BNB" — a number with the unit
#: glued to it, which is how Binance prints every quantity and every amount.
_AMOUNT_RE = re.compile(r"^\s*(?P<number>-?[\d.,]+)\s*(?P<symbol>[A-Za-z][A-Za-z0-9]*)?\s*$")


def parse_amount(value: str | None) -> tuple[Decimal | None, str]:
    """Split ``"0.00308BTC"`` into ``(Decimal("0.00308"), "BTC")``.

    Returns ``(None, "")`` for anything unreadable — the caller decides whether
    a missing number is fatal for that column.
    """
    text = (value or "").strip()
    if not text or text in {"-", "--"}:
        return None, ""
    match = _AMOUNT_RE.match(text)
    if match is None:
        return None, ""
    # Exchange exports are US-formatted: the comma only ever groups thousands.
    number = match.group("number").replace(",", "")
    try:
        parsed = Decimal(number)
    except InvalidOperation:
        return None, ""
    return parsed, (match.group("symbol") or "").upper()


def parse_timestamp(value: str) -> datetime:
    """Parse the export's ``Time`` column (local time, seconds resolution)."""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise CryptoFormatError(f"unrecognised timestamp: {value!r}")


@dataclass(frozen=True, slots=True)
class CryptoTrade:
    """One executed swap, normalised and independent of the exchange.

    ``base_quantity`` and ``quote_amount`` are always positive; ``side`` says
    which way they flow. Fees are kept in their own currency because exchanges
    charge them in whichever asset suits them — the received coin, the coin
    spent, or a third one entirely (Binance discounts fees paid in BNB) — and
    each of those three needs a different treatment.
    """

    trade_date: date
    executed_at: datetime
    #: The instrument: what the trade is *about*.
    base_symbol: str
    base_quantity: Decimal
    #: What paid for it (or what it was sold for).
    quote_symbol: str
    quote_amount: Decimal
    #: "BUY" (base in, quote out) or "SELL" (base out, quote in).
    side: str
    price: Decimal | None = None
    #: ``((amount, symbol), …)``, positive amounts. A tuple rather than one pair
    #: because a single trade can be charged in two coins at once — the exchange
    #: takes part of the fee in BNB and the rest in the quote currency.
    fees: tuple[tuple[Decimal, str], ...] = ()
    pair: str = ""
    order_ref: str = ""
    line_number: int | None = None
    raw_text: str = ""

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"

    def fee_in(self, symbol: str) -> Decimal:
        """How much of the fee was charged in ``symbol``."""
        return sum((amount for amount, coin in self.fees if coin == symbol), Decimal(0))


@dataclass(frozen=True, slots=True)
class CryptoEvent:
    """A balance change that is not a trade.

    A ledger export reports far more than trading: coins arrive from a deposit,
    accrue as a staking reward, leave for a withdrawal, or are written off in a
    dust conversion. Each is a single-coin delta with no counterparty, so it
    cannot be expressed as a :class:`CryptoTrade` — but it moves the position
    just as much, and leaving it out is what makes a spot-only history show
    coins being sold from nowhere.

    ``movement`` is one of the canonical labels the classifier knows (see
    :mod:`app.importer.classifier`), so an event needs no special handling
    downstream. ``gross`` is the money involved, which is zero for everything
    that arrives free and face value for a stablecoin deposit.
    """

    trade_date: date
    executed_at: datetime
    symbol: str
    #: Always positive; ``movement`` plus ``direction`` say which way it went.
    quantity: Decimal
    movement: str
    #: "CREDIT" when the coin arrived, "DEBIT" when it left.
    direction: str
    gross: Decimal = Decimal(0)
    currency: str = "USD"
    operation: str = ""
    account: str = ""
    line_number: int | None = None
    raw_text: str = ""

    @property
    def is_credit(self) -> bool:
        return self.direction == "CREDIT"


@dataclass(slots=True)
class ParsedTradeFile:
    """Everything read from one exchange export."""

    #: Parser identity, stored on the batch: "binance-spot-trades".
    format: str
    #: Canonical exchange name, used as the broker.
    exchange: str
    trades: list[CryptoTrade] = field(default_factory=list)
    #: Balance changes that are not trades. Empty for the spot exports, which
    #: only ever report trading.
    events: list[CryptoEvent] = field(default_factory=list)
    #: Rows the export itself marks as not executed (cancelled/expired orders),
    #: plus ledger rows that describe cash or an account's mirror of a movement
    #: already counted elsewhere.
    skipped_rows: int = 0
    #: Per-row problems, in the shape the import log expects.
    errors: list[dict] = field(default_factory=list)
    #: File-level notes worth showing the user.
    warnings: list[str] = field(default_factory=list)
    total_rows: int = 0

    @property
    def _days(self) -> list[date]:
        return [t.trade_date for t in self.trades] + [e.trade_date for e in self.events]

    @property
    def period_start(self) -> date | None:
        return min(self._days, default=None)

    @property
    def period_end(self) -> date | None:
        return max(self._days, default=None)
