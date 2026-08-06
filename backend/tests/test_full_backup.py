"""The .gumbinvest whole-database export/import round-trip."""
from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.db.session import get_db
from app.main import app
from app.services.portfolio_registry import get_default_portfolio

from app.db.models import (
    AppSetting,
    Asset,
    Base,
    Broker,
    Portfolio,
    PortfolioSnapshot,
    Quote,
    Transaction,
)
from app.services.full_backup import (
    FullBackupError,
    export_snapshot,
    import_snapshot,
    is_full_backup,
)

PRICE = Decimal("31.415926")
QTY = Decimal("0.00012345")
FACTOR = Decimal("1.00123456789012")


@pytest.fixture
def client(engine, db: Session):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    portfolio = get_default_portfolio(db)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_portfolio] = lambda: portfolio
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _populate(db: Session) -> None:
    portfolio = Portfolio(name="Origem", base_currency="BRL", is_default=True)
    broker = Broker(canonical_name="Corretora X")
    asset = Asset(ticker="PETR4", name="Petrobras", kind="stock")
    db.add_all([portfolio, broker, asset])
    db.flush()
    db.add_all(
        [
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                broker_id=broker.id,
                trade_date=date(2024, 3, 1),
                direction="CREDIT",
                op_type="BUY",
                effect="POSITION",
                quantity=QTY,
                unit_price=PRICE,
                gross_amount=PRICE * QTY,
                dedup_key="petr4|2024-03-01|buy|1",
                raw_movement="Compra",
                raw_product="PETR4 - PETROBRAS",
            ),
            Quote(asset_id=asset.id, price=PRICE, source="test"),
            AppSetting(key="currency", value={"v": "BRL"}),
            PortfolioSnapshot(
                portfolio_id=portfolio.id, date=date(2024, 3, 2), return_factor=FACTOR
            ),
        ]
    )
    db.commit()


def _wipe(db: Session) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()


def test_round_trip_restores_everything_exactly(db: Session) -> None:
    _populate(db)
    payload = export_snapshot(db)
    assert is_full_backup(payload, "x.gumbinvest")
    assert is_full_backup(payload, "renamed.bin")  # gzip magic is enough

    _wipe(db)
    result = import_snapshot(db, payload, "backup.gumbinvest")
    assert result["status"] == "COMPLETED"
    assert result["rows_imported"] == result["rows_total"] > 0

    tx = db.query(Transaction).one()
    assert tx.quantity == QTY
    assert tx.unit_price == PRICE
    assert tx.trade_date == date(2024, 3, 1)
    assert tx.dedup_key == "petr4|2024-03-01|buy|1"
    assert db.query(Portfolio).filter_by(name="Origem").one().is_default is True
    assert db.query(Quote).one().price == PRICE
    assert db.query(AppSetting).filter_by(key="currency").one().value == {"v": "BRL"}
    assert db.query(PortfolioSnapshot).one().return_factor == FACTOR
    # Foreign keys survived with their original ids.
    assert tx.asset_id == db.query(Asset).one().id
    assert tx.broker_id == db.query(Broker).one().id


def test_import_refuses_a_target_with_history(db: Session) -> None:
    _populate(db)
    payload = export_snapshot(db)
    with pytest.raises(FullBackupError, match="instalação vazia"):
        import_snapshot(db, payload, "backup.gumbinvest")
    # And nothing was deleted by the refused attempt.
    assert db.query(Transaction).count() == 1


def test_import_rejects_garbage_and_future_formats(db: Session) -> None:
    with pytest.raises(FullBackupError, match="válido"):
        import_snapshot(db, b"not gzip at all", "x.gumbinvest")
    future = gzip.compress(
        json.dumps({"format": "gumbinvest-export", "format_version": 999}).encode()
    )
    with pytest.raises(FullBackupError, match="versão mais nova"):
        import_snapshot(db, future, "x.gumbinvest")


def test_export_and_import_via_api(client, db: Session) -> None:
    _populate(db)
    response = client.get("/api/imports/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/gzip")
    assert ".gumbinvest" in response.headers["content-disposition"]
    payload = response.content

    # Uploading into an instance that already has history: refused, 422.
    refused = client.post(
        "/api/imports", files={"file": ("backup.gumbinvest", payload, "application/gzip")}
    )
    assert refused.status_code == 422
    assert "vazia" in refused.json()["detail"]

    _wipe(db)
    accepted = client.post(
        "/api/imports", files={"file": ("backup.gumbinvest", payload, "application/gzip")}
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] == "COMPLETED"
    assert db.query(Transaction).count() == 1


class TestUniverseExclusion:
    """The asset universe is public data an instance rebuilds for itself.

    Carrying it would inflate every export, and — the part that actually bites —
    the import path deletes every table it touches. Excluding it from export but
    not from the delete would silently wipe a universe the user already built.
    """

    def _seed_universe(self, db: Session) -> None:
        from app.db.models import AssetUniverse

        db.add(
            AssetUniverse(
                ticker="TST3",
                name="Empresa Teste",
                kind="STOCK",
                currency="BRL",
                market="B3",
                identity_source="teste",
            )
        )
        db.commit()

    def test_not_carried_in_the_export(self, db: Session, portfolio):
        from app.services.full_backup import export_snapshot

        self._seed_universe(db)
        document = json.loads(gzip.decompress(export_snapshot(db)))
        assert "asset_universe" not in document["tables"]

    def test_survives_an_import_instead_of_being_wiped(self, db: Session, portfolio):
        from app.db.models import AssetUniverse
        from app.services.full_backup import export_snapshot, import_snapshot

        payload = export_snapshot(db)
        self._seed_universe(db)
        import_snapshot(db, payload, "teste.gumbinvest")
        # Rebuilding it costs a download; losing it silently costs the user
        # a run they already paid for.
        assert db.query(AssetUniverse).count() == 1
