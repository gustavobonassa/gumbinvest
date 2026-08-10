"""Executions of the automated collectors (Configurações → Automações).

Keeping the portfolio current used to mean logging into the broker, exporting
a file and dragging it onto the Importar page. A pipeline does that walk
unattended, and this table is its whole nervous system: the run's status is
what the UI polls, its ``log`` is the narration, and the ``input_request`` /
``input_response`` pair is how a worker parked on a broker's 2FA challenge and
the human with the code find each other across processes.

Revision ID: 0026_pipeline_runs
Revises: 0025_asset_cnpj
Create Date: 2026-08-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_pipeline_runs"
down_revision = "0025_asset_cnpj"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline", sa.String(length=40), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("log", sa.JSON(), nullable=False),
        sa.Column("input_request", sa.JSON(), nullable=True),
        sa.Column("input_response", sa.JSON(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "portfolio_id",
            sa.Integer(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_pipeline_runs_pipeline", "pipeline_runs", ["pipeline"])
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index("ix_pipeline_runs_started_at", "pipeline_runs", ["started_at"])
    op.create_index("ix_pipeline_runs_portfolio_id", "pipeline_runs", ["portfolio_id"])


def downgrade() -> None:
    op.drop_table("pipeline_runs")
