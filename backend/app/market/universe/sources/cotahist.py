"""B3 COTAHIST: the whole exchange's daily prices, from published files.

One file gives every listed instrument's closes, volumes and ISINs for a whole
year — which is why the universe needs no per-ticker quote calls at all. The
files are fixed-width text inside a zip, a format B3 has published unchanged for
decades, and the layout below was verified byte-for-byte against a live file
rather than taken from documentation.

**Nothing raw is stored.** The stream is reduced as it is read, into one small
accumulator per ticker (last close, a 21-session volume window, the close a year
back, 52-week extremes, daily returns for volatility). Keeping 2.3 million daily
records to answer twenty questions per ticker would cost hundreds of megabytes
in a database the desktop build ships to a laptop; storing the answers costs
nothing. Retaining the raw series is a separate, opt-in Phase 3 concern.

Sizes and timings, measured 2026-08-05: the annual file is 67 MB compressed and
567 MB of text, and downloads plus reduces in about 7 s. Monthly files are ~10 MB
and daily ones ~400 KB, which is what makes the nightly refresh cheap: a run
that already holds a year only needs the days it is missing.
"""
from __future__ import annotations

import io
import math
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.core.logging import get_logger

from . import SourceShapeError, fetch_bytes

logger = get_logger(__name__)

BASE_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist"

SOURCE = "b3-cotahist"

#: Record layout, 0-indexed slices into a 245-byte line. Verified against
#: COTAHIST_A2026: PETR4 reads 42.50 with 147 sessions year-to-date.
_TIPREG = slice(0, 2)
_DATA = slice(2, 10)
_CODBDI = slice(10, 12)
_CODNEG = slice(12, 24)
_TPMERC = slice(24, 27)
_NOMRES = slice(27, 39)  # abbreviated issuer name
_ESPECI = slice(39, 49)  # share class: ON, PN, CI, DRN, UNT
_PREULT = slice(108, 121)  # last price, 2 implied decimals
_QUATOT = slice(152, 170)  # quantity traded
_VOLTOT = slice(170, 188)  # financial volume, 2 implied decimals
_CODISI = slice(230, 242)

#: Mercado à vista. This is the filter that matters: it selects the 1 988
#: tradable cash-market instruments and excludes options (070/080), forward
#: (030) and the fractional market (020, the ``…F`` tickers). Filtering on
#: CODBDI instead — the obvious first guess — silently drops every FII and ETF.
CASH_MARKET = b"010"

#: CODBDI to AssetKind, within the cash market. B3 states the instrument family
#: here, which settles the ``…11`` question that a ticker suffix only guesses at:
#: HGLG11 is CODBDI 12 (FII) and BOVA11 is 14 (ETF), no heuristic required.
_KIND_BY_CODBDI = {
    b"02": "STOCK",  # lote padrão: ON, PN, UNT
    b"05": "STOCK",  # ações com direitos
    b"06": "STOCK",
    b"07": "STOCK",
    b"08": "STOCK",  # empresas em recuperação judicial — listadas, e negociadas
    b"12": "FII",
    b"14": "ETF",  # ETFs, FI-Infra, FIAGRO e afins
    b"22": "UNIT",
    b"34": "BDR",
    b"35": "BDR",
    b"36": "BDR",  # BDR de ETF (…39)
}

#: Sessions in a rolling liquidity window, and in a year.
VOLUME_WINDOW = 21
YEAR_SESSIONS = 252

#: COTAHIST publishes prices **as traded**, never adjusted for splits, reverse
#: splits or bonuses — and its quotation-factor field does not flag them: a
#: market-wide scan found 208 of 2 479 tickers with a session-to-session price
#: discontinuity and exactly one with a varying FATCOT. Left alone, ALZR11's
#: grupamento (9,99 -> 97,92) reads as a 880 % return, and the same distortion
#: reaches the 52-week range and the volatility.
#:
#: A move beyond this ratio in a single session is treated as a corporate
#: action rather than trading. Wide enough that ordinary volatility — even a
#: limit-up penny stock — never trips it.
SPLIT_RATIO = Decimal("2.5")

#: Ratios a corporate action actually uses. A price that jumps by almost
#: exactly one of these did not trade there; it was restated. Anything else is
#: left unadjusted and its window figures are withheld instead, because
#: inventing a factor is worse than admitting the series is broken.
_SPLIT_FACTORS = (
    Decimal(2), Decimal(3), Decimal(4), Decimal(5), Decimal(6), Decimal(8),
    Decimal(10), Decimal(15), Decimal(20), Decimal(25), Decimal(50), Decimal(100),
)
#: How far from a round ratio still counts as that ratio — the price moves on
#: the day of the event too, so the observed jump is never exact.
_SPLIT_TOLERANCE = Decimal("0.08")

