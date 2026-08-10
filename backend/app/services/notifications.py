"""The feed behind the bell in the header.

Two halves, kept apart on purpose:

*Live* entries are derived on every read from a registry of sources. Today's
only one — quote fetches waiting on a retry — is state that resolves itself,
and a persisted row would then have to be reaped to stay truthful. Deriving it
means the feed cannot disagree with reality. Live entries never paginate: there
are at most a handful and they are all about *right now*.

*Stored* entries are rows in ``notifications`` written when something happened
(:func:`record`). A backup that ran last night does not resolve itself and is
worth scrolling back through, so it gets a row, an id, and a place in the
history the panel pages through.

Adding a source to either half needs no change here or in the UI: append a
function to :data:`SOURCES` for live state, or call :func:`record` from
wherever the event occurs.

Read and archive state
----------------------
Stored rows carry their own ``read_at``/``archived_at``. Live entries have no
row to mark, so their state lives in one settings blob keyed by the entry id —
and, crucially, is **dropped as soon as the entry stops being produced**. That
single rule is what keeps "archive" from becoming "mute forever": archiving the
retry banner hides it for this episode, and when the queue drains the state
goes with it, so the next genuine failure announces itself again.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, true as sa_true, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import AppSetting, Notification as NotificationRow

logger = get_logger(__name__)

#: Most attention-worthy first, so the panel can sort without knowing the kinds.
LEVEL_ORDER = {"warning": 0, "info": 1, "success": 2}

#: Read/archive state for the derived entries. Machinery, not a preference —
#: filtered out of the settings payload like the cloud-backup rows.
LIVE_STATE_KEY = "notifications_live_state"
INTERNAL_KEYS = (LIVE_STATE_KEY,)

#: Kinds the user has switched **off**. A *preference*, unlike the key above, so
#: it travels in the settings payload and the Settings page edits it directly.
#:
#: Deliberately the muted set and not the enabled set, though the card shows the
#: latter. Storing what is on means the list is a snapshot of the kinds that
#: existed the day it was saved, so a producer added in a later release arrives
#: silently switched off for every user who had ever opened this screen — and
#: nothing about muting "Backup local" says "and also anything invented later".
#: Storing what is off makes the default the absence of a choice, which is the
#: only shape that keeps new kinds audible.
MUTED_SETTING = "notification_muted_kinds"


@dataclass(frozen=True, slots=True)
class NotificationKind:
    """One switch on the Settings page.

    The catalogue is served to the UI rather than duplicated in it, so a new
    producer becomes a new switch by appending here — the same bargain the rest
    of this module makes with :data:`SOURCES`.
    """

    kind: str
    label: str
    description: str


KINDS: tuple[NotificationKind, ...] = (
    NotificationKind(
        "quotes.retry",
        "Cotações na fila",
        "Enquanto ativos que não responderam esperam uma nova tentativa.",
    ),
    NotificationKind(
        "import",
        "Importações",
        "Quando um arquivo termina de ser importado, e o que ficou pendente nele.",
    ),
    NotificationKind(
        "backup",
        "Backup local",
        "Cada cópia automática do banco de dados gravada neste computador.",
    ),
    NotificationKind(
        "cloud_backup",
        "Backup na nuvem",
        "Cada envio para o Google Drive ou Dropbox, e as falhas que precisam de atenção.",
    ),
    NotificationKind(
        "pipeline",
        "Automações",
        "Quando uma coleta automática termina — e quando ela está parada esperando um código de verificação.",
    ),
)

#: Everything, which is what an installation that never touched the setting gets.
ALL_KINDS = tuple(item.kind for item in KINDS)


def catalog() -> list[dict]:
    """The switches the Settings page draws."""
    return [{"kind": k.kind, "label": k.label, "description": k.description} for k in KINDS]


def muted_kinds(db: Session) -> set[str]:
    """Kinds the user has switched off.

    Silence is filtering, not deletion: a muted kind keeps being recorded and is
    merely left out of the feed, so turning it back on returns its history
    instead of a gap.
    """
    row = db.get(AppSetting, MUTED_SETTING)
    value = row.value if row else None
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return {str(item) for item in value} if isinstance(value, list) else set()

#: Rows returned per page. The panel asks for the first page on open and one
#: more each time the list is scrolled to its end.
PAGE_SIZE = 5


@dataclass(slots=True)
class Notification:
    """One *live* entry — a condition that holds right now.

    ``id`` is stable for as long as the condition holds, so the same pending
    refresh keeps its identity across polls: the UI can animate rather than
    flash, and read/archive state has something to hang on.
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


