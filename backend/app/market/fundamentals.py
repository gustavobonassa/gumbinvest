"""Company fundamentals: what the business earns, pays and is worth.

Three sources, because none covers the ground alone:

* **Yahoo ``quoteSummary``** — valuation, margins, revenue and profit for both
  B3 and US listings. It is gated behind a cookie/crumb pair, so a session is
  established once and reused; the crumb is refreshed when Yahoo rejects it.
* **B3** — the *declared* dividend schedule for B3 tickers: amount per share,
  ex-date and payment date, from the registry the numbers are declared to.
  Yahoo publishes a single next date, which is not enough to say "R$ 0,42
  lands on the 14th", and the commercial APIs put this behind a paid plan.
* **brapi.dev** — fallback for the few tickers B3's endpoint does not answer
  for; its free tier covers a handful.

Everything here is best-effort: a missing or refused field is left out rather
than raised, so a page never fails because one provider is having a bad day.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta

from app.core.dates import local_today
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_MODULES = "assetProfile,summaryDetail,financialData,defaultKeyStatistics,calendarEvents,earnings"


def _num(value: object) -> float | None:
    """Yahoo mixes bare numbers with ``{"raw": ...}`` envelopes and nulls."""
    if isinstance(value, dict):
        value = value.get("raw")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def _stamp(value: object) -> str | None:
    """A Yahoo epoch (or an ISO date) as ``yyyy-mm-dd``."""
    if isinstance(value, dict):
        value = value.get("raw") or value.get("fmt")
    if isinstance(value, str):
        return value[:10] or None
    number = _num(value)
    if not number:
        return None
    try:
        return datetime.fromtimestamp(number, tz=UTC).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


class YahooSummary:
    """A crumb-authenticated ``quoteSummary`` client.

    The cookie and crumb are process-wide and cheap to re-establish, so a single
    instance is shared; concurrency is not a concern here because refreshes run
    from one request at a time.
    """

    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._crumb: str | None = None

    def _session(self) -> tuple[httpx.Client, str] | None:
        if self._client is not None and self._crumb:
            return self._client, self._crumb
        client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        )
        try:
            # Any Yahoo host will set the consent cookie the crumb is tied to.
            client.get("https://fc.yahoo.com")
        except Exception:  # noqa: BLE001 — the cookie often arrives on a failed request
            pass
        try:
            response = client.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
            crumb = response.text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("yahoo crumb request failed: %s", exc)
            client.close()
            return None
        if response.status_code != 200 or not crumb or len(crumb) > 32:
            logger.warning("yahoo refused a crumb (status %s)", response.status_code)
            client.close()
            return None
        self._client, self._crumb = client, crumb
        return client, crumb

    def _reset(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._crumb = None

    def fetch(self, symbol: str) -> dict | None:
        """Raw ``quoteSummary`` modules for a symbol, or ``None``."""
        for attempt in (1, 2):  # a stale crumb is worth exactly one retry
            session = self._session()
            if session is None:
                return None
            client, crumb = session
            url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            try:
                response = client.get(url, params={"modules": _MODULES, "crumb": crumb})
            except Exception as exc:  # noqa: BLE001
                logger.warning("yahoo fundamentals failed for %s: %s", symbol, exc)
                return None
            if response.status_code in (401, 403, 429) and attempt == 1:
                self._reset()
                continue
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                logger.info("yahoo fundamentals %s for %s", response.status_code, symbol)
                return None
            try:
                results = (response.json().get("quoteSummary") or {}).get("result") or []
            except ValueError:
                return None
            return results[0] if results else None
        return None


_yahoo = YahooSummary()


#: B3 publishes the declared payment schedule here — for funds *and*, despite
#: the endpoint's name, for listed companies: the ticker root is the key
#: (ITSA -> ITAUSA, TAEE -> TAESA). It carries the payment date, the ex-date
#: and the amount per share, which is exactly the "incoming dividends" answer,
#: from the registry the numbers are declared to, free and without a key.
_B3_SUPPLEMENT = "https://sistemaswebb3-listados.b3.com.br/fundsProxy/fundsCall/GetListedSupplementFunds"


def _b3_call(client: httpx.Client, url: str, params: dict) -> object | None:
    """B3's proxies take one base64'd JSON blob as the path segment."""
    blob = base64.b64encode(json.dumps(params).encode()).decode()
    try:
        response = client.get(f"{url}/{blob}")
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("b3 call failed (%s): %s", url.rsplit("/", 1)[-1], exc)
        return None
    # Some fund endpoints answer with a JSON *string* holding the real payload.
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return payload


def _br_date(value: object) -> str | None:
    """``dd/mm/yyyy`` (B3's format) as ``yyyy-mm-dd``.

    B3 files a payment whose date the company has not fixed yet as 31/12/9999.
    That is "a definir", not a date in the year 9999, so it comes back empty.
    """
    if not isinstance(value, str) or "/" not in value:
        return _stamp(value)
    day, month, year = (part.strip() for part in value.split("/")[:3])
    if len(year) != 4 or year == "9999":
        return None
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _br_number(value: object) -> float | None:
    """``"0,18633271466"`` — a Brazilian decimal — as a float."""
    if isinstance(value, str):
        value = value.replace(".", "").replace(",", ".")
    return _num(value)


#: Share class per ticker suffix, as it appears inside the ISIN. B3 files one
#: record per class and the classes are not paid the same — Bradesco's PN gets
#: 10 % more than its ON — so a BBDC3 holder must not be shown BBDC4's amount.
_ISIN_CLASS = {"3": "ACNOR", "4": "ACNPR", "5": "ACNPA", "6": "ACNPB"}


def _b3_dividends(ticker: str) -> list[dict]:
    """The declared dividend schedule for a B3 ticker, newest first.

    One call: the supplement endpoint answers for both funds and companies. The
    code it echoes back is checked against the one asked for, because a wrong
    match here would attribute another company's payments to this asset.
    """
    code = "".join(char for char in ticker if char.isalpha())
    if not code:
        return []
    with httpx.Client(timeout=settings.request_timeout, headers={"User-Agent": _UA}) as client:
        payload = _b3_call(client, _B3_SUPPLEMENT, {"identifierFund": code, "typeFund": 7})
    if not isinstance(payload, dict):
        return []
    if (payload.get("code") or code).upper() != code.upper():
        return []

    suffix = ticker[len(code) :]
    wanted_class = _ISIN_CLASS.get(suffix)  # units and fund cotas: no filter

    rows: list[dict] = []
    seen: set[tuple] = set()
    for item in payload.get("cashDividends") or []:
        isin = str(item.get("isinCode") or item.get("assetIssued") or "")
        if wanted_class and isin and wanted_class not in isin:
            continue
        rate = _br_number(item.get("rate"))
        if rate is None:
            continue
        raw_payment = item.get("paymentDate")
        row = {
            "payment_date": _br_date(raw_payment),
            "approved_on": _br_date(item.get("approvedOn")),
            "record_date": _br_date(item.get("lastDatePrior")),
            "label": item.get("label") or item.get("relatedTo"),
            "period": item.get("relatedTo") or None,
            "rate": rate,
            # Declared, amount known, date still to be announced by the company.
            "date_pending": isinstance(raw_payment, str) and "9999" in raw_payment,
        }
        # A company with two share classes files the same payment once per ISIN;
        # to a holder of one of them it is a single dividend.
        key = (row["payment_date"], row["record_date"], row["label"], round(rate, 8))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    rows.sort(key=lambda row: row["payment_date"] or row["record_date"] or "", reverse=True)
    return rows


def _brapi_dividends(ticker: str) -> list[dict]:
    """Announced cash dividends for a B3 ticker, newest first.

    The free tier serves this without a token; a token only raises the rate
    limit, so the call is made either way.
    """
    params: dict[str, str] = {"dividends": "true"}
    if settings.brapi_token:
        params["token"] = settings.brapi_token
    url = f"{settings.brapi_base_url.rstrip('/')}/quote/{ticker}"
    try:
        with httpx.Client(timeout=settings.request_timeout, headers={"User-Agent": _UA}) as client:
            response = client.get(url, params=params)
            if response.status_code != 200:
                return []
            results = response.json().get("results") or []
    except Exception as exc:  # noqa: BLE001
        logger.info("brapi dividends failed for %s: %s", ticker, exc)
        return []
    if not results:
        return []

    payments: list[dict] = []
    cash = ((results[0].get("dividendsData") or {}).get("cashDividends")) or []
    for item in cash:
        payment = _stamp(item.get("paymentDate"))
        rate = _num(item.get("rate"))
        if rate is None:
            continue
        payments.append(
            {
                "payment_date": payment,
                "approved_on": _stamp(item.get("approvedOn")),
                "record_date": _stamp(item.get("lastDatePrior")),
                "label": item.get("label") or item.get("relatedTo"),
                "rate": rate,
            }
        )
    payments.sort(key=lambda row: row["payment_date"] or "", reverse=True)
    return payments


def fetch(symbol: str, ticker: str, is_brazilian: bool) -> dict:
    """Everything known about the company behind a symbol.

    Returns a flat, provider-neutral dict. Values absent from the source are
    simply missing keys, so the UI can tell "zero" from "not published".
    """
    raw = _yahoo.fetch(symbol) or {}
    profile = raw.get("assetProfile") or {}
    summary = raw.get("summaryDetail") or {}
    financial = raw.get("financialData") or {}
    stats = raw.get("defaultKeyStatistics") or {}
    calendar = raw.get("calendarEvents") or {}

    # Yahoo reports the dividend yield as a fraction for some listings and as a
    # percentage for others; anything under 1 is a fraction. (A 100 %+ yield is
    # not a thing outside a data error, so the cut is safe.)
    yield_raw = _num(summary.get("dividendYield")) or _num(summary.get("trailingAnnualDividendYield"))
    dividend_yield = None if yield_raw is None else (yield_raw * 100 if yield_raw < 1 else yield_raw)

    today = local_today().isoformat()
    earnings = calendar.get("earnings") or {}
    earnings_dates = [
        stamp
        for stamp in (_stamp(item) for item in (earnings.get("earningsDate") or []))
        if stamp and stamp >= today
    ]

    # Yahoo keeps publishing the *last* payment under `dividendDate` long after
    # it happened, and a past date labelled "próximo pagamento" is a lie.
    next_dividend = _stamp(calendar.get("dividendDate") or summary.get("dividendDate"))
    if next_dividend and next_dividend < today:
        next_dividend = None

    data: dict[str, object] = {
        "symbol": symbol,
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "website": profile.get("website"),
        "employees": _num(profile.get("fullTimeEmployees")),
        "summary": profile.get("longBusinessSummary"),
        "currency": financial.get("financialCurrency") or summary.get("currency"),
        # Valuation
        "market_cap": _num(summary.get("marketCap")) or _num(stats.get("marketCap")),
        "pe_trailing": _num(summary.get("trailingPE")),
        "pe_forward": _num(summary.get("forwardPE")),
        "price_to_book": _num(stats.get("priceToBook")),
        "book_value": _num(stats.get("bookValue")),
        "eps_trailing": _num(stats.get("trailingEps")),
        "beta": _num(summary.get("beta")) or _num(stats.get("beta")),
        "fifty_two_week_low": _num(summary.get("fiftyTwoWeekLow")),
        "fifty_two_week_high": _num(summary.get("fiftyTwoWeekHigh")),
        # Business
        "revenue": _num(financial.get("totalRevenue")),
        "gross_profit": _num(financial.get("grossProfits")),
        "ebitda": _num(financial.get("ebitda")),
        "net_income": _num(stats.get("netIncomeToCommon")),
        "profit_margin": _pct(financial.get("profitMargins")),
        "operating_margin": _pct(financial.get("operatingMargins")),
        "return_on_equity": _pct(financial.get("returnOnEquity")),
        "revenue_growth": _pct(financial.get("revenueGrowth")),
        "earnings_growth": _pct(financial.get("earningsGrowth")),
        "debt_to_equity": _num(financial.get("debtToEquity")),
        "free_cashflow": _num(financial.get("freeCashflow")),
        # Income
        "dividend_yield": dividend_yield,
        "dividend_rate": _num(summary.get("dividendRate"))
        or _num(summary.get("trailingAnnualDividendRate")),
        # A payer always distributes *something*; Yahoo reports 0 when it simply
        # has no figure, and "payout 0,0 %" beside a 8 % yield is a contradiction.
        "payout_ratio": _pct(summary.get("payoutRatio")) or None,
        "ex_dividend_date": _stamp(summary.get("exDividendDate")),
        "next_dividend_date": next_dividend,
        "earnings_dates": earnings_dates,
        # Analysts
        "target_mean_price": _num(financial.get("targetMeanPrice")),
        "recommendation": financial.get("recommendationKey"),
        "analyst_count": _num(financial.get("numberOfAnalystOpinions")),
    }

    # Yahoo's `earnings` module carries ~4 years of annual revenue and net
    # income — enough for a per-year chart, and free on the same call.
    yearly = ((raw.get("earnings") or {}).get("financialsChart") or {}).get("yearly") or []
    yearly_financials = [
        {"year": int(year), "revenue": _num(item.get("revenue")), "earnings": _num(item.get("earnings"))}
        for item in yearly
        for year in [_num(item.get("date"))]
        if year
    ]
    if yearly_financials:
        data["yearly_financials"] = yearly_financials

    if is_brazilian:
        # B3 first — it is the registry the payments are declared to. brapi is
        # kept as a fallback because its free tier covers a few tickers B3's
        # fund proxy does not answer for.
        payments = _b3_dividends(ticker) or _brapi_dividends(ticker)

        # The full declared history, folded per year: with the stored daily
        # closes the UI turns this into dividend-per-share and DY-per-year
        # charts. Independent of the holding period — it is the company's
        # payout record, not the owner's.
        by_year: dict[str, dict] = {}
        for payment in payments:
            when = payment.get("payment_date") or payment.get("record_date")
            if not when:
                continue
            entry = by_year.setdefault(when[:4], {"year": int(when[:4]), "total_rate": 0.0, "payments": 0})
            entry["total_rate"] += payment["rate"]
            entry["payments"] += 1
        if by_year:
            data["dividends_by_year"] = sorted(by_year.values(), key=lambda entry: entry["year"])
        # Declared but not yet paid is the question this answers, so a payment
        # whose date the company has not fixed stays in the upcoming list.
        def upcoming(row: dict) -> bool:
            if row.get("date_pending"):
                return True
            return (row.get("payment_date") or row.get("record_date") or "") >= today

        data["announced_dividends"] = [p for p in payments if upcoming(p)][:8]
        data["recent_dividends"] = [p for p in payments if not upcoming(p)][:8]

    data["fetched_at"] = datetime.now(UTC).isoformat()
    data["has_data"] = any(
        data.get(key) is not None
        for key in ("market_cap", "revenue", "dividend_yield", "pe_trailing", "net_income")
    ) or bool(data.get("announced_dividends"))
    return {key: value for key, value in data.items() if value is not None}


def _pct(value: object) -> float | None:
    """A Yahoo ratio (0.184) as the percentage every screen shows (18.4)."""
    number = _num(value)
    return None if number is None else number * 100


def to_decimal(value: object) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


#: Families whose payments are declared to B3's registry — the only assets the
#: "upcoming dividends" question can be answered for. Everything offshore,
#: crypto or synthetic (CDB, Tesouro) has no declared schedule here.
DECLARABLE_KINDS = frozenset({"STOCK", "FII", "ETF", "BDR", "UNIT"})

#: Fundamentals move quarterly; a half-day-old copy is a current copy.
FUNDAMENTALS_TTL = timedelta(hours=12)


def refresh_held_fundamentals(db, portfolio_id: int, only_stale: bool = True) -> dict:
    """Fetch and store fundamentals for every held B3 asset.

    This is what keeps the dividend calendar current without anyone opening
    each asset page: the beat schedule runs it daily, and the calendar's
    "atualizar" button runs it on demand. Per-asset failures are logged and
    skipped — one delisted ticker must not empty the whole calendar.
    """
    from app.db.models import AssetFundamentals
    from app.market.service import resolve_market_symbol
    from app.portfolio.service import PortfolioService

    service = PortfolioService(db, portfolio_id)
    now = datetime.now(UTC)
    updated = fresh = failed = 0
    for ap in service.asset_positions():
        asset = ap.asset
        if asset.kind not in DECLARABLE_KINDS or asset.price_manual:
            continue
        if (asset.currency or "BRL").upper() != "BRL":
            continue
        cached = db.get(AssetFundamentals, asset.id)
        is_fresh = (
            cached is not None
            and cached.fetched_at is not None
            and cached.fetched_at.replace(tzinfo=cached.fetched_at.tzinfo or UTC) > now - FUNDAMENTALS_TTL
        )
        if is_fresh and only_stale:
            fresh += 1
            continue
        try:
            data = fetch(resolve_market_symbol(asset), asset.ticker, True)
        except Exception:  # noqa: BLE001 — one bad ticker must not stop the sweep
            logger.exception("fundamentals refresh failed for %s", asset.ticker)
            failed += 1
            continue
        if not data.get("has_data"):
            failed += 1
            continue
        db.merge(AssetFundamentals(asset_id=asset.id, data=data, source="yahoo", fetched_at=datetime.now(UTC)))
        updated += 1
    db.commit()
    return {"updated": updated, "fresh": fresh, "failed": failed}
