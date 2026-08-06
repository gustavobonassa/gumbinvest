"""Corporate actions: link an asset to the one that replaced it.

Revision ID: 0004_successions
Revises: 0003_treasury
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004_successions'
down_revision = '0003_treasury'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('asset_successions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('from_asset_id', sa.Integer(), nullable=False),
    sa.Column('to_asset_id', sa.Integer(), nullable=True),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('cash_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['from_asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portfolio_id', 'from_asset_id', name='uq_succession_portfolio_from')
    )
    op.create_index(op.f('ix_asset_successions_portfolio_id'), 'asset_successions', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_asset_successions_from_asset_id'), 'asset_successions', ['from_asset_id'], unique=False)
    op.create_index(op.f('ix_asset_successions_effective_date'), 'asset_successions', ['effective_date'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_asset_successions_effective_date'), table_name='asset_successions')
    op.drop_index(op.f('ix_asset_successions_from_asset_id'), table_name='asset_successions')
    op.drop_index(op.f('ix_asset_successions_portfolio_id'), table_name='asset_successions')
    op.drop_table('asset_successions')
