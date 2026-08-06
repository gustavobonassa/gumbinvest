"""The staged ingest: idempotency, resume, cancel, and the two invariants.

The invariants are the reason this file exists. Both are the kind that erode
quietly during a refactor and cause damage nowhere near the change:

* **No Asset rows.** ``market.service.quotable_assets`` treats an asset with no
  transactions as watch-only and refreshes its quote every half hour, so a
  universe that minted Asset rows would turn a 30-minute job into two thousand
  outbound requests.
* **No per-ticker HTTP.** The whole design rests on bulk files; a well-meant
  "just fetch the missing ones from Yahoo" would reintroduce exactly the
  pattern the feature exists to avoid.

Sources are stubbed — nothing here touches the network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import Asset, AppSetting, AssetUniverse
from app.market.universe import ingest, state
from app.market.universe.sources import SourceShapeError, cotahist, cvm_fii, cvm_statements, registry

D = Decimal


def reduction(ticker: str, kind: str = "STOCK", **fields) -> cotahist.Reduction:
    item = cotahist.Reduction(
        ticker=ticker, kind=kind, isin=f"BR{ticker[:4]}CTF001", name=f"Nome {ticker}"
    )
    for day in range(1, 4):
        item.observe(__import__("datetime").date(2026, 8, day), D("10.00"), D("1000"))
    for key, value in fields.items():
        setattr(item, key, value)
    return item


def full_run(db, markets=("B3",)) -> dict:
    """start -> slice -> finish, the way ``run_ingest`` sequences a real run.

    Tests that run the ingest twice must close the first block, because the
    persisted run block is a real lock — leaving it open is exactly the state
    a killed process leaves behind.
    """
    block = state.start(db, list(markets))
    finished = ingest.ingest_slice(db, block)
    state.finish(db, block, "done" if finished else "paused")
    return block


@pytest.fixture
def stub_sources(monkeypatch):
    """Replace every network call with a fixed, inspectable payload."""
    calls: list[str] = []

    def fake_reduce(urls, into=None, since=None, on_file=None):
        calls.append("cotahist")
        result = into if into is not None else {}
        if on_file is not None:
            on_file(0, 1, "COTAHIST_TEST.ZIP")
        for ticker, kind in (("TST3", "STOCK"), ("TST4", "STOCK"), ("AAA11", "FII")):
            result[ticker] = reduction(ticker, kind)
        return result

    monkeypatch.setattr(cotahist, "fetch_and_reduce", fake_reduce)
    monkeypatch.setattr(
        registry,
        "fetch_b3_companies",
        lambda: (calls.append("b3"), [
            registry.Company(
                root="TST", name="Teste S.A.", cnpj="11111111000111",
                cvm_code="1234", segment="Novo Mercado", status="ATIVO",
            )
        ])[1],
    )
    monkeypatch.setattr(
        registry,
        "fetch_cvm_registry",
        lambda: (calls.append("cvm"), {
            "11111111000111": registry.CvmCompany(
                cnpj="11111111000111", name="TESTE S.A.", cvm_code="1234",
                sector="Energia Elétrica", status="ATIVO",
            )
        })[1],
    )
    monkeypatch.setattr(
        cvm_statements,
        "fetch",
        lambda: (calls.append("dfp"), ({
            "11111111000111": cvm_statements.Fundamentals(
                cnpj="11111111000111", period="2025", revenue=D("1000000"),
                net_income=D("100000"), equity=D("500000"), debt=D("250000"),
                dividends_paid=D("40000"), shares_outstanding=D("50000"),
                prior_revenue=D("800000"), prior_net_income=D("80000"),
            )
        }, []))[1],
    )
    monkeypatch.setattr(
        cvm_fii,
        "fetch",
        lambda: (calls.append("fii"), ({
            "22222222000122": cvm_fii.FundInfo(
                cnpj="22222222000122", isin="BRAAA1CTF001", name="Fundo Teste",
                segment="Papel", management="Ativa", net_assets=D("1000000"),
                quotas=D("10000"), book_value_per_quota=D("100"),
                monthly_yields=[(f"2026-{m:02d}", D("1")) for m in range(1, 13)],
                period="2026-07",
            )
        }, []))[1],
    )
    return calls


class TestFullRun:
    def test_populates_the_universe(self, db, stub_sources):
        block = state.start(db, ["B3"])
        assert ingest.ingest_slice(db, block) is True
        rows = {row.ticker: row for row in db.query(AssetUniverse).all()}
        assert set(rows) == {"TST3", "TST4", "AAA11"}
        assert rows["TST3"].price == D("10.000000")
        assert rows["AAA11"].kind == "FII"

    def test_never_creates_asset_rows(self, db, stub_sources):
        """Load-bearing: Asset rows enter the half-hourly quote refresh."""
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        assert db.query(Asset).count() == 0

    def test_makes_no_per_ticker_requests(self, db, stub_sources):
        """One call per bulk source, regardless of how many tickers exist."""
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        # Four sources, four calls — not one per instrument.
        assert sorted(stub_sources) == ["b3", "cotahist", "cvm", "dfp", "fii"][:len(stub_sources)]
        assert len(stub_sources) == 5

    def test_registry_enriches_matching_tickers(self, db, stub_sources):
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        row = db.query(AssetUniverse).filter_by(ticker="TST3").one()
        assert row.sector == "Energia Elétrica"
        assert row.cnpj == "11111111000111"
        assert row.b3_segment == "Novo Mercado"

    def test_fundamentals_are_computed_from_the_filings(self, db, stub_sources):
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        row = db.query(AssetUniverse).filter_by(ticker="TST3").one()
        # equity 500 000 / 50 000 shares -> VPA 10; price 10 -> P/VP 1.
        assert row.pb == D("1.000000")
        assert row.roe_pct == D("20.000000")  # 100 000 / 500 000
        assert row.net_margin_pct == D("10.000000")
        assert row.revenue_growth_pct == D("25.000000")
        assert row.fundamentals_period == "2025"

    def test_fii_rows_come_from_the_informe(self, db, stub_sources):
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        row = db.query(AssetUniverse).filter_by(ticker="AAA11").one()
        assert row.fund_segment == "Papel"
        assert row.fii_pl == D("1000000.00")
        assert row.dividend_yield_pct == D("12.000000")

    def test_us_stage_is_skipped_when_not_requested(self, db, stub_sources):
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        assert db.query(AssetUniverse).filter_by(market="US").count() == 0


class TestIdempotency:
    def test_running_twice_changes_nothing(self, db, stub_sources):
        for _ in range(2):
            full_run(db)
        assert db.query(AssetUniverse).count() == 3

    def test_the_price_stage_does_not_clear_fundamentals(self, db, stub_sources):
        """Each stage owns disjoint columns; upserts must list them explicitly."""
        full_run(db)
        before = db.query(AssetUniverse).filter_by(ticker="TST3").one().roe_pct
        assert before is not None

        block = state.start(db, ["B3"])
        ingest.stage_prices(db, block, 1)
        state.finish(db, block, "done")
        db.expire_all()
        after = db.query(AssetUniverse).filter_by(ticker="TST3").one()
        assert after.roe_pct == before
        assert after.sector == "Energia Elétrica"


class TestResumeAndCancel:
    def test_completed_stages_are_not_repeated(self, db, stub_sources):
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        stub_sources.clear()
        # Everything is done; a second slice on the same block does no work.
        assert ingest.ingest_slice(db, block) is True
        assert stub_sources == []

    def test_a_run_resumes_at_the_first_unfinished_stage(self, db, stub_sources):
        block = state.start(db, ["B3"])
        state.stage_done(db, block, "prices", 3)
        stub_sources.clear()
        ingest.ingest_slice(db, block)
        assert "cotahist" not in stub_sources
        assert "dfp" in stub_sources

    def test_cancel_stops_the_run_and_keeps_partial_rows(self, db, stub_sources):
        block = state.start(db, ["B3"])
        ingest.stage_prices(db, block, 1)
        state.request_cancel(db)
        assert ingest.ingest_slice(db, block) is False
        state.finish(db, block, "cancelled")
        # What was written before the cancel stays; that is the point of a
        # row-level cursor.
        assert db.query(AssetUniverse).count() == 3

    def test_budget_exhaustion_pauses_rather_than_failing(self, db, stub_sources):
        block = state.start(db, ["B3"])
        assert ingest.ingest_slice(db, block, budget_seconds=-1) is False
        assert block["stages_done"] == []


class TestSourceFailures:
    def test_a_changed_shape_skips_the_stage_without_writing(self, db, stub_sources, monkeypatch):
        """Previous rows must survive a source that changed format."""
        full_run(db)
        before = db.query(AssetUniverse).filter_by(ticker="TST3").one().roe_pct

        def broken():
            raise SourceShapeError("coluna VL_CONTA ausente")

        monkeypatch.setattr(cvm_statements, "fetch", broken)
        block = full_run(db)
        db.expire_all()
        assert db.query(AssetUniverse).filter_by(ticker="TST3").one().roe_pct == before
        assert any("VL_CONTA" in warning for warning in block["warnings"])

    def test_one_failing_stage_does_not_stop_the_others(self, db, stub_sources, monkeypatch):
        monkeypatch.setattr(cvm_fii, "fetch", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        block = state.start(db, ["B3"])
        assert ingest.ingest_slice(db, block) is True
        assert db.query(AssetUniverse).filter_by(ticker="TST3").one().sector is not None
        assert any("funds" in warning for warning in block["warnings"])

    def test_b3_silence_is_warned_not_swallowed(self, db, stub_sources, monkeypatch):
        monkeypatch.setattr(registry, "fetch_b3_companies", list)
        block = state.start(db, ["B3"])
        ingest.ingest_slice(db, block)
        assert any("B3" in warning for warning in block["warnings"])


class TestRunState:
    def test_a_second_start_is_refused(self, db):
        state.start(db, ["B3"])
        with pytest.raises(state.AlreadyRunning):
            state.start(db, ["B3"])

    def test_a_stale_run_does_not_block_a_new_one(self, db):
        """A killed desktop app must not wedge the feature forever."""
        block = state.start(db, ["B3"])
        block["heartbeat_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        state.write(db, block)
        db.commit()
        assert state.read(db)["stale"] is True
        assert state.read(db)["active"] is False
        state.start(db, ["B3"])  # allowed

    def test_cancel_crosses_processes_via_the_database(self, db):
        """The worker re-reads; an in-memory flag could not reach it."""
        block = state.start(db, ["B3"])
        assert state.cancel_requested(db, block) is False
        state.request_cancel(db)
        assert state.cancel_requested(db, block) is True

    def test_a_superseded_run_stops_itself(self, db):
        old = state.start(db, ["B3"])
        state.finish(db, old, "done")
        state.start(db, ["B3"])  # a newer run claims the block
        assert state.cancel_requested(db, old) is True

    def test_settings_default_to_off(self, db):
        assert state.is_enabled(db) is False
        assert state.markets(db) == ["B3"]
        assert state.history_years(db) == 2

    def test_history_years_is_clamped(self, db):
        db.merge(AppSetting(key="universe_history_years", value={"value": 99}))
        db.commit()
        assert state.history_years(db) == 5

    def test_finish_records_a_terminal_state(self, db):
        block = state.start(db, ["B3"])
        state.finish(db, block, "done", "pronto")
        stored = state.read(db)
        assert stored["state"] == "done"
        assert stored["active"] is False
        assert stored["requested_cancel"] is False


class TestJobsView:
    """The jobs tab reads this: per-stage status, timing, and an estimate."""

    def test_stages_start_pending(self, db):
        block = state.start(db, ["B3"])
        jobs = state.read(db)["jobs"]
        assert [j["name"] for j in jobs] == [name for name, _ in state.STAGES]
        assert all(j["status"] == "pending" for j in jobs)
        state.finish(db, block, "cancelled")

    def test_a_running_stage_reports_itself(self, db):
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.heartbeat(db, block, message="Baixando…", processed=10, total=100)
        job = next(j for j in state.read(db)["jobs"] if j["name"] == "prices")
        assert job["status"] == "running"
        assert job["processed"] == 10 and job["total"] == 100
        assert job["message"] == "Baixando…"
        state.finish(db, block, "cancelled")

    def test_finished_stage_records_duration_and_rows(self, db):
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.stage_done(db, block, "prices", 2475)
        job = next(j for j in state.read(db)["jobs"] if j["name"] == "prices")
        assert job["status"] == "done"
        assert job["rows"] == 2475
        assert job["seconds"] is not None and job["seconds"] >= 0
        state.finish(db, block, "done")

    def test_a_skipped_stage_never_becomes_an_estimate(self, db):
        """A stage that returned in a second must not promise a one-second run."""
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "us")
        state.stage_done(db, block, "us", 0, state="skipped")
        state.finish(db, block, "done")
        assert "us" not in (state.read(db)["stage_baseline"] or {})

    def test_no_estimate_before_anything_has_been_measured(self, db):
        # An invented countdown is decoration; "—" is the honest first answer.
        block = state.start(db, ["B3"])
        assert state.read(db)["eta_seconds"] is None
        state.finish(db, block, "cancelled")

    def test_estimate_appears_once_a_stage_has_a_rate(self, db):
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.heartbeat(db, block, processed=25, total=100)
        eta = state.read(db)["eta_seconds"]
        assert eta is not None and eta >= 0
        state.finish(db, block, "cancelled")

    def test_one_sample_is_not_enough_to_extrapolate(self, db):
        """The price stage's first file is 89 MB and the next eight are 10 MB.

        Projecting the stage from item one announced twelve minutes remaining
        on a run that took two.
        """
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.heartbeat(db, block, processed=1, total=9)
        assert state.read(db)["eta_seconds"] is None
        state.finish(db, block, "cancelled")

    def test_a_measured_stage_beats_an_in_flight_extrapolation(self, db):
        # First run measures the stage; the second should lean on that rather
        # than on whatever the first few items happen to suggest.
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.stage_done(db, block, "prices", 2475)
        state.finish(db, block, "done")

        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.heartbeat(db, block, processed=1, total=9)
        # One sample would not extrapolate, but the baseline still answers.
        assert state.read(db)["eta_seconds"] is not None
        state.finish(db, block, "cancelled")

    def test_baseline_and_history_survive_the_next_run(self, db):
        """What earlier runs measured is what lets the next one estimate."""
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.stage_done(db, block, "prices", 100)
        state.finish(db, block, "done", "pronto")

        after = state.read(db)
        assert "prices" in after["stage_baseline"]
        assert len(after["history"]) == 1

        state.start(db, ["B3"])
        carried = state.read(db)
        assert "prices" in carried["stage_baseline"]
        assert len(carried["history"]) == 1

    def test_history_records_the_outcome_of_each_run(self, db):
        for outcome in ("done", "cancelled", "error"):
            block = state.start(db, ["B3"])
            state.stage_started(db, block, "prices")
            state.stage_done(db, block, "prices", 5)
            state.finish(db, block, outcome)
        history = state.read(db)["history"]
        assert [entry["state"] for entry in history] == ["error", "cancelled", "done"]
        assert all(entry["seconds"] is not None for entry in history)

    def test_history_is_capped(self, db):
        for _ in range(state.HISTORY_LIMIT + 3):
            block = state.start(db, ["B3"])
            state.finish(db, block, "done")
        assert len(state.read(db)["history"]) == state.HISTORY_LIMIT

    def test_reset_forgets_everything(self, db):
        block = state.start(db, ["B3"])
        state.stage_started(db, block, "prices")
        state.stage_done(db, block, "prices", 100)
        state.finish(db, block, "done")
        state.reset(db)
        db.commit()
        after = state.read(db)
        assert after["state"] == "idle"
        assert after["history"] == [] and after["stage_baseline"] == {}
