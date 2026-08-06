"""The ratio arithmetic behind the screener.

Pure functions, so these run without a database and cover the cases where a
wrong answer would be worse than no answer: negative equity, a loss-making
company, a zero denominator, and the share-count scale CVM does not publish.
All figures are invented.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.market.universe import compute

D = Decimal


class TestShareScale:
    """CVM publishes no scale for share counts and filers disagree.

    Getting this wrong misprices a company by a factor of a thousand, so the
    rule is: deduce it from the price, or return nothing.
    """

    def test_units_filing_is_recognised(self):
        # 5,73 bi shares, R$ 200 bi equity -> VPA R$ 34,9; at R$ 25 that is a
        # P/VP of 0,72. The thousands reading would give a P/VP of 716.
        shares, scale = compute.resolve_share_scale(D("5730834040"), D("200e9"), D("25"))
        assert scale == "unidades"
        assert shares == D("5730834040")

    def test_thousands_filing_is_recognised(self):
        # Vale's shape: 4 539 007 filed for 4,54 bi shares.
        shares, scale = compute.resolve_share_scale(D("4539007"), D("188.93e9"), D("76.31"))
        assert scale == "milhares"
        assert shares == D("4539007") * 1000

    def test_ambiguous_input_yields_nothing_rather_than_a_guess(self):
        # No price to test against: both readings stay possible, so neither is
        # published. Market cap simply goes missing for this company.
        assert compute.resolve_share_scale(D("1000"), D("1e6"), None) == (None, None)

    def test_missing_equity_yields_nothing(self):
        assert compute.resolve_share_scale(D("1000"), None, D("10")) == (None, None)

    @pytest.mark.parametrize("shares", [None, D(0), D(-5)])
    def test_absent_or_impossible_counts(self, shares):
        assert compute.resolve_share_scale(shares, D("1e9"), D("10")) == (None, None)


class TestValuation:
    def test_price_earnings(self):
        # 100 shares, lucro 1 000 -> LPA 10; a R$ 150 the P/L is 15.
        assert compute.price_earnings(D(150), D(1000), D(100)) == D(15)

    def test_loss_making_company_has_no_pe(self):
        # A negative P/L sorts as "cheapest in the market"; nothing is safer.
        assert compute.price_earnings(D(150), D(-1000), D(100)) is None

    def test_zero_earnings_does_not_raise(self):
        assert compute.price_earnings(D(150), D(0), D(100)) is None

    def test_negative_equity_has_no_price_book(self):
        assert compute.price_book(D(10), D(-500), D(100)) is None

    def test_absurd_ratio_is_rejected(self):
        # Earnings of a centavo over a million shares: arithmetically a P/L of
        # ten billion, and meaningless.
        assert compute.price_earnings(D(100), D("0.01"), D("1e6")) is None

    def test_market_cap(self):
        assert compute.market_cap(D("42.50"), D("1000")) == D("42500.00")

    def test_market_cap_needs_both_sides(self):
        assert compute.market_cap(None, D(1000)) is None
        assert compute.market_cap(D(10), None) is None


class TestProfitability:
    def test_return_on_equity(self):
        assert compute.return_on_equity_pct(D(200), D(1000)) == D(20)

    def test_roe_undefined_on_negative_equity(self):
        # Loss over negative equity computes to a positive ROE — the sign would
        # invert the meaning and rank a distressed company as a strong one.
        assert compute.return_on_equity_pct(D(-200), D(-1000)) is None

    def test_margin(self):
        assert compute.margin_pct(D(150), D(1000)) == D(15)

    def test_margin_without_revenue(self):
        assert compute.margin_pct(D(150), D(0)) is None

    def test_growth(self):
        assert compute.growth_pct(D(120), D(100)) == D(20)

    def test_growth_from_a_loss_is_meaningless(self):
        # -1 to +100 is not "10 100 % growth" in any sense worth screening on.
        assert compute.growth_pct(D(100), D(-1)) is None

    def test_debt_to_equity(self):
        assert compute.debt_to_equity(D(500), D(1000)) == D("0.5")

    def test_debt_to_equity_on_negative_equity(self):
        assert compute.debt_to_equity(D(500), D(-1000)) is None


class TestIncome:
    def test_dividend_yield(self):
        assert compute.dividend_yield_pct(D(80), D(1000)) == D(8)

    def test_no_dividends_is_none_not_zero(self):
        # "Did not pay" and "we have no figure" must not look the same; a 0 %
        # yield is a claim, and this data cannot support it.
        assert compute.dividend_yield_pct(None, D(1000)) is None

    def test_payout(self):
        assert compute.payout_pct(D(50), D(200)) == D(25)

    def test_payout_needs_positive_earnings(self):
        assert compute.payout_pct(D(50), D(-200)) is None


class TestQuantization:
    def test_big_money_is_whole_units(self):
        # SQLite stores Numeric through a float that quantizes past fifteen
        # significant digits; whole units keep a trillion-real cap exact.
        assert compute.quantize_big(D("3500000000000.987")) == D("3500000000001")

    def test_ratio_keeps_six_decimals(self):
        assert compute.quantize_ratio(D("12.3456789")) == D("12.345679")

    def test_none_passes_through(self):
        assert compute.quantize_big(None) is None
        assert compute.quantize_ratio(None) is None
        assert compute.quantize_money(None) is None
