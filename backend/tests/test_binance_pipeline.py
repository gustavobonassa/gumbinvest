"""The Binance pipeline end to end, against a fake Binance (no network).

Every response below is synthetic. The fake answers the endpoints the pipeline
calls, so the test exercises the real request building, paging, filtering,
CSV writing, import and balance check together.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.db.models import FxRate
from app.importer.service import ImportService
from app.pipelines import binance as pipeline_module
from app.pipelines.binance import BinancePipeline, compare_balances
from app.portfolio.service import PortfolioService

T0 = int(datetime(2024, 3, 2, 10, 0, tzinfo=UTC).timestamp() * 1000)
DAY = 86_400_000


def _fake_binance(request: httpx.Request) -> httpx.Response:
    path = urlparse(str(request.url)).path
    q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
    start = int(q.get("startTime", q.get("beginTime", 0)) or 0)
    end = int(q.get("endTime", 10**15) or 10**15)

    def within(ms: int) -> bool:
        return start <= ms <= end

    if path == "/api/v3/time":
        return httpx.Response(200, json={"serverTime": int(datetime.now(UTC).timestamp() * 1000)})
    if path == "/sapi/v1/account/apiRestrictions":
        return httpx.Response(200, json={"enableReading": True, "enableWithdrawals": False})
    if path == "/api/v3/account":
        return httpx.Response(200, json={"balances": [
            {"asset": "DOT", "free": "52.9", "locked": "0"},
            {"asset": "ETH", "free": "0.05", "locked": "0"},
            {"asset": "LDDOT", "free": "50", "locked": "0"},  # the flexible position, mirrored
            {"asset": "BRL", "free": "12", "locked": "0"},
        ]})
    if path == "/sapi/v1/simple-earn/flexible/position":
        return httpx.Response(200, json={"rows": [{"asset": "DOT", "totalAmount": "50"}], "total": 1})
    if path == "/sapi/v1/simple-earn/locked/position":
        return httpx.Response(200, json={"rows": [], "total": 0})
    if path == "/sapi/v1/asset/get-funding-asset":
        return httpx.Response(200, json=[])
    if path == "/sapi/v1/capital/deposit/hisrec":
        rows = [{"id": 1, "coin": "USDT", "amount": "100", "status": 1, "insertTime": T0 - DAY}]
        return httpx.Response(200, json=[r for r in rows if within(r["insertTime"])] if int(q.get("offset", 0)) == 0 else [])
    if path == "/sapi/v1/capital/withdraw/history":
        return httpx.Response(200, json=[])
    if path == "/sapi/v1/fiat/payments":
        return httpx.Response(200, json={"data": [], "total": 0})
    if path == "/sapi/v1/convert/tradeFlow":
        rows = [{"orderId": 7, "orderStatus": "SUCCESS", "fromAsset": "USDT", "fromAmount": "100",
                 "toAsset": "ETH", "toAmount": "0.05", "createTime": T0 + 2 * DAY}]
        return httpx.Response(200, json={"list": [r for r in rows if within(r["createTime"])], "moreData": False})
    if path.startswith("/sapi/v1/simple-earn/"):
        rows = []
        if path.endswith("flexible/history/subscriptionRecord"):
            rows = [{"asset": "DOT", "amount": "50", "time": T0 + 3 * DAY, "purchaseId": 1, "status": "SUCCESS"}]
        if path.endswith("flexible/history/rewardsRecord"):
            rows = [{"asset": "DOT", "rewards": "3", "time": T0 + 10 * DAY}]
        rows = [r for r in rows if within(r["time"])]
        return httpx.Response(200, json={"rows": rows, "total": len(rows)})
    if path == "/sapi/v1/asset/assetDividend":
        rows = [
            {"tranId": 1, "asset": "DOT", "amount": "3", "divTime": T0 + 10 * DAY,
             "enInfo": "Simple Earn Flexible Interest", "direction": 1},  # dropped: earn already counts it
            {"tranId": 2, "asset": "ETH", "amount": "0.001", "divTime": T0 + 12 * DAY,
             "enInfo": "ETH airdrop", "direction": 1},
        ]
        rows = [r for r in rows if within(r["divTime"])]
        return httpx.Response(200, json={"rows": rows, "total": len(rows)})
    if path == "/sapi/v1/asset/dribblet":
        return httpx.Response(200, json={"total": 0, "userAssetDribblets": []})
    if path == "/api/v3/exchangeInfo":
        return httpx.Response(200, json={"symbols": [
            {"symbol": "DOTBRL", "baseAsset": "DOT", "quoteAsset": "BRL"},
            {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT"},
            {"symbol": "XRPUSDT", "baseAsset": "XRP", "quoteAsset": "USDT"},
        ]})
    if path == "/api/v3/myTrades":
        trades = {
            "DOTBRL": [{"id": 11, "qty": "100", "quoteQty": "5000", "price": "50", "commission": "0.1",
                        "commissionAsset": "DOT", "time": T0, "isBuyer": True}],
            # Bought and sold back in full: appears in no balance, no deposit.
            "XRPUSDT": [
                {"id": 1, "qty": "10", "quoteQty": "5", "price": "0.5", "commission": "0",
                 "commissionAsset": "USDT", "time": T0 + DAY, "isBuyer": True},
                {"id": 2, "qty": "10", "quoteQty": "5", "price": "0.5", "commission": "0",
                 "commissionAsset": "USDT", "time": T0 + DAY + 1000, "isBuyer": False},
            ],
        }.get(q["symbol"], [])
        return httpx.Response(200, json=[t for t in trades if t["id"] >= int(q.get("fromId", 0))])
    return httpx.Response(404, json={"code": -1, "msg": f"unexpected {path}"})


class _Ctx:
    def __init__(self, db, portfolio, tmp_path: Path, options: dict | None = None) -> None:
        self.db, self.portfolio, self.tmp = db, portfolio, tmp_path
        self.options = options or {}
        self.run_id = 1
        self.lines: list[tuple[str, str]] = []
        self.last_success: datetime | None = None

    def log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def check_cancel(self) -> None:
        pass

    def last_success_at(self):
        return self.last_success

    def downloads_dir(self) -> Path:
        return self.tmp

    def import_file(self, payload: bytes, filename: str):
        return ImportService(self.db, self.portfolio).import_crypto_csv(payload, filename)

    def preview_file(self, payload: bytes, filename: str):
        return ImportService(self.db, self.portfolio).import_crypto_csv(
            payload, filename, dry_run=True
        )


@pytest.fixture
def fake(monkeypatch, db, portfolio):
    day = date(2024, 1, 1)
    while day <= date(2024, 12, 31):
        db.add(FxRate(base="USD", quote="BRL", date=day, rate=Decimal("5")))
        day += timedelta(days=1)
    db.commit()

    monkeypatch.setattr(pipeline_module, "_TRANSPORT", httpx.MockTransport(_fake_binance))
    monkeypatch.setattr(pipeline_module, "_INTERVALS", {"/api/v3/myTrades": 0.0})
    monkeypatch.setattr(pipeline_module, "_DEFAULT_INTERVAL", 0.0)
    # The real code checks the whole history since 2017; the fake only needs 2024.
    monkeypatch.setattr(pipeline_module, "FULL_HISTORY_START", datetime(2024, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(pipeline_module, "EARN_HISTORY_START", datetime(2024, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(pipeline_module, "read_secret", lambda key: "k")
    monkeypatch.setattr(BinancePipeline, "_known_coins", staticmethod(lambda: set()))

    def quantities():
        service = PortfolioService(db, portfolio.id)
        assets = service.assets()
        return {assets[a].ticker: Decimal(str(p.quantity)) for a, p in service.positions().items()}

    monkeypatch.setattr(pipeline_module, "_portfolio_quantities", quantities)


def test_a_full_run_imports_the_history_and_balances(fake, db, portfolio, tmp_path):
    ctx = _Ctx(db, portfolio, tmp_path)
    result = BinancePipeline().run(ctx)

    assert result["rows_failed"] == 0
    assert result["rows_imported"] > 0
    service = PortfolioService(db, portfolio.id)
    assets = service.assets()
    held = {assets[a].ticker: p for a, p in service.positions().items()}
    # 100 bought - 0,1 fee + 3 earn interest (counted once, not again from the
    # distribution history); 50 of it subscribed to Earn stays in the position.
    assert held["DOT"].quantity == Decimal("102.9")
    assert held["ETH"].quantity == Decimal("0.051")
    # The coin traded in and out was found even though nothing else mentions it.
    assert "XRP" in held and held["XRP"].quantity == 0
    # Binance: 52,9 spot + 50 flexible (the LDDOT mirror not added again) = 102,9.
    assert result["balance_mismatches"] == 1  # ETH: the airdrop is not in the fake balance
    assert any("ETH" in message for level, message in ctx.lines if level == "warning")
    # Clean import: the courier file is gone.
    assert not list(tmp_path.glob("*.csv"))


def test_a_second_run_adds_nothing(fake, db, portfolio, tmp_path):
    BinancePipeline().run(_Ctx(db, portfolio, tmp_path))
    ctx = _Ctx(db, portfolio, tmp_path)
    ctx.last_success = datetime(2024, 3, 20, tzinfo=UTC)
    again = BinancePipeline().run(ctx)
    assert again["rows_imported"] == 0


def test_a_rejected_key_says_what_to_do(monkeypatch, fake, db, portfolio, tmp_path):
    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 0})
        return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions"})

    monkeypatch.setattr(pipeline_module, "_TRANSPORT", httpx.MockTransport(refuse))
    from app.pipelines.base import PipelineError

    with pytest.raises(PipelineError, match="recusou a chave"):
        BinancePipeline().run(_Ctx(db, portfolio, tmp_path))


def test_compare_ignores_fiat_and_dust():
    gaps = compare_balances(
        {"BTC": Decimal("0.5"), "BRL": Decimal("100"), "DOT": Decimal("10.00000001")},
        {"BTC": Decimal("0.4"), "DOT": Decimal("10")},
    )
    assert [g["coin"] for g in gaps] == ["BTC"]


# -- dates follow the zone of the user's own exports -----------------------
#: 22:30 in São Paulo on 2 March is 01:30 UTC on 3 March: the case where an
#: API run dated in UTC would disagree with a ledger exported in local time.
LATE = int(datetime(2024, 3, 3, 1, 30, tzinfo=UTC).timestamp() * 1000)

LOCAL_LEDGER = (
    "ID do Usuário,Tempo,Conta,Operação,Moeda,Alterar,Observação\n"
    "1,2024-03-02 22:30:00,Spot,Transaction Buy,DOT,10,\n"
    "1,2024-03-02 22:30:00,Spot,Transaction Spend,BRL,-500,\n"
)


def _late_trade_binance(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v3/myTrades":
        q = dict(request.url.params)
        if q["symbol"] != "DOTBRL":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{
            "id": 1, "qty": "10", "quoteQty": "500", "price": "50", "commission": "0",
            "commissionAsset": "BRL", "time": LATE, "isBuyer": True,
        }])
    if path == "/api/v3/account":
        return httpx.Response(200, json={"balances": [{"asset": "DOT", "free": "10", "locked": "0"}]})
    if path in ("/sapi/v1/capital/deposit/hisrec", "/sapi/v1/capital/withdraw/history",
                "/sapi/v1/asset/get-funding-asset"):
        return httpx.Response(200, json=[])
    if path == "/sapi/v1/convert/tradeFlow":
        return httpx.Response(200, json={"list": [], "moreData": False})
    if path == "/sapi/v1/fiat/payments":
        return httpx.Response(200, json={"data": [], "total": 0})
    if path.startswith("/sapi/v1/simple-earn/") or path == "/sapi/v1/asset/assetDividend":
        return httpx.Response(200, json={"rows": [], "total": 0})
    if path == "/sapi/v1/asset/dribblet":
        return httpx.Response(200, json={"userAssetDribblets": []})
    if path == "/api/v3/exchangeInfo":
        return httpx.Response(200, json={"symbols": [
            {"symbol": "DOTBRL", "baseAsset": "DOT", "quoteAsset": "BRL"}]})
    return _fake_binance(request)


def test_zone_is_read_from_the_export_filename():
    zone = pipeline_module.zone_from_filenames(
        ["Binance-Histórico-de-Transações-202608012000(UTC--3)-part1-of1.csv"]
    )
    assert zone.utcoffset(None) == timedelta(hours=-3)
    ahead = pipeline_module.zone_from_filenames(["Binance-Transaction-History(UTC+8).csv"])
    assert ahead.utcoffset(None) == timedelta(hours=8)
    assert pipeline_module.zone_from_filenames(["movimentacao.csv"]) is None


def test_a_late_evening_trade_matches_a_local_time_export(monkeypatch, fake, db, portfolio, tmp_path):
    """The export says 2 March (local), the API says 3 March (UTC): still one trade."""
    ImportService(db, portfolio).import_crypto_csv(
        LOCAL_LEDGER.encode("utf-8"),
        "Binance-Histórico-de-Transações-202404010000(UTC--3)-part1-of1.csv",
    )
    monkeypatch.setattr(pipeline_module, "_TRANSPORT", httpx.MockTransport(_late_trade_binance))

    result = BinancePipeline().run(_Ctx(db, portfolio, tmp_path))

    assert result["rows_imported"] == 0
    held = _held(db, portfolio)
    assert held["DOT"].quantity == Decimal("10")
    # The choice is kept, so a later export in another zone cannot re-date
    # what this run already booked.
    from app.db.models import AppSetting

    assert db.get(AppSetting, pipeline_module.STATE_KEY).value["timezone"] == "UTC-03:00"


def test_dating_in_utc_would_have_duplicated_it(monkeypatch, fake, db, portfolio, tmp_path):
    """Guards the reason for the zone logic: with UTC dates the same trade
    lands on another day and is booked a second time."""
    ImportService(db, portfolio).import_crypto_csv(
        LOCAL_LEDGER.encode("utf-8"), "Binance-Histórico-de-Transações-(UTC--3).csv"
    )
    monkeypatch.setattr(pipeline_module, "_TRANSPORT", httpx.MockTransport(_late_trade_binance))
    monkeypatch.setattr(
        BinancePipeline, "_account_zone", staticmethod(lambda ctx, persist: UTC)
    )

    result = BinancePipeline().run(_Ctx(db, portfolio, tmp_path))
    assert result["rows_imported"] > 0
    assert _held(db, portfolio)["DOT"].quantity == Decimal("20")


# -- simulation ---------------------------------------------------------------
def test_a_simulation_saves_nothing(fake, db, portfolio, tmp_path):
    from sqlalchemy import func, select

    from app.db.models import AppSetting, ImportBatch, Transaction

    ctx = _Ctx(db, portfolio, tmp_path, options={"preview": True})
    result = BinancePipeline().run(ctx)

    assert result["preview"] is True
    assert result["would_import"] > 0
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0
    assert db.scalar(select(func.count()).select_from(ImportBatch)) == 0
    assert db.get(AppSetting, pipeline_module.STATE_KEY) is None  # zone not fixed by a dry run
    assert not list(tmp_path.iterdir())
    assert any("Nada foi gravado" in message for _, message in ctx.lines)

    # The real run afterwards books exactly what the simulation announced.
    real = BinancePipeline().run(_Ctx(db, portfolio, tmp_path))
    assert real["rows_imported"] == result["would_import"]


def test_a_simulation_flags_rows_inside_the_exported_period(monkeypatch, fake, db, portfolio, tmp_path):
    """A row the exports should already have had is the shape of a duplicate."""
    # The export runs on to 10 March, so the mis-dated 3 March trade falls
    # inside the period it covers.
    covering = LOCAL_LEDGER + "1,2024-03-10 12:00:00,Spot,Staking Rewards,DOT,1,\n"
    ImportService(db, portfolio).import_crypto_csv(
        covering.encode("utf-8"), "Binance-Histórico-de-Transações-(UTC--3).csv"
    )
    monkeypatch.setattr(
        BinancePipeline, "_account_zone", staticmethod(lambda ctx, persist: UTC)
    )
    monkeypatch.setattr(pipeline_module, "_TRANSPORT", httpx.MockTransport(_late_trade_binance))
    ctx = _Ctx(db, portfolio, tmp_path, options={"preview": True})

    result = BinancePipeline().run(ctx)

    assert result["inside_covered"] > 0
    assert any(level == "warning" and "podem ser duplicatas" in message for level, message in ctx.lines)


def test_a_simulation_is_not_where_the_next_run_resumes(db, portfolio):
    from app.db.models import PipelineRun
    from app.pipelines.runner import RunContext

    db.add_all([
        PipelineRun(pipeline="binance", trigger="manual", status="success", log=[],
                    portfolio_id=portfolio.id, finished_at=datetime(2024, 5, 1, tzinfo=UTC)),
        PipelineRun(pipeline="binance", trigger="manual", status="success", log=[],
                    portfolio_id=portfolio.id, options={"preview": True},
                    finished_at=datetime(2024, 6, 1, tzinfo=UTC)),
    ])
    db.commit()
    ctx = RunContext(0, BinancePipeline(), {})
    assert ctx.last_success_at() == datetime(2024, 5, 1, tzinfo=UTC)


def _held(db, portfolio):
    service = PortfolioService(db, portfolio.id)
    assets = service.assets()
    return {assets[a].ticker: p for a, p in service.positions().items()}
