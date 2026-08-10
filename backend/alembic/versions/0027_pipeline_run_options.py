"""Per-run options on a pipeline run (e.g. full-history backfill).

A collection can be incremental (the weekly default) or a one-off "bring
everything" — B3 lets an investor export several years at once, which is what
a new user wants on the first run. The choice is per-run, so it lives on the
run rather than in settings.

Revision ID: 0027_pipeline_run_options
Revises: 0026_pipeline_runs
Create Date: 2026-08-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_pipeline_run_options"
down_revision = "0026_pipeline_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipeline_runs", sa.Column("options", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pipeline_runs", "options")
