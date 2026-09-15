"""insert the treasury cat

The account every deposit is funded from. This is schema infrastructure rather
than sample data, so it belongs in a migration and not in `make seed`: the
ledger's zero-sum property depends on the row existing, migrations run in every
environment including a test database, and no test should have to run the seed
script first.

Its balance is the negative of every treat in circulation, which is why
ck_cats_balance_non_negative exempts is_system rows.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with meowpay.constants.TREASURY_CAT_ID. Migrations are pinned to
# a point in history and must not import moving application constants, so this
# is written out rather than imported. It is a fixed literal, never user input,
# so interpolating it into the statement carries no injection surface.
TREASURY_ID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    # auth_user_id stays NULL, which is what makes this row unreachable. A
    # caller is resolved by `WHERE auth_user_id = :sub`, and SQL NULL is never
    # equal to anything, so no token can ever land here whatever the auth code
    # does. That holds only while the lookup is by auth_user_id. Resolving a
    # caller by handle or by email would quietly undo it.
    op.execute(
        sa.text(
            f"""
            INSERT INTO cats (id, handle, display_name, auth_user_id, balance, is_system)
            VALUES ('{TREASURY_ID}'::uuid, 'meowpay_treasury', 'MeowPay Treasury',
                    NULL, 0, TRUE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM cats WHERE id = '{TREASURY_ID}'::uuid"))