# ---------------------------------------------------------------- live sources


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


def _cloud_backup_failing(db: Session, portfolio_id: int | None) -> list[Notification]:
    """Cloud backup still broken as of its last run.

    Only the *unresolved* failure is live. Every individual run — success or
    failure — is also recorded as a stored event, which is what the history
    below shows; this entry exists so a provider that has been failing for a
    week is visible without scrolling, and it disappears the moment one run
    succeeds.
    """
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
SOURCES = [_quote_retries, _cloud_backup_failing]


# ------------------------------------------------------------- live state blob


def _read_live_state(db: Session) -> dict:
    row = db.get(AppSetting, LIVE_STATE_KEY)
    value = row.value if row else None
    return value.get("value") if isinstance(value, dict) and "value" in value else (value or {})


def _write_live_state(db: Session, state: dict) -> None:
    db.merge(AppSetting(key=LIVE_STATE_KEY, value={"value": state}))
    # Flushed, not just staged: the very next read in this same transaction is
    # `feed()` deciding what to hide, and `Session.get` would otherwise answer
    # from the database and miss a merge still sitting in the unit of work.
    db.flush()


def _live(db: Session, portfolio_id: int | None) -> list[Notification]:
    """Every live entry, most attention-worthy first.

    A source that raises is logged and skipped: the bell is a convenience, and
    one broken producer must not take the header down with it.
    """
    items: list[Notification] = []
    for source in SOURCES:
        try:
            items.extend(source(db, portfolio_id))
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception("notification source %s failed", getattr(source, "__name__", source))
    muted = muted_kinds(db)
    items = [item for item in items if item.kind not in muted]
    items.sort(key=lambda item: (LEVEL_ORDER.get(item.level, 9), item.title))
    return items


# ------------------------------------------------------------------- recording


def record(
    db: Session,
    *,
    kind: str,
    level: str,
    title: str,
    body: str = "",
    items: list[str] | None = None,
    portfolio_id: int | None = None,
    dedup_key: str | None = None,
) -> NotificationRow | None:
    """Write one event into the history. Returns ``None`` if it already existed.

    Does not commit — the caller's transaction owns it, so an event can never
    outlive the thing it describes. Never raises: a notification is the least
    important part of whatever the caller was doing, and must not be the reason
    a backup or an import reports failure.
    """
    try:
        if dedup_key:
            existing = db.scalar(select(NotificationRow).where(NotificationRow.dedup_key == dedup_key))
            if existing is not None:
                return None
        row = NotificationRow(
            kind=kind,
            level=level,
            title=title[:160],
            body=body,
            items=list(items or []),
            portfolio_id=portfolio_id,
            dedup_key=dedup_key,
        )
        db.add(row)
        db.flush()
        return row
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("could not record notification %s/%s", kind, title)
        return None


# ---------------------------------------------------------------------- output


def _in_scope(portfolio_id: int | None):
    """Rows this reader may see: installation-wide ones, plus their portfolio's.

    ``None`` means unscoped — every row — which is what a caller outside a
    request context (a test, a scheduler) gets when it names no portfolio.
    """
    if portfolio_id is None:
        return sa_true()
    return NotificationRow.portfolio_id.is_(None) | (NotificationRow.portfolio_id == portfolio_id)


def _wanted(db: Session):
    """Rows of a kind the user has not muted. See :func:`muted_kinds`."""
    muted = muted_kinds(db)
    return NotificationRow.kind.notin_(sorted(muted)) if muted else sa_true()


def _row_payload(row: NotificationRow) -> dict:
    return {
        "source": "stored",
        "id": str(row.id),
        "kind": row.kind,
        "level": row.level,
        "title": row.title,
        "body": row.body,
        "progress": None,
        "items": list(row.items or []),
        "at": row.at,
        "read": row.read_at is not None,
    }


