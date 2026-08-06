"""Binance transaction history — the complete account ledger.

Exported from *Wallet → Transaction History*, this is one row per **balance
change**: ``time, account, operation, coin, change, remark``. It is the only
Binance export that describes everything, and it is the one to use.

Why it matters
--------------
The spot exports (:mod:`app.importer.crypto.binance`) cover trading and nothing
else, so every coin that arrived by deposit, Convert, Earn, staking or an
airdrop looks like it was sold from a position that was never opened. On the
reference account that is not a rounding error — reconstructing from spot
trades alone leaves six coins with a *negative* balance and misses five
holdings entirely. The same account read from this ledger balances exactly,
with no coin below zero.

The ``Earn`` account is a mirror
--------------------------------
Binance reports a Simple Earn reward twice: once in the ``Earn`` account
(``… - Rewards Income``) and again in ``Spot`` (``… Interest`` / ``… Rewards``)
minutes later, with the same amount and no offsetting debit on either side.
Counting both doubles every reward. The ``Earn`` rows are therefore dropped and
the ``Spot`` side is authoritative — which is what reproduces the balances the
exchange actually shows.

Reconstructing trades
---------------------
A trade arrives as separate rows for what was bought, what was spent and what
was charged, sharing a timestamp. Rows are grouped by (account, second), then
adjacent groups a second or two apart are merged when that resolves them into
exactly one coin in and one coin out — Binance Convert sometimes books its two
halves in consecutive seconds. 384 of the 390 trade groups in the reference file
are already unambiguous on their own.

Aggregating a whole group into one trade is exact rather than a simplification:
when an order fills in five parts, the five ``Transaction Sold`` rows and the
five ``Transaction Revenue`` rows are the same trade seen piecewise, and their
sums are what actually left and arrived.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from app.importer.crypto.base import (
    CryptoEvent,
    CryptoFormatError,
    CryptoTrade,
    ParsedTradeFile,
    parse_timestamp,
)
from app.importer.crypto.symbols import is_fiat, is_stablecoin, is_tracked
from app.importer.parser import normalize_key

EXCHANGE = "Binance"
LEDGER_FORMAT = "binance-transactions"

#: The account whose rows mirror movements already counted in ``Spot``.
#:
#: Tempting to read as a second pot of coins to be added on — the operations
#: under it appear under no other account, so they look like holdings nobody is
#: counting. They are not: the balances the exchange reports are the ``Spot``
#: totals alone, confirmed against the account itself. Adding ``Earn`` on top
#: would have inflated DOT from 180 to 617 and ADA from 359 to 718.
MIRROR_ACCOUNT = "earn"

#: Accounts that settle in cash rather than holding an instrument.
_DERIVATIVE_ACCOUNTS = {"usdmfutures", "coinmfutures"}

#: Two halves of one trade may be a second or two apart.
GROUP_WINDOW = timedelta(seconds=2)

# --- operation vocabulary --------------------------------------------------
# Matched on the normalised operation name, so the Portuguese and English
# exports (``Operação`` / ``Operation``) both resolve to the same behaviour.

#: Rows that together describe a trade: what came in, what went out, what it cost.
_TRADE_OPS = {
    "transactionbuy",
    "transactionspend",
    "transactionsold",
    "transactionrevenue",
    "transactionrelated",
    "buy",
    "sell",
    "binanceconvert",
}
#: Trading fees. Part of a trade group when one surrounds them.
_FEE_OPS = {"transactionfee", "fee"}

#: Coins arriving free: rewards, rebates, airdrops, promotional credits.
_REWARD_OPS = {
    "stakingrewards",
    "simpleearnlockedrewards",
    "simpleearnflexibleinterest",
    "commissionhistory",
    "commissionrebate",
    "strategytradingfeerebate",
    "airdropassets",
    "distribution",
    "tokenswapdistribution",
    "launchpoolairdropuserclaimdistribution",
    "campaignrelatedreward",
    "cryptobox",
    "assetrecovery",
}
#: Coins crossing the exchange's boundary.
_DEPOSIT_OPS = {"deposit", "fiatdeposit", "buycryptowithfiat"}
_WITHDRAW_OPS = {"withdraw", "fiatwithdraw"}
#: Coins moving into or out of Simple Earn / staking.
_EARN_OPS = {
    "simpleearnflexiblesubscription",
    "simpleearnflexibleredemption",
    "simpleearnlockedsubscription",
    "simpleearnlockedredemption",
    "stakingpurchase",
    "stakingredemption",
    "launchpoolsubscriptionredemption",
    "simpleearnflexibleinternaltransfer",
}
#: Derivatives settlement, in the margin coin.
_FUTURES_OPS = {"realizedprofitandloss", "fundingfee"}
#: Dust swept into BNB. Several coins out, BNB in, no usable per-coin price.
_DUST_OPS = {"smallassetsexchangebnb"}
#: A rename, not a movement: the old ticker out and the new one in.
_RENAME_OPS = {"tokenswapredenominationrebranding"}
#: Internal moves between the user's own account buckets. Both sides are in the
#: file and cancel out, so applying them would only add noise.
_INTERNAL_PREFIX = "transferbetween"

#: Canonical movement labels the classifier already understands.
_LABELS = {
    "reward": "Reward",
    "deposit": "Exchange deposit",
    "withdraw": "Exchange withdrawal",
    "earn": "Earn transfer",
    "futures": "Futures result",
    "fee": "Trading fee",
}

#: Column keys, normalised. Both language variants of the export normalise onto
#: the same names except for the header wording, which is why each is listed.
_COLUMNS = {
    "time": ("tempo", "utctime", "time", "data"),
    "account": ("conta", "account"),
    "operation": ("operacao", "operation"),
    "coin": ("moeda", "coin"),
    "change": ("alterar", "change", "alteracao"),
    "remark": ("observacao", "remark"),
}


def _columns(header: list[str]) -> dict[str, int]:
    """Column name -> index, tolerating both language variants of the export."""
    normalised = {normalize_key(name): index for index, name in enumerate(header) if name}
    mapping: dict[str, int] = {}
    for key, aliases in _COLUMNS.items():
        for alias in aliases:
            if alias in normalised:
                mapping[key] = normalised[alias]
                break
    return mapping


def _read(payload: bytes | str) -> tuple[list[str], list[list[str]]]:
    from app.importer.parser import decode_bytes, sniff_delimiter

    text = decode_bytes(payload) if isinstance(payload, (bytes, bytearray)) else payload
    text = text.lstrip("﻿")
    if not text.strip():
        raise CryptoFormatError("the file is empty")
    reader = csv.reader(io.StringIO(text), delimiter=sniff_delimiter(text))
    rows = [row for row in reader if any((cell or "").strip() for cell in row)]
    if not rows:
        raise CryptoFormatError("the file has no rows")
    return rows[0], rows[1:]


def matches(payload: bytes | str) -> str | None:
    try:
        header, _ = _read(payload)
    except CryptoFormatError:
        return None
    columns = set(_columns(header))
    return LEDGER_FORMAT if {"time", "account", "operation", "coin", "change"} <= columns else None


class _Row:
    """One ledger line, normalised."""

    __slots__ = ("at", "account", "operation", "raw_operation", "coin", "change", "line", "text")

    def __init__(self, at: datetime, account: str, operation: str, raw_operation: str,
                 coin: str, change: Decimal, line: int, text: str) -> None:
        self.at = at
        self.account = account
        self.operation = operation
        self.raw_operation = raw_operation
        self.coin = coin
        self.change = change
        self.line = line
        self.text = text


def _parse_rows(header: list[str], rows: list[list[str]], result: ParsedTradeFile) -> list[_Row]:
    columns = _columns(header)
    parsed: list[_Row] = []
    for offset, row in enumerate(rows, start=2):
        def cell(name: str) -> str:
            index = columns.get(name)
            return (row[index] or "").strip() if index is not None and index < len(row) else ""

        text = ",".join(row)[:200]
        try:
            if normalize_key(cell("account")) == MIRROR_ACCOUNT:
                # The Earn account restates what Spot already reports.
                result.skipped_rows += 1
                continue
            change = Decimal(cell("change").replace(",", ""))
            if change == 0:
                result.skipped_rows += 1
                continue
            parsed.append(
                _Row(
                    at=parse_timestamp(cell("time")),
                    account=cell("account"),
                    operation=normalize_key(cell("operation")),
                    raw_operation=cell("operation"),
                    coin=cell("coin").upper(),
                    change=change,
                    line=offset,
                    text=text,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not kill the import
            result.errors.append({"line": offset, "error": str(exc), "raw": text})
    return parsed


def _group_trades(rows: list[_Row]) -> list[list[_Row]]:
    """Cluster trade rows into groups of one coin in and one coin out.

    Rows sharing an account and a second are one group. A group that resolves to
    a single inflow and a single outflow is done; anything else is merged with
    the next group within :data:`GROUP_WINDOW` if that resolves it, which is what
    picks up a Convert whose two halves landed a second apart.
    """
    buckets: dict[tuple[str, datetime], list[_Row]] = defaultdict(list)
    for row in rows:
        buckets[(row.account, row.at)].append(row)

    groups: list[list[_Row]] = []
    pending: list[_Row] | None = None
    for key in sorted(buckets, key=lambda k: (k[0], k[1])):
        group = buckets[key]
        if pending is not None:
            same_account = pending[0].account == group[0].account
            close_enough = group[0].at - pending[0].at <= GROUP_WINDOW
            if same_account and close_enough and _sides(pending + group)[0]:
                groups.append(pending + group)
                pending = None
                continue
            groups.append(pending)
            pending = None
        if _sides(group)[0] or len(group) > 1:
            groups.append(group)
        else:
            pending = group
    if pending is not None:
        groups.append(pending)
    return groups


def _sides(group: list[_Row]) -> tuple[bool, dict[str, Decimal], dict[str, Decimal]]:
    """``(resolved, inflow, outflow)`` for a candidate trade group."""
    inflow: dict[str, Decimal] = defaultdict(Decimal)
    outflow: dict[str, Decimal] = defaultdict(Decimal)
    for row in group:
        if row.operation in _FEE_OPS:
            continue
        (inflow if row.change > 0 else outflow)[row.coin] += abs(row.change)
    return (len(inflow) == 1 and len(outflow) == 1), dict(inflow), dict(outflow)


def _cash_rank(symbol: str) -> int:
    """How far a symbol is from being money: fiat 0, stablecoin 1, coin 2."""
    if is_fiat(symbol):
        return 0
    return 1 if is_stablecoin(symbol) else 2


def _as_trade(group: list[_Row]) -> CryptoTrade | None:
    """Fold a resolved group into a single swap."""
    resolved, inflow, outflow = _sides(group)
    if not resolved:
        return None
    received, received_qty = next(iter(inflow.items()))
    given, given_qty = next(iter(outflow.items()))

    fees: dict[str, Decimal] = defaultdict(Decimal)
    for row in group:
        if row.operation in _FEE_OPS:
            fees[row.coin] += abs(row.change)

    # Which side is the instrument and which is the money: whichever is further
    # from cash is what the trade is *about*. Reais fund a Tether purchase,
    # Tether funds an Ether purchase, and selling Tether back into reais is a
    # disposal of the Tether — not a purchase of reais, which is what a rule
    # based on "the arriving side wins" would conclude, silently dropping the
    # disposal because cash is not a holding.
    if _cash_rank(received) > _cash_rank(given) or (
        _cash_rank(received) == _cash_rank(given)
    ):
        side, base, base_qty, quote, quote_qty = "BUY", received, received_qty, given, given_qty
    else:
        side, base, base_qty, quote, quote_qty = "SELL", given, given_qty, received, received_qty

    first = group[0]
    return CryptoTrade(
        trade_date=first.at.date(),
        executed_at=first.at,
        base_symbol=base,
        base_quantity=base_qty,
        quote_symbol=quote,
        quote_amount=quote_qty,
        side=side,
        price=(quote_qty / base_qty) if base_qty else None,
        fees=tuple((amount, coin) for coin, amount in sorted(fees.items()) if amount > 0),
        pair=f"{base}{quote}",
        line_number=first.line,
        raw_text=first.text,
    )


def _warn(result: ParsedTradeFile, message: str) -> None:
    """Record a file-level note once, however many rows provoked it.

    A ledger has twenty thousand rows; a warning repeated four thousand times
    is noise that buries the one that matters.
    """
    if message not in result.warnings:
        result.warnings.append(message)


def _event(row: _Row, movement: str, gross: Decimal = Decimal(0)) -> CryptoEvent:
    return CryptoEvent(
        trade_date=row.at.date(),
        executed_at=row.at,
        symbol=row.coin,
        quantity=abs(row.change),
        movement=movement,
        direction="CREDIT" if row.change > 0 else "DEBIT",
        gross=gross,
        operation=row.raw_operation,
        account=row.account,
        line_number=row.line,
        raw_text=row.text,
    )


def parse(payload: bytes | str) -> ParsedTradeFile:
    """Read a Binance transaction history into a :class:`ParsedTradeFile`."""
    header, rows = _read(payload)
    if matches(payload) is None:
        raise CryptoFormatError(
            "unexpected CSV layout — expected a Binance transaction history export, "
            f"found columns: {', '.join(h for h in header if h)}"
        )
    result = ParsedTradeFile(format=LEDGER_FORMAT, exchange=EXCHANGE, total_rows=len(rows))
    ledger = _parse_rows(header, rows, result)

    def is_trading(row: _Row) -> bool:
        # Derivatives never take part in a spot trade group: a futures fee has
        # no purchase around it, and grouping it with one would attach the cost
        # of a leveraged position to whatever coin happened to be traded in the
        # same second.
        if normalize_key(row.account) in _DERIVATIVE_ACCOUNTS:
            return False
        return row.operation in _TRADE_OPS or row.operation in _FEE_OPS

    trade_rows = [r for r in ledger if is_trading(r)]
    other_rows = [r for r in ledger if not is_trading(r)]

    unresolved: list[_Row] = []
    for group in _group_trades(trade_rows):
        trade = _as_trade(group)
        if trade is not None and is_tracked(trade.base_symbol):
            result.trades.append(trade)
        elif trade is not None:
            # Both sides were cash (a fiat-to-fiat conversion). Nothing to hold.
            result.skipped_rows += len(group)
        else:
            unresolved.extend(group)

    for row in unresolved:
        # A trade whose counterparty could not be identified still moved the
        # coin, so it is booked as a quantity change rather than dropped — the
        # cost is what is unknown, not the movement.
        if not is_tracked(row.coin):
            result.skipped_rows += 1
            continue
        label = _LABELS["fee"] if row.operation in _FEE_OPS else _LABELS["reward"]
        result.events.append(_event(row, label))
        _warn(
            result,
            f"{row.raw_operation} on {row.coin} had no identifiable counterparty — "
            "booked as a quantity change with no cost",
        )

    for row in other_rows:
        result.events.extend(_ledger_event(row, result))
    return result


def _ledger_event(row: _Row, result: ParsedTradeFile) -> list[CryptoEvent]:
    """Map one non-trade ledger row onto a movement, or drop it."""
    operation = row.operation

    if operation.startswith(_INTERNAL_PREFIX):
        # Both halves are in the file and cancel; the portfolio has no notion of
        # which pocket of the exchange a coin sits in.
        result.skipped_rows += 1
        return []

    if not is_tracked(row.coin):
        # Fiat moving in and out of the account. Real, but it is cash, and the
        # portfolio tracks positions — the same rule the broker statements
        # follow for deposits and withdrawals.
        result.skipped_rows += 1
        return []

    if operation in _FUTURES_OPS:
        return [_event(row, _LABELS["futures"])]
    if operation in _FEE_OPS:
        # A fee with no trade around it — the running cost of a derivatives
        # position, charged in the margin coin.
        return [_event(row, _LABELS["fee"])]
    if operation in _REWARD_OPS or operation in _DUST_OPS or operation in _RENAME_OPS:
        return [_event(row, _LABELS["reward"])]
    if operation in _EARN_OPS:
        return [_event(row, _LABELS["earn"])]
    if operation in _WITHDRAW_OPS:
        return [_event(row, _LABELS["withdraw"])]
    if operation in _DEPOSIT_OPS:
        # A stablecoin arrives at a known value — it is a dollar — so it is
        # booked as a purchase at face value rather than as free quantity.
        # Anything else has no cost on file and must not be given an invented
        # one: selling it later realises the whole proceeds, and the position
        # says so.
        if is_stablecoin(row.coin) and row.change > 0:
            return [_event(row, "Buy", gross=abs(row.change))]
        return [_event(row, _LABELS["deposit"])]

    _warn(
        result,
        f"unmapped operation {row.raw_operation!r} — booked as a quantity change with no cost",
    )
    return [_event(row, _LABELS["reward"])]
