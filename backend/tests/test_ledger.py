"""The settlement path, single threaded.

The concurrent cases live in test_ledger_concurrency.py, because they need real
connections and real commits rather than the rolled-back one these use.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Connection, event, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from meowpay.constants import JS_SAFE_INTEGER, MAX_AMOUNT, TREASURY_CAT_ID
from meowpay.errors import (
    AmountOutOfRangeError,
    BalanceLimitExceededError,
    IdempotencyKeyInvalidError,
    IdempotencyKeyReusedError,
    InsufficientFundsError,
    RecipientNotFoundError,
    SelfTransferError,
    TreasuryIsNotAPartyError,
)
from meowpay.ledger import IDEMPOTENCY_CONSTRAINT, Ledger
from meowpay.models import Cat, Entry, MovementKind, Transfer

pytestmark = pytest.mark.db

MakeCat = Callable[..., uuid.UUID]


def _balance(sessions: sessionmaker[Session], cat_id: uuid.UUID) -> int:
    with sessions() as session:
        return session.scalar(select(Cat.balance).where(Cat.id == cat_id)) or 0


def _counts(sessions: sessionmaker[Session]) -> tuple[int, int]:
    with sessions() as session:
        transfers = session.scalar(select(func.count()).select_from(Transfer)) or 0
        entries = session.scalar(select(func.count()).select_from(Entry)) or 0
    return transfers, entries


# -- the happy paths -------------------------------------------------------


def test_a_transfer_moves_treats_and_writes_two_balanced_lines(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    alice = make_cat(balance=100)
    bob = make_cat(balance=5)

    result = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=30, idempotency_key="transfer-001"
    )

    assert _balance(sessions_factory, alice) == 70
    assert _balance(sessions_factory, bob) == 35
    assert result.owner_balance_after == 70
    assert result.replayed is False

    with sessions_factory() as session:
        lines = session.scalars(select(Entry).where(Entry.transfer_id == result.transfer_id)).all()
    assert len(lines) == 2
    assert sum(line.amount for line in lines) == 0
    by_cat = {line.cat_id: line for line in lines}
    assert by_cat[alice].amount == -30
    assert by_cat[alice].balance_after == 70
    assert by_cat[alice].counterparty_cat_id == bob
    assert by_cat[bob].amount == 30
    assert by_cat[bob].balance_after == 35
    assert all(line.kind is MovementKind.TRANSFER for line in lines)


def test_a_deposit_credits_the_cat_and_debits_the_treasury(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    cat = make_cat()
    before = _balance(sessions_factory, TREASURY_CAT_ID)

    result = ledger.deposit(to_cat_id=cat, amount=500, idempotency_key="deposit-001")

    assert _balance(sessions_factory, cat) == 500
    # Treats are moved off the treasury, never conjured, which is what keeps the
    # ledger summing to zero globally.
    assert _balance(sessions_factory, TREASURY_CAT_ID) == before - 500
    assert result.kind is MovementKind.DEPOSIT
    assert result.owner_balance_after == 500


def test_the_stored_owner_matches_the_one_the_ledger_computed(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """The Python twin of the generated column must not drift from it.

    `owner_cat_id` is computed by Postgres from `kind`, and the ledger computes
    the same value in Python to scope its replay lookup. This reads the stored
    value back and compares, rather than trusting that the two expressions look
    alike.
    """
    alice = make_cat(balance=50)
    bob = make_cat()

    sent = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="owner-check-1"
    )
    topped_up = ledger.deposit(to_cat_id=bob, amount=10, idempotency_key="owner-check-2")

    with sessions_factory() as session:
        stored: dict[uuid.UUID, uuid.UUID] = {
            row.id: row.owner_cat_id
            for row in session.execute(select(Transfer.id, Transfer.owner_cat_id))
        }

    assert stored[sent.transfer_id] == sent.owner_cat_id == alice  # sender
    assert stored[topped_up.transfer_id] == topped_up.owner_cat_id == bob  # recipient


# -- idempotency -----------------------------------------------------------


def test_replaying_settles_nothing_and_returns_the_original(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    alice = make_cat(balance=100)
    bob = make_cat()

    first = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=40, idempotency_key="replay-me-1"
    )
    second = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=40, idempotency_key="replay-me-1"
    )

    assert second.transfer_id == first.transfer_id
    assert second.created_at == first.created_at
    assert second.owner_balance_after == first.owner_balance_after
    assert second.replayed is True
    assert _balance(sessions_factory, alice) == 60
    assert _counts(sessions_factory) == (1, 2)


def test_a_replay_returns_the_balance_as_it_was_not_as_it_is(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """This is why the field is `owner_balance_after` and not `balance`."""
    alice = make_cat(balance=100)
    bob = make_cat()

    first = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="historical-1"
    )
    ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=50, idempotency_key="moves-things-on")
    replay = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="historical-1"
    )

    assert replay.owner_balance_after == first.owner_balance_after == 90
    assert _balance(sessions_factory, alice) == 40  # the live balance has moved on


def test_a_replay_after_the_money_is_gone_still_returns_the_original(
    ledger: Ledger, make_cat: MakeCat
) -> None:
    """Proves the idempotency gate sits before the funds check."""
    alice = make_cat(balance=100)
    bob = make_cat()

    original = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=100, idempotency_key="spent-it-all"
    )
    replay = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=100, idempotency_key="spent-it-all"
    )

    assert replay.transfer_id == original.transfer_id
    assert replay.replayed is True


@pytest.mark.parametrize("field", ["amount", "recipient"])
def test_the_same_key_for_a_different_movement_is_a_conflict(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session], field: str
) -> None:
    alice = make_cat(balance=100)
    bob = make_cat()
    carol = make_cat()
    ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="reused-key-1")

    with pytest.raises(IdempotencyKeyReusedError):
        ledger.transfer(
            from_cat_id=alice,
            to_cat_id=carol if field == "recipient" else bob,
            amount=99 if field == "amount" else 10,
            idempotency_key="reused-key-1",
        )

    assert _counts(sessions_factory) == (1, 2)


def test_a_cat_cannot_reuse_one_key_for_a_deposit_and_a_transfer(
    ledger: Ledger, make_cat: MakeCat
) -> None:
    """The hazard owner scoping introduces.

    A deposit's owner is its recipient and a transfer's owner is its sender, so
    for one cat those are the same value and both live in one key namespace.
    Without `kind` in the replay comparison, this cat would be handed its own
    deposit back as a successful transfer.
    """
    alice = make_cat()
    bob = make_cat()
    ledger.deposit(to_cat_id=alice, amount=100, idempotency_key="shared-key-1")

    with pytest.raises(IdempotencyKeyReusedError):
        ledger.transfer(
            from_cat_id=alice, to_cat_id=bob, amount=100, idempotency_key="shared-key-1"
        )


def test_two_cats_may_use_the_same_key_to_send(ledger: Ledger, make_cat: MakeCat) -> None:
    alice = make_cat(balance=100)
    bob = make_cat(balance=100)
    carol = make_cat()

    first = ledger.transfer(
        from_cat_id=alice, to_cat_id=carol, amount=10, idempotency_key="shared-key-2"
    )
    second = ledger.transfer(
        from_cat_id=bob, to_cat_id=carol, amount=20, idempotency_key="shared-key-2"
    )

    assert first.transfer_id != second.transfer_id


def test_two_cats_may_use_the_same_key_to_top_up(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """The regression the owner_cat_id change exists to prevent.

    Under sender scoping both of these would carry the treasury as sender, so the
    constraint would collide and the second cat would be handed the first's
    deposit. Different amounts, so a wrong replay shows up in a balance and not
    only in an id.
    """
    alice = make_cat()
    bob = make_cat()

    ledger.deposit(to_cat_id=alice, amount=500, idempotency_key="topup-0001")
    ledger.deposit(to_cat_id=bob, amount=700, idempotency_key="topup-0001")

    assert _balance(sessions_factory, alice) == 500
    assert _balance(sessions_factory, bob) == 700


# -- rejections ------------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, MAX_AMOUNT + 1, True])
def test_an_amount_outside_the_allowed_range_is_refused(
    ledger: Ledger, make_cat: MakeCat, amount: object
) -> None:
    # True is in there because bool subclasses int, so without an explicit check
    # it would be a perfectly valid transfer of one treat.
    alice = make_cat(balance=100)
    bob = make_cat()

    with pytest.raises(AmountOutOfRangeError):
        ledger.transfer(
            from_cat_id=alice,
            to_cat_id=bob,
            amount=amount,  # type: ignore[arg-type]
            idempotency_key="bad-amount-1",
        )


@pytest.mark.parametrize("key", ["short", "x" * 256, "has\x00null"])
def test_a_malformed_idempotency_key_is_refused_before_anything_is_written(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session], key: str
) -> None:
    # Asserts the service rejection, not the constraint. The lower bound of 8
    # exists only in the CHECK, so without the pre-emption this would be a 500.
    alice = make_cat(balance=100)
    bob = make_cat()

    with pytest.raises(IdempotencyKeyInvalidError):
        ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key=key)

    assert _counts(sessions_factory) == (0, 0)


def test_a_self_transfer_is_refused(ledger: Ledger, make_cat: MakeCat) -> None:
    alice = make_cat(balance=100)

    with pytest.raises(SelfTransferError):
        ledger.transfer(from_cat_id=alice, to_cat_id=alice, amount=10, idempotency_key="self-001")


@pytest.mark.parametrize("direction", ["to", "from"])
def test_the_treasury_cannot_be_a_party_to_a_transfer(
    ledger: Ledger, make_cat: MakeCat, direction: str
) -> None:
    # Nothing in the schema stops this, so the service is the only guard. A
    # transfer to the treasury would burn treats, one from it would be minting.
    cat = make_cat(balance=100)
    parties = (cat, TREASURY_CAT_ID) if direction == "to" else (TREASURY_CAT_ID, cat)

    with pytest.raises(TreasuryIsNotAPartyError):
        ledger.transfer(
            from_cat_id=parties[0],
            to_cat_id=parties[1],
            amount=10,
            idempotency_key="treasury-001",
        )


def test_an_unknown_recipient_is_refused(ledger: Ledger, make_cat: MakeCat) -> None:
    alice = make_cat(balance=100)

    with pytest.raises(RecipientNotFoundError):
        ledger.transfer(
            from_cat_id=alice,
            to_cat_id=uuid.uuid4(),
            amount=10,
            idempotency_key="ghost-cat-1",
        )


def test_an_overdraft_is_refused_by_the_service(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    # The database backstop is covered in test_schema.py. This is the service
    # refusing first, which is what should actually happen.
    alice = make_cat(balance=10)
    bob = make_cat()

    with pytest.raises(InsufficientFundsError):
        ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=11, idempotency_key="too-poor-1")

    assert _balance(sessions_factory, alice) == 10
    assert _counts(sessions_factory) == (0, 0)


def test_a_deposit_that_would_overflow_the_recipient_is_refused(
    ledger: Ledger, make_cat: MakeCat
) -> None:
    cat = make_cat(balance=JS_SAFE_INTEGER - 5)

    with pytest.raises(BalanceLimitExceededError):
        ledger.deposit(to_cat_id=cat, amount=100, idempotency_key="too-rich-1")


def test_a_deposit_that_would_underflow_the_treasury_is_refused(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """The sender side of the same check, which the recipient case never reaches.

    The treasury is the negative of everything in circulation, so in a real
    system it hits the floor long before any cat hits the ceiling.
    """
    cat = make_cat()
    with sessions_factory.begin() as session:
        session.execute(
            update(Cat).where(Cat.id == TREASURY_CAT_ID).values(balance=-JS_SAFE_INTEGER + 5)
        )

    with pytest.raises(BalanceLimitExceededError):
        ledger.deposit(to_cat_id=cat, amount=100, idempotency_key="treasury-floor-1")


def test_a_replay_cannot_return_another_cats_movement(ledger: Ledger, make_cat: MakeCat) -> None:
    """The replay lookup must be scoped by owner, not by key alone.

    Both cats use the same key, so both rows exist, and alice replaying must get
    her own row back. Nothing else in the suite reaches _replay with a second cat
    holding the same key.

    Note this asserts the property, not one particular line. Scoping is enforced
    twice over: the WHERE filters on owner_cat_id, and the join predicate
    Entry.cat_id == owner_cat_id independently excludes any transfer the owner is
    not party to. Deleting either one alone leaves the behaviour correct, which is
    the point of having both.
    """
    alice = make_cat()
    bob = make_cat()
    ledger.deposit(to_cat_id=alice, amount=500, idempotency_key="shared-topup")
    ledger.deposit(to_cat_id=bob, amount=700, idempotency_key="shared-topup")

    replay = ledger.deposit(to_cat_id=alice, amount=500, idempotency_key="shared-topup")

    assert replay.replayed is True
    assert replay.owner_cat_id == alice
    assert replay.amount == 500


def test_a_refused_transfer_does_not_consume_its_key(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """A deliberate choice, and the opposite of Stripe.

    The claim rolls back with everything else, so a retry after funding succeeds
    rather than replaying the failure.
    """
    alice = make_cat(balance=10)
    bob = make_cat()
    with pytest.raises(InsufficientFundsError):
        ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=50, idempotency_key="retry-me-1")

    ledger.deposit(to_cat_id=alice, amount=100, idempotency_key="funding-001")
    result = ledger.transfer(
        from_cat_id=alice, to_cat_id=bob, amount=50, idempotency_key="retry-me-1"
    )

    assert result.replayed is False
    assert _balance(sessions_factory, bob) == 50


# -- invariants ------------------------------------------------------------


def test_every_balance_equals_the_sum_of_its_entries(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """The reconciliation the schema deliberately does not enforce.

    Nothing stops a hand-written INSERT unbalancing the ledger, so this test is
    what actually holds the zero-sum property. It is the substitute for the
    deferred constraint trigger this slice chose not to build.
    """
    cats = [make_cat() for _ in range(4)]
    for index, cat in enumerate(cats):
        ledger.deposit(to_cat_id=cat, amount=1000, idempotency_key=f"recon-fund-{index}")
    for index in range(12):
        sender = cats[index % len(cats)]
        recipient = cats[(index + 1) % len(cats)]
        ledger.transfer(
            from_cat_id=sender,
            to_cat_id=recipient,
            amount=10 + index,
            idempotency_key=f"recon-move-{index:04d}",
        )

    with sessions_factory() as session:
        drift = session.execute(
            select(Cat.id, Cat.balance - func.coalesce(func.sum(Entry.amount), 0))
            .outerjoin(Entry, Entry.cat_id == Cat.id)
            .group_by(Cat.id, Cat.balance)
        ).all()
        ledger_total = session.scalar(select(func.coalesce(func.sum(Entry.amount), 0)))
        balance_total = session.scalar(select(func.coalesce(func.sum(Cat.balance), 0)))

    assert all(difference == 0 for _, difference in drift)
    assert ledger_total == 0
    assert balance_total == 0


def test_the_idempotency_constraint_name_still_exists(ledger: Ledger) -> None:
    """IDEMPOTENCY_CONSTRAINT is a string, so mypy cannot catch a rename."""
    names = {constraint.name for constraint in Transfer.__table__.constraints}  # type: ignore[attr-defined]
    assert IDEMPOTENCY_CONSTRAINT in names


def test_the_locking_select_orders_by_id_and_takes_the_weak_lock(
    ledger: Ledger, make_cat: MakeCat, connection: Connection
) -> None:
    """Guards the two properties of the lock statement that nothing else does.

    `ORDER BY` is the deadlock rule: the plan puts LockRows above Sort, so it is
    what decides acquisition order. Without it the plan locks in physical page
    order. Sorting the ids in Python looks like it would do this and does not,
    because `id IN (...)` compiles to `id = ANY(array)` and array order is not
    lock order.

    `FOR NO KEY UPDATE` rather than `FOR UPDATE` is what stops a lock upgrade
    deadlocking against the `FOR KEY SHARE` that foreign key checks take.

    Asserting the emitted SQL rather than the behaviour is deliberate. Both are
    plan-shape properties whose absence shows up as a deadlock under load and
    never in a functional test.
    """
    statements: list[str] = []

    @event.listens_for(connection.engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        statements.append(statement)

    try:
        alice = make_cat(balance=100)
        bob = make_cat()
        ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="sql-shape-1")
    finally:
        event.remove(connection.engine, "before_cursor_execute", capture)

    locking = [s for s in statements if "FOR NO KEY UPDATE" in s]
    assert len(locking) == 1, statements
    assert "ORDER BY" in locking[0]
    assert "FOR UPDATE" not in locking[0].replace("FOR NO KEY UPDATE", "")


def test_the_settlement_sets_its_own_timeouts_before_taking_any_lock(
    ledger: Ledger, make_cat: MakeCat, connection: Connection
) -> None:
    """lock_timeout has to be applied inside the transaction, and applied first.

    Setting it on the connection instead, through libpq's startup options, looks
    equivalent and is not. A pooler does not forward those to the backend it
    owns, so the setting silently does not arrive: connecting with
    `-c lock_timeout=3s` and then asking the server returns 0, with no error
    anywhere. A contended transfer then hangs until the server's own
    statement_timeout and fails as 57014 rather than the retryable 55P03 the
    ledger translates.

    Asserting position, not just presence. Applied after the locking SELECT it
    would be useless, because the wait it is meant to bound has already happened.

    This test locates a regression. The one in test_ledger_concurrency proves the
    timeout actually bites.
    """
    statements: list[str] = []

    @event.listens_for(connection.engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        statements.append(statement)

    try:
        alice = make_cat(balance=100)
        bob = make_cat()
        statements.clear()  # drop the fixture inserts
        ledger.transfer(from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="timeout-1")
    finally:
        event.remove(connection.engine, "before_cursor_execute", capture)

    settle = [s for s in statements if "SAVEPOINT" not in s.upper()]
    assert settle, statements
    assert "set_config" in settle[0], f"first settle statement was: {settle[0]!r}"
    assert "lock_timeout" in settle[0]
    # Before anything takes a lock, which is the whole point of it being first.
    locking = next(i for i, s in enumerate(settle) if "FOR NO KEY UPDATE" in s)
    assert locking > 0


def test_every_entry_records_the_running_balance_at_its_point_in_the_ledger(
    ledger: Ledger, make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    """`balance_after` must be the running sum, not merely the final balance.

    This is what lets a replay reproduce the original response without anyone
    storing response bodies, so a line that recorded the wrong number would be a
    silent lie in every replayed answer. It holds because the row lock serialises
    a cat's settlements, so entry id order and commit order agree per cat.
    """
    alice = make_cat()
    bob = make_cat()
    ledger.deposit(to_cat_id=alice, amount=500, idempotency_key="running-fund-1")
    ledger.deposit(to_cat_id=bob, amount=500, idempotency_key="running-fund-2")
    for index in range(5):
        sender, recipient = (alice, bob) if index % 2 == 0 else (bob, alice)
        ledger.transfer(
            from_cat_id=sender,
            to_cat_id=recipient,
            amount=10 + index,
            idempotency_key=f"running-move-{index}",
        )

    with sessions_factory() as session:
        for cat_id in (alice, bob):
            running = 0
            lines = session.scalars(
                select(Entry).where(Entry.cat_id == cat_id).order_by(Entry.id)
            ).all()
            assert lines, "expected this cat to have ledger lines"
            for line in lines:
                running += line.amount
                assert line.balance_after == running
