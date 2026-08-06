"""The bulk-file parsers, against synthetic fixtures built in memory.

No network and no real financial data: every ticker, CNPJ and figure here is
invented, per the repo's data-sensitivity rule. What is *not* invented is the
shape — the layouts and column names mirror what the live files were verified
to publish, so a source changing shape breaks these rather than production.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from app.market.universe.sources import (
    SourceShapeError,
    cotahist,
    cvm_fii,
    digits,
    read_csv,
    read_zip_csv,
    require_columns,
    to_decimal,
)


# ---------------------------------------------------------------------------
# Helpers


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def cotahist_line(
    *,
    day: str = "20260804",
    codbdi: str = "02",
    ticker: str = "TST3",
    tpmerc: str = "010",
    name: str = "TESTE SA",
    especi: str = "ON",
    close: str = "1000",  # two implied decimals -> 10,00
    volume: str = "500000",  # -> 5 000,00
    isin: str = "BRTSTAACNOR0",
    tipreg: str = "01",
) -> bytes:
    """One 245-byte COTAHIST record, at the verified offsets."""
    line = bytearray(b" " * 245)

    def put(start: int, end: int, text: str, numeric: bool = False) -> None:
        width = end - start
        raw = text.rjust(width, "0") if numeric else text.ljust(width)
        line[start:end] = raw[:width].encode("latin-1")

    put(0, 2, tipreg)
    put(2, 10, day)
    put(10, 12, codbdi)
    put(12, 24, ticker)
    put(24, 27, tpmerc)
    put(27, 39, name)
    put(39, 49, especi)
    put(108, 121, close, numeric=True)
    put(152, 170, "100", numeric=True)
    put(170, 188, volume, numeric=True)
    put(230, 242, isin)
    return bytes(line) + b"\n"


# ---------------------------------------------------------------------------
# Shared helpers


class TestSharedHelpers:
    def test_cnpj_reduces_to_digits(self):
        # B3 publishes bare, CVM punctuated. Digits are the only common form,
        # which is what makes every join here exact instead of fuzzy.
        assert digits("08.773.135/0001-00") == "08773135000100"
        assert digits("46639922000144") == "46639922000144"

    def test_cnpj_pads_to_fourteen(self):
        assert digits("123") == "00000000000123"

    def test_decimal_treats_blank_and_dash_as_absent(self):
        assert to_decimal("") is None
        assert to_decimal("-") is None
        assert to_decimal(None) is None

    def test_decimal_keeps_dotted_notation(self):
        # These datasets use dotted decimals; swapping separators the way the
        # Tesouro feed needs would turn 1.234 into 1234.
        assert to_decimal("1.234") == Decimal("1.234")

    def test_unparsable_value_is_absent_not_zero(self):
        assert to_decimal("N/A") is None

    def test_require_columns_names_what_is_missing(self):
        with pytest.raises(SourceShapeError) as excinfo:
            require_columns("arquivo", ["A", "B"], {"A", "B", "C", "D"})
        assert "C" in str(excinfo.value) and "D" in str(excinfo.value)

    def test_require_columns_passes_when_present(self):
        require_columns("arquivo", ["A", "B", "C"], {"A", "B"})


class TestCsvReading:
    def test_latin1_and_semicolons(self):
        raw = "NOME;SETOR\nAÇÚCAR S.A.;Energia Elétrica\n".encode("latin-1")
        rows = list(read_csv(raw, {"NOME", "SETOR"}))
        assert rows[0]["NOME"] == "AÇÚCAR S.A."
        assert rows[0]["SETOR"] == "Energia Elétrica"

    def test_missing_column_raises_before_any_row_is_used(self):
        raw = b"NOME\nX\n"
        with pytest.raises(SourceShapeError):
            list(read_csv(raw, {"NOME", "CNPJ_CIA"}))

    def test_zip_member_absence_is_a_shape_error(self):
        payload = make_zip({"presente.csv": b"A\n1\n"})
        with pytest.raises(SourceShapeError):
            list(read_zip_csv(payload, "ausente.csv", set()))


# ---------------------------------------------------------------------------
# COTAHIST


class TestCotahist:
    def test_reduces_a_single_session(self):
        payload = make_zip({"COTAHIST.TXT": cotahist_line()})
        result = cotahist.reduce_archive(payload)
        row = result["TST3"]
        assert row.last_close == Decimal("10.00")
        assert row.last_date == date(2026, 8, 4)
        assert row.isin == "BRTSTAACNOR0"
        assert row.name == "TESTE SA"

    def test_only_the_cash_market_is_kept(self):
        # Options and the fractional market share the file; screening them
        # would drown the roster in strikes.
        payload = make_zip(
            {
                "C.TXT": cotahist_line(ticker="TST3", tpmerc="010")
                + cotahist_line(ticker="TSTA772", tpmerc="080", codbdi="82")
                + cotahist_line(ticker="TST3F", tpmerc="020", codbdi="96")
            }
        )
        assert set(cotahist.reduce_archive(payload)) == {"TST3"}

    def test_codbdi_separates_fii_from_etf(self):
        # The whole reason CODBDI is preferred over the ticker suffix: both of
        # these end in 11 and no ticker shape can tell them apart.
        payload = make_zip(
            {
                "C.TXT": cotahist_line(ticker="AAA11", codbdi="12", especi="CI ER")
                + cotahist_line(ticker="BBB11", codbdi="14", especi="CI")
                + cotahist_line(ticker="CCC34", codbdi="34", especi="DRN")
            }
        )
        result = cotahist.reduce_archive(payload)
        assert result["AAA11"].kind == "FII"
        assert result["BBB11"].kind == "ETF"
        assert result["CCC34"].kind == "BDR"

    def test_header_and_trailer_records_are_ignored(self):
        payload = make_zip(
            {
                "C.TXT": cotahist_line(tipreg="00")
                + cotahist_line(ticker="TST3")
                + cotahist_line(tipreg="99")
            }
        )
        assert set(cotahist.reduce_archive(payload)) == {"TST3"}

    def test_archives_fold_together_additively(self):
        # Twelve monthly files must build the same window as one annual file.
        first = make_zip({"C.TXT": cotahist_line(day="20260701", close="1000")})
        second = make_zip({"C.TXT": cotahist_line(day="20260801", close="1200")})
        result = cotahist.reduce_archive(first)
        cotahist.reduce_archive(second, result)
        row = result["TST3"]
        assert row.sessions == 2
        assert row.last_close == Decimal("12.00")
        assert row.high == Decimal("12.00")
        assert row.low == Decimal("10.00")

    def test_window_figures_need_enough_sessions(self):
        # A 12-month return computed over two sessions is noise, not a return.
        payload = make_zip({"C.TXT": cotahist_line(day="20260801") + cotahist_line(day="20260804")})
        row = cotahist.reduce_archive(payload)["TST3"]
        assert row.change_12m_pct is None
        assert row.volatility_pct is None

    def test_change_and_extremes_over_a_full_window(self):
        lines = b"".join(
            cotahist_line(day=f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", close=str(1000 + i * 10))
            for i in range(cotahist.MIN_SESSIONS_FOR_12M + 5)
        )
        row = cotahist.reduce_archive(make_zip({"C.TXT": lines}))["TST3"]
        assert row.change_12m_pct is not None and row.change_12m_pct > 0
        assert row.low == Decimal("10.00")
        assert row.traded_days == cotahist.MIN_SESSIONS_FOR_12M + 5

    def test_52_week_window_does_not_grow_past_a_year(self):
        # Feeding two years must still leave a 52-week high, not a 2-year one.
        lines = b"".join(
            cotahist_line(day="20260101", close=str(9000 - i))
            for i in range(cotahist.YEAR_SESSIONS + 60)
        )
        row = cotahist.reduce_archive(make_zip({"C.TXT": lines}))["TST3"]
        assert row.traded_days == cotahist.YEAR_SESSIONS
        # The 60 oldest (and highest) closes fell out of the window.
        assert row.high < Decimal("90.00")

    def test_a_file_with_no_cash_market_records_is_a_shape_error(self):
        # Silence here would look like "the market listed nothing today".
        payload = make_zip({"C.TXT": cotahist_line(tpmerc="080", codbdi="82")})
        with pytest.raises(SourceShapeError):
            cotahist.reduce_archive(payload)

    def test_a_non_zip_download_is_a_shape_error(self):
        with pytest.raises(SourceShapeError):
            cotahist.reduce_archive(b"<html>error page</html>")


# ---------------------------------------------------------------------------
# FII informes


def fii_zip(year: int) -> bytes:
    geral = (
        "Tipo_Fundo_Classe;CNPJ_Fundo_Classe;Data_Referencia;Versao;Nome_Fundo_Classe;"
        "Codigo_ISIN;Segmento_Atuacao;Tipo_Gestao\n"
        f"Classe;11.111.111/0001-11;{year}-01-01;1;FUNDO TESTE;BRAAA1CTF001;Shoppings;Ativa\n"
        f"Classe;11.111.111/0001-11;{year}-02-01;1;FUNDO TESTE;BRAAA1CTF001;Shoppings;Ativa\n"
    ).encode("latin-1")
    complemento = (
        "CNPJ_Fundo_Classe;Data_Referencia;Versao;Patrimonio_Liquido;Cotas_Emitidas;"
        "Valor_Patrimonial_Cotas;Percentual_Dividend_Yield_Mes\n"
        f"11.111.111/0001-11;{year}-01-01;1;1000000;10000;100.0;0.008\n"
        f"11.111.111/0001-11;{year}-02-01;1;1100000;10000;110.0;0.009\n"
    ).encode("latin-1")
    return make_zip(
        {
            f"inf_mensal_fii_geral_{year}.csv": geral,
            f"inf_mensal_fii_complemento_{year}.csv": complemento,
        }
    )


class TestFiiInformes:
    def _folded(self, year: int = 2026):
        funds: dict[str, cvm_fii.FundInfo] = {}
        cvm_fii._fold(fii_zip(year), year, funds, {})
        return funds["11111111000111"]

    def test_registry_fields_come_from_the_newest_month(self):
        record = self._folded()
        assert record.segment == "Shoppings"
        assert record.isin == "BRAAA1CTF001"
        assert record.management == "Ativa"

    def test_balance_figures_are_the_newest_month_not_a_sum(self):
        record = self._folded()
        assert record.net_assets == Decimal("1100000")
        assert record.book_value_per_quota == Decimal("110.0")

    def test_monthly_yields_are_percentages_not_fractions(self):
        # CVM files 0,008 for a 0,8 % month; storing the fraction would show
        # every fund yielding under one percent a year.
        record = self._folded()
        assert dict(record.monthly_yields)["2026-01"] == Decimal("0.800")

    def test_yield_needs_enough_months(self):
        # Two months annualised six-fold is a fabrication, not a yield.
        assert self._folded().dividend_yield_pct is None

    def test_yield_annualises_a_partial_year(self):
        record = self._folded()
        record.monthly_yields = [(f"2026-{m:02d}", Decimal("0.5")) for m in range(1, 7)]
        # Six months at 0,5 % -> 3 % observed, annualised to 6 %.
        assert record.dividend_yield_pct == Decimal(6)

    def test_full_year_is_summed_not_compounded(self):
        record = self._folded()
        record.monthly_yields = [(f"2026-{m:02d}", Decimal("1")) for m in range(1, 13)]
        # A distribution rate on the quota, not a reinvested return.
        assert record.dividend_yield_pct == Decimal(12)

    def test_by_isin_rekeys_for_the_cotahist_join(self):
        funds = {"x": self._folded()}
        assert set(cvm_fii.by_isin(funds)) == {"BRAAA1CTF001"}

    def test_missing_column_is_a_shape_error(self):
        broken = make_zip(
            {
                "inf_mensal_fii_geral_2026.csv": b"CNPJ_Fundo_Classe\n1\n",
                "inf_mensal_fii_complemento_2026.csv": b"CNPJ_Fundo_Classe\n1\n",
            }
        )
        with pytest.raises(SourceShapeError):
            cvm_fii._fold(broken, 2026, {}, {})


# ---------------------------------------------------------------------------
# Trailing twelve months from the quarterly filings


def itr_row(**over) -> str:
    base = {
        "CNPJ_CIA": "11.111.111/0001-11", "DT_REFER": "2026-06-30", "VERSAO": "1",
        "DENOM_CIA": "TESTE SA", "CD_CVM": "1234", "GRUPO_DFP": "DF Consolidado",
        "MOEDA": "REAL", "ESCALA_MOEDA": "MIL", "ORDEM_EXERC": "ÚLTIMO",
        "DT_INI_EXERC": "2026-01-01", "DT_FIM_EXERC": "2026-06-30",
        "CD_CONTA": "3.01", "DS_CONTA": "Receita de Venda", "VL_CONTA": "0",
        "ST_CONTA_FIXA": "S",
    }
    base.update(over)
    return ";".join(str(base[k]) for k in ITR_HEADER.split(";"))


ITR_HEADER = (
    "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;GRUPO_DFP;MOEDA;ESCALA_MOEDA;"
    "ORDEM_EXERC;DT_INI_EXERC;DT_FIM_EXERC;CD_CONTA;DS_CONTA;VL_CONTA;ST_CONTA_FIXA"
)


def dre_member(rows: list[str]) -> bytes:
    return (ITR_HEADER + "\n" + "\n".join(rows) + "\n").encode("latin-1")


class TestQuarterlyPeriods:
    """A Q2 filing carries four variants of every income line.

    This year and last, each as the quarter alone and as the year to date.
    Reading the quarter where the year-to-date was meant halves a half-year.
    """

    def _read(self, rows: list[str]) -> "cvm_statements.Quarterly":
        from app.market.universe.sources import cvm_statements

        payload = make_zip({"itr_cia_aberta_DRE_con_2026.csv": dre_member(rows)})
        out: dict = {}
        cvm_statements._collect_quarterly_flows(
            payload, "itr_cia_aberta_DRE_con_2026.csv", out, cvm_statements._BY_CODE
        )
        return out["11111111000111"]

    def test_year_to_date_is_taken_not_the_quarter(self):
        record = self._read(
            [
                # the accumulated column and the quarter alone, both ÚLTIMO
                itr_row(DT_INI_EXERC="2026-01-01", VL_CONTA="102000"),
                itr_row(DT_INI_EXERC="2026-04-01", VL_CONTA="53000"),
            ]
        )
        assert record.ytd["revenue"] == Decimal("102000") * 1000

    def test_the_prior_year_twin_is_kept_separately(self):
        record = self._read(
            [
                itr_row(DT_INI_EXERC="2026-01-01", VL_CONTA="102000"),
                itr_row(
                    ORDEM_EXERC="PENÚLTIMO", DT_INI_EXERC="2025-01-01",
                    DT_FIM_EXERC="2025-06-30", VL_CONTA="97000",
                ),
                itr_row(
                    ORDEM_EXERC="PENÚLTIMO", DT_INI_EXERC="2025-04-01",
                    DT_FIM_EXERC="2025-06-30", VL_CONTA="50000",
                ),
            ]
        )
        assert record.ytd["revenue"] == Decimal("102000") * 1000
        assert record.ytd_prior["revenue"] == Decimal("97000") * 1000

    def test_the_quarter_label_reads_as_brazilians_write_it(self):
        assert self._read([itr_row(VL_CONTA="1")]).label == "2026T2"
        record = self._read([itr_row(DT_REFER="2026-03-31", DT_FIM_EXERC="2026-03-31", VL_CONTA="1")])
        assert record.label == "2026T1"

    def test_a_total_is_not_displaced_by_a_line_inside_it(self):
        record = self._read(
            [
                itr_row(CD_CONTA="3.01", VL_CONTA="102000"),
                itr_row(CD_CONTA="3.01.01", DS_CONTA="Receita de Juros", VL_CONTA="70000"),
            ]
        )
        assert record.ytd["revenue"] == Decimal("102000") * 1000


class TestTrailingTwelveMonths:
    def _annual(self, **over) -> "cvm_statements.Fundamentals":
        from app.market.universe.sources import cvm_statements

        base = dict(
            cnpj="11111111000111", period="2025", revenue=Decimal(1000),
            net_income=Decimal(100), equity=Decimal(500),
        )
        base.update(over)
        return cvm_statements.Fundamentals(**base)

    def _quarter(self, **over) -> "cvm_statements.Quarterly":
        from app.market.universe.sources import cvm_statements

        base = dict(
            cnpj="11111111000111", refer="2026-06-30", fiscal_year=2026,
            ytd={"revenue": Decimal(600), "net_income": Decimal(70)},
            ytd_prior={"revenue": Decimal(500), "net_income": Decimal(50)},
        )
        base.update(over)
        return cvm_statements.Quarterly(**base)

    def test_the_formula_is_annual_minus_last_years_stub_plus_this_years(self):
        from app.market.universe.sources import cvm_statements

        annual = self._annual()
        assert cvm_statements._apply_quarterly(annual, self._quarter()) is True
        assert annual.revenue == Decimal(1000) - Decimal(500) + Decimal(600)
        assert annual.net_income == Decimal(100) - Decimal(50) + Decimal(70)
        assert annual.period == "2026T2 (UDM)"
        assert annual.basis == "udm"

    def test_growth_compares_the_matching_spans(self):
        # 600 against 500 over the identical half-year, not a trailing total
        # against a calendar year that ends somewhere else.
        from app.market.universe.sources import cvm_statements

        annual = self._annual()
        cvm_statements._apply_quarterly(annual, self._quarter())
        assert annual.revenue_growth_pct == Decimal(20)

    def test_the_newest_balance_wins(self):
        from app.market.universe.sources import cvm_statements

        annual = self._annual()
        cvm_statements._apply_quarterly(
            annual, self._quarter(equity=Decimal(560), shares_outstanding=Decimal(42))
        )
        assert annual.equity == Decimal(560)
        assert annual.shares_outstanding == Decimal(42)

    def test_a_mismatched_annual_year_is_refused(self):
        """Rolling FY2023 onto a 2026 quarter would span the wrong months."""
        from app.market.universe.sources import cvm_statements

        annual = self._annual(period="2023")
        assert cvm_statements._apply_quarterly(annual, self._quarter()) is False
        assert annual.period == "2023" and annual.basis == "anual"

    def test_a_missing_component_leaves_the_annual_figure_alone(self):
        from app.market.universe.sources import cvm_statements

        annual = self._annual()
        quarter = self._quarter(ytd_prior={})  # no prior-year span to subtract
        assert cvm_statements._apply_quarterly(annual, quarter) is False
        assert annual.revenue == Decimal(1000)

    def test_an_implausible_result_falls_back(self):
        """A tenfold jump means the periods did not line up, not a tenfold year."""
        from app.market.universe.sources import cvm_statements

        annual = self._annual()
        quarter = self._quarter(ytd={"revenue": Decimal(20000)}, ytd_prior={"revenue": Decimal(10)})
        assert cvm_statements._apply_quarterly(annual, quarter) is False
        assert annual.revenue == Decimal(1000)
        assert annual.basis == "anual"


class TestCorporateActions:
    """COTAHIST publishes prices as traded, never split-adjusted.

    A market-wide scan found 208 of 2 479 tickers with a session-to-session
    discontinuity, and the file's own quotation-factor field flagged exactly
    one of them — so a grupamento reads as a several-hundred-percent return
    unless the series is restated or withheld.
    """

    def _series(self, prices: list[str], start_day: int = 1) -> cotahist.Reduction:
        lines = b"".join(
            cotahist_line(day=f"202601{start_day + i:02d}", close=price)
            for i, price in enumerate(prices)
        )
        return cotahist.reduce_archive(make_zip({"C.TXT": lines}))["TST3"]

    def test_a_reverse_split_is_absorbed(self):
        # 1:10 grupamento at R$ 1,00 -> R$ 10,00. The paper did not gain 900 %.
        row = self._series(["100", "100", "1000", "1000"])
        assert row.splits == 1
        assert row.discontinuous is False
        # History was restated onto the new basis, so the range is coherent.
        assert row.high == Decimal("10.00")
        assert row.low == Decimal("10.00")

    def test_a_forward_split_is_absorbed(self):
        row = self._series(["1000", "1000", "250", "250"])  # 4:1
        assert row.splits == 1 and row.discontinuous is False
        assert row.high == Decimal("2.50")

    def test_ordinary_volatility_is_not_a_split(self):
        row = self._series(["1000", "1200", "900", "1100"])
        assert row.splits == 0 and row.discontinuous is False

    def test_an_unrecognisable_jump_withholds_the_window(self):
        # 7,3x matches no ratio a corporate action uses; guessing one would
        # fabricate a return, so nothing is published for this paper.
        row = self._series(["1000", "7300"])
        assert row.discontinuous is True
        assert row.change_12m_pct is None
        assert row.high is None and row.low is None

    def test_penny_prices_are_never_treated_as_splits(self):
        """At R$ 0,03 a single centavo is a third of the price.

        The first version of this detector reported ALZR11 splitting fifty
        times in a year, which is tick noise, not a corporate action.
        """
        row = self._series(["1", "3", "1", "3"])
        assert row.splits == 0
        assert row.discontinuous is True

    def test_a_series_that_keeps_jumping_is_disbelieved(self):
        row = self._series(["1000", "10000", "1000", "10000", "1000", "10000"])
        assert row.discontinuous is True
        assert row.splits <= cotahist.MAX_SPLITS

    def test_an_adjusted_series_still_reports_a_return(self):
        prices = ["1000"] * 40 + ["10000"] * (cotahist.MIN_SESSIONS_FOR_12M + 5)
        lines = b"".join(
            cotahist_line(day=f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", close=price)
            for i, price in enumerate(prices)
        )
        row = cotahist.reduce_archive(make_zip({"C.TXT": lines}))["TST3"]
        # Flat before and after a 1:10 restatement is a flat year, not +900 %.
        assert row.discontinuous is False
        assert row.change_12m_pct == Decimal(0)


# ---------------------------------------------------------------------------
# US fundamentals — SEC bulk XBRL


SUB_HEADER = "adsh\tcik\tname\tsic\tform\tperiod\tfy\tfp\tfiled"
NUM_HEADER = "adsh\ttag\tddate\tqtrs\tuom\tsegments\tcoreg\tvalue"


def sec_zip(subs: list[str], nums: list[str]) -> bytes:
    return make_zip(
        {
            "sub.txt": (SUB_HEADER + "\n" + "\n".join(subs) + "\n").encode(),
            "num.txt": (NUM_HEADER + "\n" + "\n".join(nums) + "\n").encode(),
        }
    )


def sub(adsh="a1", cik="99", name="TESTE INC", sic="3571", form="10-K", fy="2026", fp="FY"):
    return f"{adsh}\t{cik}\t{name}\t{sic}\t{form}\t20260331\t{fy}\t{fp}\t20260401"


def num(tag, value, qtrs=4, ddate="20260331", uom="USD", adsh="a1", segments="", coreg=""):
    return f"{adsh}\t{tag}\t{ddate}\t{qtrs}\t{uom}\t{segments}\t{coreg}\t{value}"


class TestSecFundamentals:
    """The SEC's bulk datasets, which is what makes a US leg possible at all.

    ``qtrs`` carries the span of each fact, so a trailing figure needs no
    assembly where a company files an annual one — verified against NVIDIA's
    10-K, whose ``qtrs=4`` net income matches its published fiscal year.
    """

    def _read(self, payload: bytes):
        from app.market.universe.sources import sec_financials as sf

        facts: dict = {}
        sf._read_quarter(payload, facts)
        return {cik: sf._reduce(cik, rec) for cik, rec in facts.items()}

    def test_an_annual_fact_is_the_trailing_figure(self):
        out = self._read(sec_zip([sub()], [num("Revenues", "1000"), num("NetIncomeLoss", "100")]))
        record = out["99"]
        assert record.revenue == Decimal(1000)
        assert record.net_income == Decimal(100)

    def test_quarterly_only_filers_are_summed_into_a_year(self):
        nums = [
            num("Revenues", "250", qtrs=1, ddate=f"2026{m:02d}31") for m in (3,)
        ] + [
            num("Revenues", "250", qtrs=1, ddate=d)
            for d in ("20251231", "20250930", "20250630")
        ]
        record = self._read(sec_zip([sub(form="10-Q")], nums))["99"]
        assert record.revenue == Decimal(1000)
        assert record.basis == "udm"

    def test_three_quarters_is_not_a_year(self):
        nums = [
            num("Revenues", "250", qtrs=1, ddate=d)
            for d in ("20260331", "20251231", "20250930")
        ]
        assert self._read(sec_zip([sub()], nums))["99"].revenue is None

    def test_balance_items_take_the_newest_date(self):
        nums = [
            num("StockholdersEquity", "500", qtrs=0, ddate="20250331"),
            num("StockholdersEquity", "800", qtrs=0, ddate="20260331"),
        ]
        assert self._read(sec_zip([sub()], nums))["99"].equity == Decimal(800)

    def test_segment_breakdowns_are_not_the_company(self):
        nums = [
            num("Revenues", "1000"),
            num("Revenues", "400", segments="ProductOrService=Widgets"),
        ]
        assert self._read(sec_zip([sub()], nums))["99"].revenue == Decimal(1000)

    def test_foreign_currency_facts_are_ignored(self):
        # A figure in euros cannot be compared to anything else here.
        nums = [num("Revenues", "9999", uom="EUR"), num("Revenues", "1000")]
        assert self._read(sec_zip([sub()], nums))["99"].revenue == Decimal(1000)

    def test_the_preferred_tag_wins_and_is_not_mixed(self):
        # Both spellings exist for revenue; a company must use one of them
        # throughout, never a total from one and a comparative from the other.
        nums = [
            num("RevenueFromContractWithCustomerExcludingAssessedTax", "800"),
            num("Revenues", "1000"),
        ]
        assert self._read(sec_zip([sub()], nums))["99"].revenue == Decimal(1000)

    def test_prior_year_comes_from_the_older_annual_fact(self):
        nums = [
            num("Revenues", "1000", ddate="20260331"),
            num("Revenues", "800", ddate="20250331"),
        ]
        record = self._read(sec_zip([sub()], nums))["99"]
        assert record.revenue == Decimal(1000)
        assert record.prior_revenue == Decimal(800)

    def test_a_reit_declares_itself_by_sic(self):
        from app.market.universe.sources import sec_financials as sf

        assert sf.kind_for("6798") == "REIT"
        assert sf.kind_for("3571") == "STOCK_INTL"

    def test_sector_prefers_the_narrower_range(self):
        """Otherwise Microsoft lands in "business services" and Apple in
        "machinery" — both are technology companies."""
        from app.market.universe.sources import sec_financials as sf

        assert sf.sector_for("7372") == "Software e Tecnologia"
        assert sf.sector_for("3571") == "Tecnologia (Hardware)"
        assert sf.sector_for("3674") == "Semicondutores"
        assert sf.sector_for("9999") is None

    def test_a_missing_column_is_a_shape_error(self):
        from app.market.universe.sources import sec_financials as sf

        broken = make_zip({"sub.txt": b"adsh\tcik\n1\t2\n", "num.txt": b"adsh\ttag\n1\tX\n"})
        with pytest.raises(SourceShapeError):
            sf._read_quarter(broken, {})

    def test_quarter_labels_walk_backwards_from_today(self):
        from app.market.universe.sources import sec_financials as sf

        assert sf._recent_quarters(date(2026, 8, 6), 3) == ["2026q2", "2026q1", "2025q4"]
        assert sf._recent_quarters(date(2026, 1, 15), 2) == ["2025q4", "2025q3"]


class TestSecIdentification:
    """The SEC serves nothing without an e-mail-shaped contact.

    Measured back to back from one container: a bare client name gets 403, the
    same name with a contact address gets 200. An earlier round of tests from
    the host suggested otherwise and did not reproduce — which is why this is
    pinned rather than remembered.
    """

    def _agent(self, db, value):
        from app.db.models import AppSetting
        from app.market.universe.sources import sec

        db.merge(AppSetting(key=sec.SETTING_KEY, value={"value": value}))
        db.commit()

    def test_a_contact_address_is_accepted(self, db):
        from app.market.universe.sources import sec

        self._agent(db, "GumbInvest/1.0 (contato: alias@meudominio.com)")
        assert "alias@meudominio.com" in sec.check_user_agent(db)

    def test_a_bare_name_is_refused(self, db):
        """It would 403 at the SEC; refusing here explains why."""
        from app.market.universe.sources import sec

        self._agent(db, "GumbInvest/1.0 (self-hosted)")
        with pytest.raises(sec.UserAgentNotConfigured):
            sec.check_user_agent(db)

    def test_a_url_is_refused(self, db):
        from app.market.universe.sources import sec

        self._agent(db, "GumbInvest/1.0 (+https://github.com/x/y)")
        with pytest.raises(sec.UserAgentNotConfigured):
            sec.check_user_agent(db)

    def test_the_shipped_placeholder_is_refused(self, db):
        """Every install would otherwise send one identical address."""
        from app.market.universe.sources import sec

        self._agent(db, "GumbInvest/1.0 (self-hosted; contact: admin@example.com)")
        with pytest.raises(sec.UserAgentNotConfigured):
            sec.check_user_agent(db)

    def test_nothing_is_invented_when_none_is_set(self, db):
        # A generated contact would be a lie, and a 403 nobody could explain.
        from app.market.universe.sources import sec

        assert sec.resolve_user_agent(db) == ""
        with pytest.raises(sec.UserAgentNotConfigured):
            sec.check_user_agent(db)

    def test_the_refusal_offers_the_way_out(self, db):
        """Leaving the US market off costs nothing Brazilian."""
        from app.market.universe.sources import sec

        self._agent(db, "")
        with pytest.raises(sec.UserAgentNotConfigured) as excinfo:
            sec.check_user_agent(db)
        message = str(excinfo.value)
        assert "alias" in message and "desmarque o mercado EUA" in message
