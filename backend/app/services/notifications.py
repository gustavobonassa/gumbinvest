"""The feed behind the bell in the header.

A registry of sources rather than an inbox table. Today's only source — quote
fetches waiting on a retry — is *derived* state that resolves itself, and a
persisted row would then have to be reaped to stay truthful; deriving it means
the feed cannot disagree with reality.

A future source that owns real events (a price target being hit, an import
finishing) persists its own rows and exposes them through the same contract:
one function, ``(db, portfolio_id) -> list[Notification]``, appended to
:data:`SOURCES`. Nothing else in the app has to change, and the panel renders
it with no new code as long as it fills in the fields below.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Most attention-worthy first, so the panel can sort without knowing the kinds.
LEVEL_ORDER = {"warning": 0, "info": 1, "success": 2}


@dataclass(slots=True)
class Notification:
    """One entry in the panel.

    ``id`` is stable for as long as the condition holds — the same pending
    refresh keeps the same id across polls — so the UI can animate rather than
    flash, and a future "dismiss" can remember what was dismissed.
    """

    id: str
    kind: str
    level: str
    title: str
    body: str
    #: Optional bar: ``{"done": int, "total": int, "label": str}``.
    progress: dict | None = None
    #: Subjects the entry is about (tickers, file names) — rendered as chips.
    items: list[str] = field(default_factory=list)
    #: When the next thing happens, if anything is scheduled.
    at: datetime | None = None


def _quote_retries(db: Session, portfolio_id: int | None) -> list[Notification]:
    """Quotes that failed transiently and are queued for another attempt."""
    from app.market.service import pending_quotes, quotable_assets  # local: avoids a cycle

    pending = pending_quotes(db, portfolio_id)
    if not pending:
        return []

    tracked = quotable_assets(db, portfolio_id)
    next_at = min(item["next_attempt_at"] for item in pending)
    tickers = [item["ticker"] for item in pending]
    # Counted as "this round", not "has any price at all": an asset that kept
    # yesterday's quote through a failed fetch still has not been updated, and
    # a bar reading 83 of 83 while three tickers sit in the queue below it is
    # a bar nobody believes.
    total = max(len(tracked), len(tickers))
    done = total - len(tickers)

    return [
        Notification(
            id="quotes.retry",
            kind="quotes.retry",
            level="info",
            title="Atualizando cotações",
            body=(
                f"{len(tickers)} ativo(s) não responderam na última busca e estão "
                "na fila. A cotação chega sozinha, não é preciso fazer nada."
            ),
            progress={"done": done, "total": total, "label": "atualizadas"},
            items=tickers,
            at=next_at,
        )
    ]


def _cloud_backup(db: Session, portfolio_id: int | None) -> list[Notification]:
    """Cloud uploads that failed on their last run — silence when all is well."""
    from app.services.cloud_backup import STATUS_KEY, get_provider  # local: keeps the header light
    from app.services.cloud_backup.base import parse_remote_dt, read_row

    row = read_row(db, STATUS_KEY) or {}
    items: list[Notification] = []
    for name, result in (row.get("providers") or {}).items():
        if result.get("state") != "error":
            continue
        try:
            label = get_provider(name).label
        except Exception:  # noqa: BLE001 — a renamed provider must not break the bell
            label = name
        items.append(
            Notification(
                id=f"cloud_backup.{name}",
                kind="cloud_backup",
                level="warning",
                title="Backup na nuvem falhou",
                body=f"{label}: {result.get('message') or 'falha no último envio'}",
                at=parse_remote_dt(result.get("at")),
            )
        )
    return items


#: Every source consulted when the panel is opened. Append to extend the feed.
SOURCES = [_quote_retries, _cloud_backup]


def feed(db: Session, portfolio_id: int | None = None) -> list[dict]:
    """Every active notification, most attention-worthy first.

    A source that raises is logged and skipped: the bell is a convenience, and
    one broken producer must not take the header down with it.
    """
    items: list[Notification] = []
    for source in SOURCES:
        try:
            items.extend(source(db, portfolio_id))
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception("notification source %s failed", getattr(source, "__name__", source))
    items.sort(key=lambda item: (LEVEL_ORDER.get(item.level, 9), item.title))
    return [asdict(item) for item in items]
