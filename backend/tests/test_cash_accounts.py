"""Bank balances kept by hand: the two rules that make them not a CDB."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.models import IndexRate
from app.market.fixed_income import value_account
from app.portfolio import accounts as accounts_api
from app.portfolio.engine import build_positions
from app.portfolio.service import PortfolioService
from app.services.portfolio_registry import get_default_portfolio

#: A flat 1 % per business day makes every factor readable by eye.
DAILY = Decimal("1.0")
START = date(2026, 1, 1)


@pytest.fixture
def cdi(db: Session):
    """Ten days of a 1 %-a-day CDI, so a factor is just 1,01 ** days."""
    for offset in range(0, 40):
        db.add(IndexRate(code="CDI", date=START + timedelta(days=offset), value=DAILY))
    db.commit()


def factor(days: int) -> Decimal:
    return (Decimal("1.01")) ** days


def make(db: Session, name: str = "Nubank"):
    portfolio = get_default_portfolio(db)
    asset = accounts_api.create_account(db, portfolio.id, name)
    return portfolio, asset


def balance(db: Session, portfolio, asset, when: date) -> Decimal:
    from app.db.models import FixedIncomeTerms

    terms = db.get(FixedIncomeTerms, asset.id)
    accrual = value_account(db, asset, terms, portfolio.id, through=when)
    return accrual.value if accrual else Decimal(0)


def test_a_deposit_accrues_from_its_own_date(db: Session, cdi):
    """Money added later must not be paid interest it never earned."""
    portfolio, asset = make(db)
    accounts_api.add_entry(db, portfolio.id, asset, Decimal(100_000), START, deposit=True)
    accounts_api.add_entry(
        db, portfolio.id, asset, Decimal(100_000), START + timedelta(days=10), deposit=True
    )
    db.commit()

    # Ten days in, only the first deposit has earned anything.
    at_ten = balance(db, portfolio, asset, START + timedelta(days=10))
    assert at_ten == pytest.approx(Decimal(100_000) * factor(10) + Decimal(100_000), rel=Decimal("1e-9"))

    # Twenty days in: the second deposit has ten days of its own, and the first
    # keeps the head start it built — "200 mil mais o rendimento do primeiro".
    at_twenty = balance(db, portfolio, asset, START + timedelta(days=20))
    expected = Decimal(100_000) * factor(20) + Decimal(100_000) * factor(10)
    assert at_twenty == pytest.approx(expected, rel=Decimal("1e-9"))


def test_a_withdrawal_takes_only_what_was_withdrawn(db: Session, cdi):
    """The interest the balance already earned stays behind and keeps earning.

    This is what separates an account from a paper. Redeeming part of a CDB
    takes a *share* of the position, principal and its interest together; taking
    R$ 1.000 out of a bank account takes R$ 1.000.
    """
    portfolio, asset = make(db)
    accounts_api.add_entry(db, portfolio.id, asset, Decimal(100_000), START, deposit=True)
    db.commit()

    grown = balance(db, portfolio, asset, START + timedelta(days=10))
    assert grown > Decimal(100_000)

    accounts_api.add_entry(
        db, portfolio.id, asset, Decimal(1_000), START + timedelta(days=10), deposit=False
    )
    db.commit()

    # Immediately after: exactly the thousand is gone, nothing else.
    after = balance(db, portfolio, asset, START + timedelta(days=10))
    assert after == pytest.approx(grown - Decimal(1_000), rel=Decimal("1e-9"))

    # And what is left compounds from there — including the interest earned
    # before the withdrawal, which is the part a proportional redemption loses.
    later = balance(db, portfolio, asset, START + timedelta(days=20))
    assert later == pytest.approx(after * factor(10), rel=Decimal("1e-9"))


def test_a_withdrawal_cannot_exceed_the_balance(db: Session, cdi):
    portfolio, asset = make(db)
    accounts_api.add_entry(db, portfolio.id, asset, Decimal(1_000), START, deposit=True)
    db.commit()
    with pytest.raises(accounts_api.AccountError, match="saldo insuficiente"):
        accounts_api.add_entry(
            db, portfolio.id, asset, Decimal(5_000), START + timedelta(days=1), deposit=False
        )


def test_the_balance_reaches_the_portfolio_as_fixed_income(db: Session, cdi):
    """The point of modelling an account as an asset: everything else just works."""
    portfolio, asset = make(db, "Inter")
    accounts_api.add_entry(db, portfolio.id, asset, Decimal(50_000), START, deposit=True)
    accounts_api.add_entry(
        db, portfolio.id, asset, Decimal(10_000), START + timedelta(days=5), deposit=False
    )
    db.commit()

    service = PortfolioService(db, portfolio.id)
    position = service.positions()[asset.id]
    # The unit is one real, so the replay tracks the money put in and a
    # withdrawal realises nothing: interest is not a realised result.
    assert position.quantity == Decimal(40_000)
    assert position.cost_basis == Decimal(40_000)
    assert position.realized_pnl == Decimal(0)
    assert asset.kind == "FIXED_INCOME"

    # And it is marked at its accrued value, not at cost.
    priced = next(ap for ap in service.asset_positions() if ap.asset.id == asset.id)
    assert priced.has_market_price
    assert priced.market_value > Decimal(40_000)


def test_an_empty_account_is_harmless(db: Session, cdi):
    """A conta with no entries yet must not break any total."""
    portfolio, asset = make(db, "Sem saldo")
    payload = accounts_api.serialize(db, portfolio.id, asset)
    assert payload["balance"] == Decimal(0)
    assert payload["entries"] == []
    assert build_positions([]) == {}


def test_names_collapse_to_one_account(db: Session):
    portfolio, _asset = make(db, "Nubank")
    with pytest.raises(accounts_api.AccountError, match="já existe"):
        accounts_api.create_account(db, portfolio.id, "Nu bank")
