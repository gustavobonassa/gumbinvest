"""Canonical movement labels emitted by the PDF parsers.

Every parser translates its broker's wording into one of these, so the
classifier keeps a single table for all sources instead of one per broker (see
:mod:`app.importer.classifier`). The strings are English because that is what
three of the four formats already use; the label is stored verbatim on the
transaction, so the UI shows what the classifier actually matched on.
"""
from __future__ import annotations

BUY = "Buy"
SELL = "Sell"
DIVIDEND = "Dividend"
#: Non-resident alien withholding taken out of a dividend.
DIVIDEND_TAX = "Dividend Tax Withheld"
INTEREST = "Interest"
FEE = "Fee"
#: Depositary fee charged by the ADR custodian bank.
ADR_FEE = "ADR Fee"
#: Shares moving between custodians (ACATS, "clearing firm conversion").
CUSTODY_TRANSFER = "Custody Transfer"
MERGER = "Merger"
#: Free shares from a forward split; the quantity is the *extra* shares.
SPLIT = "Stock Split"
#: Money in/out of the brokerage cash account: deposits, withdrawals, journals
#: and money-market sweeps. Parsed so that section totals reconcile, then
#: dropped by the import service — they belong to no asset.
CASH_MOVEMENT = "Cash Movement"

#: Labels that carry no asset and are therefore never persisted.
CASH_ONLY = frozenset({CASH_MOVEMENT})
