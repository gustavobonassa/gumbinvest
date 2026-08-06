"""US tickers, from the SEC's own registry — and the identification to use.

``company_tickers.json`` is a single 795 KB file listing every ticker the SEC
knows, with its CIK and company name. It is what maps a filing (which knows
only CIKs) to something tradable, so it is the companion to
:mod:`.sec_financials`, where the actual US fundamentals come from.

The identification below is the part worth reading: the SEC will not serve any
of this without a contact address, which makes the US market opt-in in a way
the Brazilian sources are not. See :data:`_EMAIL`.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger
from app.domain.enums import AssetKind

from . import SourceShapeError, fetch_bytes

logger = get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

SOURCE = "sec-tickers"

#: The contact address the setting ships with. A run must not go out with it.
PLACEHOLDER_CONTACT = "admin@example.com"

#: Where the chosen identification lives, so ``.env`` never has to be edited.
SETTING_KEY = "sec_user_agent"


class UserAgentNotConfigured(RuntimeError):
    """The SEC User-Agent is still the shipped placeholder; message is pt-BR."""


@dataclass(frozen=True, slots=True)
class UsTicker:
    ticker: str
    name: str
    cik: str


#: The SEC requires an e-mail-shaped contact in the User-Agent, and answers
#: 403 without one. Measured from one container, back to back, two seconds
#: apart on 2026-08-06:
#:
#:   ``GumbInvest-1e19ba/1.0 (self-hosted)``   -> 403
#:   ``GumbInvest/1.0 (contato: x@y.com)``     -> 200
#:
#: An earlier round of tests from the host suggested a bare name was enough.
#: It was not reproducible, and this repo already knew better — see the note in
#: :mod:`app.market.superinvestors`, which recorded the same finding when that
#: feature was written. There is no way to read SEC data without offering a
#: contact address, so the honest options are to give one or to leave the US
#: market switched off; everything Brazilian works either way.
#:
#: It need not be personal. Any address the user controls satisfies both the
#: server and the intent, which is that someone can be reached if the client
#: misbehaves.
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

def _example_agent() -> str:
    """A well-formed identification, for the message that asks for one."""
    return "GumbInvest/1.0 (contato: seu-email@exemplo.com)"


def resolve_user_agent(db=None) -> str:
    """The identification to send: the user's setting, else the environment.

    Nothing is generated as a fallback. The SEC needs a contact address and
    this code cannot invent one, so an install that has not provided one simply
    does not fetch — which is a clearer outcome than a 403 nobody can explain.
    """
    if db is not None:
        from app.db.models import AppSetting

        row = db.get(AppSetting, SETTING_KEY)
        value = row.value if row is not None else None
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()

    configured = (settings.sec_user_agent or "").strip()
    if configured and PLACEHOLDER_CONTACT not in configured:
        return configured

    return ""


def check_user_agent(db=None) -> str:
    """The identification to use, or a refusal explaining what to change.

    Only two things are refused, both because they demonstrably fail: the
    shipped placeholder, which every install would otherwise share, and a URL,
    which ``www.sec.gov`` answers 403 to.
    """
    agent = resolve_user_agent(db)
    if not agent or PLACEHOLDER_CONTACT in agent or not _EMAIL.search(agent):
        raise UserAgentNotConfigured(
            "A SEC exige um e-mail de contato na identificação do cliente e recusa "
            "(403) qualquer requisição sem ele — não há como contornar. Informe um "
            "endereço em Configurações → Dados (pode ser um alias, não precisa ser "
            f"o seu principal), no formato: {_example_agent()}. "
            "Se preferir não informar, desmarque o mercado EUA: todo o resto do "
            "universo vem da B3 e da CVM e não usa a SEC."
        )
    return agent


def fetch(db=None) -> list[UsTicker]:
    """Every ticker in the SEC registry. One request."""
    agent = check_user_agent(db)
    raw = fetch_bytes(TICKERS_URL, headers={"User-Agent": agent})
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SourceShapeError("registro de tickers da SEC: resposta não é JSON") from exc
    if not isinstance(payload, dict):
        raise SourceShapeError("registro de tickers da SEC: formato inesperado")

    out: list[UsTicker] = []
    seen: set[str] = set()
    for item in payload.values():
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        cik = str(item.get("cik_str") or "").strip()
        if not ticker or not cik or ticker in seen:
            continue
        # Share-class suffixes ("BRK-B") and warrants are not what the wallet
        # categories accept, and the app has no currency knowledge for them.
        if not ticker.isalpha():
            continue
        seen.add(ticker)
        out.append(
            UsTicker(ticker=ticker, name=str(item.get("title") or ticker)[:255], cik=cik.zfill(10))
        )
    if not out:
        raise SourceShapeError("registro de tickers da SEC: nenhum ticker lido")
    return out


#: What a US row looks like before anything enriches it. The SEC registry says
#: nothing about instrument family, and guessing ETF-vs-stock from a name is
#: exactly the sort of quiet invention the classifier rule forbids.
DEFAULT_KIND = AssetKind.STOCK_INTL.value
DEFAULT_CURRENCY = "USD"
