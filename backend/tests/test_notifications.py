"""The bell's inbox half: recording, paging, reading and archiving.

The live half — derived entries and the rule that their archived flag dies with
the condition — is covered in tests/test_quote_retry.py, next to the producer
that makes one.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Portfolio
from app.services.notifications import (
    archive,
    feed,
    mark_all_read,
    record,
    unread_count,
)


def _seed(db: Session, count: int, *, portfolio_id: int | None = None) -> None:
    for index in range(count):
        record(
            db,
            kind="backup",
            level="success",
            title=f"Backup {index}",
            body=f"corpo {index}",
            portfolio_id=portfolio_id,
            dedup_key=f"backup:{index}",
        )


def test_history_pages_newest_first_without_gaps_or_repeats(db: Session, portfolio: Portfolio):
    _seed(db, 12)

    seen: list[str] = []
    cursor = None
    for _ in range(3):
        page = feed(db, portfolio.id, cursor=cursor, limit=5)
        seen.extend(item["title"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen[0] == "Backup 11"  # newest first
    assert len(seen) == len(set(seen)) == 12
    assert cursor is None


def test_the_last_page_reports_no_cursor_even_when_it_is_exactly_full(
    db: Session, portfolio: Portfolio
):
    """The off-by-one that would make the panel spin forever.

    With exactly PAGE_SIZE rows left, a naive "a full page means there is more"
    hands back a cursor for an empty page — and an infinite scroll that keeps
    asking.
    """
    _seed(db, 5)
    page = feed(db, portfolio.id, limit=5)
    assert len(page["items"]) == 5
    assert page["next_cursor"] is None


def test_opening_and_closing_marks_everything_read_not_just_the_first_page(
    db: Session, portfolio: Portfolio
):
    _seed(db, 12)
    assert unread_count(db, portfolio.id) == 12

    # The user never scrolled past the first page, but they did open the bell.
    feed(db, portfolio.id, limit=5)
    marked = mark_all_read(db, portfolio.id)

    assert marked == 12
    assert unread_count(db, portfolio.id) == 0
    assert all(item["read"] for item in feed(db, portfolio.id, limit=12)["items"])


def test_a_new_event_after_reading_lights_the_dot_again(db: Session, portfolio: Portfolio):
    _seed(db, 3)
    mark_all_read(db, portfolio.id)
    assert unread_count(db, portfolio.id) == 0

    record(db, kind="import", level="success", title="Importação concluída")
    assert unread_count(db, portfolio.id) == 1


def test_archiving_a_stored_row_hides_it_but_keeps_it(db: Session, portfolio: Portfolio):
    from app.db.models import Notification as Row

    _seed(db, 3)
    target = feed(db, portfolio.id)["items"][0]

    assert archive(db, "stored", target["id"]) is True

    titles = [item["title"] for item in feed(db, portfolio.id)["items"]]
    assert target["title"] not in titles
    # Hidden from the panel, still on the record.
    assert db.get(Row, int(target["id"])).archived_at is not None
    # And it stops counting against the dot.
    assert unread_count(db, portfolio.id) == 2


def test_archiving_is_idempotent_and_honest_about_misses(db: Session, portfolio: Portfolio):
    _seed(db, 1)
    target = feed(db, portfolio.id)["items"][0]

    assert archive(db, "stored", target["id"]) is True
    assert archive(db, "stored", target["id"]) is False  # already gone
    assert archive(db, "stored", "999999") is False  # never existed
    assert archive(db, "stored", "not-a-number") is False  # a live id sent as stored


def test_events_of_another_portfolio_stay_out_of_this_one(db: Session, portfolio: Portfolio):
    other = Portfolio(name="Outra", base_currency="BRL")
    db.add(other)
    db.flush()

    record(db, kind="import", level="success", title="Minha", portfolio_id=portfolio.id)
    record(db, kind="import", level="success", title="Alheia", portfolio_id=other.id)
    record(db, kind="backup", level="success", title="De todos")  # portfolio_id None

    titles = [item["title"] for item in feed(db, portfolio.id)["items"]]
    assert "Alheia" not in titles
    assert {"Minha", "De todos"} <= set(titles)


def _mute(db: Session, *kinds: str) -> None:
    """Turn the named kinds off, the way the Settings page would."""
    from app.db.models import AppSetting
    from app.services.notifications import MUTED_SETTING

    db.merge(AppSetting(key=MUTED_SETTING, value={"value": list(kinds)}))
    db.flush()


def test_muting_a_kind_hides_it_without_losing_it(db: Session, portfolio: Portfolio):
    from app.db.models import Notification as Row
    from sqlalchemy import func as sa_func, select as sa_select

    _seed(db, 3)  # kind="backup"
    record(db, kind="import", level="success", title="Importação concluída")
    assert unread_count(db, portfolio.id) == 4

    _mute(db, "backup")

    page = feed(db, portfolio.id)
    assert [item["kind"] for item in page["items"]] == ["import"]
    assert page["unread"] == 1
    # Hidden, not deleted.
    assert db.scalar(sa_select(sa_func.count()).select_from(Row)) == 4

    # And turning it back on returns the history rather than a gap.
    _mute(db)
    assert len(feed(db, portfolio.id)["items"]) == 4


def test_reading_the_bell_does_not_consume_muted_entries(db: Session, portfolio: Portfolio):
    """Un-muting must not hand back a stack of things already marked seen."""
    _seed(db, 3)  # kind="backup"
    record(db, kind="import", level="success", title="Importação concluída")

    _mute(db, "backup")
    assert mark_all_read(db, portfolio.id) == 1  # only the import was on screen

    _mute(db)
    assert unread_count(db, portfolio.id) == 3


def test_a_kind_no_longer_in_the_catalogue_keeps_showing(db: Session, portfolio: Portfolio):
    """Retiring a producer must not silently swallow what it already wrote."""
    record(db, kind="some.retired.kind", level="info", title="De antigamente")
    _mute(db, "backup", "import", "cloud_backup", "quotes.retry")

    assert [i["title"] for i in feed(db, portfolio.id)["items"]] == ["De antigamente"]


def test_the_default_is_to_hear_everything(db: Session, portfolio: Portfolio):
    """An installation that never opened the setting misses nothing."""
    from app.services.notifications import muted_kinds

    assert muted_kinds(db) == set()


def test_a_kind_invented_later_is_heard_by_someone_who_already_chose(
    db: Session, portfolio: Portfolio
):
    """Why the *muted* set is what gets stored.

    Save the enabled set instead and it freezes the catalogue as it stood that
    day: every user who had ever opened the Settings page would find the next
    release's notifications already switched off, having never been asked. A
    stored mute list only silences what it names.
    """
    _mute(db, "backup")  # the user makes one choice, today

    # A later release adds a producer this installation has never heard of.
    record(db, kind="price.target", level="info", title="Alvo de preço atingido")

    titles = [item["title"] for item in feed(db, portfolio.id)["items"]]
    assert "Alvo de preço atingido" in titles


def test_recording_never_raises_into_the_caller(db: Session, portfolio: Portfolio):
    """A notification is the least important part of whatever produced it.

    An oversized title would violate the column and take the backup down with
    it; instead the row is trimmed to fit.
    """
    row = record(db, kind="backup", level="success", title="x" * 400)
    assert row is not None
    assert len(row.title) == 160
