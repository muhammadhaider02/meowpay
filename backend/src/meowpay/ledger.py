"""The settlement path.

The only writer of `cats.balance`, `transfers` and `entries`. Everything the
schema deliberately does not enforce rests on that being true: the ledger summing
to zero, deposits originating at the treasury, and the idempotency claim being
safe against a concurrent duplicate.

One movement is one transaction. `transfer()` and `deposit()` both funnel into
`_settle()`, which locks both parties, claims the key, moves the balances and
writes the two ledger lines. They differ only in who the sender is.

The order of steps inside `_settle()` is not arbitrary. Three of them would be
silent correctness bugs if moved, and each says so where it stands.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from meowpay.constants import JS_SAFE_INTEGER, MAX_AMOUNT, TREASURY_CAT_ID
from meowpay.errors import (
    AmountOutOfRangeError,
    BalanceLimitExceededError,
    IdempotencyKeyInvalidError,
    IdempotencyKeyReusedError,
    InsufficientFundsError,
    LedgerBusyError,
    LedgerInvariantError,
    RecipientNotFoundError,
    SelfTransferError,
    SenderNotFoundError,
    TreasuryIsNotAPartyError,
)
from meowpay.models import Cat, Entry, MovementKind, Transfer

logger = logging.getLogger(__name__)

# Mirrors ck_transfers_idempotency_key_shape. Postgres length() counts characters
# and so does Python len(), so the bounds agree on every input.
IDEMPOTENCY_KEY_MIN_LENGTH = 8
IDEMPOTENCY_KEY_MAX_LENGTH = 255

# Named rather than inferred from a column list. Inference would silently pick
# whichever unique index happens to cover the pair; naming it ties this statement
# to the constraint models.py calls LOAD BEARING, so one grep finds all three
# places. A test asserts the name still exists on the model.
IDEMPOTENCY_CONSTRAINT = "uq_transfers_owner_cat_id_idempotency_key"

# The settlement transaction's own timeouts, applied by _apply_transaction_timeouts
# as the first statement inside it. NOT on the connection: see that function.
#
# Eight seconds is derived rather than picked. A settle is about seven round trips
# and the row locks are held from the locking SELECT through COMMIT, so against a
# remote database at ~50ms RTT each holder keeps them for roughly 300ms. Postgres
# queues waiters, so with eight threads contending on one pair of rows the last one
# waits about 7 x 300ms = 2.1s before jitter. A three second budget passes on a good
# run and fails on a bad one, which is the definition of a flaky test. This is still
# a defensible production ceiling: a user on a contended account waits at most eight
# seconds and then gets a retryable 503 rather than a hang.
LOCK_TIMEOUT = "8s"
# Must exceed LOCK_TIMEOUT, or a legitimate lock wait is killed as a slow statement
# and surfaces as 57014 instead of the retryable 55P03.
SETTLE_STATEMENT_TIMEOUT = "15s"
SETTLE_IDLE_TIMEOUT = "15s"

# Raised when LOCK_TIMEOUT expires. Translated to LedgerBusyError, a retryable 503.
LOCK_NOT_AVAILABLE = "55P03"


@dataclass(frozen=True, slots=True)
class Settlement:
    """What a settled movement reports back.

    Frozen and made only of plain values. No ORM instance escapes the ledger, so
    reading this after the session closes cannot trigger a lazy load.
    """

    transfer_id: uuid.UUID
    kind: MovementKind
    from_cat_id: uuid.UUID
    to_cat_id: uuid.UUID
    amount: int
    idempotency_key: str
    owner_cat_id: uuid.UUID

    # The balance of the cat that made the call, as it stood the instant this
    # movement settled: the sender for a transfer, the recipient for a deposit,
    # which is owner_cat_id in both cases.
    #
    # The name is doing real work. On a replay this is the HISTORICAL value read
    # back out of the ledger line, not the balance now. A client repeating a
    # week-old request gets a week-old number, and a field called `balance` would
    # make that look like GET /me contradicting itself.
    owner_balance_after: int

    created_at: datetime

    # True when this call found the key already settled and reproduced the
    # original answer without moving anything.
    replayed: bool


class Ledger:
    """The transfer service.

    Takes a sessionmaker, never a Session. A movement is exactly one transaction
    and the ledger has to own its boundaries: the row locks it takes are released
    by COMMIT, and the idempotency argument depends on the losing duplicate seeing
    only committed state. A caller handing in a live Session could commit around
    us and drop the locks halfway through.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    # -- public surface ----------------------------------------------------

    def transfer(
        self,
        *,
        from_cat_id: uuid.UUID,
        to_cat_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
    ) -> Settlement:
        """Move treats from one cat to another.

        `from_cat_id` comes from the auth context and never from a request body.
        """
        # No constraint stops the treasury being a party to a transfer, so this is
        # the only guard. It lives here rather than in _settle because it is true
        # of transfers and false of deposits, and _settle stays free of per-kind
        # branches on purpose.
        if TREASURY_CAT_ID in (from_cat_id, to_cat_id):
            raise TreasuryIsNotAPartyError()
        return self._settle(
            MovementKind.TRANSFER,
            from_cat_id=from_cat_id,
            to_cat_id=to_cat_id,
            amount=amount,
            idempotency_key=idempotency_key,
        )

    def deposit(self, *, to_cat_id: uuid.UUID, amount: int, idempotency_key: str) -> Settlement:
        """Top a cat up from the treasury.

        The human on the other end is outside the system boundary, so there is no
        second party to model. The treasury going negative by exactly this amount
        is what keeps the ledger summing to zero.
        """
        if to_cat_id == TREASURY_CAT_ID:
            raise TreasuryIsNotAPartyError()
        return self._settle(
            MovementKind.DEPOSIT,
            from_cat_id=TREASURY_CAT_ID,
            to_cat_id=to_cat_id,
            amount=amount,
            idempotency_key=idempotency_key,
        )

    # -- the settlement path -----------------------------------------------

    def _settle(
        self,
        kind: MovementKind,
        *,
        from_cat_id: uuid.UUID,
        to_cat_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
    ) -> Settlement:
        # Step 1. Pure validation, before a connection is checked out.
        #
        # Every check here pre-empts a CHECK constraint that would otherwise
        # arrive as an IntegrityError and a 500. Doing it first means a malformed
        # request never occupies a pool slot and never takes a row lock.
        _check_amount(amount)
        check_idempotency_key(idempotency_key)
        if from_cat_id == to_cat_id:
            raise SelfTransferError()

        # The Python twin of the generated column, character for character the
        # same expression as models.Transfer.owner_cat_id. A test asserts the two
        # agree on a real row rather than trusting that they look alike.
        owner_cat_id = to_cat_id if kind is MovementKind.DEPOSIT else from_cat_id

        # The claim below is only safe because the uniqueness scope sits inside
        # the lock set. Stated as code so that narrowing _lock_parties fails a
        # test rather than quietly losing the guarantee.
        if owner_cat_id not in (from_cat_id, to_cat_id):
            raise LedgerInvariantError("owner_cat_id is not a party to its own movement")

        try:
            with self._sessions.begin() as session:
                # Step 1c. Scope the timeouts to this transaction, before anything
                # takes a lock. This is the first statement on purpose and a test
                # asserts that it is.
                _apply_transaction_timeouts(session)

                # Step 2. Lock both parties, lowest id first, before anything is
                # written. Three things need this, and the idempotency claim is
                # NOT one of them: the overdraft check below reads a balance that
                # must not move under it, the balance-ceiling check does the same,
                # and consistent acquisition order is what keeps opposing
                # transfers deadlock free. Narrowing this set breaks all three.
                parties = self._lock_parties(session, from_cat_id, to_cat_id)
                sender = parties.get(from_cat_id)
                recipient = parties.get(to_cat_id)

                # Step 3. Existence, from the lock result, BEFORE the claim.
                #
                # Not politeness. The claim has two foreign keys to cats, and a
                # missing party would make it raise ForeignKeyViolation, which
                # aborts the transaction and makes the replay read impossible.
                # That is exactly the failure ON CONFLICT DO NOTHING was chosen to
                # avoid, so checking here is what keeps the choice worth making.
                if sender is None:
                    raise SenderNotFoundError(from_cat_id)
                if recipient is None:
                    raise RecipientNotFoundError(f"id {to_cat_id}")

                # Step 4. Claim the key.
                #
                # ON CONFLICT DO NOTHING RETURNING rather than catching
                # IntegrityError: a unique violation puts the transaction into
                # 25P02 and every later statement, including the replay SELECT,
                # fails until a ROLLBACK TO SAVEPOINT. ON CONFLICT never aborts.
                #
                # It also waits rather than skipping. Postgres blocks on a
                # conflicting in-flight insert and re-checks once that
                # transaction resolves, so a concurrent duplicate either sees the
                # committed row here or wins the claim itself. Zero rows back
                # therefore means the key is genuinely settled, not merely
                # in flight somewhere.
                #
                # owner_cat_id is ABSENT ON PURPOSE. It is GENERATED ALWAYS and
                # Postgres rejects any INSERT that names it, whatever the value
                # (SQLSTATE 428C9). SQLAlchemy does NOT stop you: Computed lands on
                # server_default, so passing it compiles cleanly and fails at
                # runtime. Never build these values from a splatted dict.
                claimed = session.execute(
                    pg_insert(Transfer)
                    .values(
                        id=uuid.uuid4(),
                        kind=kind,
                        from_cat_id=from_cat_id,
                        to_cat_id=to_cat_id,
                        amount=amount,
                        idempotency_key=idempotency_key,
                    )
                    .on_conflict_do_nothing(constraint=IDEMPOTENCY_CONSTRAINT)
                    .returning(Transfer.id, Transfer.created_at)
                ).first()

                # Step 5. A conflict means this key already settled. Reproduce the
                # answer, BEFORE the funds check. A client retrying a transfer
                # that already went through must get its original result back even
                # if it has since spent the money and could no longer afford it.
                if claimed is None:
                    return self._replay(
                        session,
                        kind,
                        owner_cat_id=owner_cat_id,
                        from_cat_id=from_cat_id,
                        to_cat_id=to_cat_id,
                        amount=amount,
                        idempotency_key=idempotency_key,
                    )

                transfer_id, created_at = claimed

                # Step 6. Business validation, on values read under the lock.
                new_sender_balance = sender.balance - amount
                new_recipient_balance = recipient.balance + amount

                # Mirrors ck_cats_balance_non_negative exactly, including keying
                # the exemption on the sentinel id rather than on is_system. If
                # the two predicates diverge the database wins and it is a 500, so
                # they are written the same way deliberately.
                if new_sender_balance < 0 and from_cat_id != TREASURY_CAT_ID:
                    raise InsufficientFundsError(balance=sender.balance, amount=amount)

                _check_js_safe(new_sender_balance)
                _check_js_safe(new_recipient_balance)

                # Step 7. Move the balances.
                #
                # Arithmetic on the SQL side, with the new value coming back from
                # RETURNING rather than being recomputed in Python, so what lands
                # in entries.balance_after is provably the value the row now holds.
                # The reconciliation test then has one fewer way to be satisfied by
                # two copies of the same mistake.
                #
                # The ORM Cat instances are never mutated: assigning to
                # sender.balance would make the next execute() autoflush an UPDATE
                # of its own, out of order.
                debited = session.execute(
                    update(Cat)
                    .where(Cat.id == from_cat_id)
                    .values(balance=Cat.balance - amount)
                    .returning(Cat.balance)
                ).scalar_one()
                credited = session.execute(
                    update(Cat)
                    .where(Cat.id == to_cat_id)
                    .values(balance=Cat.balance + amount)
                    .returning(Cat.balance)
                ).scalar_one()

                # Step 8. Two ledger lines, summing to zero, in one statement, so
                # there is no window in which the ledger holds half a movement.
                session.execute(
                    insert(Entry).values(
                        [
                            {
                                "transfer_id": transfer_id,
                                "cat_id": from_cat_id,
                                "counterparty_cat_id": to_cat_id,
                                "kind": kind,
                                "amount": -amount,
                                "balance_after": debited,
                            },
                            {
                                "transfer_id": transfer_id,
                                "cat_id": to_cat_id,
                                "counterparty_cat_id": from_cat_id,
                                "kind": kind,
                                "amount": amount,
                                "balance_after": credited,
                            },
                        ]
                    )
                )

                settlement = Settlement(
                    transfer_id=transfer_id,
                    kind=kind,
                    from_cat_id=from_cat_id,
                    to_cat_id=to_cat_id,
                    amount=amount,
                    idempotency_key=idempotency_key,
                    owner_cat_id=owner_cat_id,
                    owner_balance_after=(debited if owner_cat_id == from_cat_id else credited),
                    created_at=created_at,
                    replayed=False,
                )

            # Logged AFTER the with block, so after COMMIT. Inside it, a
            # commit-time failure would leave an audit line asserting a
            # settlement that never happened, which is the worst possible lie for
            # a money log.
            logger.info(
                "settled %s transfer_id=%s amount=%s from=%s to=%s",
                kind.value,
                settlement.transfer_id,
                amount,
                from_cat_id,
                to_cat_id,
            )
            return settlement

        except OperationalError as exc:
            # A lock wait that exceeds lock_timeout is retryable, and 503 says so
            # where the 500 it would otherwise become says the opposite. A
            # deadlock (40P01) is deliberately NOT translated: the sorted lock
            # order means one would be a bug, and it should be loud.
            if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                logger.warning("lock timeout settling %s for %s", kind.value, owner_cat_id)
                raise LedgerBusyError() from exc
            raise

    def _replay(
        self,
        session: Session,
        kind: MovementKind,
        *,
        owner_cat_id: uuid.UUID,
        from_cat_id: uuid.UUID,
        to_cat_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
    ) -> Settlement:
        """Reproduce the original answer for a key that has already settled.

        Looked up by (owner_cat_id, idempotency_key), which is exactly the unique
        constraint, so it is one index lookup. The join predicate is exactly
        uq_entries_transfer_id_cat_id, so the owner's ledger line is one more.
        Reading balance_after out of the immutable entry is what lets a replay
        reproduce the original response without anyone storing response bodies.

        This lookup is scoped to the caller by construction: owner_cat_id is the
        authenticated cat in both directions, so the index key contains the
        caller's own id and this query cannot return anybody else's movement.
        """
        row = session.execute(
            select(Transfer, Entry.balance_after)
            .join(
                Entry,
                and_(Entry.transfer_id == Transfer.id, Entry.cat_id == owner_cat_id),
            )
            .where(
                Transfer.owner_cat_id == owner_cat_id,
                Transfer.idempotency_key == idempotency_key,
            )
        ).first()

        if row is None:
            # Should be unreachable. The claim conflicted, which means Postgres
            # saw a committed row, and READ COMMITTED gives this statement a
            # fresh snapshot of it. Kept as a guard rather than an assertion
            # because the alternative on a wrong day is settling the same
            # movement twice, which is the bug this whole mechanism exists to
            # prevent. Never retry the insert, never fall through.
            raise LedgerInvariantError(
                f"claim conflicted but no row for owner={owner_cat_id}: "
                "the lock no longer covers the unique scope"
            )

        original, owner_balance_after = row

        # Same owner, same key, different movement. Tenancy is already closed by
        # the lookup key above, so this is not about who owns the row. It is about
        # a client reusing a key for a genuinely different payment, which without
        # this check would get a cheerful 200 naming the wrong recipient.
        #
        # `kind` is redundant today and kept deliberately. Owner scoping merges a
        # cat's deposits and its transfers into ONE key namespace, so the same
        # cat reusing a key across both lands here. `from_cat_id` already catches
        # that, because a deposit's sender is always the treasury and transfer()
        # forbids the treasury as a party. Comparing `kind` too means the tuple
        # is a complete description of the movement, so it stays correct if
        # either of those two facts ever stops being true.
        if (
            original.kind,
            original.from_cat_id,
            original.to_cat_id,
            original.amount,
        ) != (kind, from_cat_id, to_cat_id, amount):
            logger.warning("idempotency key reused with different parameters by %s", owner_cat_id)
            raise IdempotencyKeyReusedError()

        return Settlement(
            transfer_id=original.id,
            kind=original.kind,
            from_cat_id=original.from_cat_id,
            to_cat_id=original.to_cat_id,
            amount=original.amount,
            idempotency_key=original.idempotency_key,
            owner_cat_id=original.owner_cat_id,
            owner_balance_after=owner_balance_after,
            created_at=original.created_at,
            replayed=True,
        )

    @staticmethod
    def _lock_parties(
        session: Session, first: uuid.UUID, second: uuid.UUID
    ) -> dict[uuid.UUID, Cat]:
        """Lock both parties FOR NO KEY UPDATE, lowest id first.

        ORDER BY is the deadlock rule, and it is the whole of it. Two
        simultaneous movements in opposite directions touch the same two rows,
        and without a global acquisition order each would sit holding the row the
        other wants. The treasury's all-zeros id sorts first, so it can never sit
        mid-cycle in a wait graph.

        It is easy to assume sorting the ids in Python does this. It does not:
        `id IN (...)` compiles to `id = ANY(array)` and array order has no
        bearing on lock order. What decides it is the plan shape. EXPLAIN shows
        LockRows sitting ABOVE Sort with the ORDER BY, and LockRows directly over
        a bitmap heap scan without it, which takes locks in physical page order.
        Drop the ORDER BY and the discipline silently becomes "whatever order the
        heap happens to be in", which is stable enough to pass a test and
        unstable enough to deadlock after a VACUUM FULL.

        populate_existing=True is belt and braces. If either Cat were already in
        this session's identity map, SQLAlchemy would return the existing
        instance without refreshing its attributes: the lock would still be
        taken, so nothing would look wrong, but `cat.balance` would be the value
        from before it, and the overdraft check would read stale data while
        holding the lock that exists to make it fresh. Today `_settle` opens a
        fresh session per movement so the map is always empty and this cannot
        fire, which is exactly why it is cheap to keep against the day someone
        passes in a warm session.

        Rows that do not exist are simply absent from the result, so this
        statement doubles as the existence check.
        """
        rows = session.scalars(
            select(Cat)
            .where(Cat.id.in_({first, second}))
            .order_by(Cat.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        ).all()
        return {cat.id: cat for cat in rows}


# -- transaction scoped settings -------------------------------------------


def _apply_transaction_timeouts(session: Session) -> None:
    """Bound this transaction's lock wait, and nothing else's.

    Do not move these onto the connection, whether through libpq's startup
    `options` or a pool event. Through a pooler, startup options silently do not
    arrive: Supavisor parses the startup packet for its own tenant routing and
    does not forward arbitrary settings to the backend it owns. Measured against
    the real database, connecting with `-c lock_timeout=3s` and then asking the
    server returns `0`. The connection succeeds and nothing errors, so the failure
    is invisible until the system is under contention, at which point a contended
    transfer hangs until the server's own statement_timeout and fails as 57014
    rather than the retryable 55P03 this module translates.

    Transaction scope is the right home for them anyway, independently of the
    pooler. `idle_in_transaction_session_timeout` set on the connection would
    clamp every session in the process, including test scaffolding that
    legitimately holds an open transaction while it waits on a barrier. Here it
    constrains only the transaction that actually holds row locks.

    One statement, so it costs one round trip rather than three. `set_config` with
    is_local true is `SET LOCAL` with a bindable value, which keeps the timeouts as
    named constants instead of interpolated SQL.
    """
    session.execute(
        text(
            "SELECT set_config('lock_timeout', :lock, true),"
            "       set_config('statement_timeout', :statement, true),"
            "       set_config('idle_in_transaction_session_timeout', :idle, true)"
        ),
        {
            "lock": LOCK_TIMEOUT,
            "statement": SETTLE_STATEMENT_TIMEOUT,
            "idle": SETTLE_IDLE_TIMEOUT,
        },
    )


# -- pure validation, shared by both kinds ---------------------------------


def _check_amount(amount: int) -> None:
    """Mirrors ck_transfers_amount_positive and ck_transfers_amount_within_cap.

    bool is excluded explicitly: it is a subclass of int, so True would otherwise
    be a perfectly valid transfer of one treat.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise AmountOutOfRangeError(amount)
    if not 0 < amount <= MAX_AMOUNT:
        raise AmountOutOfRangeError(amount)


def check_idempotency_key(key: str) -> None:
    """Mirrors ck_transfers_idempotency_key_shape plus the varchar(255) cast.

    The lower bound of 8 exists only in the CHECK, so without this a seven
    character key is a 500. The NUL check is separate: psycopg refuses to send a
    string containing one and raises ValueError before the database sees it.
    """
    if not isinstance(key, str):
        raise IdempotencyKeyInvalidError("it must be a string")
    if "\x00" in key:
        raise IdempotencyKeyInvalidError("it cannot contain a null character")
    try:
        # A lone surrogate survives json.loads and pydantic's str and then raises
        # UnicodeEncodeError inside the driver, which would be a 500 rather than
        # a rejection. Same class of problem as the NUL above: caller-supplied
        # text the database can never be sent.
        key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IdempotencyKeyInvalidError("it must be valid text") from exc
    if not IDEMPOTENCY_KEY_MIN_LENGTH <= len(key) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        raise IdempotencyKeyInvalidError(
            f"it must be between {IDEMPOTENCY_KEY_MIN_LENGTH} and "
            f"{IDEMPOTENCY_KEY_MAX_LENGTH} characters, got {len(key)}"
        )


def _check_js_safe(balance: int) -> None:
    """Mirrors ck_cats_balance_is_js_safe, which is bounded both ways."""
    if not -JS_SAFE_INTEGER <= balance <= JS_SAFE_INTEGER:
        raise BalanceLimitExceededError()
