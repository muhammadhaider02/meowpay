"""Transfers and deposits over HTTP.

Only the cryptography is stubbed. `deps.claims` is overridden; `get_current_cat`,
the ledger and the database are all real, so this is the first suite in which a
request actually moves treats end to end.

That is the point of it. `test_ledger.py` proves the settlement algorithm and
`test_onboarding.py` proves the identity path, but nothing until now proved the
two are wired together: that the sender comes from the token rather than the
body, that a replay reaches the client as a 200, and that a ledger rejection
arrives in the error envelope with the code the frontend branches on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from meowpay.auth import Claims
from meowpay.constants import MAX_AMOUNT, TREASURY_CAT_ID, TREASURY_HANDLE
from meowpay.models import Cat

pytestmark = pytest.mark.db

MakeCat = Callable[..., uuid.UUID]
AsIdentity = Callable[..., Claims]

TRANSFERS = "/api/v1/transfers"
DEPOSITS = "/api/v1/deposits"

KEY = "test-key-00000001"


def _headers(key: str | None = KEY) -> dict[str, str]:
    return {} if key is None else {"Idempotency-Key": key}


def _balance(sessions_factory: sessionmaker[Session], cat_id: uuid.UUID) -> int:
    with sessions_factory() as session:
        return session.scalar(select(Cat.balance).where(Cat.id == cat_id)) or 0


def _signed_in(
    make_cat: MakeCat, as_identity: AsIdentity, balance: int = 0, handle: str | None = None
) -> uuid.UUID:
    """Create a cat and present its identity as the caller."""
    auth_user_id = uuid.uuid4()
    return make_cat(
        balance=balance, handle=handle, auth_user_id=as_identity(auth_user_id).auth_user_id
    )


# -- the happy paths --------------------------------------------------------


def test_a_transfer_moves_treats_and_reports_the_senders_new_balance(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    recipient = make_cat(handle="dahlia")
    sender = _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": 120}, headers=_headers()
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["amount"] == 120
    assert body["kind"] == "transfer"
    assert body["balance_after"] == 380
    assert body["replayed"] is False

    assert _balance(sessions_factory, sender) == 380
    assert _balance(sessions_factory, recipient) == 120


def test_a_deposit_credits_the_caller_and_needs_no_recipient_field(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    cat = _signed_in(make_cat, as_identity, balance=0, handle="lotus")

    response = client.post(DEPOSITS, json={"amount": 300}, headers=_headers())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "deposit"
    assert body["balance_after"] == 300
    assert _balance(sessions_factory, cat) == 300

    # The other half of the double entry. A deposit that did not debit the
    # treasury would break the zero-sum property the reconciliation test asserts.
    assert _balance(sessions_factory, TREASURY_CAT_ID) == -300


def test_a_handle_is_normalised_so_the_sender_can_type_it_how_they_like(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    recipient = make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=100, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "  DAHLIA  ", "amount": 10}, headers=_headers()
    )

    assert response.status_code == 201, response.text
    # The status alone would only prove it resolved to some cat.
    assert _balance(sessions_factory, recipient) == 10


# -- idempotency ------------------------------------------------------------


def test_repeating_a_transfer_is_200_replays_and_moves_nothing(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    """The contract the frontend's retry-once depends on.

    The second call must report the balance as it was when the movement
    settled, not as it is now, which is why `balance_after` is not called
    `balance`.
    """
    make_cat(handle="dahlia")
    sender = _signed_in(make_cat, as_identity, balance=500, handle="milo")

    first = client.post(TRANSFERS, json={"to_handle": "dahlia", "amount": 120}, headers=_headers())
    second = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": 120}, headers=_headers()
    )

    assert first.status_code == 201
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["balance_after"] == 380

    # Moved once, not twice.
    assert _balance(sessions_factory, sender) == 380


def test_a_missing_idempotency_key_is_refused_in_our_own_envelope(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """Not FastAPI's `validation_error`.

    The route declares the header optional and refuses it itself precisely so
    the code is the same one a malformed key produces. A client branching on
    `code` should not need to know that one came from Pydantic.

    The message is asserted, not just the code. Both the route and the ledger
    refuse a missing key as `idempotency_key_invalid`, so the code alone cannot
    tell which one did it, and deleting the route's check would leave this test
    green while the refusal quietly moved to the far side of a pool connection.
    """
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": 10}, headers=_headers(key=None)
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "idempotency_key_invalid"
    # This phrase exists only in the route's own refusal.
    assert "header is required" in error["message"], error["message"]


@pytest.mark.parametrize("key", ["short", "x" * 256], ids=["too-short", "too-long"])
def test_a_malformed_idempotency_key_is_422_and_never_reaches_a_constraint(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
    key: str,
) -> None:
    make_cat(handle="dahlia")
    sender = _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": 10}, headers=_headers(key)
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "idempotency_key_invalid"
    assert _balance(sessions_factory, sender) == 500


def test_two_cats_may_use_the_same_key(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """The key is unique per owner, not globally.

    A global unique would make one cat's key collide with another's, so the
    second sender would be told their key was reused for a movement they never
    made.
    """
    make_cat(handle="dahlia")

    _signed_in(make_cat, as_identity, balance=500, handle="milo")
    first = client.post(TRANSFERS, json={"to_handle": "dahlia", "amount": 10}, headers=_headers())

    _signed_in(make_cat, as_identity, balance=500, handle="lotus")
    second = client.post(TRANSFERS, json={"to_handle": "dahlia", "amount": 10}, headers=_headers())

    assert first.status_code == 201
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]


def test_repeating_a_deposit_is_200_replays_and_credits_nothing(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    """Deposits have their own replay path and it is not the transfer's.

    `owner_cat_id` resolves to the RECIPIENT for a deposit and the sender for a
    transfer, so the lookup that finds a settled movement is a different branch.
    Covering only transfers would leave it unexercised over HTTP.
    """
    cat = _signed_in(make_cat, as_identity, balance=0, handle="lotus")

    first = client.post(DEPOSITS, json={"amount": 300}, headers=_headers())
    second = client.post(DEPOSITS, json={"amount": 300}, headers=_headers())

    assert first.status_code == 201
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]
    assert _balance(sessions_factory, cat) == 300


def test_a_deposit_without_an_idempotency_key_is_refused(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """`top_up` checks the key itself, so deleting that call has to fail something.

    Without this the deletion is invisible: the header defaults to None, reaches
    the ledger, and is refused there with the same code.
    """
    _signed_in(make_cat, as_identity, balance=0, handle="lotus")

    response = client.post(DEPOSITS, json={"amount": 100}, headers=_headers(key=None))

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "idempotency_key_invalid"
    # Only the route says this. The ledger's own refusal of a missing key says
    # "it must be a string", so asserting the code alone would pass either way.
    assert "header is required" in error["message"], error["message"]


def test_the_same_key_for_a_different_movement_is_409(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """The one non-default status a LedgerError carries through these routes.

    `LedgerError` defaults to 422; `IdempotencyKeyReusedError` overrides it to
    409. Nothing proved that override survived the trip to the wire, because the
    ledger suite asserts the exception class rather than the response.
    """
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    deposited = client.post(DEPOSITS, json={"amount": 100}, headers=_headers())
    reused = client.post(TRANSFERS, json={"to_handle": "dahlia", "amount": 10}, headers=_headers())

    assert deposited.status_code == 201
    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["code"] == "idempotency_key_reused"


def test_sending_the_idempotency_key_twice_is_refused_rather_than_silently_picking_one(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    """A repeated header is a double spend waiting to happen.

    FastAPI hands a `str`-typed header only the first value and drops the rest,
    so the request would settle under one key while the client believed it had
    sent another, and the retry under that other key would move the treats a
    second time. The parameter is a list precisely so this is visible.

    httpx needs a list of tuples to express a repeated header; a dict cannot.
    """
    make_cat(handle="dahlia")
    sender = _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS,
        json={"to_handle": "dahlia", "amount": 120},
        headers=[
            ("Idempotency-Key", "first-key-000001"),
            ("Idempotency-Key", "second-key-00001"),
        ],
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "idempotency_key_invalid"
    assert _balance(sessions_factory, sender) == 500


def test_a_malformed_key_is_refused_before_the_recipient_is_looked_up(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """Ordering, and it is observable.

    With the shape checked after resolution, a request that is wrong in both
    ways answers 404 about the recipient and says nothing about the key, so the
    caller fixes the handle only to be told about the key on the next attempt.
    It also means a malformed request occupies a pool connection, which the
    ledger's own validation order exists to avoid.
    """
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "nobody_here", "amount": 10}, headers=_headers("short")
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "idempotency_key_invalid"


# -- refusals ---------------------------------------------------------------


def test_an_overdraft_is_refused_in_the_envelope_and_moves_nothing(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    make_cat(handle="dahlia")
    sender = _signed_in(make_cat, as_identity, balance=50, handle="milo")

    response = client.post(
        TRANSFERS,
        json={"to_handle": "dahlia", "amount": 500},
        headers={**_headers(), "Origin": "http://localhost:3000"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "insufficient_funds"
    # The error has to reach the browser, or the frontend cannot show it.
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert _balance(sessions_factory, sender) == 50


def test_a_refused_transfer_does_not_consume_its_key(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """Deliberately the opposite of Stripe, and the reason is the demo.

    A user who overdraws, tops up and retries the same intent should succeed
    rather than be told their key is spent.
    """
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=50, handle="milo")

    refused = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": 500}, headers=_headers()
    )
    client.post(DEPOSITS, json={"amount": 1000}, headers=_headers("top-up-00000001"))
    retried = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": 500}, headers=_headers()
    )

    assert refused.status_code == 422
    assert retried.status_code == 201, retried.text
    assert retried.json()["replayed"] is False


def test_sending_to_yourself_is_refused(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(TRANSFERS, json={"to_handle": "milo", "amount": 10}, headers=_headers())

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "self_transfer"


def test_an_unknown_recipient_is_404(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "nobody_here", "amount": 10}, headers=_headers()
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "recipient_not_found"


def test_the_treasury_cannot_be_named_as_a_recipient(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    """Refused as an invalid handle, not as a missing one.

    The reserved prefix is rejected during normalisation, so the response never
    confirms whether that row exists. Only that first guard can fire here; the
    `~Cat.is_system` filter and `Ledger.transfer`'s own treasury check sit behind
    it and are unreachable through this path, which is why they are documented as
    redundant rather than claimed as coverage.
    """
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": TREASURY_HANDLE, "amount": 10}, headers=_headers()
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "handle_invalid"
    assert _balance(sessions_factory, TREASURY_CAT_ID) == 0


@pytest.mark.parametrize(
    "amount",
    [0, -1, MAX_AMOUNT + 1],
    ids=["zero", "negative", "over-the-cap"],
)
def test_an_amount_outside_the_allowed_range_is_refused(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity, amount: int
) -> None:
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": amount}, headers=_headers()
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "amount_out_of_range"


@pytest.mark.parametrize(
    "amount",
    [1.5, "100", True, None],
    ids=["float", "numeric-string", "bool", "null"],
)
def test_an_amount_that_is_not_an_integer_is_refused_by_the_schema(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity, amount: object
) -> None:
    """Strict mode's job, and `True` is the interesting one.

    bool is a subclass of int, so without strict mode a transfer of `true` would
    be a perfectly valid transfer of one treat.
    """
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS, json={"to_handle": "dahlia", "amount": amount}, headers=_headers()
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_an_unknown_field_in_the_body_is_refused(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """`from_handle` is the field this is really guarding against.

    Silently ignoring it would let a caller believe they had chosen a sender.
    """
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=500, handle="milo")

    response = client.post(
        TRANSFERS,
        json={"to_handle": "dahlia", "amount": 10, "from_handle": "dahlia"},
        headers=_headers(),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_a_deposit_cannot_name_a_recipient(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """The one that would be a mint into someone else's account."""
    make_cat(handle="dahlia")
    _signed_in(make_cat, as_identity, balance=0, handle="milo")

    response = client.post(
        DEPOSITS, json={"amount": 100, "to_handle": "dahlia"}, headers=_headers()
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


# -- identity ---------------------------------------------------------------


@pytest.mark.parametrize("path", [TRANSFERS, DEPOSITS], ids=["transfers", "deposits"])
def test_moving_treats_without_a_token_is_401(client: TestClient, path: str) -> None:
    response = client.post(path, json={"amount": 10, "to_handle": "dahlia"}, headers=_headers())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", [TRANSFERS, DEPOSITS], ids=["transfers", "deposits"])
def test_a_valid_token_with_no_cat_is_403_and_not_401(
    client: TestClient, as_identity: AsIdentity, path: str
) -> None:
    """The first time this reaches a caller through a real route.

    Until this commit `CurrentCatDep` had no consumers, so the 403 existed and
    could not fire. 401 would make the client re-authenticate, receive an
    identical token and loop forever.
    """
    as_identity()

    response = client.post(path, json={"amount": 10, "to_handle": "dahlia"}, headers=_headers())

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "cat_not_onboarded"


def test_the_debit_lands_on_the_token_holder_and_not_on_another_funded_cat(
    client: TestClient,
    make_cat: MakeCat,
    as_identity: AsIdentity,
    sessions_factory: sessionmaker[Session],
) -> None:
    """The security property this whole module exists to protect.

    Deliberately not another `extra="forbid"` test: rejecting a body field proves
    Pydantic works, and would stay green if the route read its sender from a
    header, a query parameter or `to_handle`.

    So there are two funded cats and a third party to receive. The transfer must
    debit the one the token resolves to and leave the other untouched. That fails
    for any sender source other than the token.
    """
    bystander = make_cat(balance=1000, handle="dahlia")
    sender = _signed_in(make_cat, as_identity, balance=700, handle="milo")
    make_cat(handle="lotus")

    response = client.post(
        TRANSFERS, json={"to_handle": "lotus", "amount": 300}, headers=_headers()
    )

    assert response.status_code == 201, response.text
    assert _balance(sessions_factory, sender) == 400
    assert _balance(sessions_factory, bystander) == 1000