#: Below this the tick size dominates: at R$ 0,03 a single centavo is a third
#: of the price, so ordinary oscillation clears any ratio test. Papers that
#: cheap are marked discontinuous rather than "adjusted" — the first attempt
#: at this found ALZR11 splitting fifty times in a year, which is not a thing.
MIN_PRICE_FOR_SPLIT = Decimal("1.00")

#: Corporate actions per year beyond which the detector is not to be believed.
#: A company restates its shares once or twice; a series that keeps jumping is
#: telling us the prices are unreliable, not that it keeps splitting.
MAX_SPLITS = 4

#: A paper that barely trades produces a meaningless 12-month return; below
#: this the window figures are withheld rather than published as fact.
MIN_SESSIONS_FOR_12M = 60


@dataclass(slots=True)
class Reduction:
    """Everything the screener needs from one ticker's price history.

    Accumulated in one pass. ``closes`` is bounded by the year window, so the
    memory here is a few hundred bytes per ticker however long the file is.
    """

    ticker: str
    isin: str | None = None
    kind: str | None = None
    codbdi: str | None = None
    #: B3's abbreviated issuer name ("PETROBRAS"). A placeholder until the
    #: registry stage replaces it with the company's full legal name — but a
    #: real one, so a run that stops after this stage still reads sensibly.
    name: str = ""
    #: Share class as B3 files it ("PN N2", "CI ER", "DRN"); feeds the ticker
    #: classifier for the few CODBDI values not in the table above.
    especi: str = ""
    last_date: date | None = None
    last_close: Decimal | None = None
    #: Every session seen, across every file folded in. Diagnostic only — the
    #: published figures below are all window figures.
    sessions: int = 0
    #: Corporate actions absorbed by rescaling the earlier closes.
    splits: int = 0
    #: A price discontinuity that matched no recognisable split ratio. The
    #: series is not comparable across it, so the window figures are withheld.
    discontinuous: bool = False
    #: (date, close) for the trailing year, oldest first. Bounded, so feeding
    #: two year-files still leaves a 52-week window here rather than a
    #: two-year one — a "52-week high" computed over 24 months is simply wrong.
    _closes: deque = field(default_factory=lambda: deque(maxlen=YEAR_SESSIONS))
    #: Financial volume of the most recent sessions, for the 21-day average.
    _volumes: deque = field(default_factory=lambda: deque(maxlen=VOLUME_WINDOW))

    def observe(self, day: date, close: Decimal, volume: Decimal) -> None:
        previous = self._closes[-1][1] if self._closes else None
        if previous is not None and previous > 0 and close > 0:
            self._absorb_corporate_action(previous, close)
        self.sessions += 1
        self.last_date = day
        self.last_close = close
        self._closes.append((day, close))
        self._volumes.append(volume)

    def _absorb_corporate_action(self, previous: Decimal, close: Decimal) -> None:
        """Restate history when a price discontinuity is a split, not a move.

        The stored series is *as traded*, so a 1:10 grupamento multiplies every
        quote overnight. Rescaling the earlier closes by the same factor keeps
        the series comparable, which is all a return needs — and it is only
        done when the jump lands on a ratio corporate actions actually use.

        A jump that matches nothing recognisable is left alone and the paper is
        flagged: its window figures are then withheld rather than published
        wrong, because guessing the factor would fabricate a return.
        """
        ratio = close / previous
        if SPLIT_RATIO > ratio > 1 / SPLIT_RATIO:
            return  # ordinary trading, however lively
        if min(previous, close) < MIN_PRICE_FOR_SPLIT:
            # Too cheap for a ratio to mean anything; see MIN_PRICE_FOR_SPLIT.
            self.discontinuous = True
            return
        if self.splits >= MAX_SPLITS:
            self.discontinuous = True
            return

        factor: Decimal | None = None
        for candidate in _SPLIT_FACTORS:
            for direction in (candidate, Decimal(1) / candidate):
                if abs(ratio / direction - 1) <= _SPLIT_TOLERANCE:
                    factor = direction
                    break
            if factor is not None:
                break
        if factor is None:
            self.discontinuous = True
            return
        # Everything before the event is restated onto the new basis.
        self._closes = deque(
            ((day, price * factor) for day, price in self._closes), maxlen=self._closes.maxlen
        )
        self.splits += 1

    # -- derived figures, all over the trailing window ---------------------
    @property
    def traded_days(self) -> int:
        return len(self._closes)

    @property
    def high(self) -> Decimal | None:
        if self.discontinuous:
            return None
        return max((close for _, close in self._closes), default=None)

    @property
    def low(self) -> Decimal | None:
        if self.discontinuous:
            return None
        return min((close for _, close in self._closes), default=None)

    @property
    def avg_volume_21d(self) -> Decimal | None:
        if not self._volumes:
            return None
        return sum(self._volumes, Decimal(0)) / Decimal(len(self._volumes))

    @property
    def change_12m_pct(self) -> Decimal | None:
        """Return over the window, or None when there is not enough of one."""
        if self.discontinuous:
            return None  # the series spans a restatement of unknown size
        if self.traded_days < MIN_SESSIONS_FOR_12M or self.last_close is None:
            return None
        base = self._closes[0][1]
        if base <= 0:
            return None
        return (self.last_close / base - 1) * 100

    @property
    def volatility_pct(self) -> Decimal | None:
        """Annualised standard deviation of daily returns, in percent."""
        if self.discontinuous or self.traded_days < MIN_SESSIONS_FOR_12M:
            return None
        closes = [close for _, close in self._closes]
        returns: list[float] = []
        for previous, current in zip(closes, closes[1:]):
            if previous > 0:
                returns.append(float(current / previous) - 1.0)
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        annual = math.sqrt(variance) * math.sqrt(YEAR_SESSIONS) * 100
        if not math.isfinite(annual):
            return None
        return Decimal(str(round(annual, 6)))


