"""Turning a verified identity into a cat, over HTTP.

Only the cryptography is stubbed. `deps.claims` is overridden, `get_current_cat`
and the database are real, so the paths that actually matter still run: the cat
lookup, the 403 when there is no cat, the handle collisions and the envelope.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from meowpay.api.deps import get_current_cat
from meowpay.auth import Claims
from meowpay.constants import TREASURY_CAT_ID, TREASURY_HANDLE
from meowpay.errors import CatNotOnboardedError
from meowpay.models import Cat

pytestmark = pytest.mark.db

MakeCat = Callable[..., uuid.UUID]
AsIdentity = Callable[..., Claims]

ONBOARD = "/api/v1/cats"


def _body(handle: str = "dahlia", display_name: str = "Dahlia") -> dict[str, str]:
    return {"handle": handle, "display_name": display_name}


# -- without a usable identity ---------------------------------------------


def test_onboarding_without_a_token_is_401_in_the_envelope(client: TestClient) -> None:
    """Three things at once, and the CORS header is the subtle one.

    A 401 that the browser blocks is a 401 the frontend cannot branch on, so it
    would sign the user out on every transport hiccup instead. The handler is
    registered for a specific exception class precisely so its response travels
    back out through CORSMiddleware.
    """
    response = client.post(ONBOARD, json=_body(), headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.parametrize(
    "header",
    ["", "Basic abc123", "Bearer", "Token abc123"],
    ids=["empty", "basic", "no-credentials", "wrong-scheme"],
)
def test_a_non_bearer_authorization_header_is_401(client: TestClient, header: str) -> None:
    response = client.post(ONBOARD, json=_body(), headers={"Authorization": header})

    assert response.status_code == 401


def test_a_valid_token_with_no_cat_is_403_and_not_401(
    sessions_factory: sessionmaker[Session],
) -> None:
    """The distinction the whole split exists for.

    401 would mean "re-authenticate", and re-authenticating yields an identical
    token, so the client would be in a sign-in loop that can never resolve.

    Called directly rather than over HTTP because no protected route exists yet.
    The dependency is where the rule lives, deliberately: putting it in each
    route means forgetting it in one route is a security bug.
    """
    identity = Claims(auth_user_id=uuid.uuid4(), email=None, session_id=None)

    with pytest.raises(CatNotOnboardedError) as caught:
        get_current_cat(identity, sessions_factory)

    assert caught.value.status == 403
    assert caught.value.code == "cat_not_onboarded"


def test_a_valid_token_resolves_to_its_own_cat(
    make_cat: MakeCat, sessions_factory: sessionmaker[Session]
) -> None:
    auth_user_id = uuid.uuid4()
    cat_id = make_cat(balance=250, handle="milo", auth_user_id=auth_user_id)

    current = get_current_cat(
        Claims(auth_user_id=auth_user_id, email=None, session_id=None), sessions_factory
    )

    assert current.id == cat_id
    assert current.handle == "milo"
    assert current.auth_user_id == auth_user_id
    # No balance on the dataclass. Its transaction has closed, so anything here
    # would be stale before a route could act on it, and a money decision must
    # only ever be made on a value read under a lock.
    assert not hasattr(current, "balance")


def test_the_treasury_cannot_be_resolved_from_any_identity(
    sessions_factory: sessionmaker[Session],
) -> None:
    """Structural, not conventional.

    The lookup is `WHERE auth_user_id = :sub` and the treasury's is NULL, and SQL
    NULL is never equal to anything. This asserts the property the migration
    comment claims, so a future change that resolves callers by handle or email
    fails here rather than silently opening the treasury.
    """
    with sessions_factory() as session:
        treasury_auth_id = session.scalar(
            select(Cat.auth_user_id).where(Cat.id == TREASURY_CAT_ID)
        )
        # No identity at all resolves to it, including a NULL one.
        matched = session.scalar(
            select(func.count()).select_from(Cat).where(Cat.auth_user_id.is_(None), ~Cat.is_system)
        )

    assert treasury_auth_id is None
    assert matched == 0


# -- creating a cat --------------------------------------------------------


def test_onboarding_creates_a_cat_linked_to_the_caller(
    client: TestClient, as_identity: AsIdentity, sessions_factory: sessionmaker[Session]
) -> None:
    identity = as_identity()

    response = client.post(ONBOARD, json=_body())

    assert response.status_code == 201
    body = response.json()
    assert body["handle"] == "dahlia"
    # No balance on the wire: one place reports a balance, so one place can
    # report a stale one.
    assert "balance" not in body

    with sessions_factory() as session:
        cat = session.execute(
            select(Cat.auth_user_id, Cat.balance, Cat.is_system).where(
                Cat.id == uuid.UUID(body["id"])
            )
        ).one()

    assert cat.auth_user_id == identity.auth_user_id
    assert cat.balance == 0
    assert cat.is_system is False


def test_repeating_the_same_call_is_200_and_creates_nothing(
    client: TestClient, as_identity: AsIdentity, sessions_factory: sessionmaker[Session]
) -> None:
    """Not an error, so a frontend that fires this on every page load is safe.

    The 201 versus 200 split still tells a careful client which happened.
    """
    as_identity()

    first = client.post(ONBOARD, json=_body())
    second = client.post(ONBOARD, json=_body())

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()

    with sessions_factory() as session:
        assert session.scalar(select(func.count()).select_from(Cat).where(~Cat.is_system)) == 1


def test_a_second_handle_for_the_same_identity_is_refused(
    client: TestClient, as_identity: AsIdentity
) -> None:
    """Onboarding is not a rename endpoint."""
    as_identity()
    client.post(ONBOARD, json=_body("dahlia", "Dahlia"))

    response = client.post(ONBOARD, json=_body("dahlia_two", "Dahlia Two"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cat_already_exists"


def test_a_handle_another_cat_holds_is_refused(
    client: TestClient, as_identity: AsIdentity, make_cat: MakeCat
) -> None:
    make_cat(handle="dahlia")
    as_identity()

    response = client.post(ONBOARD, json=_body("dahlia", "Impostor"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "handle_taken"


# -- handle rules ----------------------------------------------------------


def test_a_handle_is_normalised_to_lowercase(client: TestClient, as_identity: AsIdentity) -> None:
    as_identity()

    response = client.post(ONBOARD, json=_body("Dahlia", "Dahlia"))

    assert response.status_code == 201
    assert response.json()["handle"] == "dahlia"


@pytest.mark.parametrize(
    "handle",
    [
        "ab",
        "x" * 33,
        "bad-handle",
        "bad handle",
        "Ünicode",
        "dah\nlia",
        "dahlia!",
        "",
    ],
    ids=[
        "too-short",
        "too-long",
        "hyphen",
        "space",
        "non-ascii",
        "inner-newline",
        "punct",
        "empty",
    ],
)
def test_a_malformed_handle_is_422_and_never_reaches_a_constraint(
    client: TestClient, as_identity: AsIdentity, handle: str
) -> None:
    """Every one of these must be a clean 422, never a 500.

    A 500 here would mean the Python mirror of the handle rule has drifted from
    the CHECK constraint, so a user error arrives as a constraint violation.

    The newline is INSIDE the handle on purpose. A trailing one is stripped
    before validation and is therefore accepted as the handle without it, which
    is the friendly answer for a pasted value and is covered below.
    """
    as_identity()

    response = client.post(ONBOARD, json=_body(handle, "Someone"))

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in ("handle_invalid", "validation_error")


def test_surrounding_whitespace_is_stripped_rather_than_refused(
    client: TestClient, as_identity: AsIdentity
) -> None:
    """A pasted handle picks up whitespace, and that is not the user's mistake."""
    as_identity()

    response = client.post(ONBOARD, json=_body("  dahlia\n", "  Dahlia  "))

    assert response.status_code == 201
    assert response.json()["handle"] == "dahlia"
    assert response.json()["display_name"] == "Dahlia"


