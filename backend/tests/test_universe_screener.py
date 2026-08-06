"""The screener, over seeded synthetic rows.

The test that earns its place here is the NULL-ordering one: SQLite sorts NULLs
first and Postgres sorts them last, so a screener that does not say which it
wants returns *different* top-N lists on the desktop build and the Docker build
from identical data. That divergence is invisible until someone compares two
installs, which is exactly why it is pinned.

Every ticker and figure is invented.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models import Asset, AssetUniverse
from app.services import universe as screener

D = Decimal
NOW = datetime(2026, 8, 1, tzinfo=UTC)


def row(ticker: str, **fields) -> AssetUniverse:
    base = {
        "market": "B3",
        "currency": "BRL",
        "name": f"Empresa {ticker}",
        "kind": "STOCK",
        "status": "ATIVO",
        "identity_source": "teste",
        "fundamentals_fetched_at": NOW,
        "fundamentals_period": "2025",
    }
    return AssetUniverse(ticker=ticker, **{**base, **fields})


@pytest.fixture
def seeded(db):
    db.add_all(
        [
            row("AAA3", market_cap=D(500), pe=D(10), pb=D(1), roe_pct=D(20),
                dividend_yield_pct=D(8), sector="Bancos", avg_volume_21d=D(900)),
            row("BBB3", market_cap=D(400), pe=D(20), pb=D(2), roe_pct=D(10),
                dividend_yield_pct=D(4), sector="Bancos", avg_volume_21d=D(800)),
            row("CCC3", market_cap=D(300), pe=D(5), pb=D("0.5"), roe_pct=D(30),
                dividend_yield_pct=D(12), sector="Energia", avg_volume_21d=D(700)),
            row("DDD3", market_cap=D(200), sector="Energia", avg_volume_21d=D(600)),  # no ratios
            row("EEE3", market_cap=D(100), pe=D(15), sector="Varejo", avg_volume_21d=D(500)),
            row("ZZZ11", market_cap=D(250), kind="FII", fund_segment="Papel",
                dividend_yield_pct=D(11), avg_volume_21d=D(400)),
            row("YYY11", market_cap=D(240), kind="FII", fund_segment="Lajes",
                dividend_yield_pct=D(9), avg_volume_21d=D(390)),
            row("OLD3", market_cap=D(50), pe=D(3), sector="Bancos",
                status="CANCELADA", avg_volume_21d=D(10)),
            row("USA", market="US", currency="USD", kind="STOCK_INTL",
                market_cap=D(999), avg_volume_21d=D(999)),
        ]
    )
    db.commit()
    return db


class TestNullOrdering:
    """The cross-dialect trap. Must hold identically on SQLite and Postgres."""

    def test_nulls_sort_last_descending(self, seeded):
        result = screener.screen(
            seeded,
            screener.ScreenRequest(order_by="p_l", descending=True, require=(), limit=50),
        )
        tickers = [item["ticker"] for item in result.rows]
        # order_by implies IS NOT NULL on the ranked column, so the rows with
        # no P/L are excluded rather than parked at either end.
        assert "DDD3" not in tickers
        assert tickers[0] == "BBB3"  # highest P/L

    def test_nulls_sort_last_ascending(self, seeded):
        result = screener.screen(
            seeded,
            screener.ScreenRequest(order_by="p_l", descending=False, limit=50),
        )
        tickers = [item["ticker"] for item in result.rows]
        assert tickers[0] == "CCC3"  # lowest P/L
        assert "DDD3" not in tickers

    def test_rows_dropped_for_missing_data_are_counted(self, seeded):
        # A screener that quietly omits part of the market is worse than one
        # that says how much it omitted.
        result = screener.screen(seeded, screener.ScreenRequest(order_by="p_l", limit=50))
        assert result.dropped_for_missing_data >= 1

    def test_ties_break_deterministically_on_ticker(self, seeded):
        seeded.add_all([row("TIE1", market_cap=D(7), pe=D(7)), row("TIE2", market_cap=D(7), pe=D(7))])
        seeded.commit()
        first = screener.screen(seeded, screener.ScreenRequest(order_by="valor_de_mercado", limit=50))
        second = screener.screen(seeded, screener.ScreenRequest(order_by="valor_de_mercado", limit=50))
        assert [r["ticker"] for r in first.rows] == [r["ticker"] for r in second.rows]
        tied = [r["ticker"] for r in first.rows if r["valor_de_mercado"] == D(7)]
        assert tied == ["TIE1", "TIE2"]


class TestFilters:
    def test_gte(self, seeded):
        result = screener.screen(
            seeded,
            screener.ScreenRequest(
                filters=(screener.Filter("dividend_yield_pct", "gte", 9),),
                order_by="dividend_yield_pct",
                limit=50,
            ),
        )
        assert {r["ticker"] for r in result.rows} == {"CCC3", "ZZZ11", "YYY11"}

    def test_lte(self, seeded):
        result = screener.screen(
            seeded,
            screener.ScreenRequest(
                filters=(screener.Filter("p_l", "lte", 10),), order_by="p_l", limit=50
            ),
        )
        assert {r["ticker"] for r in result.rows} == {"AAA3", "CCC3"}

    def test_text_matches_ticker_or_name(self, seeded):
        result = screener.screen(seeded, screener.ScreenRequest(text="ccc", limit=50))
        assert [r["ticker"] for r in result.rows] == ["CCC3"]

    def test_cancelled_registrations_are_excluded_by_default(self, seeded):
        result = screener.screen(seeded, screener.ScreenRequest(limit=50))
        assert "OLD3" not in {r["ticker"] for r in result.rows}

    def test_cancelled_can_be_asked_for(self, seeded):
        result = screener.screen(seeded, screener.ScreenRequest(only_active=False, limit=50))
        assert "OLD3" in {r["ticker"] for r in result.rows}

    def test_unknown_field_is_rejected(self, seeded):
        with pytest.raises(ValueError):
            screener.screen(
                seeded, screener.ScreenRequest(filters=(screener.Filter("lucro_secreto", "gte", 1),))
            )

    def test_unsortable_field_is_rejected(self, seeded):
        with pytest.raises(ValueError):
            screener.screen(seeded, screener.ScreenRequest(order_by="setor"))

    def test_paging_is_stable_across_pages(self, seeded):
        first = screener.screen(
            seeded, screener.ScreenRequest(order_by="valor_de_mercado", limit=3, offset=0)
        )
        second = screener.screen(
            seeded, screener.ScreenRequest(order_by="valor_de_mercado", limit=3, offset=3)
        )
        assert not set(r["ticker"] for r in first.rows) & set(r["ticker"] for r in second.rows)


class TestCategoryGating:
    """A category's currency and instrument rules come from the AI wallet."""

    def test_acoes_never_returns_a_usd_row(self, seeded):
        rows = screener.category_screen(seeded, "ACOES", limit=10)
        assert "USA" not in {item["ticker"] for item in rows}

    def test_fii_never_returns_a_stock(self, seeded):
        rows = screener.category_screen(seeded, "FII", limit=10)
        assert all(item["ticker"].endswith("11") for item in rows)

    def test_returns_nothing_when_disabled(self, seeded):
        # Not enabled in this fixture, so the wallet keeps working exactly as
        # it did before the universe existed.
        assert screener.category_screen(seeded, "ACOES") == []

    def test_returns_nothing_when_too_thin(self, seeded, monkeypatch):
        monkeypatch.setattr(screener.state, "is_enabled", lambda db: True)
        # Only a handful of ACOES rows exist; below the floor it is not worth a
        # prompt block.
        assert screener.category_screen(seeded, "ACOES") == []

    def test_crypto_and_fixed_income_have_no_rows(self, seeded, monkeypatch):
        monkeypatch.setattr(screener.state, "is_enabled", lambda db: True)
        assert screener.category_screen(seeded, "CRIPTO") == []
        assert screener.category_screen(seeded, "RENDA_FIXA") == []

    def test_a_screener_fault_never_propagates(self, seeded, monkeypatch):
        # A generation that works today must not start failing because the
        # universe is broken.
        monkeypatch.setattr(screener.state, "is_enabled", lambda db: True)
        monkeypatch.setattr(screener, "screen", lambda *a, **k: 1 / 0)
        assert screener.category_screen(seeded, "ACOES") == []


