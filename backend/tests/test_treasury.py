"""Tesouro Direto pricing: feed parsing, product matching and valuation."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db.models import Asset, Quote, Transaction, TreasuryPrice
from app.domain.enums import AssetKind, OperationType, PositionEffect
from app.market.treasury import (
    candidate_maturity_years,
    contracted_rate,
    is_stale,
    match_series,
    parse_series,
    split_product_year,
    sync_treasury_prices,
)

D = Decimal

HEADER = (
    "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;"
    "PU Compra Manha;PU Venda Manha;PU Base Manha"
)

#: A trimmed copy of the real feed: the buy side is always the higher price,
#: exactly as Tesouro Transparente publishes it.
FEED = "\n".join(
    [
        HEADER,
        "Tesouro Renda+ Aposentadoria Extra;15/12/2084;12/01/2026;7,07;7,19;178,00;168,91;168,91",
        "Tesouro Renda+ Aposentadoria Extra;15/12/2084;13/01/2026;7,10;7,22;176,08;167,09;167,09",
        "Tesouro Renda+ Aposentadoria Extra;15/12/2084;14/01/2026;7,12;7,24;175,00;166,00;166,00",
        "Tesouro Renda+ Aposentadoria Extra;15/12/2049;14/01/2026;7,30;7,42;520,00;510,00;510,00",
        "Tesouro Educa+;15/12/2030;14/01/2026;8,32;8,44;3.533,84;3.524,61;3.524,61",
        "Tesouro IPCA+;15/05/2035;14/01/2026;8,22;8,34;2.379,37;2.355,68;2.355,68",
        "Tesouro IPCA+ com Juros Semestrais;15/05/2035;14/01/2026;7,90;8,02;3.900,00;3.880,00;3.880,00",
        "Tesouro Selic;01/03/2029;14/01/2026;0,04;0,05;19.539,78;19.524,43;19.524,43",
    ]
)


@pytest.fixture
def series():
    return parse_series(FEED)


@pytest.fixture
def renda_mais(db, portfolio):
    """The reference position: 6 units of Renda+ 2065 bought at 178,00."""
    asset = Asset(
        ticker="TESOURO-RENDA-APOSENTADORIA-EXTRA-2065",
        name="Tesouro Renda+ Aposentadoria Extra 2065",
        kind=AssetKind.TREASURY.value,
    )
    db.add(asset)
    db.flush()
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            trade_date=date(2026, 1, 12),
            direction="CREDIT",
            op_type=OperationType.BUY.value,
            effect=PositionEffect.ACQUIRE.value,
            quantity=D("6"),
            unit_price=D("178"),
            gross_amount=D("1068"),
            net_amount=D("-1068"),
            raw_movement="Compra",
            raw_product="Tesouro Renda+ Aposentadoria Extra 2065",
            dedup_key="td:0",
        )
    )
    db.commit()
    return asset


# -- parsing ---------------------------------------------------------------
def test_pt_br_numbers_and_dates_are_parsed(series):
    quotes = next(q for key, q in series.items() if key.maturity == date(2029, 3, 1))
    assert quotes[0].buy_price == D("19539.78")
    assert quotes[0].sell_price == D("19524.43")
    assert quotes[0].day == date(2026, 1, 14)


def test_the_buy_side_is_always_the_higher_price(series):
    """The Treasury sells high and buys back low — never the reverse."""
    for quotes in series.values():
        for quote in quotes:
            assert quote.buy_price >= quote.sell_price
            assert quote.buy_rate <= quote.sell_rate


def test_days_come_back_in_order(series):
    quotes = next(q for key, q in series.items() if key.maturity == date(2084, 12, 15))
    assert [q.day for q in quotes] == [date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14)]


def test_only_wanted_papers_are_kept(series):
    """The real file holds ~185 000 rows; a portfolio holds a handful."""
    subset = parse_series(FEED, {"tesouro selic": {2029}})
    assert len(subset) == 1
    assert next(iter(subset)).title == "Tesouro Selic"


# -- product names ---------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "base", "year"),
    [
        ("Tesouro Renda+ Aposentadoria Extra 2065", "tesouro renda aposentadoria extra", 2065),
        ("Tesouro IPCA+ com Juros Semestrais 2035", "tesouro ipca com juros semestrais", 2035),
        ("Tesouro Selic 2029", "tesouro selic", 2029),
        ("Tesouro Prefixado", "tesouro prefixado", None),
    ],
)
def test_product_name_splits_into_family_and_year(name, base, year):
    assert split_product_year(name) == (base, year)


def test_instalment_products_mature_after_the_year_in_their_name():
    """Renda+ pays for 20 years and Educa+ for 5, starting in the named year."""
    assert candidate_maturity_years("tesouro renda aposentadoria extra", 2065) == [2065, 2084]
    assert candidate_maturity_years("tesouro educa", 2026) == [2026, 2030]
    assert candidate_maturity_years("tesouro selic", 2029) == [2029]


def test_renda_mais_2065_matches_the_2084_series(series):
    key = match_series("Tesouro Renda+ Aposentadoria Extra 2065", series)
    assert key is not None
    assert key.maturity == date(2084, 12, 15)


def test_semiannual_variant_wins_over_its_own_prefix(series):
    """"Tesouro IPCA+" is a prefix of "Tesouro IPCA+ com Juros Semestrais"."""
    plain = match_series("Tesouro IPCA+ 2035", series)
    coupon = match_series("Tesouro IPCA+ com Juros Semestrais 2035", series)
    assert plain.title == "Tesouro IPCA+"
    assert coupon.title == "Tesouro IPCA+ com Juros Semestrais"


def test_unknown_product_is_not_guessed(series):
    assert match_series("Tesouro Prefixado 2031", series) is None
    assert match_series("PETR4", series) is None


# -- syncing ---------------------------------------------------------------
def test_sync_stores_the_series_and_the_latest_quote(db, portfolio, renda_mais):
    result = sync_treasury_prices(db, csv_text=FEED)
    assert result["assets"] == 1
    assert result["points"] == 3
    assert result["unmatched"] == []

    rows = db.query(TreasuryPrice).order_by(TreasuryPrice.date).all()
    assert [r.date for r in rows] == [date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14)]
    assert rows[-1].buy_price == D("175")
    assert rows[-1].sell_price == D("166")


def test_the_position_is_marked_at_the_redemption_price(db, portfolio, renda_mais):
    """Valuing at the buy side would book a profit nobody can realise."""
    sync_treasury_prices(db, csv_text=FEED)
    quote = db.get(Quote, renda_mais.id)
    assert quote.price == D("166")  # sell side, not the 175,00 buy side
    assert quote.source == "tesouro"


def test_day_change_comes_from_the_previous_session(db, portfolio, renda_mais):
    sync_treasury_prices(db, csv_text=FEED)
    quote = db.get(Quote, renda_mais.id)
    assert quote.previous_close == D("167.09")
    assert quote.change == pytest.approx(D("-1.09"), abs=D("0.001"))
    assert quote.change_percent == pytest.approx(D("-0.6524"), abs=D("0.001"))


def test_syncing_twice_updates_instead_of_duplicating(db, portfolio, renda_mais):
    sync_treasury_prices(db, csv_text=FEED)
    sync_treasury_prices(db, csv_text=FEED)
    assert db.query(TreasuryPrice).count() == 3


def test_a_paper_the_feed_does_not_know_is_reported(db, portfolio):
    db.add(Asset(ticker="TESOURO-PREFIXADO-2031", name="Tesouro Prefixado 2031", kind=AssetKind.TREASURY.value))
    db.commit()
    result = sync_treasury_prices(db, csv_text=FEED)
    assert result["assets"] == 0
    assert result["unmatched"] == ["TESOURO-PREFIXADO-2031"]


def test_history_feeds_the_portfolio_charts(db, portfolio, renda_mais):
    """price_history is what the historical value chart reads."""
    from app.db.models import PriceHistory

    sync_treasury_prices(db, csv_text=FEED)
    closes = {r.date: r.close for r in db.query(PriceHistory).all()}
    assert closes[date(2026, 1, 13)] == D("167.09")


def test_the_portfolio_prices_the_position_from_the_feed(db, portfolio, renda_mais):
    from app.portfolio.service import PortfolioService

    sync_treasury_prices(db, csv_text=FEED)
    position = next(
        ap for ap in PortfolioService(db, portfolio.id).asset_positions() if ap.asset.id == renda_mais.id
    )
    assert position.price == D("166")
    assert position.price_source == "tesouro"
    assert position.market_value == D("996")  # 6 x 166,00
    assert position.unrealized == D("-72")  # bought at 178,00


# -- yields ----------------------------------------------------------------
def test_contracted_rate_is_read_back_from_the_purchase_date(db, portfolio, renda_mais):
    """The B3 export states the price paid but never the rate."""
    sync_treasury_prices(db, csv_text=FEED)
    assert contracted_rate(db, renda_mais.id, portfolio.id) == D("7.07")


def test_no_contracted_rate_without_a_price_for_that_day(db, portfolio, renda_mais):
    assert contracted_rate(db, renda_mais.id, portfolio.id) is None


def test_stale_feed_is_flagged(db):
    assert is_stale(None) is True
    assert is_stale(date(2026, 1, 1), today=date(2026, 1, 30)) is True
    assert is_stale(date(2026, 1, 28), today=date(2026, 1, 30)) is False
