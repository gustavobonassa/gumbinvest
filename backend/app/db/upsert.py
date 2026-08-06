"""Dialect-aware ``INSERT ... ON CONFLICT``.

``on_conflict_do_update`` is not on the generic ``insert()`` construct — each
dialect carries its own. Production runs on PostgreSQL and the test suite runs
on SQLite by default, and both support the clause, so the only thing needed is
to pick the right construct for the session's bind.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def dialect_insert(db: Session) -> Any:
    """The ``insert()`` construct that supports upserts on this connection."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert
