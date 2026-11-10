"""Binance through its API: collect the account's history, import it, check the balances.

Where the B3 collector has to drive a browser, Binance publishes a REST API, so
this pipeline only needs a read-only API key. The API has no endpoint for the
*Transaction History* ledger, so the history is assembled from the endpoints
that exist:

* spot fills (``/api/v3/myTrades``, every symbol the account could have traded);
* Convert orders;
* crypto deposits and withdrawals;
* crypto bought with fiat (card / PIX);
* Simple Earn subscriptions, redemptions and rewards;
* distributions (airdrops, staking payouts) and dust swept into BNB.

Fiat deposits and withdrawals (BRL in and out) are not fetched: reais are
cash, which the portfolio never holds as a position, and that endpoint costs a
quarter of the account's per-minute allowance per call.

Everything is written to a CSV in the format of
:mod:`app.importer.crypto.binance_api` and imported from it, so the run leaves
the same audit trail a manual upload does.

Then the result is checked against the balances Binance reports now (spot,
funding and Simple Earn). The import cannot invent a movement the API did not
report, and some of what Binance does is not reported anywhere (a pair delisted
years ago, a product this collector does not know). So every coin whose
computed position disagrees with the exchange is listed in the run log. That
check replaces the printed totals the statement parsers validate against: a
gap shows up there instead of quietly shifting a position.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.dates import local_today
from app.importer.crypto import binance_api as fmt
from app.importer.crypto import symbols as coins
from app.pipelines.base import Pipeline, PipelineError, PipelineSpec, register
from app.services.secrets import read_secret

BASE_URL = "https://api.binance.com"
#: Tests swap in an ``httpx.MockTransport``; ``None`` is the real network.
_TRANSPORT: httpx.BaseTransport | None = None

#: Where a full collection starts. Binance opened in July 2017; windows before
#: the account's first deposit are skipped once that date is known.
FULL_HISTORY_START = datetime(2017, 7, 1, tzinfo=UTC)
#: Simple Earn's history endpoints serve nothing older than the product.
EARN_HISTORY_START = datetime(2020, 1, 1, tzinfo=UTC)
#: An incremental run re-reads this much before the last success. Dedup makes
#: the overlap free, and it catches records Binance finalises late (a deposit
#: that sat waiting for confirmations).
OVERLAP = timedelta(days=7)

#: Where this pipeline keeps its own state in ``app_settings``. The prefix keeps
#: it out of the preferences the settings endpoint returns.
STATE_KEY = "pipeline_state:binance"

#: The zone Binance stamps into an export's filename:
#: ``Binance-Histórico-de-Transações-202608012000(UTC--3)-part1-of1.csv``.
_TZ_IN_FILENAME = re.compile(r"\(UTC([+-]*)(\d{1,2})(?::?(\d{2}))?\)", re.IGNORECASE)

#: How a preview names each kind of movement it would add.
_PREVIEW_LABELS = {
    "BUY": "compras",
    "SELL": "vendas",
    "FEE": "taxas",
    "REWARD": "recompensas",
    "TRANSFER_IN": "entradas",
    "TRANSFER_OUT": "saídas",
    "DERIVATIVE": "futuros",
}

#: Quote assets tried against every coin the account has touched. A pair is
#: only queried when Binance lists it, so a long list costs nothing extra.
COMMON_QUOTES = ("USDT", "BRL", "BTC", "ETH", "BNB", "FDUSD", "USDC", "BUSD", "EUR", "TRY")

#: Minimum seconds between calls to one endpoint, derived from its documented
#: weight so a full collection stays at about 80 % of each limit (6 000/min per
#: IP for /api, 12 000/min per IP per /sapi endpoint, 180 000/min per account
#: for the UID-weighted ones). Waiting is cheap; a 418 ban is not.
_INTERVALS = {
    "/api/v3/myTrades": 0.25,
    "/sapi/v1/convert/tradeFlow": 1.3,
    "/sapi/v1/capital/withdraw/history": 7.5,
    "/sapi/v1/simple-earn/": 1.0,
}
_DEFAULT_INTERVAL = 0.1

#: Distribution entries that describe Simple Earn interest, which is already
#: collected from the Simple Earn endpoints. The docs do not say whether the
#: distribution history repeats it, so anything worded like it is dropped here
#: and the balance check reports it if that was wrong.
_EARN_WORDING = re.compile(r"simple earn|savings|flexible|locked", re.IGNORECASE)

_RELATIVE_TOLERANCE = Decimal("0.005")
_DUST = Decimal("0.00000001")

#: Error codes that mean the key itself is the problem.
_KEY_ERRORS = {-2014, -2015, -1022}


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _zone_label(zone: tzinfo) -> str:
    if isinstance(zone, ZoneInfo):
        return zone.key
    offset = zone.utcoffset(None) or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _zone_from_label(label: str) -> tzinfo:
    match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", label)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    return ZoneInfo(label)


def zone_from_filenames(names: list[str]) -> tzinfo | None:
    """The zone of the newest Binance export that names one, else ``None``.

    Binance writes ``(UTC--3)`` for three hours behind and ``(UTC-+8)`` (or
    ``(UTC+8)``) for ahead: the sign next to the number is the one that counts.
    """
    for name in names:
        match = _TZ_IN_FILENAME.search(name or "")
        if match:
            sign = -1 if match.group(1).endswith("-") else 1
            return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0)))
    return None


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _windows(start: datetime, end: datetime, days: int) -> Iterator[tuple[int, int]]:
    """``[start, end)`` in consecutive windows no longer than ``days``."""
    cursor = start
    step = timedelta(days=days)
    while cursor < end:
        upper = min(cursor + step, end)
        yield _ms(cursor), _ms(upper) - 1
        cursor = upper


class _Client:
    """Signed GETs with the pacing and error translation every call needs."""

    def __init__(self, ctx, key: str, secret: str) -> None:
        self._ctx = ctx
        self._secret = secret.encode()
        self._http = httpx.Client(
            base_url=BASE_URL, timeout=30, headers={"X-MBX-APIKEY": key}, transport=_TRANSPORT
        )
        self._offset_ms = 0
        self._last_call: dict[str, float] = {}

    def close(self) -> None:
        self._http.close()

    def sync_clock(self) -> None:
        """Measure the gap to Binance's clock: a signed call more than a second
        ahead of the server is refused, and PCs drift."""
        server = self.call("GET", "/api/v3/time", signed=False)["serverTime"]
        self._offset_ms = int(server) - int(time.time() * 1000)

    def call(self, method: str, path: str, params: dict | None = None, *, signed: bool = True):
        self._ctx.check_cancel()
        self._pace(path)
        query = dict(params or {})
        if signed:
            query["timestamp"] = int(time.time() * 1000) + self._offset_ms
            query["recvWindow"] = 30_000
            encoded = urlencode(query)
            signature = hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()
            encoded = f"{encoded}&signature={signature}"
        else:
            encoded = urlencode(query)

        for attempt in range(4):
            try:
                response = self._http.request(method, f"{path}?{encoded}" if encoded else path)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise PipelineError(
                        "Não foi possível falar com a Binance. Verifique a conexão e tente de novo."
                    ) from exc
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "30"))
                self._ctx.log(f"A Binance pediu uma pausa de {wait}s (limite de requisições).", level="warning")
                time.sleep(min(wait, 120))
                continue
            return self._parse(response)
        raise PipelineError("A Binance continuou recusando por excesso de requisições. Tente mais tarde.")

    def _pace(self, path: str) -> None:
        interval = next((v for prefix, v in _INTERVALS.items() if path.startswith(prefix)), _DEFAULT_INTERVAL)
        elapsed = time.monotonic() - self._last_call.get(path, 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_call[path] = time.monotonic()

    @staticmethod
    def _parse(response: httpx.Response):
        if response.status_code == 418:
            raise PipelineError(
                "A Binance bloqueou temporariamente este endereço por excesso de requisições. "
                "Espere algumas horas antes de tentar de novo."
            )
        if response.status_code == 451:
            raise PipelineError("A Binance não atende a partir da localização desta conexão.")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            message = body.get("msg") if isinstance(body, dict) else response.text[:200]
            if code in _KEY_ERRORS:
                raise PipelineError(
                    "A Binance recusou a chave de API. Confira a API Key e a Secret Key e se a chave "
                    "tem a permissão de leitura (e, se tiver restrição de IP, se este IP está liberado)."
                )
            if code == -1021:
                raise PipelineError("O relógio deste computador está fora de sincronia com a Binance.")
            raise PipelineError(f"A Binance respondeu com erro ({code}): {message}")
        return body


class BinancePipeline(Pipeline):
    spec = PipelineSpec(
        key="binance",
        name="Binance",
        description=(
            "Busca pela API da Binance suas negociações, conversões, depósitos, saques, "
            "Simple Earn e distribuições, importa as novidades e confere o resultado com os "
            "saldos da corretora. Crie a chave em Binance → Perfil → Gerenciamento de API, "
            "deixando marcada só a permissão de leitura: com ela não é possível negociar nem sacar."
        ),
        credentials=(
            ("binance_api_key", "API Key"),
            ("binance_api_secret", "Secret Key"),
        ),
        supports_preview=True,
    )

    _exchange_info: list[dict] | None = None

    def run(self, ctx) -> dict:
        self._exchange_info = None
        client = _Client(ctx, read_secret("binance_api_key").strip(), read_secret("binance_api_secret").strip())
        try:
            client.sync_clock()
            self._check_permissions(client, ctx)
            now = datetime.now(UTC)
            since = self._since(ctx)
            full = since == FULL_HISTORY_START
            ctx.log(
                "Coletando todo o histórico da conta — a primeira vez leva alguns minutos."
                if full
                else f"Coletando movimentações desde {since.strftime('%d/%m/%Y')}."
            )

            zone = self._account_zone(ctx, persist=not ctx.options.get("preview"))
            collected = _Collection(zone)
            balances = self._balances(client, ctx)
            collected.touch(balances)
            collected.touch(self._known_coins())

            self._deposits(client, ctx, collected, since, now)
            self._fiat_purchases(client, ctx, collected, since, now)
            self._converts(client, ctx, collected, since, now)
            queried: set[str] = set()
            self._spot_trades(client, ctx, collected, queried, every_pair=full)

            withdrawals_from = since
            if full and collected.first_activity:
                # A withdrawal needs a coin on the account, and by now every
                # way one arrives (deposit, fiat purchase, Convert, trade) has
                # been read. The years before that are empty, and this is the
                # endpoint that costs the most to walk.
                withdrawals_from = max(since, collected.first_activity - timedelta(days=1))
            self._withdrawals(client, ctx, collected, withdrawals_from, now)
            self._earn(client, ctx, collected, max(since, EARN_HISTORY_START), now)
            self._distributions(client, ctx, collected, since, now)
            self._dust(client, ctx, collected)
            # Coins first seen in Earn, distributions or dust may have pairs of
            # their own that the first pass had no reason to query.
            self._spot_trades(client, ctx, collected, queried, every_pair=False)
        finally:
            client.close()

        preview = bool(ctx.options.get("preview"))
        if not collected.rows:
            ctx.log("Nenhuma movimentação encontrada no período.")
            if preview:
                return {"preview": True, "would_import": 0, "would_skip": 0, "inside_covered": 0}
            mismatches = self._compare(ctx, balances)
            return {"rows_imported": 0, "rows_duplicate": 0, "rows_failed": 0, "balance_mismatches": len(mismatches)}

        collected.rows.sort(key=lambda row: (row["Time"], row["Source"], row["Ref"]))
        payload = fmt.write(collected.rows)
        filename = f"binance-api-{local_today().strftime('%Y%m%d')}-{ctx.run_id}.csv"
        if preview:
            # Nothing touches the disk either: a file left in the auto-import
            # folder would be imported for real on the next start.
            return self._preview(ctx, payload, filename, len(collected.rows))
        (ctx.downloads_dir() / filename).write_bytes(payload)
        ctx.log(f"{len(collected.rows)} registros coletados; importando.")
        result = ctx.import_file(payload, filename)
        ctx.log(
            f"Importação concluída: {result.rows_imported} novas, "
            f"{result.rows_duplicate} já conhecidas, {result.rows_failed} com erro."
        )
        if result.rows_failed == 0:
            # The ledger now holds the movements; keep the file only when a row
            # failed, so there is something to inspect.
            try:
                (ctx.downloads_dir() / filename).unlink(missing_ok=True)
            except OSError:
                pass

        mismatches = self._compare(ctx, balances)
        return {
            "file": filename,
            "rows_imported": result.rows_imported,
            "rows_duplicate": result.rows_duplicate,
            "rows_failed": result.rows_failed,
            "balance_mismatches": len(mismatches),
        }

    def _preview(self, ctx, payload: bytes, filename: str, collected: int) -> dict:
        """Run the import with nothing saved, and say what it would add."""
        ctx.log(f"{collected} registros coletados; simulando a importação (nada é gravado).")
        result = ctx.preview_file(payload, filename)
        rows = result.summary.get("preview", [])
        covered_until = _exports_cover_until()
        inside = [row for row in rows if covered_until and row["date"] <= covered_until]

        kinds: dict[str, int] = {}
        for row in rows:
            label = _PREVIEW_LABELS.get(row["op_type"], row["op_type"].lower())
            kinds[label] = kinds.get(label, 0) + 1
        breakdown = ", ".join(f"{count} {label}" for label, count in sorted(kinds.items(), key=lambda kv: -kv[1]))
        ctx.log(
            f"Simulação: {len(rows)} movimentações seriam adicionadas"
            + (f" ({breakdown})" if breakdown else "")
            + f"; {result.rows_duplicate} já estão na carteira e seriam ignoradas."
        )
        if covered_until is None:
            ctx.log("Não há exportações da Binance importadas para comparar períodos.")
        elif inside:
            sample = "; ".join(
                f"{row['date']} {row['ticker']} {row['quantity']} ({row['movement']})" for row in inside[:12]
            )
            more = f" e mais {len(inside) - 12}" if len(inside) > 12 else ""
            ctx.log(
                f"Atenção: {len(inside)} delas têm data até {_br_date(covered_until)}, período que suas "
                f"exportações já cobrem — podem ser duplicatas. {sample}{more}.",
                level="warning",
            )
        else:
            ctx.log(
                f"Nenhuma delas cai no período que suas exportações já cobrem (até "
                f"{_br_date(covered_until)}): são todas posteriores, ou seja, movimentações novas."
            )
        ctx.log("Nada foi gravado. O confronto com os saldos da Binance acontece na execução de verdade.")
        return {
            "preview": True,
            "would_import": len(rows),
            "would_skip": result.rows_duplicate,
            "inside_covered": len(inside),
            "covered_until": covered_until,
            "rows_failed": result.rows_failed,
        }

    @staticmethod
    def _account_zone(ctx, *, persist: bool) -> tzinfo:
        """The zone this pipeline dates its records in, chosen once and kept.

        The reconciliation with uploaded exports matches day by day, so the API
        has to date things the way those exports do. The newest Binance export
        on file decides (its filename names the zone). An export with no zone in
        its name is the English one, whose column is ``UTC_Time``. With no
        exports at all, the app's own zone. Kept after the first real run:
        changing it later would re-date movements already imported and break
        their dedup keys.
        """
        from sqlalchemy import select

        from app.db.models import AppSetting, ImportBatch
        from app.db.session import session_scope

        with session_scope() as db:
            row = db.get(AppSetting, STATE_KEY)
            state = dict(row.value or {}) if row is not None and isinstance(row.value, dict) else {}
            if state.get("timezone"):
                return _zone_from_label(state["timezone"])

            names = db.scalars(
                select(ImportBatch.filename)
                .where(
                    ImportBatch.source_kind == "CRYPTO",
                    ImportBatch.broker_name == fmt.EXCHANGE,
                    ImportBatch.source_format != fmt.API_FORMAT,
                )
                .order_by(ImportBatch.id.desc())
            ).all()
            if names:
                zone = zone_from_filenames(list(names)) or timezone.utc
                reason = "das suas exportações da Binance"
            else:
                zone = ZoneInfo(settings.timezone)
                reason = "do aplicativo (não há exportações da Binance para seguir)"
            ctx.log(f"Datas no fuso {_zone_label(zone)}, o mesmo {reason}.")
            if persist:
                db.merge(AppSetting(key=STATE_KEY, value={**state, "timezone": _zone_label(zone)}))
            return zone

    # -- setup ----------------------------------------------------------------
    def _since(self, ctx) -> datetime:
        if ctx.options.get("full_history"):
            return FULL_HISTORY_START
        last = ctx.last_success_at()
        if last is None:
            # The first run brings everything: unlike a browser walk, the API
            # only costs time, and a partial first load would leave every coin
            # held from before it looking like it was sold from nowhere.
            return FULL_HISTORY_START
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        start = last - OVERLAP
        # Whole days only: the importer numbers identical movements within a
        # day, and a window starting mid-day would renumber them.
        return start.replace(hour=0, minute=0, second=0, microsecond=0)

    def _check_permissions(self, client: _Client, ctx) -> None:
        rights = client.call("GET", "/sapi/v1/account/apiRestrictions")
        if not rights.get("enableReading", True):
            raise PipelineError("A chave de API não tem a permissão de leitura. Ative “Enable Reading” na Binance.")
        if rights.get("enableWithdrawals") or rights.get("enableSpotAndMarginTrading"):
            ctx.log(
                "Esta chave também pode negociar ou sacar. O GumbInvest só lê, mas é mais seguro "
                "usar uma chave apenas de leitura.",
                level="warning",
            )

    @staticmethod
    def _known_coins() -> set[str]:
        """Coins already in the portfolio, from earlier uploads included: their
        pairs are worth querying even when the balance is zero today."""
        from sqlalchemy import select

        from app.db.models import Asset
        from app.db.session import session_scope

        with session_scope() as db:
            tickers = db.scalars(select(Asset.ticker).where(Asset.kind.in_(coins.CRYPTO_KINDS))).all()
        return {coins.asset_symbol(ticker) for ticker in tickers}

    # -- what the exchange holds now -------------------------------------------
    def _balances(self, client: _Client, ctx) -> dict[str, Decimal]:
        ctx.log("Lendo os saldos atuais.")
        totals: dict[str, Decimal] = {}

        def add(asset: str, amount: Decimal) -> None:
            if amount:
                totals[asset.upper()] = totals.get(asset.upper(), Decimal(0)) + amount

        flexible = self._pages(client, "/sapi/v1/simple-earn/flexible/position", {})
        for row in flexible:
            add(row["asset"], _dec(row.get("totalAmount")))
        for row in self._pages(client, "/sapi/v1/simple-earn/locked/position", {}):
            add(row["asset"], _dec(row.get("amount")))
        in_flexible = {row["asset"].upper() for row in flexible}

        account = client.call("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        for balance in account.get("balances", []):
            asset = balance["asset"].upper()
            # Older accounts show flexible Earn as an "LD" token in spot
            # (LDUSDT). The position above already counts it.
            if asset.startswith("LD") and asset[2:] in in_flexible:
                continue
            add(asset, _dec(balance.get("free")) + _dec(balance.get("locked")))

        for balance in client.call("POST", "/sapi/v1/asset/get-funding-asset") or []:
            add(
                balance["asset"],
                _dec(balance.get("free")) + _dec(balance.get("locked")) + _dec(balance.get("freeze")),
            )
        return totals

    # -- history ---------------------------------------------------------------
    def _deposits(self, client, ctx, collected, since, now) -> None:
        ctx.log("Depósitos de cripto…")
        for start, end in _windows(since, now, 89):
            for row in self._offset_pages(client, "/sapi/v1/capital/deposit/hisrec", {"startTime": start, "endTime": end}):
                # 1 = success; 6 = credited, only withdrawal-locked. Both are on
                # the balance.
                if int(row.get("status", -1)) not in (1, 6):
                    continue
                collected.add("deposit", row["insertTime"], "Deposit", row["coin"], _dec(row["amount"]), ref=f"dep:{row.get('id') or row.get('txId')}")

    def _withdrawals(self, client, ctx, collected, since, now) -> None:
        ctx.log("Saques de cripto (a Binance limita esta consulta, então ela é mais lenta)…")
        for start, end in _windows(since, now, 89):
            for row in self._offset_pages(client, "/sapi/v1/capital/withdraw/history", {"startTime": start, "endTime": end}):
                if int(row.get("status", -1)) != 6:
                    continue
                # What left the balance is the amount sent plus the network fee.
                total = _dec(row["amount"]) + _dec(row.get("transactionFee"))
                applied = datetime.strptime(row["applyTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                collected.add("withdraw", _ms(applied), "Withdraw", row["coin"], -total, ref=f"wd:{row['id']}")

    def _fiat_purchases(self, client, ctx, collected, since, now) -> None:
        ctx.log("Compras de cripto com reais (cartão/PIX)…")
        for start, end in _windows(since, now, 29):
            page = 1
            while True:
                body = client.call(
                    "GET",
                    "/sapi/v1/fiat/payments",
                    {"transactionType": 0, "beginTime": start, "endTime": end, "page": page, "rows": 500},
                )
                data = body.get("data") or []
                for row in data:
                    if row.get("status") != "Completed":
                        continue
                    # Read the way the ledger reads it ("Buy Crypto With
                    # Fiat"), so an uploaded ledger and this run reconcile.
                    collected.add(
                        "fiat-payments", row["createTime"], "Buy Crypto With Fiat",
                        row["cryptoCurrency"], _dec(row["obtainAmount"]), ref=f"fiat:{row['orderNo']}",
                    )
                if len(data) < 500 or page * 500 >= int(body.get("total") or 0):
                    break
                page += 1

    def _converts(self, client, ctx, collected, since, now) -> None:
        ctx.log("Conversões (Binance Convert)…")
        for start, end in _windows(since, now, 29):
            for row in self._convert_window(client, start, end):
                if row.get("orderStatus") != "SUCCESS":
                    continue
                collected.add(
                    "convert", row["createTime"], fmt.CONVERT_OPERATION, row["toAsset"], _dec(row["toAmount"]),
                    ref=f"cv:{row['orderId']}", Quote_Coin=row["fromAsset"], Quote_Qty=_dec(row["fromAmount"]),
                )
                collected.touch({row["toAsset"], row["fromAsset"]})

    def _convert_window(self, client, start: int, end: int) -> list[dict]:
        body = client.call("GET", "/sapi/v1/convert/tradeFlow", {"startTime": start, "endTime": end, "limit": 1000})
        rows = body.get("list") or []
        if body.get("moreData") and end - start > 60_000:
            # No cursor: halve the window until each half fits.
            middle = (start + end) // 2
            return self._convert_window(client, start, middle) + self._convert_window(client, middle + 1, end)
        return rows

    def _earn(self, client, ctx, collected, since, now) -> None:
        ctx.log("Simple Earn (aplicações, resgates e rendimentos)…")
        if since >= now:
            return
        # Flexible rewards are split by kind and the endpoint refuses a call
        # without `type` (-1102), whatever the newer docs say about a default
        # of ALL. The three kinds are separate credits, so asking for each one
        # counts every reward once.
        flexible_rewards = tuple(
            ("flexible/history/rewardsRecord", {"type": kind}, "Simple Earn Flexible Interest", 1, "rewards", None)
            for kind in ("BONUS", "REALTIME", "REWARDS")
        )
        endpoints = (
            ("flexible/history/subscriptionRecord", {}, "Simple Earn Flexible Subscription", -1, "amount", ("SUCCESS",)),
            ("flexible/history/redemptionRecord", {}, "Simple Earn Flexible Redemption", 1, "amount", ("PAID",)),
            *flexible_rewards,
            ("locked/history/subscriptionRecord", {}, "Simple Earn Locked Subscription", -1, "amount", ("SUCCESS",)),
            ("locked/history/redemptionRecord", {}, "Simple Earn Locked Redemption", 1, "amount", ("PAID",)),
            ("locked/history/rewardsRecord", {}, "Simple Earn Locked Rewards", 1, "amount", None),
        )
        for path, extra, operation, sign, field, ok_statuses in endpoints:
            for start, end in _windows(since, now, 29):
                params = {**extra, "startTime": start, "endTime": end}
                for row in self._pages(client, f"/sapi/v1/simple-earn/{path}", params):
                    if ok_statuses and row.get("status") not in ok_statuses:
                        continue
                    amount = _dec(row.get(field))
                    ref = (
                        row.get("purchaseId") or row.get("redeemId") or row.get("positionId")
                        or extra.get("type") or ""
                    )
                    collected.add("earn", row["time"], operation, row["asset"], sign * amount, ref=f"{path.split('/')[0]}:{ref}")

    def _distributions(self, client, ctx, collected, since, now) -> None:
        ctx.log("Distribuições, airdrops e recompensas de staking…")
        skipped = 0
        for start, end in _windows(since, now, 179):
            upper = end
            while True:
                body = client.call(
                    "GET", "/sapi/v1/asset/assetDividend", {"startTime": start, "endTime": upper, "limit": 500}
                )
                rows = body.get("rows") or []
                for row in rows:
                    if _EARN_WORDING.search(row.get("enInfo") or ""):
                        skipped += 1
                        continue
                    sign = -1 if int(row.get("direction") or 1) < 0 else 1
                    collected.add(
                        "distribution", row["divTime"], "Distribution", row["asset"],
                        sign * _dec(row["amount"]), ref=f"div:{row.get('tranId') or row.get('id')}",
                    )
                if len(rows) < 500:
                    break
                # No paging parameter: narrow the window below the oldest row.
                upper = min(int(row["divTime"]) for row in rows) - 1
                if upper <= start:
                    break
        if skipped:
            ctx.log(f"{skipped} distribuições de Simple Earn ignoradas (já vêm do Simple Earn).")

    def _dust(self, client, ctx, collected) -> None:
        # Only the last 100 conversions exist (and none before Dec 2020). Older
        # dust is a fraction of a cent, and the balance check would name it.
        body = client.call("GET", "/sapi/v1/asset/dribblet")
        for sweep in body.get("userAssetDribblets") or []:
            for detail in sweep.get("userAssetDribbletDetails") or []:
                ref = f"dust:{detail.get('transId') or sweep.get('transId')}"
                when = detail.get("operateTime") or sweep["operateTime"]
                collected.add("dust", when, "Small Assets Exchange BNB", detail["fromAsset"], -_dec(detail["amount"]), ref=f"{ref}:{detail['fromAsset']}")
                collected.add("dust", when, "Small Assets Exchange BNB", detail.get("targetAsset") or "BNB", _dec(detail["transferedAmount"]), ref=f"{ref}:in")

    def _spot_trades(self, client, ctx, collected, queried: set[str], *, every_pair: bool) -> None:
        """Every fill on the pairs worth asking about.

        Binance has no account-wide trade history: fills can only be asked for
        one symbol at a time. On a full collection every listed pair quoted in
        a currency the account has used is queried, because a coin bought and
        later sold back appears nowhere else. Incremental runs query the pairs
        of coins already known (after the first run, every coin ever traded) plus
        any seen in this run. A coin bought and sold within one week, never
        seen before, waits for the next full run; the USDT it moved still
        shows up in the balance check.
        """
        if self._exchange_info is None:
            self._exchange_info = client.call("GET", "/api/v3/exchangeInfo", signed=False).get("symbols", [])
        touched = {coin for coin in collected.touched if coin}
        quotes = touched | set(COMMON_QUOTES)
        pairs = sorted(
            (item["symbol"], item["baseAsset"], item["quoteAsset"])
            for item in self._exchange_info
            if item["symbol"] not in queried
            and item["quoteAsset"] in quotes
            and ((every_pair and item["quoteAsset"] in touched) or item["baseAsset"] in touched)
        )
        if not pairs:
            return
        queried.update(symbol for symbol, _, _ in pairs)
        minutes = max(1, round(len(pairs) * _INTERVALS["/api/v3/myTrades"] / 60))
        ctx.log(f"Negociações spot: consultando {len(pairs)} pares (cerca de {minutes} min)…")
        fills = 0
        for index, (symbol, base, quote) in enumerate(pairs, start=1):
            from_id = 0
            while True:
                rows = client.call("GET", "/api/v3/myTrades", {"symbol": symbol, "fromId": from_id, "limit": 1000})
                for row in rows:
                    collected.add(
                        "myTrades", row["time"], fmt.TRADE_OPERATION, base, _dec(row["qty"]),
                        ref=f"{symbol}:{row['id']}", Pair=symbol, Side="BUY" if row["isBuyer"] else "SELL",
                        Quote_Coin=quote, Quote_Qty=_dec(row["quoteQty"]), Price=_dec(row["price"]),
                        Fee=_dec(row["commission"]), Fee_Coin=row["commissionAsset"],
                    )
                fills += len(rows)
                if len(rows) < 1000:
                    break
                from_id = max(int(row["id"]) for row in rows) + 1
            if index % 25 == 0:
                ctx.log(f"Negociações spot: {index}/{len(pairs)} pares, {fills} execuções.")
        ctx.log(f"Negociações spot: {fills} execuções encontradas.")

    # -- paging helpers ----------------------------------------------------------
    @staticmethod
    def _pages(client, path: str, params: dict) -> list[dict]:
        """``current``/``size`` paging (Simple Earn)."""
        rows: list[dict] = []
        current = 1
        while True:
            body = client.call("GET", path, {**params, "current": current, "size": 100})
            page = body.get("rows") or []
            rows.extend(page)
            if len(page) < 100 or len(rows) >= int(body.get("total") or 0):
                return rows
            current += 1

    @staticmethod
    def _offset_pages(client, path: str, params: dict) -> list[dict]:
        """``offset``/``limit`` paging (deposits, withdrawals)."""
        rows: list[dict] = []
        while True:
            page = client.call("GET", path, {**params, "offset": len(rows), "limit": 1000}) or []
            rows.extend(page)
            if len(page) < 1000:
                return rows

    # -- the check -------------------------------------------------------------
    def _compare(self, ctx, balances: dict[str, Decimal]) -> list[dict]:
        """Every coin whose computed position differs from Binance's balance."""
        mismatches = compare_balances(balances, _portfolio_quantities())
        if not mismatches:
            ctx.log("Saldos conferidos: a carteira bate com a Binance em todas as moedas.")
            return []
        listed = "; ".join(
            f"{m['coin']} (Binance {m['binance']} · carteira {m['carteira']})" for m in mismatches[:10]
        )
        more = f" e mais {len(mismatches) - 10}" if len(mismatches) > 10 else ""
        ctx.log(
            f"Saldos diferentes da Binance em {len(mismatches)} moeda(s): {listed}{more}. "
            "Pode ser algo que a API não informa (um par antigo já removido, um produto que a "
            "coleta não conhece). Ajuste em cada ativo com “Saldo informado pela corretora”.",
            level="warning",
        )
        return mismatches