class TestDiversify:
    def test_round_robin_interleaves_groups(self):
        rows = [
            {"ticker": "A1", "setor": "Bancos"},
            {"ticker": "A2", "setor": "Bancos"},
            {"ticker": "A3", "setor": "Bancos"},
            {"ticker": "B1", "setor": "Energia"},
            {"ticker": "B2", "setor": "Energia"},
        ]
        out = screener._diversify(rows, "setor", 4)
        # One sector must not fill the list while another waits.
        assert [item["ticker"] for item in out] == ["A1", "B1", "A2", "B2"]

    def test_is_deterministic(self):
        rows = [{"ticker": f"T{i}", "setor": f"S{i % 3}"} for i in range(9)]
        assert screener._diversify(rows, "setor", 5) == screener._diversify(rows, "setor", 5)

    def test_handles_a_single_group(self):
        rows = [{"ticker": "A1", "setor": "X"}, {"ticker": "A2", "setor": "X"}]
        assert len(screener._diversify(rows, "setor", 5)) == 2


class TestCoverageAndHeld:
    def test_coverage_counts_by_market_and_kind(self, seeded):
        summary = screener.coverage(seeded)
        assert summary["total"] == 9
        assert summary["by_market"]["B3"] == 8
        assert summary["by_kind"]["FII"] == 2

    def test_coverage_of_an_empty_universe(self, db):
        assert screener.coverage(db)["total"] == 0

    def test_an_asset_row_alone_does_not_mean_owned(self, seeded):
        """This asserted the opposite until a user noticed the badge lying.

        An ``Asset`` row is created for AI wallet positions, watchlist entries
        and even a ticker whose page was opened once. Only an open position in
        the real portfolio is "na carteira".
        """
        seeded.add(Asset(ticker="AAA3", name="Empresa AAA3", kind="STOCK", currency="BRL"))
        seeded.commit()
        result = screener.screen(seeded, screener.ScreenRequest(text="AAA3", limit=5))
        assert result.rows[0]["na_carteira"] is False


