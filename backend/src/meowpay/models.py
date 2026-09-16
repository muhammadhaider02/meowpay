"""The schema.

Three tables. `cats` holds identity, auth and a materialized balance.
`transfers` is one row per movement. `entries` is the append-only double-entry
ledger, two lines per movement, always summing to zero.

On what the database enforces versus what the ledger does: the constraints here
stop an overdraft, a self-transfer, a duplicate settlement and two lines for one
cat on one movement, and they hold even if the service is wrong. The zero-sum
property is NOT a constraint. Nothing stops a hand-written INSERT adding a lone
unbalanced line. It is upheld by `meowpay.ledger` being the only writer and
asserted by a reconciliation test. Enforcing it in the database would need a
deferred constraint trigger, which is more machinery than this slice earns.

The balance is materialized rather than derived from the ledger on every read.
That is genuine derived state, and the reason to accept it is the lock: a
derived balance has nothing to lock, because SELECT ... FOR UPDATE locks rows
that exist and cannot stop a concurrent INSERT of a new entry. Making
read-sum-then-write safe without a lock target needs SERIALIZABLE and a retry
loop. A derived balance also cannot carry a CHECK constraint, since CHECK is
per-row and cannot reference an aggregate over another table, and that
constraint is the database-level backstop this exercise is about.

The price is paid in `meowpay.ledger`, which is the only writer of
both, writes them in one transaction, and is covered by a test asserting that
every cat's balance equals the sum of its entries.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from meowpay.constants import HANDLE_REGEX, JS_SAFE_INTEGER, MAX_AMOUNT, TREASURY_CAT_ID
from meowpay.db import Base

# Built from the one definition in constants.py rather than written out again,
# so the CHECK and the service's own validation cannot drift apart. This renders
# byte for byte what migration 0001 already contains, so alembic check is
# unaffected.
HANDLE_PATTERN = f"handle ~ '^{HANDLE_REGEX}$'"


class MovementKind(enum.StrEnum):
    TRANSFER = "transfer"
    DEPOSIT = "deposit"


# native_enum=False renders VARCHAR plus a CHECK. A real Postgres ENUM would
# need ALTER TYPE ADD VALUE to extend, which cannot run in the same transaction
# that adds it and which autogenerate handles badly.
KindType = Enum(
    MovementKind,
    native_enum=False,
    length=16,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_constraint=True,
    name="kind",
)


class Cat(Base):
    """A cat is both the user and the account holder.

    Humans are deliberately not modelled. A human is a funding source outside
    the system boundary, so a top-up is a deposit from the treasury rather than
    a movement between two entities we store.
    """

    __tablename__ = "cats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    handle: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    # The Supabase auth.users id this cat signs in as. NULL only for the
    # treasury, and a caller is resolved by `WHERE auth_user_id = :sub`, so that
    # row is structurally unreachable from any token: SQL NULL is never equal to
    # anything. That property holds only while the lookup is by this column. If
    # a caller is ever resolved by handle or by email it evaporates.
    #
    # Deliberately no ForeignKey to auth.users. A cat outlives its login, so
    # deleting an identity must not delete a money account, and a foreign key
    # would force every test fixture to create a real auth.users row and drag
    # GoTrue into the ledger suite. auth.users is Supabase's to change, not ours
    # to depend on.
    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True, default=None)
    balance: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The overdraft backstop. The service refuses overdrafts already, so
        # reaching this is a bug rather than a user error, and it should abort
        # loudly instead of being caught.
        #
        # The treasury is exempt because its balance is the negative of every
        # treat in circulation, so it is negative by construction.
        CheckConstraint(
            f"id = '{TREASURY_CAT_ID}'::uuid OR balance >= 0",
            name="balance_non_negative",
        ),
        # Only the sentinel row may be a system account. Without this, flipping
        # is_system on an ordinary cat would exempt it from the overdraft check
        # above, which is unlimited minting one mass-assignment bug away.
        CheckConstraint(
            f"NOT is_system OR id = '{TREASURY_CAT_ID}'::uuid",
            name="only_the_sentinel_is_system",
        ),
        # Makes "the treasury cannot be logged in as" structural rather than a
        # convention: it can never carry an identity, and every ordinary cat
        # must. The pair of this and only_the_sentinel_is_system also makes an
        # unlinked ordinary cat impossible, so there is never an
        # UPDATE cats SET auth_user_id = ... path anywhere.
        CheckConstraint("is_system = (auth_user_id IS NULL)", name="only_system_lacks_auth_user"),
        # Keeps every balance exactly representable in JavaScript. The per-movement
        # cap alone does not: enough deposits still walk a balance past 2**53 - 1,
        # and the treasury, being the negative of all circulation, gets there first.
        CheckConstraint(
            f"balance BETWEEN -{JS_SAFE_INTEGER} AND {JS_SAFE_INTEGER}",
            name="balance_is_js_safe",
        ),
        CheckConstraint("handle = lower(handle)", name="handle_is_lowercase"),
        CheckConstraint(HANDLE_PATTERN, name="handle_shape"),
    )


class Transfer(Base):
    """One row per settled movement, transfer or deposit.

    There is no status column. A movement is one transaction, so a failure rolls
    the row away and 'settled' would be the only value this could ever hold.
    """

    __tablename__ = "transfers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[MovementKind] = mapped_column(KindType)
    from_cat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cats.id", ondelete="RESTRICT")
    )
    to_cat_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cats.id", ondelete="RESTRICT"))
    amount: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    # Whoever chose the idempotency key: the sender of a transfer, the recipient
    # of a deposit. Generated by the database rather than written by the
    # service, so it cannot drift from kind and cannot be set wrongly by a
    # caller. See the unique constraint below for why this column exists.
    owner_cat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        Computed(
            "CASE WHEN kind = 'deposit' THEN to_cat_id ELSE from_cat_id END",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The idempotency gate, and the index the replay lookup reads.
        #
        # Scoped to the OWNER of the key, because uniqueness must never span a
        # trust boundary: a shared namespace would let one cat's key collide
        # with another's, and a replay could then surface someone else's
        # movement.
        #
        # Scoping to from_cat_id alone would be wrong. Every deposit has the
        # treasury as its sender, so that column is constant across all
        # deposits and the constraint would degenerate to a GLOBAL unique on
        # the key. Two cats topping up with the same client-generated key would
        # collide, and the replay would hand the second cat the first cat's
        # amount and balance. owner_cat_id is the sender for a transfer and the
        # recipient for a deposit, which is the cat that actually chose the key
        # in both cases.
        #
        # The gate itself is enough to serialize duplicates. Postgres waits on
        # a conflicting in-flight insert rather than skipping past it: the
        # speculative-insertion path blocks on the inserting transaction and
        # re-checks once it resolves, so a second caller with the same key
        # either sees the committed row or gets to insert its own. Measured on
        # Postgres 17 against this schema, where the loser waited out the
        # settlement's lock_timeout rather than returning zero rows immediately.
        #
        # Worth stating because the obvious assumption is the opposite. The
        # lock still comes first, for three other reasons: the overdraft check
        # and the balance-ceiling check both read a balance that must not move
        # under them, and consistent acquisition order is what keeps opposing
        # transfers deadlock free.
        UniqueConstraint("owner_cat_id", "idempotency_key"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(f"amount <= {MAX_AMOUNT}", name="amount_within_cap"),
        # A self-transfer cannot exist in the table even if the service is wrong.
        CheckConstraint("from_cat_id <> to_cat_id", name="parties_differ"),
        CheckConstraint("length(idempotency_key) BETWEEN 8 AND 255", name="idempotency_key_shape"),
    )


class Entry(Base):
    """One ledger line. Two per movement, always summing to zero.

    `counterparty_cat_id` and `kind` are denormalized from `transfers` so a
    cat's statement is a single index scan with no join. Denormalization is only
    dangerous where updates happen, and entries are append-only: nothing ever
    rewrites a line, so these cannot drift after insert.
    """

    __tablename__ = "entries"

    # BIGINT identity rather than UUID: entries are internal ledger lines never
    # exposed as an external identifier, so there is nothing to enumerate, and a
    # monotonic key orders the statement for free.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transfer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("transfers.id", ondelete="RESTRICT")
    )
    cat_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cats.id", ondelete="RESTRICT"))
    counterparty_cat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cats.id", ondelete="RESTRICT")
    )
    kind: Mapped[MovementKind] = mapped_column(KindType)
    # Signed. Negative debits, positive credits. Direction is the sign, so there
    # is no separate direction column that could disagree with it.
    amount: Mapped[int] = mapped_column(BigInteger)
    # The balance this line settled at. Immutable, which is what lets a replayed
    # transfer reproduce its original response without storing response bodies.
    balance_after: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Exactly one line per cat per movement, so the same cat cannot be
        # credited twice for one transfer. It does not make the ledger balance:
        # a line for a cat that is not party to the movement would still be
        # accepted. That is the ledger's job, not this constraint's. It also
        # means one leg per cat, which would need revisiting for fee or FX legs.
        UniqueConstraint("transfer_id", "cat_id"),
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        CheckConstraint("cat_id <> counterparty_cat_id", name="parties_differ"),
        # The only read path on this table: one cat's statement, newest first.
        Index("ix_entries_cat_id_id", "cat_id", text("id DESC")),
    )