class _Collection:
    """The rows gathered so far, and every coin they mention."""

    def __init__(self, zone: tzinfo = UTC) -> None:
        self.zone = zone
        self.rows: list[dict] = []
        self.touched: set[str] = set()
        self.first_activity: datetime | None = None

    def touch(self, symbols) -> None:
        self.touched.update(symbol.upper() for symbol in symbols if symbol)

    def add(self, source: str, when_ms: int | str, operation: str, coin: str, change: Decimal, *, ref: str, **extra) -> None:
        if not change:
            return
        moment = datetime.fromtimestamp(int(when_ms) / 1000, tz=UTC)
        if self.first_activity is None or moment < self.first_activity:
            self.first_activity = moment
        self.touch({coin})
        self.rows.append(
            {
                "Source": source,
                "Time": moment.astimezone(self.zone).strftime("%Y-%m-%d %H:%M:%S"),
                "Timezone": _zone_label(self.zone),
                "Operation": operation,
                "Coin": coin.upper(),
                "Change": change,
                "Ref": ref,
                **extra,
            }
        )


def _br_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def _exports_cover_until() -> str | None:
    """The last day the uploaded Binance exports cover (ISO), or ``None``."""
    from sqlalchemy import func, select

    from app.db.models import ImportBatch
    from app.db.session import session_scope

    with session_scope() as db:
        day = db.scalar(
            select(func.max(ImportBatch.period_end)).where(
                ImportBatch.source_kind == "CRYPTO",
                ImportBatch.broker_name == fmt.EXCHANGE,
                ImportBatch.source_format != fmt.API_FORMAT,
            )
        )
    return day.isoformat() if day else None