def feed(
    db: Session,
    portfolio_id: int | None = None,
    *,
    cursor: int | None = None,
    limit: int = PAGE_SIZE,
) -> dict:
    """One page of the bell.

    The live entries ride along with the first page only (``cursor is None``) —
    they are not part of the history and must not be re-sent, or duplicated,
    as the user scrolls.
    """
    state = _read_live_state(db)
    live_items = _live(db, portfolio_id)

    # Garbage-collect first: state for a condition that no longer holds is what
    # would otherwise silence its recurrence forever.
    live_ids = {item.id for item in live_items}
    pruned = {key: value for key, value in state.items() if key in live_ids}
    if pruned != state:
        _write_live_state(db, pruned)
        state = pruned

    live_payload = [
        {
            "source": "live",
            **asdict(item),
            "read": bool(state.get(item.id, {}).get("read_at")),
        }
        for item in live_items
        if not state.get(item.id, {}).get("archived_at")
    ]

    stmt = select(NotificationRow).where(
        NotificationRow.archived_at.is_(None), _in_scope(portfolio_id), _wanted(db)
    )
    if cursor is not None:
        stmt = stmt.where(NotificationRow.id < cursor)
    # One extra row answers "is there another page?" without a second COUNT.
    rows = db.scalars(stmt.order_by(NotificationRow.id.desc()).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    return {
        "live": live_payload if cursor is None else [],
        "items": [_row_payload(row) for row in rows],
        "next_cursor": rows[-1].id if (has_more and rows) else None,
        "unread": unread_count(db, portfolio_id),
    }


def unread_count(db: Session, portfolio_id: int | None = None) -> int:
    """What the dot on the bell is counting."""
    state = _read_live_state(db)
    live = sum(
        1
        for item in _live(db, portfolio_id)
        if not state.get(item.id, {}).get("read_at")
        and not state.get(item.id, {}).get("archived_at")
    )
    stored = db.scalar(
        select(func.count())
        .select_from(NotificationRow)
        .where(
            NotificationRow.archived_at.is_(None),
            NotificationRow.read_at.is_(None),
            _in_scope(portfolio_id),
            _wanted(db),
        )
    )
    return live + int(stored or 0)


# ----------------------------------------------------------------- state edits


def mark_all_read(db: Session, portfolio_id: int | None = None) -> int:
    """Everything visible becomes read. What closing the panel calls.

    Marks the whole feed rather than only the page that was on screen: the user
    opened the bell, and leaving page three unread because they did not scroll
    that far would keep a dot lit over things they have already been told.
    """
    now = datetime.now(UTC)
    state = _read_live_state(db)
    for item in _live(db, portfolio_id):
        entry = dict(state.get(item.id) or {})
        entry.setdefault("read_at", now.isoformat())
        state[item.id] = entry
    _write_live_state(db, state)

    result = db.execute(
        update(NotificationRow)
        .where(
            NotificationRow.archived_at.is_(None),
            NotificationRow.read_at.is_(None),
            _in_scope(portfolio_id),
            # Muted kinds are not marked: they were never on screen, and
            # consuming them here would mean un-muting a kind produced a stack
            # of entries already counted as seen.
            _wanted(db),
        )
        .values(read_at=now)
    )
    return int(result.rowcount or 0)


def archive(db: Session, source: str, entry_id: str) -> bool:
    """Hide one entry. ``True`` when something was actually hidden.

    A stored row keeps its history and only leaves the panel. A live entry has
    nothing to keep, so it is remembered as archived for exactly as long as the
    condition it describes still holds (see the module docstring).
    """
    now = datetime.now(UTC)
    if source == "live":
        state = _read_live_state(db)
        entry = dict(state.get(entry_id) or {})
        entry["archived_at"] = now.isoformat()
        entry.setdefault("read_at", now.isoformat())
        state[entry_id] = entry
        _write_live_state(db, state)
        return True

    if not entry_id.isdigit():
        return False
    row = db.get(NotificationRow, int(entry_id))
    if row is None or row.archived_at is not None:
        return False
    row.archived_at = now
    if row.read_at is None:
        row.read_at = now
    # Explicit, because sessions here are built with autoflush off: the caller's
    # next act is to re-read the feed, and an unflushed change would hand back
    # the row it just hid.
    db.flush()
    return True