class TestMembershipBadges:
    """"Na carteira" has to mean you own it.

    An ``Asset`` row exists for far more than that: the AI wallets mint one per
    virtual position, the watchlist has its own, and merely opening a ticker's
    page creates one too. Reading ownership off that row told the user they
    held papers they had never bought.
    """

    def _universe(self, db, ticker="TST3"):
        db.add(row(ticker, market_cap=D(100), pe=D(5)))
        db.commit()

    def test_a_virtual_position_is_not_yours(self, db):
        from app.db.models import AiWallet, AiWalletPosition, Asset

        self._universe(db)
        db.add(Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL"))
        wallet = AiWallet(name="IA", provider="anthropic", model="m")
        db.add(wallet)
        db.commit()
        db.add(
            AiWalletPosition(
                wallet_id=wallet.id, category="ACOES", ticker="TST3", name="Teste",
                currency="BRL", quantity=D(10), avg_price=D(1), cost_brl=D(10),
                pending_brl=D(0), is_fixed_income=False,
            )
        )
        db.commit()
        item = screener.screen(db, screener.ScreenRequest(text="TST3", limit=5)).rows[0]
        assert item["na_carteira"] is False
        assert item["na_carteira_ia"] is True

    def test_a_watchlisted_ticker_is_not_owned_either(self, db):
        from app.db.models import Asset, WatchlistItem

        self._universe(db)
        db.add_all(
            [
                Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL"),
                WatchlistItem(ticker="TST3"),
            ]
        )
        db.commit()
        item = screener.screen(db, screener.ScreenRequest(text="TST3", limit=5)).rows[0]
        assert item["na_carteira"] is False
        assert item["na_watchlist"] is True

    def test_merely_browsing_a_ticker_badges_nothing(self, db):
        """Opening an asset page creates a row; that is not a holding."""
        from app.db.models import Asset

        self._universe(db)
        db.add(Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL"))
        db.commit()
        item = screener.screen(db, screener.ScreenRequest(text="TST3", limit=5)).rows[0]
        assert not any(
            item[key] for key in ("na_carteira", "na_carteira_ia", "na_watchlist")
        )

    def test_a_real_open_position_is_yours(self, db, portfolio):
        from datetime import date

        from app.db.models import Asset, Transaction

        self._universe(db)
        asset = Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL")
        db.add(asset)
        db.commit()
        db.add(
            Transaction(
                portfolio_id=portfolio.id, asset_id=asset.id, trade_date=date(2026, 1, 2),
                direction="CREDIT", op_type="BUY", effect="ACQUIRE", quantity=D(10),
                unit_price=D(10), gross_amount=D(100), fees=D(0), taxes=D(0),
                net_amount=D(100), currency="BRL", raw_movement="t", raw_product="p",
                raw_institution="i", dedup_key="k1", occurrence=0,
            )
        )
        db.commit()
        item = screener.screen(db, screener.ScreenRequest(text="TST3", limit=5)).rows[0]
        assert item["na_carteira"] is True

    def test_a_sold_out_position_is_no_longer_yours(self, db, portfolio):
        from datetime import date

        from app.db.models import Asset, Transaction

        self._universe(db)
        asset = Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL")
        db.add(asset)
        db.commit()
        common = dict(
            portfolio_id=portfolio.id, asset_id=asset.id, unit_price=D(10),
            gross_amount=D(100), fees=D(0), taxes=D(0), net_amount=D(100),
            currency="BRL", raw_movement="t", raw_product="p", raw_institution="i",
            occurrence=0,
        )
        db.add_all(
            [
                Transaction(trade_date=date(2026, 1, 2), direction="CREDIT", op_type="BUY",
                            effect="ACQUIRE", quantity=D(10), dedup_key="k1", **common),
                Transaction(trade_date=date(2026, 2, 2), direction="DEBIT", op_type="SELL",
                            effect="DISPOSE", quantity=D(10), dedup_key="k2", **common),
            ]
        )
        db.commit()
        item = screener.screen(db, screener.ScreenRequest(text="TST3", limit=5)).rows[0]
        assert item["na_carteira"] is False


class TestDataVintage:
    def test_each_row_says_how_old_its_figures_are(self, db):
        """Price and fundamentals move on different clocks, so both travel."""
        from datetime import date

        db.add(
            row(
                "TST3", market_cap=D(100), price=D(10),
                price_date=date(2026, 8, 4), fundamentals_period="2026T1 (UDM)",
                price_source="b3-cotahist", fundamentals_source="cvm-dfp",
            )
        )
        db.commit()
        item = screener.screen(db, screener.ScreenRequest(text="TST3", limit=5)).rows[0]
        assert item["preco_em"] == date(2026, 8, 4)
        assert item["exercicio_dos_fundamentos"] == "2026T1 (UDM)"
        assert item["origem_dos_precos"] == "b3-cotahist"
        assert item["origem_dos_fundamentos"] == "cvm-dfp"
