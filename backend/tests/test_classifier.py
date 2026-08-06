"""Every movement label present in the reference export must be understood."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.enums import Direction, OperationType, PositionEffect
from app.importer.classifier import classify, parse_direction

# (movement, direction, expected op type, expected effect) — taken verbatim
# from the 3.492-row reference export.
CASES = [
    ("Transferência - Liquidação", Direction.CREDIT, OperationType.BUY, PositionEffect.ACQUIRE),
    ("Transferência - Liquidação", Direction.DEBIT, OperationType.SELL, PositionEffect.DISPOSE),
    ("Compra", Direction.CREDIT, OperationType.BUY, PositionEffect.ACQUIRE),
    ("Venda", Direction.DEBIT, OperationType.SELL, PositionEffect.DISPOSE),
    ("COMPRA / VENDA", Direction.CREDIT, OperationType.BUY, PositionEffect.ACQUIRE),
    ("Rendimento", Direction.CREDIT, OperationType.YIELD, PositionEffect.CASH_IN),
    ("Rendimento - Transferido", Direction.DEBIT, OperationType.YIELD, PositionEffect.CASH_OUT),
    ("Dividendo", Direction.CREDIT, OperationType.DIVIDEND, PositionEffect.CASH_IN),
    ("Juros Sobre Capital Próprio", Direction.CREDIT, OperationType.JCP, PositionEffect.CASH_IN),
    ("PAGAMENTO DE JUROS", Direction.CREDIT, OperationType.INTEREST, PositionEffect.CASH_IN),
    ("Restituição de Capital", Direction.CREDIT, OperationType.AMORTIZATION, PositionEffect.RETURN_OF_CAPITAL),
    ("Amortização", Direction.CREDIT, OperationType.AMORTIZATION, PositionEffect.RETURN_OF_CAPITAL),
    ("Desdobro", Direction.CREDIT, OperationType.SPLIT, PositionEffect.QTY_IN_FREE),
    ("Grupamento", Direction.CREDIT, OperationType.REVERSE_SPLIT, PositionEffect.QTY_IN_FREE),
    ("Bonificação em Ativos", Direction.CREDIT, OperationType.BONUS, PositionEffect.QTY_IN_FREE),
    ("Incorporação", Direction.CREDIT, OperationType.MERGER, PositionEffect.QTY_IN_FREE),
    ("Fração em Ativos", Direction.DEBIT, OperationType.FRACTION, PositionEffect.QTY_OUT_FREE),
    ("Direito de Subscrição", Direction.CREDIT, OperationType.SUBSCRIPTION, PositionEffect.QTY_IN_FREE),
    ("Recibo de Subscrição", Direction.DEBIT, OperationType.SUBSCRIPTION, PositionEffect.QTY_OUT_FREE),
    ("Cessão de Direitos", Direction.CREDIT, OperationType.SUBSCRIPTION, PositionEffect.QTY_IN_FREE),
    (
        "Direitos de Subscrição - Não Exercido",
        Direction.DEBIT,
        OperationType.SUBSCRIPTION,
        PositionEffect.QTY_EXPIRE,
    ),
    ("Transferência", Direction.CREDIT, OperationType.TRANSFER_IN, PositionEffect.QTY_IN_FREE),
    ("Transferência", Direction.DEBIT, OperationType.TRANSFER_OUT, PositionEffect.QTY_OUT_FREE),
    ("Atualização", Direction.CREDIT, OperationType.POSITION_UPDATE, PositionEffect.QTY_SYNC),
]


@pytest.mark.parametrize(("movement", "direction", "op_type", "effect"), CASES)
def test_reference_movements(movement, direction, op_type, effect):
    result = classify(movement, direction, Decimal("100"))
    assert result.op_type is op_type
    assert result.effect is effect


def test_leilao_de_fracao_realises_cash_without_touching_the_quantity():
    """The fraction already left via 'Fração em Ativos'; only cash arrives."""
    result = classify("Leilão de Fração", Direction.CREDIT, Decimal("9.67"))
    assert result.op_type is OperationType.FRACTION
    assert result.effect is PositionEffect.REALIZE


def test_disposal_without_amount_does_not_realise():
    """A maturity row with '-' as the amount must not invent a realised gain."""
    result = classify("VENCIMENTO", Direction.DEBIT, None)
    assert result.effect is PositionEffect.QTY_OUT_FREE


def test_exercised_subscription_is_an_acquisition():
    result = classify("Direitos de Subscrição - Exercido", Direction.DEBIT, Decimal("6.70"))
    assert result.effect is PositionEffect.ACQUIRE


def test_unknown_movement_is_flagged_but_never_applied():
    result = classify("Evento Totalmente Novo", Direction.CREDIT, Decimal("1"))
    assert result.op_type is OperationType.UNKNOWN
    assert result.effect is PositionEffect.NONE
    assert result.warning


def test_keyword_fallback_handles_unseen_wording():
    result = classify("Dividendo Extraordinário Complementar", Direction.CREDIT, Decimal("5"))
    assert result.op_type is OperationType.DIVIDEND
    assert result.effect is PositionEffect.CASH_IN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Credito", Direction.CREDIT), ("Débito", Direction.DEBIT), ("Debito", Direction.DEBIT), ("Entrada", Direction.CREDIT)],
)
def test_parse_direction(raw, expected):
    assert parse_direction(raw) is expected
