"""Adapters over the public bulk files the universe is built from.

Every adapter downloads a whole published file and reduces it — none of them
makes a per-ticker request. That is the rule the feature is designed around:
Yahoo's per-asset endpoints stay reserved for papers the portfolio actually
holds (``app.market.fundamentals``), so a market-wide index can never put them
at risk. It is also why a full run is ten HTTP calls and a few minutes rather
than two thousand calls and several hours.

Shared here: the download helper, the encoding ladder those files need, pt-BR
number parsing, and :class:`SourceShapeError`.

The shape check matters more than it looks. CVM rotates its files yearly and
column names are the only contract there is; if one disappears, the honest move
is to skip that stage with the missing names recorded and leave the previous
rows alone. Overwriting good data with NULLs because a header changed would be
the "confidently wrong" failure the classifier rule exists to prevent.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: These files are tens of megabytes; the ordinary quote timeout is far too
#: short for them (treasury.py makes the same allowance for the same reason).
BULK_TIMEOUT = max(settings.request_timeout * 12, 180.0)

#: B3's proxies answer 403 to httpx's default agent.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SourceShapeError(RuntimeError):
    """A published file no longer has the columns this adapter reads.

    Raised before anything is written. The driver records the message and skips
    the stage, so the previous — still correct — rows survive.
    """


def require_columns(where: str, header: list[str] | None, required: set[str]) -> None:
    """Fail loudly when a source has dropped or renamed a column we read."""
    present = set(header or ())
    missing = sorted(required - present)
    if missing:
        raise SourceShapeError(
            f"{where}: colunas ausentes na fonte ({', '.join(missing)}). "
            "O formato publicado mudou; a etapa foi ignorada."
        )


def fetch_bytes(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    """Download one published file whole. Raises for any non-2xx."""
    request_headers = {"User-Agent": BROWSER_UA, **(headers or {})}
    with httpx.Client(timeout=BULK_TIMEOUT, follow_redirects=True) as client:
        response = client.get(url, headers=request_headers)
        response.raise_for_status()
        return response.content


def decode(raw: bytes) -> str:
    """CVM publishes latin-1, B3 mixes; try the ladder rather than guess."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


_HREF = re.compile(r'href="([^"?][^"]*)"', re.IGNORECASE)


def list_directory(url: str) -> list[str]:
    """Filenames from an nginx autoindex page.

    CVM's datasets rotate yearly (``dfp_cia_aberta_2026.zip``), so the file to
    fetch is *discovered* here rather than hardcoded — a pinned URL would break
    every January without anyone touching this repo.
    """
    body = decode(fetch_bytes(url))
    names = [name for name in _HREF.findall(body) if not name.startswith(("/", "http"))]
    return [name for name in names if name not in ("../", "./")]


def newest_year_file(url: str, pattern: str) -> tuple[str, int] | None:
    """The highest-year file matching ``pattern`` in a directory index.

    ``pattern`` must carry one capturing group for the four-digit year, e.g.
    ``r"dfp_cia_aberta_(\\d{4})\\.zip"``.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    best: tuple[str, int] | None = None
    for name in list_directory(url):
        match = regex.fullmatch(name)
        if match is None:
            continue
        year = int(match.group(1))
        if best is None or year > best[1]:
            best = (name, year)
    return best


def zip_members(raw: bytes) -> dict[str, zipfile.ZipInfo]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise SourceShapeError("o arquivo baixado não é um zip válido") from exc
    return {member.filename: member for member in archive.infolist()}


def read_zip_csv(
    raw: bytes, member: str, required: set[str], *, delimiter: str = ";"
) -> Iterator[dict[str, str]]:
    """Stream one member of a zip as CSV rows.

    Streamed rather than read whole because these members run to tens of
    megabytes uncompressed and the desktop build has no memory to spare for a
    list of dicts that size.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if member not in archive.namelist():
            raise SourceShapeError(f"membro ausente no zip: {member}")
        with archive.open(member) as handle:
            text = io.TextIOWrapper(handle, encoding="latin-1", newline="")
            reader = csv.DictReader(text, delimiter=delimiter)
            require_columns(member, reader.fieldnames and list(reader.fieldnames), required)
            yield from reader


def read_csv(raw: bytes, required: set[str], *, delimiter: str = ";") -> Iterator[dict[str, str]]:
    """Stream a bare CSV download as rows."""
    reader = csv.DictReader(io.StringIO(decode(raw)), delimiter=delimiter)
    require_columns("csv", reader.fieldnames and list(reader.fieldnames), required)
    yield from reader


def to_decimal(value: str | None) -> Decimal | None:
    """A CVM figure as Decimal. Blank, ``-`` and unparsable all mean *absent*.

    CVM writes plain dotted decimals in these datasets, so — unlike the Tesouro
    feed — no comma swap is wanted here; doing it anyway would turn 1.234 into
    1234.
    """
    text = (value or "").strip()
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def digits(value: str | None) -> str | None:
    """A CNPJ reduced to its digits — the only form both sources agree on.

    B3 publishes ``46639922000144`` and CVM ``08.773.135/0001-00``; stripping
    punctuation makes the join exact, so nothing here has to match on names.
    """
    only = re.sub(r"\D", "", value or "")
    return only.zfill(14) if only and len(only) <= 14 else (only or None)


def normalize(text: str) -> str:
    """Fold a name to a comparable key: no accents, no punctuation."""
    stripped = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).split())
