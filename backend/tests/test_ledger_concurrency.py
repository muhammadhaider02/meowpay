"""The settlement path under real parallelism.

Separate from test_ledger.py because these need their own connections and real
commits. The `sessions_factory` fixture binds everything to one rolled-back
connection, so threads on it would share a Postgres backend and never actually
race. See the `race` fixture in conftest for how a vacuous pass is made to fail.
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from meowpay.constants import TREASURY_CAT_ID
from meowpay.errors import InsufficientFundsError, LedgerBusyError
from meowpay.ledger import LOCK_TIMEOUT, Ledger, Settlement
from meowpay.models import Cat, Entry, Transfer

pytestmark = [pytest.mark.db, pytest.mark.concurrency]

THREADS = 8


def _seconds(interval: str) -> float:
    """Read the ledger's own timeout constant rather than restating it.

    Restating it would let the two drift, and a test that agrees with a stale
    copy of the value it is checking proves nothing.
    """
    assert interval.endswith("s"), interval
    return float(interval[:-1])


def _cat(sessions: sessionmaker[Session], balance: int = 0) -> uuid.UUID:
    cat_id = uuid.uuid4()
    with sessions.begin() as session:
        session.execute(
            insert(Cat).values(
                id=cat_id,
                handle=f"cat_{cat_id.hex[:12]}",
                display_name="Race Cat",
                # Not optional: ck_cats_only_system_lacks_auth_user means a cat
                # without an identity is a system account.
                auth_user_id=uuid.uuid4(),
                balance=balance,
            )
        )
    return cat_id


def _balance(sessions: sessionmaker[Session], cat_id: uuid.UUID) -> int:
    with sessions() as session:
        return session.scalar(select(Cat.balance).where(Cat.id == cat_id)) or 0


def test_eight_threads_with_one_key_settle_exactly_once(
    committing_sessions: sessionmaker[Session], committed_tables: None, race
) -> None:
    """The core idempotency test, under genuine contention.

    If ON CONFLICT DO NOTHING ever skipped against an uncommitted row, the losers
    would raise LedgerInvariantError and show up here as exceptions, which is a
    loud failure rather than a quietly wrong number.
    """
    ledger = Ledger(committing_sessions)
    alice = _cat(committing_sessions, balance=1000)
    bob = _cat(committing_sessions)

    def send(_: int) -> Settlement:
        return ledger.transfer(
            from_cat_id=alice, to_cat_id=bob, amount=100, idempotency_key="one-key-many"
        )

    outcomes = race(committing_sessions, send, threads=THREADS)

    settlements = [o for o in outcomes if isinstance(o, Settlement)]
    assert len(settlements) == THREADS, [o for o in outcomes if not isinstance(o, Settlement)]
    assert sum(1 for s in settlements if not s.replayed) == 1
    assert len({s.transfer_id for s in settlements}) == 1
    assert len({s.owner_balance_after for s in settlements}) == 1

    assert _balance(committing_sessions, alice) == 900
    assert _balance(committing_sessions, bob) == 100
    with committing_sessions() as session:
        assert session.scalar(select(func.count()).select_from(Transfer)) == 1
        assert session.scalar(select(func.count()).select_from(Entry)) == 2


def test_eight_concurrent_transfers_cannot_overdraw(
    committing_sessions: sessionmaker[Session], committed_tables: None, race
) -> None:
    """Distinct keys, so every call is a real attempt. Only the funds stop them."""
    ledger = Ledger(committing_sessions)
    alice = _cat(committing_sessions, balance=100)
    bob = _cat(committing_sessions)

    def send(index: int) -> Settlement:
        return ledger.transfer(
            from_cat_id=alice,
            to_cat_id=bob,
            amount=20,
            idempotency_key=f"race-spend-{index:04d}",
        )

    outcomes = race(committing_sessions, send, threads=THREADS)

    settled = [o for o in outcomes if isinstance(o, Settlement)]
    refused = [o for o in outcomes if isinstance(o, InsufficientFundsError)]
    assert len(settled) == 5
    assert len(refused) == THREADS - 5
    assert _balance(committing_sessions, alice) == 0
    assert _balance(committing_sessions, bob) == 100
    with committing_sessions() as session:
        # Five settled movements and nothing left behind by the three that failed.
        assert session.scalar(select(func.count()).select_from(Transfer)) == 5
        assert session.scalar(select(func.count()).select_from(Entry)) == 10


def test_opposite_direction_transfers_do_not_deadlock(
    committing_sessions: sessionmaker[Session], committed_tables: None, race
) -> None:
    """Without sorted() in _lock_parties this raises DeadlockDetected (40P01).

    Every transfer is for one treat and both cats are funded, so no call can fail
    on funds. A deadlock is the only way this test can go wrong, which makes the
    assertion unambiguous.
    """
    ledger = Ledger(committing_sessions)
    alice = _cat(committing_sessions, balance=500)
    bob = _cat(committing_sessions, balance=500)

    def send(index: int) -> Settlement:
        sender, recipient = (alice, bob) if index % 2 == 0 else (bob, alice)
        return ledger.transfer(
            from_cat_id=sender,
            to_cat_id=recipient,
            amount=1,
            idempotency_key=f"race-cross-{index:04d}",
        )

    outcomes = race(committing_sessions, send, threads=THREADS)

    assert all(isinstance(o, Settlement) for o in outcomes), outcomes
    # Treats moved around but none were created or destroyed.
    assert _balance(committing_sessions, alice) + _balance(committing_sessions, bob) == 1000


def test_a_deposit_and_a_transfer_can_run_at_once(
    committing_sessions: sessionmaker[Session], committed_tables: None, race
) -> None:
    """The lock sets overlap on one cat, so one waits. Neither may be lost."""
    ledger = Ledger(committing_sessions)
    alice = _cat(committing_sessions, balance=100)
    bob = _cat(committing_sessions)

    def work(index: int) -> Settlement:
        if index == 0:
            return ledger.deposit(to_cat_id=alice, amount=50, idempotency_key="race-deposit-1")
        return ledger.transfer(
            from_cat_id=alice, to_cat_id=bob, amount=30, idempotency_key="race-send-1"
        )

    outcomes = race(committing_sessions, work, threads=2)

    assert all(isinstance(o, Settlement) for o in outcomes), outcomes
    assert _balance(committing_sessions, alice) == 120  # 100 + 50 - 30
    assert _balance(committing_sessions, bob) == 30
    assert _balance(committing_sessions, TREASURY_CAT_ID) == -50


def test_two_cats_racing_one_top_up_key_each_get_their_own(
    committing_sessions: sessionmaker[Session], committed_tables: None, race
) -> None:
    """The regression the owner_cat_id change exists to prevent, under real load.

    Different amounts on purpose, so a wrong replay shows up as a wrong balance
    rather than only as a shared id.
    """
    ledger = Ledger(committing_sessions)
    alice = _cat(committing_sessions)
    bob = _cat(committing_sessions)

    def top_up(index: int) -> Settlement:
        cat, amount = (alice, 500) if index == 0 else (bob, 700)
        return ledger.deposit(to_cat_id=cat, amount=amount, idempotency_key="topup-0001")

    outcomes = race(committing_sessions, top_up, threads=2)

    assert all(isinstance(o, Settlement) for o in outcomes), outcomes
    assert _balance(committing_sessions, alice) == 500
    assert _balance(committing_sessions, bob) == 700


def test_the_ledger_still_reconciles_after_a_concurrent_run(
    committing_sessions: sessionmaker[Session], committed_tables: None, race
) -> None:
    """The invariant the schema does not enforce, checked under contention."""
    ledger = Ledger(committing_sessions)
    # Funded through the ledger, not by writing balance. Seeding a balance
    # directly is exactly what breaks reconciliation, which this test proved on
    # its first run, so it is worth doing the right way here.
    cats = [_cat(committing_sessions) for _ in range(3)]
    for index, cat in enumerate(cats):
        ledger.deposit(to_cat_id=cat, amount=200, idempotency_key=f"race-fund-{index:04d}")

    def churn(index: int) -> object:
        sender = cats[index % len(cats)]
        recipient = cats[(index + 1) % len(cats)]
        try:
            return ledger.transfer(
                from_cat_id=sender,
                to_cat_id=recipient,
                amount=50,
                idempotency_key=f"race-churn-{index:04d}",
            )
        except InsufficientFundsError as exc:
            return exc

    race(committing_sessions, churn, threads=THREADS)

    with committing_sessions() as session:
        drift = session.execute(
            select(Cat.id, Cat.balance - func.coalesce(func.sum(Entry.amount), 0))
            .outerjoin(Entry, Entry.cat_id == Cat.id)
            .group_by(Cat.id, Cat.balance)
        ).all()
        ledger_total = session.scalar(select(func.coalesce(func.sum(Entry.amount), 0)))

    assert all(difference == 0 for _, difference in drift)
    assert ledger_total == 0


def test_a_transfer_blocked_on_a_lock_gives_up_and_reports_itself_retryable(
    committing_engine: Engine,
    committing_sessions: sessionmaker[Session],
    committed_tables: None,
) -> None:
    """The lock_timeout to 55P03 to LedgerBusyError chain, end to end.

    Worth writing because nothing else asserts that the timeout actually bites.
    Without this, moving it onto the connection would look harmless: a pooler
    does not forward startup options to the backend it owns, so the setting
    would silently stop arriving and every other test would still pass.

    This fails in both directions, which is what makes it worth writing:

    - If lock_timeout is not applied, the transfer blocks until the session level
      statement_timeout instead, and raises 57014. That is not 55P03, so the
      ledger does not translate it, so LedgerBusyError never arrives and this
      raises OperationalError instead.
    - If lock_timeout is set to something other than LOCK_TIMEOUT, the elapsed
      window fails.

    The holding connection is released in a finally. Without that, the wipe in
    committed_tables blocks on the rows this test is still holding and takes the
    next test down with it.
    """
    ledger = Ledger(committing_sessions)
    alice = _cat(committing_sessions, balance=1000)
    bob = _cat(committing_sessions)

    budget = _seconds(LOCK_TIMEOUT)
    holder = committing_engine.connect()
    try:
        holder.begin()
        # The same lock the settlement wants, held by someone else.
        holder.execute(
            select(Cat.id).where(Cat.id.in_({alice, bob})).order_by(Cat.id).with_for_update()
        ).all()

        started = time.perf_counter()
        with pytest.raises(LedgerBusyError) as caught:
            ledger.transfer(
                from_cat_id=alice, to_cat_id=bob, amount=10, idempotency_key="lock-timeout-1"
            )
        elapsed = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()

    assert caught.value.status == 503
    assert caught.value.code == "ledger_busy"
    # Waited out the budget rather than failing instantly, and gave up rather
    # than waiting for the far longer statement_timeout.
    assert budget <= elapsed < budget + 6, f"gave up after {elapsed:.1f}s, budget is {budget}s"

    # Nothing settled, and the key was not consumed.
    with committing_sessions() as session:
        assert session.scalar(select(func.count()).select_from(Transfer)) == 0
        assert session.scalar(select(Cat.balance).where(Cat.id == alice)) == 1000
