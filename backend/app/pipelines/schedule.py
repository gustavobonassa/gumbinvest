"""When each collector runs on its own, and how a missed slot is made up.

Off until the user turns it on: a collection signs into a broker in the
background, so it only does that on its own once asked to. The user then
picks weekdays and a time per pipeline (Automações → "Agendamento").
Rather than one cron entry per choice, the scheduler ticks every minute and
asks :func:`app.pipelines.runner.run_due` "is any slot owed?". A slot is owed
when it has passed and no run of that pipeline (manual or scheduled, whatever
its outcome) started after it. That one rule covers both the punctual case
and the desktop app being closed at the time: the first tick after the app
opens finds last Monday's slot unpaid and runs it — once, however many slots
were missed, since only the latest one is ever owed.

``since`` keeps the rule from reaching back too far. It is set when a
schedule is saved (and, as a safety net, on the first tick that finds an
enabled schedule without one), so neither turning the schedule on nor moving
the time to one earlier today fires a collection out of the blue for a slot
that predates the decision.

Stored in ``app_settings`` under ``pipeline_schedule:<key>`` — machinery, so
the Settings endpoint hides it. Times are wall-clock in the configured
timezone, like every other schedule in the app.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.db.models import AppSetting

PREFIX = "pipeline_schedule:"

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def zone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 — a bad TZ setting must not stop the schedule
        return ZoneInfo("UTC")


def parse_time(value: str) -> time:
    """``"HH:MM"`` (24h) → :class:`time`; ``ValueError`` for anything else."""
    match = _TIME.match(value or "")
    if not match:
        raise ValueError(f"invalid time: {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _parse_instant(value) -> datetime | None:  # noqa: ANN001 — whatever JSON held
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Schedule:
    #: Whether the pipeline runs on its own at all. The days and time are kept
    #: while it is off, so switching it back on restores the user's choice.
    enabled: bool
    #: Weekdays, Python's convention: 0 = Monday … 6 = Sunday.
    days: tuple[int, ...]
    at: time
    #: Slots before this instant are never owed (see the module doc).
    since: datetime | None = None

    def _slot(self, day) -> datetime:  # noqa: ANN001 — a date
        return datetime.combine(day, self.at, tzinfo=zone())

    def last_slot(self, now: datetime) -> datetime | None:
        """The latest slot at or before ``now`` (UTC), if any is scheduled."""
        if not self.enabled or not self.days:
            return None
        local = now.astimezone(zone())
        for back in range(8):
            day = local.date() - timedelta(days=back)
            if day.weekday() in self.days and self._slot(day) <= local:
                return self._slot(day).astimezone(UTC)
        return None

    def next_slot(self, now: datetime) -> datetime | None:
        """The first slot strictly after ``now`` (UTC), if any is scheduled."""
        if not self.enabled or not self.days:
            return None
        local = now.astimezone(zone())
        for ahead in range(8):
            day = local.date() + timedelta(days=ahead)
            if day.weekday() in self.days and self._slot(day) > local:
                return self._slot(day).astimezone(UTC)
        return None


def _default(pipeline) -> Schedule:  # noqa: ANN001 — Pipeline, kept loose like base.run
    return Schedule(
        enabled=False,
        days=tuple(pipeline.spec.default_days),
        at=parse_time(pipeline.spec.default_time),
    )


def load(db, pipeline) -> Schedule:  # noqa: ANN001
    """The stored schedule, or the pipeline's default (off) when none was saved.

    A row with garbage in it (hand-edited, or an older shape) reads as the
    default — off — rather than failing the tick or running unasked.
    """
    default = _default(pipeline)
    row = db.get(AppSetting, PREFIX + pipeline.spec.key)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    try:
        days = tuple(sorted({int(day) for day in value["days"] if 0 <= int(day) <= 6}))
        at = parse_time(value["time"])
    except (KeyError, TypeError, ValueError):
        return default
    return Schedule(
        enabled=value.get("enabled") is True and bool(days),
        days=days,
        at=at,
        since=_parse_instant(value.get("since")),
    )


def _store(db, pipeline, schedule: Schedule) -> None:  # noqa: ANN001
    db.merge(
        AppSetting(
            key=PREFIX + pipeline.spec.key,
            value={
                "enabled": schedule.enabled,
                "days": list(schedule.days),
                "time": schedule.at.strftime("%H:%M"),
                "since": schedule.since.isoformat() if schedule.since else None,
            },
        )
    )


def save(
    db,  # noqa: ANN001
    pipeline,  # noqa: ANN001
    *,
    enabled: bool,
    days: list[int],
    at: str,
    now: datetime,
) -> Schedule:
    """Replace the schedule. Counts from ``now``: no slot before it is owed.

    Raises ``ValueError`` for a weekday outside 0–6, a malformed time, or an
    enabled schedule with no weekday (which would be "on" yet never run).
    """
    if any(day < 0 or day > 6 for day in days):
        raise ValueError("weekday out of range")
    if enabled and not days:
        raise ValueError("an enabled schedule needs at least one weekday")
    schedule = Schedule(
        enabled=enabled, days=tuple(sorted(set(days))), at=parse_time(at), since=now
    )
    _store(db, pipeline, schedule)
    return schedule


def anchor(db, pipeline, now: datetime) -> Schedule:  # noqa: ANN001
    """Start counting owed slots from ``now``, keeping the days and time."""
    current = load(db, pipeline)
    schedule = Schedule(enabled=current.enabled, days=current.days, at=current.at, since=now)
    _store(db, pipeline, schedule)
    return schedule


def payload(schedule: Schedule, now: datetime) -> dict:
    """The wire shape the Automações card draws its label and modal from."""
    upcoming = schedule.next_slot(now)
    return {
        "enabled": schedule.enabled,
        "days": list(schedule.days),
        "time": schedule.at.strftime("%H:%M"),
        # In the app timezone, offset included: the UI shows its wall-clock
        # part as written, so it matches the chosen time even when the
        # browser sits in another zone.
        "next_run_at": upcoming.astimezone(zone()).isoformat() if upcoming else None,
    }