def _price(raw: bytes, span: slice) -> Decimal:
    """A COTAHIST amount: an integer with two implied decimals."""
    return Decimal(int(raw[span])) / 100


def _day(raw: bytes) -> date | None:
    text = raw[_DATA].decode("latin-1")
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def annual_url(year: int) -> str:
    return f"{BASE_URL}/COTAHIST_A{year}.ZIP"


def monthly_url(year: int, month: int) -> str:
    return f"{BASE_URL}/COTAHIST_M{month:02d}{year}.ZIP"


def daily_url(day: date) -> str:
    return f"{BASE_URL}/COTAHIST_D{day.day:02d}{day.month:02d}{day.year}.ZIP"


def reduce_archive(
    raw: bytes,
    into: dict[str, Reduction] | None = None,
    *,
    since: date | None = None,
) -> dict[str, Reduction]:
    """Fold one COTAHIST archive into per-ticker accumulators.

    Call it once per file, oldest first, passing the same ``into`` each time —
    the accumulators are additive, so a year is built from twelve monthly files
    exactly as it would be from one annual file.
    """
    result = into if into is not None else {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise SourceShapeError("COTAHIST: o arquivo baixado não é um zip válido") from exc

    members = archive.infolist()
    if not members:
        raise SourceShapeError("COTAHIST: zip vazio")

    seen_records = 0
    with archive.open(members[0]) as handle:
        for line in handle:
            # Header (00) and trailer (99) records carry no quotes.
            if line[_TIPREG] != b"01":
                continue
            if line[_TPMERC] != CASH_MARKET:
                continue
            seen_records += 1
            day = _day(line)
            if day is None or (since is not None and day < since):
                continue
            ticker = line[_CODNEG].decode("latin-1").strip()
            if not ticker:
                continue
            try:
                close = _price(line, _PREULT)
                volume = _price(line, _VOLTOT)
            except ValueError:
                # A malformed numeric field is one bad line, not a bad file.
                continue
            if close <= 0:
                continue

            slot = result.get(ticker)
            if slot is None:
                codbdi = line[_CODBDI]
                slot = Reduction(
                    ticker=ticker,
                    isin=line[_CODISI].decode("latin-1").strip() or None,
                    kind=_KIND_BY_CODBDI.get(codbdi),
                    codbdi=codbdi.decode("latin-1"),
                    name=line[_NOMRES].decode("latin-1").strip(),
                    especi=line[_ESPECI].decode("latin-1").strip(),
                )
                result[ticker] = slot
            slot.observe(day, close, volume)

    if seen_records == 0:
        raise SourceShapeError(
            "COTAHIST: nenhum registro de mercado à vista no arquivo — "
            "o layout publicado pode ter mudado"
        )
    return result


def fetch_and_reduce(
    urls: list[str],
    into: dict[str, Reduction] | None = None,
    *,
    since: date | None = None,
    on_file=None,
) -> dict[str, Reduction]:
    """Download and reduce each URL in turn, oldest first.

    ``on_file(index, total, url)`` is called before each download so the caller
    can report progress and check for cancellation; returning False from it
    stops the run cleanly at a file boundary.
    """
    result = into if into is not None else {}
    for index, url in enumerate(urls):
        if on_file is not None and on_file(index, len(urls), url) is False:
            break
        try:
            raw = fetch_bytes(url)
        except Exception as exc:  # noqa: BLE001 — a missing month is not fatal
            # B3 has not published this month yet, or the day was a holiday.
            logger.info("cotahist: skipping %s (%s)", url.rsplit("/", 1)[-1], exc)
            continue
        reduce_archive(raw, result, since=since)
    return result
