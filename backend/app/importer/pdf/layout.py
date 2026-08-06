"""Turning a PDF page into lines of positioned words.

Plain text extraction is not enough for these statements. Apex and Avenue put
amounts in a ``DEBIT`` and a ``CREDIT`` column whose *contents are identical*
("Retenção Impostos sobre Dividendos" is a debit, "Estorno Retenção Impostos
sobre Dividendos" a credit) — the sign lives entirely in the x-coordinate. So
words keep their horizontal position and columns are anchored on the header row.

Amounts in these tables are right-aligned, which is why :meth:`ColumnMap.assign`
matches on the right edge: a column's numbers end where its header ends,
whatever their width.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from functools import cached_property

from app.importer.pdf.values import parse_period

#: Words whose ``top`` differs by less than this belong to the same line.
LINE_TOLERANCE = 2.5
#: How far a value's right edge may sit from its column's before we give up.
COLUMN_TOLERANCE = 32.0


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    x1: float

    @property
    def middle(self) -> float:
        return (self.x0 + self.x1) / 2


# NOTE: the classes below memoise derived text with ``cached_property``, which
# needs a ``__dict__`` — so they deliberately do not use ``slots=True``.
@dataclass
class Line:
    """One visual row of a page."""

    page_number: int
    top: float
    words: list[Word] = field(default_factory=list)

    @cached_property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @cached_property
    def key(self) -> str:
        """Accent-free, lowercase, alphanumeric — for robust label matching."""
        return normalize(self.text)

    def before(self, x: float) -> list[Word]:
        return [word for word in self.words if word.x1 <= x]

    def after(self, x: float) -> list[Word]:
        return [word for word in self.words if word.x0 >= x]

    def between(self, x0: float, x1: float) -> list[Word]:
        return [word for word in self.words if word.x0 >= x0 and word.x1 <= x1]

    def find(self, pattern: str) -> Word | None:
        expression = re.compile(pattern)
        return next((word for word in self.words if expression.fullmatch(word.text)), None)


@dataclass
class Page:
    number: int
    width: float
    lines: list[Line]

    @cached_property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class Document:
    """A parsed PDF: pages of positioned lines, plus whole-document text."""

    pages: list[Page]

    @cached_property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @cached_property
    def head(self) -> str:
        """Text of the first few pages — enough to identify the format."""
        return "\n".join(page.text for page in self.pages[:3])

    def lines(self) -> list[Line]:
        return [line for page in self.pages for line in page.lines]

    def period(self):
        """The statement period, read from the page header."""
        return parse_period(self.head)


def normalize(value: str) -> str:
    """Accent-free, lowercase, alphanumeric — mirrors the CSV importer's helper."""
    from app.importer.parser import normalize_key

    return normalize_key(value)


def load_document(payload: bytes) -> Document:
    """Extract every page of a PDF into positioned lines."""
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
            pages.append(
                Page(number=index, width=float(page.width or 0), lines=_group_lines(index, words))
            )
    return Document(pages=pages)


def _group_lines(page_number: int, words: list[dict]) -> list[Line]:
    """Bucket words into visual rows, tolerating sub-pixel baseline drift."""
    grouped: list[Line] = []
    for raw in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(raw["top"])
        word = Word(text=raw["text"], x0=float(raw["x0"]), x1=float(raw["x1"]))
        if grouped and abs(grouped[-1].top - top) <= LINE_TOLERANCE:
            grouped[-1].words.append(word)
            continue
        grouped.append(Line(page_number=page_number, top=top, words=[word]))
    for line in grouped:
        line.words.sort(key=lambda w: w.x0)
    return grouped


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    x0: float
    x1: float


@dataclass(slots=True)
class ColumnMap:
    """Column boundaries taken from a table's header row."""

    columns: list[Column]

    @classmethod
    def from_line(cls, line: Line, labels: dict[str, str]) -> "ColumnMap | None":
        """Anchor ``labels`` (name -> header word) against a header line.

        Multi-word headers are matched on their first word, which is enough:
        the columns of interest ("DEBIT", "CREDIT", "AMOUNT", "QUANTITY") are
        single words in every family.
        """
        found: list[Column] = []
        for name, label in labels.items():
            target = normalize(label)
            match = next((word for word in line.words if normalize(word.text) == target), None)
            if match is None:
                return None
            found.append(Column(name=name, x0=match.x0, x1=match.x1))
        return cls(columns=sorted(found, key=lambda column: column.x0))

    def get(self, name: str) -> Column | None:
        return next((column for column in self.columns if column.name == name), None)

    def assign(self, word: Word) -> str | None:
        """Which column a right-aligned value belongs to (``None`` if unclear)."""
        best: tuple[float, str] | None = None
        for column in self.columns:
            distance = abs(word.x1 - column.x1)
            if distance <= COLUMN_TOLERANCE and (best is None or distance < best[0]):
                best = (distance, column.name)
        return best[1] if best else None
