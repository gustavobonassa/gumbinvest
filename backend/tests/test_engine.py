"""Portfolio calculation rules — the part that must never be wrong."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.enums import OperationType, PositionEffect
from app.portfolio.engine import Movement, build_positions, build_timeline

D = Decimal


def mv(
    day: str,
    effect: PositionEffect,
    qty: str = "0",
    price: str = "0",
    amount: str = "0",
    op_type: OperationType = OperationType.BUY,
    asset_id: int = 1,
    fees: str = "0",
) -> Movement:
    return Movement(
        asset_id=asset_id,
        trade_date=date.fromisoformat(day),
        op_type=op_type.value,
        effect=effect.value,
        quantity=D(qty),
        unit_price=D(price),
        gross_amount=D(amount),
        fees=D(fees),
    )


def test_staking_parks_the_cost_instead_of_writing_it_off():
    """Coins locked into an exchange product keep the cost they went in with.

    The exchange stops reporting them in the balance, so the quantity really
    does leave — but they are still owned. Removing their cost as well would
    return the position at zero and read the whole holding as profit.
    """
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-01", PositionEffect.QTY_OUT_STAKED, "80", op_type=OperationType.TRANSFER_OUT),
            mv("2024-03-01", PositionEffect.QTY_IN_STAKED, "80", op_type=OperationType.TRANSFER_IN),
        ]
    )
    position = positions[1]
    assert position.quantity == D("100")
    assert position.cost_basis == D("1000")
    assert position.parked_cost == D("0")


def test_coins_left_in_staking_are_still_part_of_the_position():
    """Staking removes coins from the *reported balance*, not from the wallet.

    Leaving them out closes the position on paper. On the reference account
    every USDT had been subscribed to Simple Earn and never redeemed, so the
    free balance was zero and roughly US$ 1.450 simply stopped appearing.
    """
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-01", PositionEffect.QTY_OUT_STAKED, "80", op_type=OperationType.TRANSFER_OUT),
        ]
    )
    position = positions[1]
    assert position.quantity == D("100")
    assert position.cost_basis == D("1000")
    # Reported apart so the UI can say 80 of the 100 are locked away.
    assert position.staked_quantity == D("80")
    assert position.staked_cost == D("800")
    assert position.is_open


def test_rewards_compounded_inside_the_product_come_back_free():
    """More can return than went in; the excess had no cost to bring with it.

    Not the same as a deposit from outside, though: this excess was earned on
    the exchange and is visible in the ledger as a reward, so it is free
    quantity rather than the untraceable kind that suppresses a result.
    """
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-01", PositionEffect.QTY_OUT_STAKED, "100", op_type=OperationType.TRANSFER_OUT),
            mv("2024-03-01", PositionEffect.QTY_IN_STAKED, "110", op_type=OperationType.TRANSFER_IN),
        ]
    )
    position = positions[1]
    assert position.quantity == D("110")
    assert position.cost_basis == D("1000")
    assert position.staked_quantity == D("0")
    assert position.uncosted_quantity == D("0")


def test_selling_quantity_that_had_no_cost_is_not_a_gain():
    """Proceeds are not profit when there is no cost to subtract.

    A result is revenue *minus cost*. With coins that arrived from a wallet the
    history cannot see, the subtraction is undefined — so the money is reported
    on its own rather than booked as a gain. Calling it profit is what produces
    a return of several hundred thousand percent against a cost basis of zero.
    """
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.QTY_IN_PARKED, "10", op_type=OperationType.TRANSFER_IN),
            mv("2024-06-01", PositionEffect.DISPOSE, "10", "300", "3000", op_type=OperationType.SELL),
        ]
    )
    position = positions[1]
    assert position.realized_pnl == D("0")
    assert position.uncosted_proceeds == D("3000")
    assert any("sem custo conhecido" in warning for warning in position.warnings)


def test_a_sale_draws_on_costed_and_uncosted_units_in_proportion():
    """Half the position was bought, half was deposited: half the sale counts."""
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "10", "100", "1000"),
            mv("2024-02-01", PositionEffect.QTY_IN_PARKED, "10", op_type=OperationType.TRANSFER_IN),
            mv("2024-06-01", PositionEffect.DISPOSE, "20", "150", "3000", op_type=OperationType.SELL),
        ]
    )
    position = positions[1]
    # Half the proceeds belong to units that cost 1.000; the rest has no cost.
    assert position.realized_pnl == D("500")
    assert position.uncosted_proceeds == D("1500")
    assert position.quantity == D("0")


def test_a_withdrawal_that_comes_back_keeps_its_cost():
    """Using an exchange as a bridge must not write off every purchase.

    Buy, withdraw to a wallet minutes later, deposit again months on, sell: the
    round trip is not a disposal, so the cost has to survive it. Otherwise the
    coins return free and the sale invents a gain the size of the purchase.
    """
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "10", "100", "1000"),
            mv("2024-01-10", PositionEffect.QTY_OUT_PARKED, "10", op_type=OperationType.TRANSFER_OUT),
            mv("2024-06-01", PositionEffect.QTY_IN_PARKED, "10", op_type=OperationType.TRANSFER_IN),
            mv("2024-06-02", PositionEffect.DISPOSE, "10", "120", "1200", op_type=OperationType.SELL),
        ]
    )
    position = positions[1]
    assert position.realized_pnl == D("200")
    assert not position.warnings


def test_average_price_over_multiple_purchases():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-10", PositionEffect.ACQUIRE, "100", "20", "2000"),
        ]
    )
    position = positions[1]
    assert position.quantity == D("200")
    assert position.cost_basis == D("3000")
    assert position.average_price == D("15")


def test_fees_are_capitalised_into_the_cost_basis():
    positions = build_positions([mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000", fees="5")])
    assert positions[1].cost_basis == D("1005")
    assert positions[1].average_price == D("10.05")


def test_partial_sale_keeps_average_and_realises_the_difference():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-10", PositionEffect.ACQUIRE, "100", "20", "2000"),
            mv("2024-03-10", PositionEffect.DISPOSE, "50", "25", "1250", op_type=OperationType.SELL),
        ]
    )
    position = positions[1]
    assert position.quantity == D("150")
    assert position.average_price == D("15")  # unchanged by the sale
    assert position.cost_basis == D("2250")
    assert position.realized_pnl == D("500")  # (25 - 15) * 50


def test_full_sale_closes_the_position():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-03-10", PositionEffect.DISPOSE, "100", "12", "1200", op_type=OperationType.SELL),
        ]
    )
    position = positions[1]
    assert position.quantity == D("0")
    assert position.cost_basis == D("0")
    assert position.realized_pnl == D("200")
    assert not position.is_open


def test_split_dilutes_the_average_price_and_preserves_cost():
    """B3 reports a split as a free credit of the *extra* shares."""
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-06-10", PositionEffect.QTY_IN_FREE, "100", op_type=OperationType.SPLIT),
        ]
    )
    position = positions[1]
    assert position.quantity == D("200")
    assert position.cost_basis == D("1000")
    assert position.average_price == D("5")


def test_bonus_shares_lower_the_average_price():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "1000", "10", "10000"),
            mv("2024-12-22", PositionEffect.QTY_IN_FREE, "32.66", op_type=OperationType.BONUS),
        ]
    )
    position = positions[1]
    assert position.quantity == D("1032.66")
    assert position.cost_basis == D("10000")
    assert position.average_price < D("10")


def test_reverse_split_removal_keeps_cost_proportional():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-06-10", PositionEffect.QTY_OUT_FREE, "50", op_type=OperationType.REVERSE_SPLIT),
        ]
    )
    position = positions[1]
    assert position.quantity == D("50")
    assert position.cost_basis == D("500")
    assert position.average_price == D("10")  # unchanged, nothing realised
    assert position.realized_pnl == D("0")


def test_return_of_capital_reduces_the_cost_basis():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "400", "10", "4000"),
            mv(
                "2024-05-10",
                PositionEffect.RETURN_OF_CAPITAL,
                "400",
                "1.252",
                "500.68",
                op_type=OperationType.AMORTIZATION,
            ),
        ]
    )
    position = positions[1]
    assert position.cost_basis == D("3499.32")
    assert position.income == D("0")  # not income — it is capital coming back
    assert position.returned_capital == D("500.68")


def test_return_of_capital_beyond_the_basis_is_realised():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "10", "1", "10"),
            mv("2024-05-10", PositionEffect.RETURN_OF_CAPITAL, "10", "2", "20", op_type=OperationType.AMORTIZATION),
        ]
    )
    assert positions[1].cost_basis == D("0")
    assert positions[1].realized_pnl == D("10")


def test_income_is_accumulated_by_type():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-10", PositionEffect.CASH_IN, "100", "0.5", "50", op_type=OperationType.DIVIDEND),
            mv("2024-03-10", PositionEffect.CASH_IN, "100", "0.2", "20", op_type=OperationType.JCP),
            mv("2024-04-10", PositionEffect.CASH_IN, "100", "0.3", "30", op_type=OperationType.YIELD),
        ]
    )
    position = positions[1]
    assert position.income == D("100")
    assert position.income_by_type[OperationType.DIVIDEND.value] == D("50")
    assert position.income_by_type[OperationType.JCP.value] == D("20")
    assert position.income_by_type[OperationType.YIELD.value] == D("30")


def test_transferred_income_nets_to_zero():
    """Broker-to-broker income transfers appear twice with opposite signs."""
    positions = build_positions(
        [
            mv("2024-02-10", PositionEffect.CASH_IN, "1276", "0.267", "289.86", op_type=OperationType.JCP),
            mv("2024-02-10", PositionEffect.CASH_OUT, "1276", "0.267", "289.86", op_type=OperationType.JCP),
        ]
    )
    assert positions[1].income == D("0")


def test_paired_custody_transfers_preserve_the_cost_basis():
    """Moving shares between your own brokers must be a no-op."""
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "698", "10", "6980"),
            mv("2025-02-07", PositionEffect.QTY_OUT_FREE, "698", op_type=OperationType.TRANSFER_OUT),
            mv("2025-02-07", PositionEffect.QTY_IN_FREE, "698", op_type=OperationType.TRANSFER_IN),
        ]
    )
    position = positions[1]
    assert position.quantity == D("698")
    assert position.cost_basis == D("6980")  # would be 0 without pairing
    assert position.average_price == D("10")


def test_unpaired_transfer_still_moves_quantity():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2025-02-07", PositionEffect.QTY_OUT_FREE, "40", op_type=OperationType.TRANSFER_OUT),
        ]
    )
    assert positions[1].quantity == D("60")
    assert positions[1].cost_basis == D("600")


def test_same_day_buy_before_sell():
    """Exports have no intraday order; credits must be applied first."""
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.DISPOSE, "100", "12", "1200", op_type=OperationType.SELL),
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
        ]
    )
    position = positions[1]
    assert position.quantity == D("0")
    assert position.realized_pnl == D("200")
    assert position.warnings == []


def test_sale_without_a_recorded_position_is_flagged():
    positions = build_positions(
        [mv("2024-01-10", PositionEffect.DISPOSE, "10", "5", "50", op_type=OperationType.SELL)]
    )
    position = positions[1]
    assert position.realized_pnl == D("50")
    assert position.warnings


def test_informational_rows_do_not_move_the_position():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "36", "10", "360"),
            mv("2024-06-10", PositionEffect.NONE, "36", op_type=OperationType.INFO),
            mv("2025-06-10", PositionEffect.NONE, "36", op_type=OperationType.INFO),
        ]
    )
    assert positions[1].quantity == D("36")


# --- "Atualização" (PositionEffect.QTY_SYNC) -------------------------------
# B3 uses one label for two different things. These cases mirror real rows
# from the reference export; see docs/ARCHITECTURE.md.


def sync(day: str, qty: str, asset_id: int = 1) -> Movement:
    return mv(day, PositionEffect.QTY_SYNC, qty, op_type=OperationType.POSITION_UPDATE, asset_id=asset_id)


def test_position_restatement_is_not_applied():
    """SMAL11: 36 units restated every few months must stay 36, not 180."""
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "36", "100", "3600"),
            sync("2025-05-29", "36"),
            sync("2025-10-08", "36"),
            sync("2026-03-05", "36"),
        ]
    )
    position = positions[1]
    assert position.quantity == D("36")
    assert position.cost_basis == D("3600")
    assert len(position.notes) == 3


def test_position_update_that_differs_is_credited():
    """IRDM11: units credited by the fund, reported as 'Atualização'."""
    positions = build_positions(
        [
            mv("2020-07-22", PositionEffect.ACQUIRE, "5", "110", "550"),
            sync("2021-01-19", "1"),
            sync("2021-05-18", "1"),
        ]
    )
    position = positions[1]
    assert position.quantity == D("7")
    assert position.cost_basis == D("550")  # free units dilute the average
    assert position.average_price < D("110")


def test_same_day_updates_across_brokers_are_judged_together():
    """PATL11: 3 units at one broker + 171 at another restate the 174 held."""
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "174", "60", "10440"),
            sync("2026-07-02", "3"),
            sync("2026-07-02", "171"),
        ]
    )
    assert positions[1].quantity == D("174")


def test_update_is_compared_after_the_same_day_purchases():
    """BBAS3: buys settle first, then B3 restates the resulting position."""
    positions = build_positions(
        [
            mv("2026-04-29", PositionEffect.ACQUIRE, "700", "20", "14000"),
            mv("2026-04-29", PositionEffect.ACQUIRE, "14", "25", "350"),
            sync("2026-04-29", "714"),
        ]
    )
    assert positions[1].quantity == D("714")


def test_update_on_an_empty_position_creates_it():
    """A ticker rename: shares from the change land as an 'Atualização'."""
    positions = build_positions([sync("2023-02-10", "672")])
    assert positions[1].quantity == D("672")
    assert positions[1].cost_basis == D("0")


# --- share restructurings and fraction auctions ----------------------------
# B3 writes a plain split as a delta but a grupamento+desdobramento as the
# resulting position. These cases mirror real rows from the reference export.


def test_lone_split_is_a_delta():
    """SNAG11: 53 held + a 477 'Desdobro' credit = the 565 B3 then reports."""
    positions = build_positions(
        [
            mv("2023-07-19", PositionEffect.ACQUIRE, "50", "10", "500"),
            mv("2023-08-02", PositionEffect.ACQUIRE, "3", "10", "30"),
            mv("2023-08-03", PositionEffect.QTY_IN_FREE, "477", op_type=OperationType.SPLIT),
            mv("2023-08-08", PositionEffect.ACQUIRE, "35", "1", "35"),
        ]
    )
    assert positions[1].quantity == D("565")
    assert positions[1].cost_basis == D("565")  # 500 + 30 + 35, untouched by the split


def test_grouping_plus_split_restates_the_position():
    """NGRD3: 3.484 shares restructured into 136,84 — not 3.484 + 136,84."""
    positions = build_positions(
        [
            mv("2024-08-21", PositionEffect.ACQUIRE, "3484", "1.3", "4537.40"),
            mv("2024-10-09", PositionEffect.QTY_IN_FREE, "102", op_type=OperationType.SPLIT),
            mv("2024-10-09", PositionEffect.QTY_IN_FREE, "34.84", op_type=OperationType.REVERSE_SPLIT),
        ]
    )
    position = positions[1]
    assert position.quantity == D("136.84")
    assert position.cost_basis == D("4537.40")  # a restructuring costs nothing
    assert position.average_price == pytest.approx(D("33.16"), abs=D("0.01"))
    assert any("grupamento + desdobramento" in note for note in position.notes)


def test_restructured_position_can_be_fully_sold():
    """The regression this was written for: NGRD3 must end at zero."""
    positions = build_positions(
        [
            mv("2024-08-21", PositionEffect.ACQUIRE, "3484", "1.3", "4537.40"),
            mv("2024-10-09", PositionEffect.QTY_IN_FREE, "102", op_type=OperationType.SPLIT),
            mv("2024-10-09", PositionEffect.QTY_IN_FREE, "34.84", op_type=OperationType.REVERSE_SPLIT),
            mv("2024-10-09", PositionEffect.QTY_OUT_FREE, "0.84", op_type=OperationType.FRACTION),
            mv("2024-12-12", PositionEffect.ACQUIRE, "5", "27.27", "136.35"),
            mv("2025-06-13", PositionEffect.ACQUIRE, "7", "26", "182"),
            mv("2026-06-11", PositionEffect.DISPOSE, "148", "33.94", "5023.12", op_type=OperationType.SELL),
        ]
    )
    position = positions[1]
    assert position.quantity == D("0")
    assert position.cost_basis == D("0")
    assert not position.is_open
    assert position.warnings == []  # the sale matched the position exactly


def test_split_ordering_puts_the_restatement_before_the_fraction_removal():
    positions = build_positions(
        [
            mv("2025-04-16", PositionEffect.QTY_OUT_FREE, "0.62", op_type=OperationType.FRACTION),
            mv("2025-04-16", PositionEffect.QTY_IN_FREE, "5.62", op_type=OperationType.REVERSE_SPLIT),
            mv("2025-04-16", PositionEffect.QTY_IN_FREE, "395", op_type=OperationType.SPLIT),
            mv("2024-01-10", PositionEffect.ACQUIRE, "225", "20", "4500"),
        ]
    )
    # VIVT3: 225 restructured into 400,62 less the 0,62 fraction = 400.
    assert positions[1].quantity == D("400")


def test_fraction_auction_does_not_remove_the_quantity_twice():
    """B3 removes the fraction first and pays for it weeks later."""
    positions = build_positions(
        [
            mv("2021-12-22", PositionEffect.ACQUIRE, "169", "10", "1690"),
            mv("2022-01-31", PositionEffect.QTY_OUT_FREE, "0.45", op_type=OperationType.FRACTION),
            mv("2022-03-04", PositionEffect.REALIZE, "0.45", "9", "4.05", op_type=OperationType.FRACTION),
        ]
    )
    position = positions[1]
    assert position.quantity == D("168.55")  # not 168.10
    # Proceeds net of the cost that left with the fraction (0,45 x 10 = 4,50).
    assert position.realized_pnl == pytest.approx(D("-0.45"), abs=D("0.001"))
    assert position.income == D("0")  # an auction is not income


def test_timeline_tracks_cumulative_state():
    timeline = build_timeline(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000"),
            mv("2024-02-10", PositionEffect.CASH_IN, "100", "0.5", "50", op_type=OperationType.DIVIDEND),
            mv("2024-03-10", PositionEffect.DISPOSE, "50", "20", "1000", op_type=OperationType.SELL),
        ]
    )
    assert [p.day.isoformat() for p in timeline] == ["2024-01-10", "2024-02-10", "2024-03-10"]
    assert timeline[0].cost_basis == D("1000")
    assert timeline[1].dividends == D("50")
    assert timeline[2].cost_basis == D("500")
    assert timeline[2].realized == D("500")
    assert timeline[2].invested_flow == D("0")  # 1000 in, 1000 back out


def test_multiple_assets_are_independent():
    positions = build_positions(
        [
            mv("2024-01-10", PositionEffect.ACQUIRE, "100", "10", "1000", asset_id=1),
            mv("2024-01-10", PositionEffect.ACQUIRE, "50", "20", "1000", asset_id=2),
            mv("2024-02-10", PositionEffect.DISPOSE, "50", "30", "1500", op_type=OperationType.SELL, asset_id=2),
        ]
    )
    assert positions[1].quantity == D("100")
    assert positions[1].realized_pnl == D("0")
    assert positions[2].quantity == D("0")
    assert positions[2].realized_pnl == D("500")


def test_unexercised_rights_expire_even_with_no_quantity():
    """B3 states that the window closed but not how many rights lapsed.

    Every one of the 57 "Não Exercido" rows in the reference export carries a
    quantity of zero, so read literally the debit removes nothing and the
    expired rights linger for years as a position that cannot be sold, priced
    or converted — 3.162 phantom SNAG12 among them.
    """
    movements = [
        mv("2024-01-10", PositionEffect.QTY_IN_FREE, qty="1581", op_type=OperationType.SUBSCRIPTION),
        mv("2024-02-05", PositionEffect.QTY_EXPIRE, qty="0", op_type=OperationType.SUBSCRIPTION),
    ]
    position = build_positions(movements)[1]
    assert position.quantity == D(0)
    assert not position.is_open
    assert any("expiraram" in note for note in position.notes)


def test_a_quantified_expiry_takes_only_what_it_names():
    movements = [
        mv("2024-01-10", PositionEffect.QTY_IN_FREE, qty="100", op_type=OperationType.SUBSCRIPTION),
        mv("2024-02-05", PositionEffect.QTY_EXPIRE, qty="40", op_type=OperationType.SUBSCRIPTION),
    ]
    assert build_positions(movements)[1].quantity == D("60")


def test_expiring_a_right_that_cost_money_realises_the_loss():
    """Cost cannot simply vanish: it would show up as profit.

    An exercised right leaves its cash on the subscription line, so when the
    line is swept the money has to be accounted for. Booking it as a realised
    loss keeps ``cost + realised`` intact — dropping it silently would inflate
    the portfolio's return by exactly that amount.
    """
    movements = [
        mv("2024-01-10", PositionEffect.ACQUIRE, qty="20", amount="134", op_type=OperationType.SUBSCRIPTION),
        mv("2024-02-05", PositionEffect.QTY_EXPIRE, qty="0", op_type=OperationType.SUBSCRIPTION),
    ]
    position = build_positions(movements)[1]
    assert position.quantity == D(0)
    assert position.cost_basis == D(0)
    assert position.realized_pnl == D("-134")


def test_expiry_sweeps_after_the_days_other_debits():
    """An unquantified sweep has to see the balance the day actually ends on."""
    movements = [
        mv("2024-01-10", PositionEffect.QTY_IN_FREE, qty="100", op_type=OperationType.SUBSCRIPTION),
        mv("2024-02-05", PositionEffect.QTY_EXPIRE, qty="0", op_type=OperationType.SUBSCRIPTION),
        mv("2024-02-05", PositionEffect.QTY_OUT_FREE, qty="30", op_type=OperationType.SUBSCRIPTION),
    ]
    assert build_positions(movements)[1].quantity == D(0)
