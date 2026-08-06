"""Daily Tesouro Direto prices from Tesouro Transparente.

Revision ID: 0003_treasury
Revises: 0002_fixed_income
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003_treasury'
down_revision = '0002_fixed_income'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('treasury_prices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('buy_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('sell_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('buy_rate', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('sell_rate', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asset_id', 'date', name='uq_treasury_price_asset_date')
    )
    op.create_index(op.f('ix_treasury_prices_asset_id'), 'treasury_prices', ['asset_id'], unique=False)
    op.create_index(op.f('ix_treasury_prices_date'), 'treasury_prices', ['date'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_treasury_prices_date'), table_name='treasury_prices')
    op.drop_index(op.f('ix_treasury_prices_asset_id'), table_name='treasury_prices')
    op.drop_table('treasury_prices')
