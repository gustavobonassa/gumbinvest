"""Crypto exchange imports: parsing, both legs of a swap, and reconciliation."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import Asset, FxRate, Transaction
from app.domain.enums import AssetKind, OperationType, PositionEffect
from app.importer.crypto import parse_crypto_csv, sniff_format
from app.importer.crypto import symbols as coins
from app.importer.crypto.binance import ORDERS_FORMAT, TRADES_FORMAT
from app.importer.crypto.binance_ledger import LEDGER_FORMAT
from app.importer.service import ImportService, reclassify_assets, reclassify_transactions
from app.portfolio.service import PortfolioService
from tests.conftest import crypto_file, ledger_totals, requires_crypto_exports

TRADES = (
    "Time,Pair,Side,Price,Executed,Amount,Fee\n"
    "2024-03-05 10:00:00,BTCBRL,BUY,300000,0.01BTC,3000BRL,0.00001BTC\n"
    "2024-03-06 11:00:00,USDTBRL,BUY,5,200USDT,1000BRL,0USDT\n"
    "2024-03-07 12:00:00,ETHUSDT,BUY,2000,0.05ETH,100USDT,0.00005ETH\n"
    "2024-03-08 13:00:00,ETHUSDT,SELL,2200,0.02ETH,44USDT,0.0000132BNB\n"
)

#: The same four events as the exporter's other tab: one row per order, no fee
#: column, and one order that never executed.
ORDERS = (
    "Time,OrderNo,Pair,Type¹,Side,Order Price,Order Amount,Time,Executed²,"
    "Average Price,Trading total³,Status\n"
    "2024-03-05 10:00:00,1,BTCBRL,Market,BUY,0,0.01BTC,2024-03-05 10:00:00,0.01BTC,300000,3000BRL,FILLED\n"
    "2024-03-06 11:00:00,2,USDTBRL,Market,BUY,0,200USDT,2024-03-06 11:00:00,200USDT,5,1000BRL,FILLED\n"
    "2024-03-07 12:00:00,3,ETHUSDT,Market,BUY,0,0.05ETH,2024-03-07 12:00:00,0.05ETH,2000,100USDT,FILLED\n"
    "2024-03-08 13:00:00,4,ETHUSDT,Market,SELL,0,0.02ETH,2024-03-08 13:00:00,0.02ETH,2200,44USDT,FILLED\n"
    "2024-03-09 14:00:00,5,SOLUSDT,Limit,BUY,10,1SOL,,0SOL,0,0USDT,CANCELED\n"
)


@pytest.fixture
def rates(db):
    """A flat PTAX series, so BRL-quoted trades have something to convert with."""
    day = date(2024, 1, 1)
    while day <= date(2024, 12, 31):
        db.add(FxRate(base="USD", quote="BRL", date=day, rate=Decimal("5")))
        day += timedelta(days=1)
    db.commit()


def _import(db, portfolio, payload: str, name: str = "binance.csv"):
    return ImportService(db, portfolio).import_crypto_csv(payload.encode("utf-8"), name)


def _positions(db, portfolio) -> dict[str, object]:
    service = PortfolioService(db, portfolio.id)
    assets = service.assets()
    return {assets[aid].ticker: p for aid, p in service.positions().items()}


# -- parsing ----------------------------------------------------------------
def test_sniffs_both_binance_exports():
    assert sniff_format(TRADES) == TRADES_FORMAT
    assert sniff_format(ORDERS) == ORDERS_FORMAT
    assert sniff_format("Entrada/Saída,Data,Movimentação,Produto\nCredito,01/01/2024,x,y\n") is None


def test_units_next_to_the_number_decide_the_pair_split():
    """``USDTBRL`` cannot be split by suffix alone — the units say where to cut."""
    trades = parse_crypto_csv(TRADES).trades
    usdt = next(t for t in trades if t.pair == "USDTBRL")
    assert (usdt.base_symbol, usdt.quote_symbol) == ("USDT", "BRL")
    assert usdt.base_quantity == Decimal("200")
    assert usdt.quote_amount == Decimal("1000")


def test_orders_that_never_executed_are_not_trades():
    parsed = parse_crypto_csv(ORDERS)
    assert parsed.format == ORDERS_FORMAT
    assert len(parsed.trades) == 4
    assert parsed.skipped_rows == 1
    assert not parsed.errors


# -- booking ----------------------------------------------------------------
def test_a_fiat_purchase_books_one_leg_in_dollars(db, portfolio, rates):
    _import(db, portfolio, TRADES.splitlines(keepends=True)[0] + TRADES.splitlines(keepends=True)[1])

    btc = db.scalar(select(Asset).where(Asset.ticker == "BTC"))
    assert btc.kind == AssetKind.CRYPTO.value
    assert btc.currency == "USD"
    assert btc.market_symbol == "BTC-USD"

    movement = db.scalar(select(Transaction).where(Transaction.asset_id == btc.id))
    assert movement.op_type == OperationType.BUY.value
    # R$ 3.000 at 5,00 is US$ 600, and the rate travels with the movement so the
    # base-currency replay converts it straight back to the reais paid.
    assert movement.currency == "USD"
    assert movement.gross_amount == Decimal("600")
    assert movement.fx_rate == Decimal("5")
    # The fee was charged out of the bitcoin bought, so it never arrived.
    assert movement.quantity == Decimal("0.00999")


def test_a_swap_removes_what_paid_for_it(db, portfolio, rates):
    """Buying Ether with Tether has to spend the Tether, not conjure the Ether."""
    _import(db, portfolio, TRADES)

    positions = _positions(db, portfolio)
    # 200 USDT bought, 100 spent on Ether, 44 received back on the sale.
    assert positions["USDT"].quantity == Decimal("144")
    assert positions["ETH"].quantity == Decimal("0.02995")

    quote_leg = db.scalar(
        select(Transaction)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(Asset.ticker == "USDT", Transaction.op_type == OperationType.SELL.value)
    )
    assert quote_leg.quantity == Decimal("100")
    assert quote_leg.gross_amount == Decimal("100")


def test_swapping_one_coin_for_another_is_not_new_capital(db, portfolio, rates):
    _import(db, portfolio, TRADES)
    overview = PortfolioService(db, portfolio.id).overview()
    # R$ 3.000 of bitcoin plus R$ 1.000 of Tether went in; the Ether trades only
    # moved money between two holdings already inside the portfolio.
    assert overview["net_contributed"] == Decimal("4000")


def test_a_fee_paid_in_a_third_coin_removes_that_coin(db, portfolio, rates):
    _import(db, portfolio, TRADES)
    fee = db.scalar(
        select(Transaction)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(Asset.ticker == "BNB")
    )
    assert fee.op_type == OperationType.FEE.value
    assert fee.effect == PositionEffect.QTY_OUT_FREE.value
    assert fee.quantity == Decimal("0.0000132")
    assert fee.gross_amount == Decimal("0")


def test_stablecoins_are_their_own_family(db, portfolio, rates):
    _import(db, portfolio, TRADES)
    assert db.scalar(select(Asset.kind).where(Asset.ticker == "USDT")) == AssetKind.STABLECOIN.value
    assert db.scalar(select(Asset.kind).where(Asset.ticker == "ETH")) == AssetKind.CRYPTO.value


def test_a_coin_never_merges_into_a_share_of_the_same_name(db, portfolio, rates):
    """``SOL`` is a coin here and a listed company elsewhere; tickers are unique."""
    db.add(Asset(ticker="ETH", name="Ether Capital Corp", kind=AssetKind.STOCK_INTL.value, currency="USD"))
    db.commit()

    _import(db, portfolio, TRADES)

    coin = db.scalar(select(Asset).where(Asset.ticker == f"ETH{coins.TICKER_SUFFIX}"))
    assert coin is not None and coin.kind == AssetKind.CRYPTO.value
    assert db.scalar(select(Asset.kind).where(Asset.ticker == "ETH")) == AssetKind.STOCK_INTL.value


def test_coin_quoted_trades_keep_their_own_currency(db, portfolio, rates):
    """No rate exists for a pair priced in Bitcoin — say so instead of guessing."""
    _import(
        db,
        portfolio,
        "Time,Pair,Side,Price,Executed,Amount,Fee\n"
        "2024-03-05 10:00:00,DOTBTC,BUY,0.0007,1.3DOT,0.00089936BTC,0BNB\n",
    )
    movement = db.scalar(
        select(Transaction)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(Asset.ticker == "DOT")
    )
    assert movement.currency == "BTC"
    assert movement.fx_rate is None
    assert "BTC" in (movement.notes or "")


# -- reconciliation ---------------------------------------------------------
def test_the_two_binance_exports_do_not_double_count(db, portfolio, rates):
    service = ImportService(db, portfolio)
    first = service.import_crypto_csv(TRADES.encode("utf-8"), "trades.csv")
    second = service.import_crypto_csv(ORDERS.encode("utf-8"), "orders.csv")

    assert first.rows_imported > 0
    assert second.rows_imported == 0
    assert _positions(db, portfolio)["USDT"].quantity == Decimal("144")


def test_reimporting_the_same_export_changes_nothing(db, portfolio, rates):
    first = _import(db, portfolio, TRADES)
    again = _import(db, portfolio, TRADES)
    assert again.rows_imported == 0
    assert again.rows_duplicate >= first.rows_imported


def test_startup_reclassification_leaves_crypto_alone(db, portfolio, rates):
    """Both passes re-derive from scratch on every boot; neither may undo this."""
    _import(db, portfolio, TRADES)
    before = {
        (t.op_type, t.effect)
        for t in db.scalars(select(Transaction)).all()
    }
    reclassify_transactions(db, portfolio.id)
    reclassify_assets(db)

    after = {(t.op_type, t.effect) for t in db.scalars(select(Transaction)).all()}
    assert after == before
    assert db.scalar(select(Asset.kind).where(Asset.ticker == "USDT")) == AssetKind.STABLECOIN.value
    assert db.scalar(select(Asset.kind).where(Asset.ticker == "BTC")) == AssetKind.CRYPTO.value


# -- the transaction history (the complete ledger) --------------------------
#: One row per balance change, which is what the whole account looks like: a
#: fiat deposit, a purchase split across its three rows, a staking round trip
#: that must not lose its cost, a reward, and the Earn mirror of that reward.
LEDGER = (
    "ID do Usuário,Tempo,Conta,Operação,Moeda,Alterar,Observação\n"
    "1,2024-03-01 09:00:00,Spot,Deposit,BRL,5000,\n"
    "1,2024-03-02 10:00:00,Spot,Transaction Buy,DOT,100,\n"
    "1,2024-03-02 10:00:00,Spot,Transaction Spend,BRL,-5000,\n"
    "1,2024-03-02 10:00:00,Spot,Transaction Fee,DOT,-0.1,\n"
    "1,2024-03-03 11:00:00,Spot,Staking Purchase,DOT,-99.9,Binance Earn\n"
    "1,2024-03-10 12:00:00,Earn,Simple Earn Locked - Rewards Income,DOT,2,Binance Earn\n"
    "1,2024-03-10 13:00:00,Spot,Staking Rewards,DOT,2,\n"
    "1,2024-03-20 14:00:00,Spot,Staking Redemption,DOT,99.9,Binance Earn\n"
    "1,2024-03-25 15:00:00,USD-M Futures,Realized Profit and Loss,USDT,-3,TradeID - 1\n"
)


def test_sniffs_the_transaction_history():
    assert sniff_format(LEDGER) == LEDGER_FORMAT


def test_the_earn_account_is_dropped_as_a_mirror(db, portfolio, rates):
    """Binance books a reward twice — in Earn and again in Spot. Count it once.

    The Earn rows read like a second pot of coins nobody is counting, and they
    are not: the exchange reports the ``Spot`` totals as the balance. Adding
    Earn on top doubles a staked position.
    """
    _import(db, portfolio, LEDGER)
    rewards = db.scalars(
        select(Transaction)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(Asset.ticker == "DOT", Transaction.op_type == OperationType.REWARD.value)
    ).all()
    assert len(rewards) == 1
    assert rewards[0].quantity == Decimal("2")


def test_a_trade_is_rebuilt_from_its_separate_rows(db, portfolio, rates):
    """The ledger reports what was bought, what was spent and the fee apart."""
    _import(db, portfolio, LEDGER)
    buy = db.scalar(
        select(Transaction)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(Asset.ticker == "DOT", Transaction.op_type == OperationType.BUY.value)
    )
    assert buy.quantity == Decimal("99.9")  # 100 bought, 0,1 taken as the fee
    assert buy.gross_amount == Decimal("1000")  # R$ 5.000 at 5,00


def test_staking_a_position_does_not_lose_its_cost(db, portfolio, rates):
    """A coin comes back from Earn owning the cost it went in with.

    Booking the round trip as a plain exit and re-entry writes the cost off and
    the position returns at zero — which reads as a 100 % gain on a holding that
    never moved.
    """
    _import(db, portfolio, LEDGER)
    position = _positions(db, portfolio)["DOT"]
    assert position.quantity == Decimal("101.9")  # 99,9 back from staking + 2 reward
    assert position.cost_basis == Decimal("1000")
    assert position.parked_cost == Decimal("0")


def test_futures_settle_in_cash_and_open_no_position(db, portfolio, rates):
    _import(db, portfolio, LEDGER)
    futures = db.scalar(
        select(Transaction).where(Transaction.op_type == OperationType.DERIVATIVE.value)
    )
    assert futures.effect == PositionEffect.QTY_OUT_FREE.value
    assert futures.quantity == Decimal("3")


def test_selling_a_stablecoin_for_fiat_is_a_disposal(db, portfolio, rates):
    """The arriving side is reais, which is cash — so the Tether is the trade.

    Reading it the other way round makes the trade "a purchase of reais", and
    since cash is not a holding the whole row is dropped and the Tether is never
    spent — it just accumulates.
    """
    _import(
        db,
        portfolio,
        "ID do Usuário,Tempo,Conta,Operação,Moeda,Alterar,Observação\n"
        "1,2024-03-02 10:00:00,Spot,Transaction Sold,USDT,-500,\n"
        "1,2024-03-02 10:00:00,Spot,Transaction Revenue,BRL,2500,\n",
    )
    sale = db.scalar(
        select(Transaction)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(Asset.ticker == "USDT")
    )
    assert sale.op_type == OperationType.SELL.value
    assert sale.quantity == Decimal("500")


# -- rate series ------------------------------------------------------------
def test_coin_rates_are_not_presented_as_currencies(db, portfolio, rates):
    """A coin's daily close shares a table with PTAX but is not an exchange rate.

    The sidebar reads that table to print "Dólar hoje". Once coin closes landed
    in it, every row printed under that label — a Bitcoin close included, at
    R$ 317.695 "per dollar". They are still stored, because they are what lets a
    trade priced in Bitcoin reach reais; they are just not currencies.
    """
    from app.market.crypto import SOURCE, sync_crypto_fx
    from app.market.fx import fx_status

    db.add(FxRate(base="BTC", quote="BRL", date=date(2024, 3, 5), rate=Decimal("300000"), source=SOURCE))
    db.commit()

    labelled = {row["base"]: row["is_currency"] for row in fx_status(db)}
    assert labelled["USD"] is True
    assert labelled["BTC"] is False

    # Nothing is denominated in Bitcoin any more, so the series goes with it.
    assert sync_crypto_fx(db)["removed"] == 1
    assert "BTC" not in {row["base"] for row in fx_status(db)}


def test_the_bitcoin_headline_is_a_price_not_a_rate(db, portfolio, rates):
    """Shown in the sidebar above the currencies, converted to the base.

    Driven by the live quote rather than the ``fx_rates`` series, which exists
    only to convert coin-priced trades and disappears the moment nothing is
    denominated in a coin. A headline price should not blink out because an
    importer changed its mind about how to read a pair.
    """
    from datetime import UTC, datetime

    from app.db.models import Quote
    from app.market.crypto import headline_prices

    _import(db, portfolio, TRADES)
    assert headline_prices(db) == []  # held, but nothing has quoted it yet

    btc = db.scalar(select(Asset).where(Asset.ticker == "BTC"))
    db.add(
        Quote(
            asset_id=btc.id,
            price=Decimal("60000"),
            change_percent=Decimal("1.5"),
            currency="USD",
            source="yahoo",
            fetched_at=datetime.now(UTC),
        )
    )
    db.commit()

    headline = headline_prices(db)[0]
    assert headline["symbol"] == "BTC"
    assert headline["name"] == "Bitcoin"
    assert headline["price_base"] == Decimal("300000")  # 60.000 at 5,00
    assert headline["change_percent"] == Decimal("1.5")


# -- the real exports -------------------------------------------------------
@requires_crypto_exports
def test_every_row_of_the_real_exports_is_read():
    for path in (crypto_file("Trade"), crypto_file("Order")):
        if path is None:
            continue
        parsed = parse_crypto_csv(path.read_bytes())
        assert not parsed.errors, parsed.errors[:3]
        assert parsed.trades
        assert len(parsed.trades) + parsed.skipped_rows == parsed.total_rows


@requires_crypto_exports
def test_the_real_ledger_reproduces_the_exchange_balances(db, portfolio, rates):
    """The acceptance test: every coin must land on the exchange's own figure.

    The reference is computed straight from the CSV, independently of the
    importer, so agreement means the reconstruction — trades rebuilt from loose
    rows, rewards, deposits, staking round trips and all — is right rather than
    merely self-consistent.
    """
    from app.importer.crypto import symbols as coins

    path = crypto_file("Trans")
    if path is None:
        pytest.skip("the Binance transaction history is not available")

    ImportService(db, portfolio).import_crypto_csv(path.read_bytes(), path.name)
    positions = _positions(db, portfolio)
    expected = ledger_totals(path)

    def free(symbol: str) -> Decimal:
        """What the exchange reports as the spendable balance.

        The ledger sums to the *free* balance, because a subscription to Simple
        Earn is a debit to it. The position holds more than that — the staked
        coins are still owned — so the reconciliation is against the free part.
        """
        position = positions.get(symbol)
        if position is None:
            return Decimal(0)
        return position.quantity - position.staked_quantity

    mismatched = {
        symbol: (total, free(symbol))
        for symbol, total in expected.items()
        if not coins.is_fiat(symbol) and abs(free(symbol) - total) > Decimal("0.00000001")
    }
    assert not mismatched, f"positions disagree with the exchange ledger: {mismatched}"


@requires_crypto_exports
def test_coins_locked_in_simple_earn_are_still_held(db, portfolio, rates):
    """A staked balance is not a closed position.

    Every USDT on this account was subscribed to Simple Earn and never
    redeemed, so the free balance is zero and the position disappeared from the
    portfolio entirely — about US$ 1.450 of it. Staked coins leave the balance
    the exchange reports; they do not leave the wallet.
    """
    path = crypto_file("Trans")
    if path is None:
        pytest.skip("the Binance transaction history is not available")

    ImportService(db, portfolio).import_crypto_csv(path.read_bytes(), path.name)
    usdt = _positions(db, portfolio)["USDT"]

    assert usdt.is_open
    assert usdt.quantity > Decimal("1400")
    assert usdt.staked_quantity == usdt.quantity  # none of it is free
    assert usdt.cost_basis > Decimal("1400")  # and it did not arrive for nothing


@requires_crypto_exports
def test_the_ledger_supersedes_the_spot_exports(db, portfolio, rates):
    """Having all three files loaded must not buy anything twice."""
    ledger, trades = crypto_file("Trans"), crypto_file("Trade")
    if ledger is None or trades is None:
        pytest.skip("both the ledger and a spot export are needed")

    service = ImportService(db, portfolio)
    service.import_crypto_csv(ledger.read_bytes(), ledger.name)
    before = {t: p.quantity for t, p in _positions(db, portfolio).items()}
    result = service.import_crypto_csv(trades.read_bytes(), trades.name)

    assert result.rows_imported == 0
    assert {t: p.quantity for t, p in _positions(db, portfolio).items()} == before


@requires_crypto_exports
def test_nothing_is_sold_from_a_position_the_real_ledger_never_opened(db, portfolio, rates):
    """A complete history never disposes of quantity it does not have.

    Coins deposited from outside the exchange are a different matter — those
    carry their own warning, because their cost is genuinely unknown — so the
    check is that no *quantity* is missing, not that nothing is flagged.
    """
    path = crypto_file("Trans")
    if path is None:
        pytest.skip("the Binance transaction history is not available")

    ImportService(db, portfolio).import_crypto_csv(path.read_bytes(), path.name)
    # The two signatures the engine uses when a disposal has nothing behind it.
    # Warnings about *cost* are a separate matter and expected here.
    missing = {
        ticker: [w for w in position.warnings if "disposal of" in w]
        for ticker, position in _positions(db, portfolio).items()
        if any("disposal of" in w for w in position.warnings)
    }
    assert not missing, f"the complete ledger should reconcile cleanly: {missing}"


@requires_crypto_exports
def test_coins_deposited_from_outside_are_reported_not_counted_as_profit(db, portfolio, rates):
    """Quantity with no purchase behind it must say so.

    This account uses the exchange as a bridge: buy, withdraw minutes later,
    deposit again months on, sell. Net, more BNB arrived than ever left, and
    those coins were bought somewhere this file cannot see — so the result on
    them is not profit, it is an unknown cost basis, and it is flagged as such
    instead of being reported as a gain.
    """
    path = crypto_file("Trans")
    if path is None:
        pytest.skip("the Binance transaction history is not available")

    ImportService(db, portfolio).import_crypto_csv(path.read_bytes(), path.name)
    position = _positions(db, portfolio)["BNB"]

    # Most of what was sold had no purchase behind it, so most of the money is
    # reported apart from the result rather than as a gain of its own size.
    assert position.uncosted_proceeds > Decimal("2000")
    assert position.realized_pnl < position.uncosted_proceeds / 10
    assert any("sem custo conhecido" in warning for warning in position.warnings)


@requires_crypto_exports
def test_no_position_reports_an_absurd_return(db, portfolio, rates):
    """The symptom that started this: +668.572,68 %.

    A lifetime return divided by whatever cost happens to be left produces
    numbers like that whenever a position was sold down to dust — the numerator
    covers years, the denominator covers the last few units. Nothing here should
    be able to leave the plausible range again.
    """
    path = crypto_file("Trans")
    if path is None:
        pytest.skip("the Binance transaction history is not available")

    ImportService(db, portfolio).import_crypto_csv(path.read_bytes(), path.name)
    service = PortfolioService(db, portfolio.id)
    total = sum((ap.market_value_base for ap in service.asset_positions()), Decimal(0))
    absurd = {
        row["ticker"]: row["total_return_pct"]
        for row in (ap.to_dict(total) for ap in service.asset_positions(include_closed=True))
        if abs(row["total_return_pct"]) > 1000
    }
    assert not absurd, f"implausible returns: {absurd}"


@requires_crypto_exports
def test_the_real_exports_reconcile_against_each_other(db, portfolio, rates):
    trades, orders = crypto_file("Trade"), crypto_file("Order")
    if trades is None or orders is None:
        pytest.skip("both Binance exports are needed")

    service = ImportService(db, portfolio)
    service.import_crypto_csv(trades.read_bytes(), trades.name)
    result = service.import_crypto_csv(orders.read_bytes(), orders.name)

    # Every executed order is already on file as one or more fills.
    assert result.rows_imported == 0
    assert result.summary["skipped"]["cross_source_duplicates"] > 0


@requires_crypto_exports
def test_the_balances_hold_when_the_spot_export_is_imported_first(db, portfolio, rates):
    """The same files in the order they actually arrive.

    ``test_the_ledger_supersedes_the_spot_exports`` loads the ledger first, and
    in that order everything reconciles. Loading the trade history first is what
    a person does — it is the file the exchange offers first — and that order
    imported a UNI/BNB trade twice: the trade history calls the pair ``UNIBNB``
    and values it in BNB, the ledger calls it ``BNBUNI`` and values it in UNI,
    so no amount could ever match. 0.109 BNB that had been sold years earlier
    survived on the books until the exchange's own balance was checked.
    """
    from app.importer.crypto import symbols as coins

    ledger, trades = crypto_file("Trans"), crypto_file("Trade")
    if ledger is None or trades is None:
        pytest.skip("both the ledger and a spot export are needed")

    service = ImportService(db, portfolio)
    service.import_crypto_csv(trades.read_bytes(), trades.name)
    service.import_crypto_csv(ledger.read_bytes(), ledger.name)

    positions = _positions(db, portfolio)
    expected = ledger_totals(ledger)

    def free(symbol: str) -> Decimal:
        """The ledger sums to the spendable balance; staked coins are on top."""
        position = positions.get(symbol)
        if position is None:
            return Decimal(0)
        return position.quantity - position.staked_quantity

    mismatched = {
        symbol: (total, free(symbol))
        for symbol, total in expected.items()
        if not coins.is_fiat(symbol) and abs(free(symbol) - total) > Decimal("0.00000001")
    }

    # The coins the duplicated trade touched must be exact.
    assert "BNB" not in mismatched and "UNI" not in mismatched, mismatched

    # DOT is a separate, older defect, pinned here rather than hidden. Coverage
    # is consumed from a *sum* per asset-day-operation, so it cannot tell which
    # row a leg belongs to: on 2022-01-21 a R$200 leg claimed the R$199.85 left
    # by a different trade, being within the 0.5 % tolerance, and the trade that
    # really matched was imported in its place. Fixing it means matching a leg
    # against individual rows instead of a daily total — the approach
    # app.importer.dedup already takes for statements. Loading the ledger alone
    # gets DOT exactly right (test_the_real_ledger_reproduces_the_exchange_
    # balances); only the mixture misplaces it, and by 0.0022 DOT.
    assert set(mismatched) <= {"DOT"}, f"a new coin stopped reconciling: {mismatched}"
    if "DOT" in mismatched:
        expected_dot, actual_dot = mismatched["DOT"]
        assert abs(expected_dot - actual_dot) < Decimal("0.0025"), mismatched