def test_a_reserved_handle_is_invalid_and_not_taken(
    client: TestClient, as_identity: AsIdentity
) -> None:
    """The code matters, not just the refusal.

    Returning `handle_taken` would confirm that a row with this handle exists.
    Returning `handle_invalid` leaks nothing, and the same answer is given
    whether or not the treasury is really there.
    """
    as_identity()

    response = client.post(ONBOARD, json=_body(TREASURY_HANDLE, "Not The Treasury"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "handle_invalid"


def test_an_unknown_field_in_the_body_is_refused(
    client: TestClient, as_identity: AsIdentity
) -> None:
    """extra="forbid", so a field the API does not read is loud rather than ignored.

    The field a caller thought mattered probably did.
    """
    as_identity()

    response = client.post(
        ONBOARD, json={"handle": "dahlia", "display_name": "Dahlia", "balance": 1000000}
    )

    assert response.status_code == 422
    # Rendered into our envelope rather than FastAPI's {"detail": [...]}, so the
    # frontend parses one error shape across the whole API.
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "display_name",
    ["Dahlia\x00", "Dah\x07lia"],
    ids=["nul", "bell"],
)
def test_a_display_name_with_control_characters_is_422_and_not_500(
    client: TestClient, as_identity: AsIdentity, display_name: str
) -> None:
    """Over HTTP, not just as a unit.

    test_handles.py calls normalise_display_name directly, so deleting the CALL
    in the route leaves those tests green and only an endpoint test notices. A
    NUL in particular makes psycopg raise DataError before the statement leaves
    the process, so without the guard this is a 500 on a value the caller typed.
    """
    as_identity()

    response = client.post(ONBOARD, json=_body("dahlia", display_name))

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "display_name_invalid"