def _portfolio_quantities() -> dict[str, Decimal]:
    """Computed quantity per coin symbol, for the crypto assets on file."""
    from app.db.session import session_scope
    from app.portfolio.service import PortfolioService
    from app.services.portfolio_registry import get_default_portfolio

    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        service = PortfolioService(db, portfolio.id)
        assets = service.assets()
        quantities: dict[str, Decimal] = {}
        for asset_id, position in service.positions().items():
            asset = assets.get(asset_id)
            if asset is None or asset.kind not in coins.CRYPTO_KINDS:
                continue
            symbol = coins.asset_symbol(asset.ticker)
            quantities[symbol] = quantities.get(symbol, Decimal(0)) + Decimal(str(position.quantity))
        return quantities


def compare_balances(binance: dict[str, Decimal], portfolio: dict[str, Decimal]) -> list[dict]:
    """Coins where the two disagree beyond rounding, largest relative gap first.

    Fiat is left out (reais on the exchange are cash, never a position), and so
    are differences of dust: a fraction of a satoshi, or under 0.5 % of the
    larger of the two figures.
    """
    out = []
    for coin in sorted(set(binance) | set(portfolio)):
        if not coins.is_tracked(coin):
            continue
        exchange = binance.get(coin, Decimal(0))
        computed = portfolio.get(coin, Decimal(0))
        gap = abs(exchange - computed)
        scale = max(abs(exchange), abs(computed))
        if gap <= _DUST or (scale and gap / scale <= _RELATIVE_TOLERANCE):
            continue
        out.append(
            {
                "coin": coin,
                "binance": format(exchange.normalize(), "f"),
                "carteira": format(computed.normalize(), "f"),
                "_gap": gap / scale if scale else Decimal(1),
            }
        )
    out.sort(key=lambda item: item["_gap"], reverse=True)
    for item in out:
        item.pop("_gap")
    return out


register(BinancePipeline())
