"""Initial schema: portfolios, assets, brokers, transactions, quotes and analytics tables.

Revision ID: 0001_initial
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('assets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ticker', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('sector', sa.String(length=120), nullable=True),
    sa.Column('market_symbol', sa.String(length=60), nullable=True),
    sa.Column('price_manual', sa.Boolean(), nullable=False),
    sa.Column('manual_price', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assets_kind'), 'assets', ['kind'], unique=False)
    op.create_index(op.f('ix_assets_ticker'), 'assets', ['ticker'], unique=True)
    op.create_table('audit_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
    op.create_index(op.f('ix_audit_logs_at'), 'audit_logs', ['at'], unique=False)
    op.create_table('brokers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('canonical_name', sa.String(length=160), nullable=False),
    sa.Column('raw_names', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_brokers_canonical_name'), 'brokers', ['canonical_name'], unique=True)
    op.create_table('portfolios',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('base_currency', sa.String(length=8), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('watchlist',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ticker', sa.String(length=40), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('target_price', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_watchlist_ticker'), 'watchlist', ['ticker'], unique=True)
    op.create_table('goals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('target_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('target_date', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_goals_portfolio_id'), 'goals', ['portfolio_id'], unique=False)
    op.create_table('import_batches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('file_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('rows_total', sa.Integer(), nullable=False),
    sa.Column('rows_imported', sa.Integer(), nullable=False),
    sa.Column('rows_duplicate', sa.Integer(), nullable=False),
    sa.Column('rows_failed', sa.Integer(), nullable=False),
    sa.Column('issues', sa.JSON(), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_import_batches_file_hash'), 'import_batches', ['file_hash'], unique=False)
    op.create_index(op.f('ix_import_batches_portfolio_id'), 'import_batches', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_import_batches_status'), 'import_batches', ['status'], unique=False)
    op.create_table('portfolio_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('market_value', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('cost_basis', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('net_invested', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('cash_flow', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('dividends_cumulative', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('realized_cumulative', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('unrealized', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portfolio_id', 'date', name='uq_snapshot_portfolio_date')
    )
    op.create_index(op.f('ix_portfolio_snapshots_date'), 'portfolio_snapshots', ['date'], unique=False)
    op.create_index(op.f('ix_portfolio_snapshots_portfolio_id'), 'portfolio_snapshots', ['portfolio_id'], unique=False)
    op.create_table('price_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('close', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asset_id', 'date', name='uq_price_history_asset_date')
    )
    op.create_index(op.f('ix_price_history_asset_id'), 'price_history', ['asset_id'], unique=False)
    op.create_index(op.f('ix_price_history_date'), 'price_history', ['date'], unique=False)
    op.create_table('quotes',
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('previous_close', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('change', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('change_percent', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('long_name', sa.String(length=255), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('asset_id')
    )
    op.create_table('transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_id', sa.Integer(), nullable=False),
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('broker_id', sa.Integer(), nullable=True),
    sa.Column('import_batch_id', sa.Integer(), nullable=True),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('op_type', sa.String(length=24), nullable=False),
    sa.Column('effect', sa.String(length=20), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('gross_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('fees', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('taxes', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('net_amount', sa.Numeric(precision=20, scale=6), nullable=False),
    sa.Column('raw_movement', sa.String(length=120), nullable=False),
    sa.Column('raw_product', sa.String(length=255), nullable=False),
    sa.Column('raw_institution', sa.String(length=255), nullable=False),
    sa.Column('source_line', sa.Integer(), nullable=True),
    sa.Column('dedup_key', sa.String(length=80), nullable=False),
    sa.Column('occurrence', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['broker_id'], ['brokers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_batch_id'], ['import_batches.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portfolio_id', 'dedup_key', name='uq_transaction_dedup')
    )
    op.create_index('ix_transactions_asset_date', 'transactions', ['asset_id', 'trade_date'], unique=False)
    op.create_index(op.f('ix_transactions_asset_id'), 'transactions', ['asset_id'], unique=False)
    op.create_index(op.f('ix_transactions_dedup_key'), 'transactions', ['dedup_key'], unique=False)
    op.create_index(op.f('ix_transactions_import_batch_id'), 'transactions', ['import_batch_id'], unique=False)
    op.create_index('ix_transactions_op_type', 'transactions', ['op_type'], unique=False)
    op.create_index('ix_transactions_portfolio_date', 'transactions', ['portfolio_id', 'trade_date'], unique=False)
    op.create_index(op.f('ix_transactions_portfolio_id'), 'transactions', ['portfolio_id'], unique=False)
    op.create_index(op.f('ix_transactions_trade_date'), 'transactions', ['trade_date'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_transactions_trade_date'), table_name='transactions')
    op.drop_index(op.f('ix_transactions_portfolio_id'), table_name='transactions')
    op.drop_index('ix_transactions_portfolio_date', table_name='transactions')
    op.drop_index('ix_transactions_op_type', table_name='transactions')
    op.drop_index(op.f('ix_transactions_import_batch_id'), table_name='transactions')
    op.drop_index(op.f('ix_transactions_dedup_key'), table_name='transactions')
    op.drop_index(op.f('ix_transactions_asset_id'), table_name='transactions')
    op.drop_index('ix_transactions_asset_date', table_name='transactions')
    op.drop_table('transactions')
    op.drop_table('quotes')
    op.drop_index(op.f('ix_price_history_date'), table_name='price_history')
    op.drop_index(op.f('ix_price_history_asset_id'), table_name='price_history')
    op.drop_table('price_history')
    op.drop_index(op.f('ix_portfolio_snapshots_portfolio_id'), table_name='portfolio_snapshots')
    op.drop_index(op.f('ix_portfolio_snapshots_date'), table_name='portfolio_snapshots')
    op.drop_table('portfolio_snapshots')
    op.drop_index(op.f('ix_import_batches_status'), table_name='import_batches')
    op.drop_index(op.f('ix_import_batches_portfolio_id'), table_name='import_batches')
    op.drop_index(op.f('ix_import_batches_file_hash'), table_name='import_batches')
    op.drop_table('import_batches')
    op.drop_index(op.f('ix_goals_portfolio_id'), table_name='goals')
    op.drop_table('goals')
    op.drop_index(op.f('ix_watchlist_ticker'), table_name='watchlist')
    op.drop_table('watchlist')
    op.drop_table('portfolios')
    op.drop_index(op.f('ix_brokers_canonical_name'), table_name='brokers')
    op.drop_table('brokers')
    op.drop_index(op.f('ix_audit_logs_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_action'), table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_index(op.f('ix_assets_ticker'), table_name='assets')
    op.drop_index(op.f('ix_assets_kind'), table_name='assets')
    op.drop_table('assets')
    op.drop_table('app_settings')
