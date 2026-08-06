"""The local asset universe: every listed instrument, from official bulk files.

The app knows a great deal about the papers a portfolio holds and nothing about
the ones it does not, so no question of the form "which assets look like this"
can be answered locally. This package builds that missing index.

Everything comes from files a publisher intends to be downloaded whole — B3's
COTAHIST series, the CVM's open data, the SEC's ticker registry — and never
from per-ticker API calls. That is a design constraint, not an accident: it
keeps the quote providers reserved for assets the portfolio actually holds, and
it is why a full run is about ten HTTP requests rather than several thousand.

* :mod:`.ingest` — the staged, resumable driver and the stage implementations
* :mod:`.state` — the run block (progress, cancel, the cross-process lock)
* :mod:`.compute` — the ratio arithmetic, pure and Decimal-exact
* :mod:`.sources` — one adapter per published file

Screening over what this produces lives in ``app.services.universe``; nothing
here imports it, and it imports nothing here.
"""
from __future__ import annotations

from app.market.universe.ingest import (
    ingest_slice,
    run_ingest,
    start_background,
)
from app.market.universe.state import (
    AlreadyRunning,
    history_years,
    is_enabled,
    markets,
    read,
    request_cancel,
    stages_public,
)

__all__ = [
    "AlreadyRunning",
    "history_years",
    "ingest_slice",
    "is_enabled",
    "markets",
    "read",
    "request_cancel",
    "run_ingest",
    "stages_public",
    "start_background",
]
