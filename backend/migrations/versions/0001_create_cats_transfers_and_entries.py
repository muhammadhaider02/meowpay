"""create cats transfers and entries

The whole schema in one migration: cats, the transfers that move treats between
them and the append-only double-entry ledger that records both sides of every
movement.

Every statement here is schema-unqualified on purpose. The connection arrives
with search_path already pointing at the target schema, which is how one
migration file serves the application database and the throwaway test database
without knowing which it is talking to.

Revision ID: 0001
Revises: None
Create Date: 2026-09-15 01:47:04.372074
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('cats',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('handle', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('auth_user_id', sa.Uuid(), nullable=True),
    sa.Column('balance', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('is_system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("NOT is_system OR id = '00000000-0000-0000-0000-000000000000'::uuid", name=op.f('ck_cats_only_the_sentinel_is_system')),
    sa.CheckConstraint("handle ~ '^[a-z0-9_]{3,32}$'", name=op.f('ck_cats_handle_shape')),
    sa.CheckConstraint("id = '00000000-0000-0000-0000-000000000000'::uuid OR balance >= 0", name=op.f('ck_cats_balance_non_negative')),
    sa.CheckConstraint('balance BETWEEN -9007199254740991 AND 9007199254740991', name=op.f('ck_cats_balance_is_js_safe')),
    sa.CheckConstraint('handle = lower(handle)', name=op.f('ck_cats_handle_is_lowercase')),
    sa.CheckConstraint('is_system = (auth_user_id IS NULL)', name=op.f('ck_cats_only_system_lacks_auth_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cats')),
    sa.UniqueConstraint('auth_user_id', name=op.f('uq_cats_auth_user_id')),
    sa.UniqueConstraint('handle', name=op.f('uq_cats_handle'))
    )
    op.create_table('transfers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('transfer', 'deposit', name='kind', native_enum=False, create_constraint=True, length=16), nullable=False),
    sa.Column('from_cat_id', sa.Uuid(), nullable=False),
    sa.Column('to_cat_id', sa.Uuid(), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('owner_cat_id', sa.Uuid(), sa.Computed("CASE WHEN kind = 'deposit' THEN to_cat_id ELSE from_cat_id END", persisted=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount <= 1000000000000', name=op.f('ck_transfers_amount_within_cap')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_transfers_amount_positive')),
    sa.CheckConstraint('from_cat_id <> to_cat_id', name=op.f('ck_transfers_parties_differ')),
    sa.CheckConstraint('length(idempotency_key) BETWEEN 8 AND 255', name=op.f('ck_transfers_idempotency_key_shape')),
    sa.ForeignKeyConstraint(['from_cat_id'], ['cats.id'], name=op.f('fk_transfers_from_cat_id_cats'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['to_cat_id'], ['cats.id'], name=op.f('fk_transfers_to_cat_id_cats'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_transfers')),
    sa.UniqueConstraint('owner_cat_id', 'idempotency_key', name=op.f('uq_transfers_owner_cat_id_idempotency_key'))
    )
    op.create_table('entries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('transfer_id', sa.Uuid(), nullable=False),
    sa.Column('cat_id', sa.Uuid(), nullable=False),
    sa.Column('counterparty_cat_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('transfer', 'deposit', name='kind', native_enum=False, create_constraint=True, length=16), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('balance_after', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount <> 0', name=op.f('ck_entries_amount_non_zero')),
    sa.CheckConstraint('cat_id <> counterparty_cat_id', name=op.f('ck_entries_parties_differ')),
    sa.ForeignKeyConstraint(['cat_id'], ['cats.id'], name=op.f('fk_entries_cat_id_cats'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['counterparty_cat_id'], ['cats.id'], name=op.f('fk_entries_counterparty_cat_id_cats'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'], name=op.f('fk_entries_transfer_id_transfers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_entries')),
    sa.UniqueConstraint('transfer_id', 'cat_id', name=op.f('uq_entries_transfer_id_cat_id'))
    )
    op.create_index('ix_entries_cat_id_id', 'entries', ['cat_id', sa.literal_column('id DESC')], unique=False)

    # Row level security with no policies at all, which denies everything to
    # every role except the table owner. The application connects as the owner
    # and owners bypass RLS unless FORCE is set, so the ledger is untouched.
    #
    # This is the second of two independent answers to the same problem. The
    # first is that these tables are not in `public`, which is the only schema
    # Supabase exposes through PostgREST, so there is no resource to address
    # with the publishable key that ships in the browser bundle. Either alone
    # would do. Both together mean that adding a permissive policy later, or
    # changing which schemas PostgREST exposes, does not reopen it on its own.
    #
    # Without this, `UPDATE cats SET balance = ...` from a browser is a path
    # around every invariant in meowpay.ledger.
    for table in ('cats', 'transfers', 'entries'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')

    # Belt and braces on the grants. Wrapped in a role check so this migration
    # still runs against a vanilla Postgres that has never heard of anon.
    # current_schema() rather than a literal, because the migration does not
    # know which schema it is being applied to.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                EXECUTE format(
                    'REVOKE ALL ON SCHEMA %I FROM anon, authenticated',
                    current_schema());
                EXECUTE format(
                    'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM anon, authenticated',
                    current_schema());
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                    'REVOKE ALL ON TABLES FROM anon, authenticated',
                    current_schema());
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index('ix_entries_cat_id_id', table_name='entries')
    op.drop_table('entries')
    op.drop_table('transfers')
    op.drop_table('cats')
