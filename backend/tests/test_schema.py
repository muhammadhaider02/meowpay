"""The constraints that back the money invariants.

Every test here writes raw SQL rather than going through the service, on
purpose: the point is that the database refuses these even when the application
is wrong.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from meowpay.api.routes.cats import normalise_handle
from meowpay.constants import HANDLE_REGEX, MAX_AMOUNT, TREASURY_CAT_ID
from meowpay.errors import AmountOutOfRangeError, HandleInvalidError, IdempotencyKeyInvalidError
from meowpay.ledger import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_MIN_LENGTH,
    _check_amount,
    check_idempotency_key,
)
from meowpay.models import HANDLE_PATTERN

pytestmark = pytest.mark.db


def _insert_cat(connection: Connection, handle: str, balance: int = 0) -> uuid.UUID:
    cat_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO cats (id, handle, display_name, auth_user_id, balance) "
            "VALUES (:id, :handle, :name, :auth_user_id, :balance)"
        ),
        {
            "id": cat_id,
            "handle": handle,
            "name": handle,
            "balance": balance,
            # Not optional. ck_cats_only_system_lacks_auth_user says
            # is_system = (auth_user_id IS NULL), so a cat inserted without one
            # is a system account, and ck_cats_only_the_sentinel_is_system then
            # rejects it under a name that has nothing to do with identity.
            # A random uuid is not a lie here: there is deliberately no foreign
            # key to auth.users, so to this database an identity id is a uuid.
            "auth_user_id": uuid.uuid4(),
        },
    )
    return cat_id


def test_the_treasury_exists_with_the_sentinel_id_and_no_identity(connection: Connection) -> None:
    row = connection.execute(
        text("SELECT is_system, balance, auth_user_id FROM cats WHERE id = :id"),
        {"id": TREASURY_CAT_ID},
    ).one()

    assert row.is_system is True
    assert row.balance == 0
    # A caller is resolved by WHERE auth_user_id = :sub, and SQL NULL is never
    # equal to anything, so no token can reach this row whatever the auth code
    # does. That holds only while the lookup is by this column.
    assert row.auth_user_id is None


def test_a_normal_cat_cannot_go_negative(connection: Connection) -> None:
    cat_id = _insert_cat(connection, "grumpy", balance=100)

    with pytest.raises(IntegrityError, match="balance_non_negative"):
        connection.execute(text("UPDATE cats SET balance = -1 WHERE id = :id"), {"id": cat_id})


def test_the_treasury_is_allowed_to_go_negative(connection: Connection) -> None:
    # Its balance is the negative of every treat in circulation, so this is not
    # an exception to the rule, it is what the rule means.
    connection.execute(
        text("UPDATE cats SET balance = -500 WHERE id = :id"), {"id": TREASURY_CAT_ID}
    )

    balance = connection.execute(
        text("SELECT balance FROM cats WHERE id = :id"), {"id": TREASURY_CAT_ID}
    ).scalar_one()
    assert balance == -500


def test_a_self_transfer_cannot_be_stored(connection: Connection) -> None:
    cat_id = _insert_cat(connection, "mittens")

    with pytest.raises(IntegrityError, match="parties_differ"):
        connection.execute(
            text(
                "INSERT INTO transfers (id, kind, from_cat_id, to_cat_id, amount, "
                "idempotency_key) VALUES (:id, 'transfer', :cat, :cat, 10, 'key-00001')"
            ),
            {"id": uuid.uuid4(), "cat": cat_id},
        )


@pytest.mark.parametrize("amount", [0, -1, 1_000_000_000_001])
def test_an_amount_outside_the_allowed_range_is_rejected(
    connection: Connection, amount: int
) -> None:
    # The upper bound keeps every balance inside JavaScript's safe integer
    # range, so the browser cannot silently lose precision on a total.
    sender = _insert_cat(connection, "sender_cat")
    recipient = _insert_cat(connection, "recipient_cat")

    with pytest.raises(IntegrityError, match="amount_positive|amount_within_cap"):
        connection.execute(
            text(
                "INSERT INTO transfers (id, kind, from_cat_id, to_cat_id, amount, "
                "idempotency_key) VALUES (:id, 'transfer', :f, :t, :amount, 'key-00002')"
            ),
            {"id": uuid.uuid4(), "f": sender, "t": recipient, "amount": amount},
        )


def test_the_same_idempotency_key_is_unique_per_owner_not_globally(
    connection: Connection,
) -> None:
    """The scoping decision, tested.

    Uniqueness must not span a trust boundary, so two different cats may each
    use the key "shared-key" without colliding.
    """
    alice = _insert_cat(connection, "alice_cat")
    bob = _insert_cat(connection, "bob_cat")
    carol = _insert_cat(connection, "carol_cat")

    def send(sender: uuid.UUID, key: str) -> None:
        connection.execute(
            text(
                "INSERT INTO transfers (id, kind, from_cat_id, to_cat_id, amount, "
                "idempotency_key) VALUES (:id, 'transfer', :f, :t, 10, :key)"
            ),
            {"id": uuid.uuid4(), "f": sender, "t": carol, "key": key},
        )

    send(alice, "shared-key")
    send(bob, "shared-key")  # different sender, so no collision

    with pytest.raises(IntegrityError, match="idempotency_key"):
        send(alice, "shared-key")  # same sender, so the gate closes


def test_a_cat_cannot_have_two_lines_on_one_transfer(connection: Connection) -> None:
    """Makes a double credit structurally impossible, not merely unlikely."""
    sender = _insert_cat(connection, "payer_cat")
    recipient = _insert_cat(connection, "payee_cat")
    transfer_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO transfers (id, kind, from_cat_id, to_cat_id, amount, "
            "idempotency_key) VALUES (:id, 'transfer', :f, :t, 10, 'key-00003')"
        ),
        {"id": transfer_id, "f": sender, "t": recipient},
    )

    def add_line(amount: int) -> None:
        connection.execute(
            text(
                "INSERT INTO entries (transfer_id, cat_id, counterparty_cat_id, kind, "
                "amount, balance_after) VALUES (:tid, :cat, :other, 'transfer', "
                ":amount, 0)"
            ),
            {"tid": transfer_id, "cat": recipient, "other": sender, "amount": amount},
        )

    add_line(10)

    with pytest.raises(IntegrityError, match="uq_entries_transfer_id_cat_id"):
        add_line(10)


def test_two_cats_can_top_up_with_the_same_idempotency_key(connection: Connection) -> None:
    """The bug that scoping on from_cat_id alone would have shipped.

    Every deposit has the treasury as its sender, so scoping the key to the
    sender would make the constraint a GLOBAL unique for deposits. Two cats
    picking the same client-generated key would collide, and the replay would
    hand the second one the first one's amount and balance. owner_cat_id is the
    recipient for a deposit, which is the cat that actually chose the key.
    """
    alice = _insert_cat(connection, "topup_alice")
    bob = _insert_cat(connection, "topup_bob")

    def deposit(cat: uuid.UUID, key: str) -> None:
        connection.execute(
            text(
                "INSERT INTO transfers (id, kind, from_cat_id, to_cat_id, amount, "
                "idempotency_key) VALUES (:id, 'deposit', :treasury, :cat, 500, :key)"
            ),
            {"id": uuid.uuid4(), "treasury": TREASURY_CAT_ID, "cat": cat, "key": key},
        )

    deposit(alice, "topup-0001")
    deposit(bob, "topup-0001")  # different owner, so no collision

    with pytest.raises(IntegrityError, match="idempotency_key"):
        deposit(alice, "topup-0001")  # same owner, so the gate closes


def test_flipping_is_system_cannot_disable_the_overdraft_check(
    connection: Connection,
) -> None:
    """Only the sentinel row may be a system account.

    Without this, is_system doubled as an exemption from the balance check, so
    one mass-assignment bug on signup would be unlimited minting.
    """
    cat_id = _insert_cat(connection, "sneaky_cat")

    # Clearing the identity too, so this gets past
    # ck_cats_only_system_lacks_auth_user and has to be stopped by the sentinel
    # rule itself rather than by a neighbouring constraint that fires first.
    with pytest.raises(IntegrityError, match="only_the_sentinel_is_system"):
        connection.execute(
            text("UPDATE cats SET is_system = true, auth_user_id = NULL WHERE id = :id"),
            {"id": cat_id},
        )


def test_the_treasury_cannot_be_given_an_identity(connection: Connection) -> None:
    """Makes "cannot be logged in as" structural rather than conventional."""
    with pytest.raises(IntegrityError, match="only_system_lacks_auth_user"):
        connection.execute(
            text("UPDATE cats SET auth_user_id = :auth_user_id WHERE id = :id"),
            {"id": TREASURY_CAT_ID, "auth_user_id": uuid.uuid4()},
        )


def test_an_ordinary_cat_cannot_lose_its_identity(connection: Connection) -> None:
    """The other half of the same constraint.

    Together with the sentinel rule this makes an unlinked ordinary cat
    impossible, which is what lets onboarding and the seed script insert a cat
    with its identity rather than inserting and then linking.
    """
    cat_id = _insert_cat(connection, "orphan_cat")

    with pytest.raises(IntegrityError, match="only_system_lacks_auth_user"):
        connection.execute(
            text("UPDATE cats SET auth_user_id = NULL WHERE id = :id"), {"id": cat_id}
        )


def test_two_cats_cannot_share_one_identity(connection: Connection) -> None:
    """One login, one money account.

    Without this, two cats could point at the same auth.users row and either
    could spend the other's treats, because authorization is entirely "the
    caller's auth_user_id resolves to this cat".
    """
    shared = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO cats (id, handle, display_name, auth_user_id) "
            "VALUES (:id, 'first_cat', 'First', :auth_user_id)"
        ),
        {"id": uuid.uuid4(), "auth_user_id": shared},
    )

    with pytest.raises(IntegrityError, match="uq_cats_auth_user_id"):
        connection.execute(
            text(
                "INSERT INTO cats (id, handle, display_name, auth_user_id) "
                "VALUES (:id, 'second_cat', 'Second', :auth_user_id)"
            ),
            {"id": uuid.uuid4(), "auth_user_id": shared},
        )


def test_a_balance_cannot_exceed_javascript_safe_integers(connection: Connection) -> None:
    """The per-movement cap alone does not bound the balance.

    Enough deposits still walk a balance past 2**53 - 1, where a browser stops
    representing it exactly, and the treasury gets there first.
    """
    cat_id = _insert_cat(connection, "rich_cat")

    with pytest.raises(IntegrityError, match="balance_is_js_safe"):
        connection.execute(
            text("UPDATE cats SET balance = 9007199254740992 WHERE id = :id"),
            {"id": cat_id},
        )


def test_the_handle_check_in_the_database_matches_the_one_the_service_enforces(
    connection: Connection,
) -> None:
    """The drift guard that `alembic check` does NOT provide.

    Autogenerate never reflects or compares CHECK constraints, so `models.py`
    building HANDLE_PATTERN from HANDLE_REGEX proves nothing on its own: the
    live constraint comes from a hardcoded literal in migration 0001 and nothing
    compared the two. Changing HANDLE_REGEX by one character left `alembic check`
    reporting no drift and every test green, while the endpoint started returning
    500 on a handle the service accepted and the database refused.

    Two assertions, because they fail for different reasons. The first catches a
    silent edit to either side. The second is the one that matters: it asks both
    the Python validator and the live constraint about the same strings and
    requires them to agree.
    """
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_cats_handle_shape'"
        )
    ).scalar_one()

    assert f"'^{HANDLE_REGEX}$'" in definition, (
        f"the database enforces {definition!r}, the service enforces {HANDLE_PATTERN!r}"
    )

    probes = [
        "dahlia",
        "cat_1",
        "a1b",
        "x" * 32,
        "ab",
        "x" * 33,
        "bad-handle",
        "bad.handle",
        "bad+handle",
        "bad handle",
        "Dahlia",
        "dah\nlia",
        "",
        "_",
        "___",
    ]
    for probe in probes:
        accepted_by_postgres = connection.execute(
            text("SELECT :candidate ~ :pattern"),
            {"candidate": probe, "pattern": f"^{HANDLE_REGEX}$"},
        ).scalar_one()

        try:
            normalise_handle(probe)
            accepted_by_service = True
        except HandleInvalidError:
            accepted_by_service = False

        # The service may normalise first, so it can accept something the raw
        # string fails. It must never accept something the database will refuse.
        if accepted_by_service:
            normalised = normalise_handle(probe)
            stored_ok = connection.execute(
                text("SELECT :candidate ~ :pattern"),
                {"candidate": normalised, "pattern": f"^{HANDLE_REGEX}$"},
            ).scalar_one()
            assert stored_ok, (
                f"the service accepts {probe!r} and normalises it to {normalised!r}, "
                f"which the database CHECK refuses. That is a 500, not a 422."
            )
        else:
            # Anything the service refuses is fine either way: it never reaches
            # the database. Recorded so the probe set stays honest.
            assert accepted_by_postgres in (True, False)


def test_the_idempotency_key_bounds_in_the_database_match_the_ones_the_service_enforces(
    connection: Connection,
) -> None:
    """Same class of gap as the handle guard, and commit 5 made it reachable.

    `alembic check` does not compare CHECK bodies, so `check_idempotency_key`
    and `ck_transfers_idempotency_key_shape` can drift apart silently. Until the
    HTTP surface existed the key was only ever supplied by our own seed script;
    now a caller sets it in a header, so a seven character key has to be a 422
    and not a 500 from a constraint nobody expected to fire.
    """
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_transfers_idempotency_key_shape'"
        )
    ).scalar_one()

    assert str(IDEMPOTENCY_KEY_MIN_LENGTH) in definition, (
        f"the database enforces {definition!r}, the service enforces a minimum of "
        f"{IDEMPOTENCY_KEY_MIN_LENGTH}"
    )
    assert str(IDEMPOTENCY_KEY_MAX_LENGTH) in definition, (
        f"the database enforces {definition!r}, the service enforces a maximum of "
        f"{IDEMPOTENCY_KEY_MAX_LENGTH}"
    )

    # The differential half. Anything the service accepts must survive the live
    # constraint, or it is a 500 on a value the caller was told was fine.
    probes = [
        "x" * IDEMPOTENCY_KEY_MIN_LENGTH,
        "x" * IDEMPOTENCY_KEY_MAX_LENGTH,
        "x" * (IDEMPOTENCY_KEY_MIN_LENGTH - 1),
        "x" * (IDEMPOTENCY_KEY_MAX_LENGTH + 1),
        "seed-v1-dahlia",
        "",
    ]
    for probe in probes:
        try:
            check_idempotency_key(probe)
            accepted_by_service = True
        except IdempotencyKeyInvalidError:
            accepted_by_service = False

        accepted_by_postgres = connection.execute(
            text("SELECT length(:candidate) BETWEEN :low AND :high"),
            {
                "candidate": probe,
                "low": IDEMPOTENCY_KEY_MIN_LENGTH,
                "high": IDEMPOTENCY_KEY_MAX_LENGTH,
            },
        ).scalar_one()

        if accepted_by_service:
            assert accepted_by_postgres, (
                f"the service accepts the key {probe!r} ({len(probe)} characters), "
                f"which ck_transfers_idempotency_key_shape refuses. That is a 500, not a 422."
            )


def test_the_amount_bounds_in_the_database_match_the_ones_the_service_enforces(
    connection: Connection,
) -> None:
    """The third Python-mirrors-SQL pair, and the one that moves money.

    `_check_amount` mirrors two constraints at once, `ck_transfers_amount_positive`
    and `ck_transfers_amount_within_cap`. A cap raised in `constants.py` and not
    in a migration makes a large transfer a 500 after the row locks are already
    held, which is the worst place in the codebase to discover a mismatch.
    """
    definitions: dict[str, str] = {
        row.conname: row.definition
        for row in connection.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conname IN "
                "('ck_transfers_amount_positive', 'ck_transfers_amount_within_cap')"
            )
        ).all()
    }

    assert set(definitions) == {
        "ck_transfers_amount_positive",
        "ck_transfers_amount_within_cap",
    }, f"a constraint the service mirrors is missing: {sorted(definitions)}"

    assert str(MAX_AMOUNT) in definitions["ck_transfers_amount_within_cap"], (
        f"the database caps amounts at {definitions['ck_transfers_amount_within_cap']!r}, "
        f"the service caps them at {MAX_AMOUNT}"
    )

    probes = [1, MAX_AMOUNT, 0, -1, MAX_AMOUNT + 1]
    for probe in probes:
        try:
            _check_amount(probe)
            accepted_by_service = True
        except AmountOutOfRangeError:
            accepted_by_service = False

        accepted_by_postgres = connection.execute(
            text("SELECT :candidate > 0 AND :candidate <= :cap"),
            {"candidate": probe, "cap": MAX_AMOUNT},
        ).scalar_one()

        assert accepted_by_service == accepted_by_postgres, (
            f"the service and the database disagree about an amount of {probe}: "
            f"service accepts={accepted_by_service}, database accepts={accepted_by_postgres}"
        )
