"""Headless desktop server: everything but the window.

The Electron shell owns the UI (window, title bar, tray); this process owns
the data: SQLite schema, the in-process scheduler, and uvicorn serving both
the API and the built SPA. Electron spawns it, reads ``port.txt`` from the
data directory to find it, and kills it on quit.

Order matters here: the environment defaults must be in place before the
first ``app.*`` import pulls in the settings singleton, so everything from
``app`` is imported inside :func:`main`, after ``configure_environment()``.
"""
from __future__ import annotations

import logging
import logging.handlers

from app.desktop import paths
from app.desktop.bootstrap import configure_environment, port as preferred_port

_PORT_FILE = "port.txt"


def _setup_file_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "gumbinvest.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(handler)


def main() -> None:
    configure_environment()

    # configure_logging *replaces* the root handlers, so it must run before
    # the file handler is added — not after, or the file handler is wiped.
    from app.core.logging import configure_logging, get_logger

    configure_logging()
    _setup_file_logging()

    from app.desktop.schema import init_db
    from app.desktop.scheduler import build_scheduler
    from app.desktop.server import pick_port

    logger = get_logger(__name__)
    logger.info("GumbInvest server starting (data at %s)", paths.data_root())

    init_db()

    port = pick_port(preferred_port())
    (paths.data_root() / _PORT_FILE).write_text(str(port))

    scheduler = build_scheduler()
    scheduler.start()
    logger.info("serving on 0.0.0.0:%s", port)

    import uvicorn

    # Blocks until the process is killed by the shell. An abrupt kill is fine:
    # SQLite in WAL mode recovers, and every scheduled job is idempotent.
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
