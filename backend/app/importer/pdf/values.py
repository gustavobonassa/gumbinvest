"""Locale-tolerant parsing of the numbers and dates found in broker statements.

These files are not consistent — not between brokers, not between format
generations, and (Apex's Portuguese statements) not even between two tables of
the *same* document, where the portfolio table prints ``1.274,58`` and the
activity table two pages later prints ``1.315.39`` for the same kind of value.

So numbers are parsed structurally rather than by locale: the separator layout
itself says which character is the decimal mark, and a locale hint is only
consulted for the one genuinely ambiguous shape (``1.234`` / ``1,234``).
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

#: Text that stands for "no value" in these statements.
NA_TOKENS = frozenset({"", "-", "--", "—", "n/a", "na", "nenhum", "none"})

_CURRENCY_RE = re.compile(r"(?i)(us\$|r\$|usd|brl|\$)")
_KEEP_RE = re.compile(r"[^0-9.,+-]")

MONTH_NAMES = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
MONTH_NAMES.update({name[:3]: number for name, number in list(MONTH_NAMES.items())})


def parse_number(text: str | None, prefer: str = "en") -> Decimal | None:
    """Parse a monetary/quantity token into a :class:`Decimal`.

    ``prefer`` (``"en"`` or ``"pt"``) only breaks the tie for a lone separator
    followed by exactly three digits — ``1.234``, which is 1234 in en-US and
    1,234 in pt-BR. Every other shape is decided by the token itself:

    >>> parse_number("1,234.56")        # en-US
    Decimal('1234.56')
    >>> parse_number("1.234,56")        # pt-BR
    Decimal('1234.56')
    >>> parse_number("1.315.39")        # Apex PT activity tables
    Decimal('1315.39')
    >>> parse_number("(3.36)")          # DriveWealth debits
    Decimal('-3.36')
    """
    if text is None:
        return None
    raw = str(text).strip()
    if raw.lower() in NA_TOKENS:
        return None

    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    raw = _CURRENCY_RE.sub("", raw).strip()
    if raw.lower() in NA_TOKENS:
        return None

    cleaned = _KEEP_RE.sub("", raw)
    if cleaned.startswith("-"):
        negative = not negative
    cleaned = cleaned.lstrip("+-")
    if not cleaned or not any(character.isdigit() for character in cleaned):
        return None

    digits = _to_plain_digits(cleaned, prefer)
    if digits is None:
        return None
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return -value if negative else value


def _to_plain_digits(cleaned: str, prefer: str) -> str | None:
    """Rewrite a separator-laden number as a plain ``123.45`` string."""
    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        # Whichever separator comes last is the decimal mark.
        decimal_mark = "." if cleaned.rfind(".") > cleaned.rfind(",") else ","
        return _split_on(cleaned, decimal_mark)

    separator = "." if has_dot else "," if has_comma else None
    if separator is None:
        return cleaned

    groups = cleaned.split(separator)
    tail = groups[-1]
    # A trailing group that is not a thousands group settles it: "626.97",
    # "2.09797" and "1.315.39" are all decimals, whatever the document's locale.
    if len(tail) != 3:
        return _split_on(cleaned, separator)
    # "1.234": genuinely ambiguous, so fall back to the document's locale.
    if len(groups) == 2:
        thousands = separator == ("." if prefer == "pt" else ",")
        return cleaned.replace(separator, "") if thousands else _split_on(cleaned, separator)
    # "1.234.567" — three or more groups of three digits are all thousands.
    return cleaned.replace(separator, "")


def _split_on(cleaned: str, decimal_mark: str) -> str:
    head, _, tail = cleaned.rpartition(decimal_mark)
    head = re.sub(r"[.,]", "", head)
    return f"{head or '0'}.{tail}"


def parse_money(text: str | None, prefer: str = "en") -> Decimal | None:
    """:func:`parse_number` for amounts (kept separate for readability)."""
    return parse_number(text, prefer)


def parse_quantity(text: str | None, prefer: str = "en") -> Decimal:
    """Quantities default to zero — cash-only rows legitimately have none."""
    value = parse_number(text, prefer)
    return Decimal(0) if value is None else value


def parse_date(text: str | None, order: str = "auto") -> date | None:
    """Parse a statement date.

    ``order`` is ``"mdy"`` (Apex/DriveWealth), ``"dmy"`` (Avenue's Portuguese
    statements), ``"iso"`` or ``"auto"``. Getting this wrong is silent and
    expensive — ``05/01/2026`` is January 5th in one family and May 1st in
    another — so parsers state it explicitly and ``auto`` is only a fallback.
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw or raw.lower() in NA_TOKENS:
        return None

    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if iso:
        return _build(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    if order == "iso":
        return None

    named = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$", raw)
    if named:
        month = MONTH_NAMES.get(named.group(1).lower())
        if month:
            return _build(int(named.group(3)), month, int(named.group(2)))

    slashed = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", raw)
    if not slashed:
        return None
    first, second = int(slashed.group(1)), int(slashed.group(2))
    year = _expand_year(int(slashed.group(3)))

    if order == "mdy":
        month, day = first, second
    elif order == "dmy":
        month, day = second, first
    else:
        # Only one reading can be a real date; ties default to month-first.
        month, day = (first, second) if first <= 12 else (second, first)
    return _build(year, month, day)


def _expand_year(year: int) -> int:
    return year if year >= 100 else 2000 + year


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_period(text: str) -> tuple[date, date] | None:
    """Find a ``start - end`` statement period anywhere in a page of text.

    Covers the three shapes in use: ``January 01, 2026 - January 31, 2026``,
    ``May 1, 2021 - May 31, 2021`` and ``2026-01-01 - 2026-01-31``.
    """
    iso = re.search(r"(\d{4}-\d{2}-\d{2})\s*[-–]\s*(\d{4}-\d{2}-\d{2})", text)
    if iso:
        start, end = parse_date(iso.group(1), "iso"), parse_date(iso.group(2), "iso")
        if start and end:
            return start, end

    named = re.search(
        r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\s*[-–]\s*([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})",
        text,
    )
    if named:
        start, end = parse_date(named.group(1)), parse_date(named.group(2))
        if start and end:
            return start, end
    return None

