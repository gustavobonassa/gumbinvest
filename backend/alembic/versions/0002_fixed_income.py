"""Fixed income yield terms and Banco Central index series.

Revision ID: 0002_fixed_income
Revises: 0001_initial
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0002_fixed_income'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('index_rates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('value', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code', 'date', name='uq_index_rate_code_date')
    )
    op.create_index(op.f('ix_index_rates_code'), 'index_rates', ['code'], unique=False)
    op.create_index(op.f('ix_index_rates_date'), 'index_rates', ['date'], unique=False)
    op.create_table('fixed_income_terms',
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('index_code', sa.String(length=16), nullable=False),
    sa.Column('percent_of_index', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('spread_annual', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('fixed_rate_annual', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('maturity_date', sa.Date(), nullable=True),
    sa.Column('pays_periodic_interest', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('asset_id')
    )


def downgrade() -> None:
    op.drop_table('fixed_income_terms')
    op.drop_index(op.f('ix_index_rates_date'), table_name='index_rates')
    op.drop_index(op.f('ix_index_rates_code'), table_name='index_rates')
    op.drop_table('index_rates')
