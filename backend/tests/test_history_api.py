"""The read side: balance, statement and the recipient picker.

Same stubbing as the movement suite. Only `deps.claims` is overridden, so the
cat lookup, the pagination and the join back to the counterparty all run against
the real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from meowpay.auth import Claims
from meowpay.constants import TREASURY_HANDLE

pytestmark = pytest.mark.db

MakeCat = Callable[..., uuid.UUID]
AsIdentity = Callable[..., Claims]

ME = "/api/v1/me"
ENTRIES = "/api/v1/me/entries"
CATS = "/api/v1/cats"
TRANSFERS = "/api/v1/transfers"
DEPOSITS = "/api/v1/deposits"


def _signed_in(
    make_cat: MakeCat, as_identity: AsIdentity, balance: int = 0, handle: str | None = None
) -> uuid.UUID:
    auth_user_id = uuid.uuid4()
    return make_cat(
        balance=balance, handle=handle, auth_user_id=as_identity(auth_user_id).auth_user_id
    )


# -- the balance ------------------------------------------------------------


def test_me_reports_the_signed_in_cat_and_its_balance(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    _signed_in(make_cat, as_identity, balance=1500, handle="dahlia")

    response = client.get(ME)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["handle"] == "dahlia"
    assert body["balance"] == 1500


def test_me_reads_the_balance_fresh_rather_than_from_the_dependency(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """`CurrentCat` carries no balance on purpose.

    If this endpoint ever started reporting one from the dependency, the number
    would be read in a transaction that closed before the route ran. Moving
    treats and reading again in the same test is what would catch that.
    """
    make_cat(handle="milo")
    _signed_in(make_cat, as_identity, balance=500, handle="dahlia")

    client.post(
        TRANSFERS,
        json={"to_handle": "milo", "amount": 200},
        headers={"Idempotency-Key": "history-0000001"},
    )
    response = client.get(ME)

    assert response.json()["balance"] == 300


def test_me_without_a_cat_is_403(client: TestClient, as_identity: AsIdentity) -> None:
    as_identity()

    response = client.get(ME)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cat_not_onboarded"


# -- the statement ----------------------------------------------------------


def test_entries_are_newest_first_and_name_the_counterparty(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    make_cat(handle="milo")
    _signed_in(make_cat, as_identity, balance=0, handle="dahlia")

    client.post(DEPOSITS, json={"amount": 500}, headers={"Idempotency-Key": "deposit-0000001"})
    client.post(
        TRANSFERS,
        json={"to_handle": "milo", "amount": 120},
        headers={"Idempotency-Key": "sent-00000001"},
    )

    response = client.get(ENTRIES)

    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert len(entries) == 2

    # Newest first: the transfer, then the deposit that funded it.
    sent, topped_up = entries
    assert sent["kind"] == "transfer"
    assert sent["amount"] == -120, "a debit is negative from this cat's side"
    assert sent["balance_after"] == 380
    assert sent["counterparty_handle"] == "milo"

    assert topped_up["kind"] == "deposit"
    assert topped_up["amount"] == 500
    assert topped_up["counterparty_handle"] == TREASURY_HANDLE


def test_the_recipient_sees_the_mirror_image_of_the_same_movement(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """Double entry, from the other side.

    The same transfer must appear as a credit on the recipient's statement, with
    the sender named. Two lines, summing to zero.
    """
    recipient_auth = uuid.uuid4()
    make_cat(handle="milo", auth_user_id=recipient_auth)
    _signed_in(make_cat, as_identity, balance=500, handle="dahlia")

    client.post(
        TRANSFERS,
        json={"to_handle": "milo", "amount": 120},
        headers={"Idempotency-Key": "mirror-00000001"},
    )

    as_identity(recipient_auth)
    entries = client.get(ENTRIES).json()["entries"]

    assert len(entries) == 1
    assert entries[0]["amount"] == 120
    assert entries[0]["counterparty_handle"] == "dahlia"


def test_a_statement_shows_only_its_own_cats_lines(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    stranger_auth = uuid.uuid4()
    make_cat(handle="stranger", auth_user_id=stranger_auth, balance=900)
    make_cat(handle="milo")
    _signed_in(make_cat, as_identity, balance=500, handle="dahlia")

    client.post(
        TRANSFERS,
        json={"to_handle": "milo", "amount": 10},
        headers={"Idempotency-Key": "mine-00000001"},
    )

    as_identity(stranger_auth)

    assert client.get(ENTRIES).json()["entries"] == []


def test_the_statement_pages_backwards_without_repeating_or_skipping(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """Keyset pagination, walked to the end.

    Five movements, two at a time. The cursor is what the previous page
    returned, never an offset, so a movement landing mid-walk cannot shift the
    window and make a row appear twice.
    """
    make_cat(handle="milo")
    _signed_in(make_cat, as_identity, balance=0, handle="dahlia")

    for index in range(5):
        client.post(
            DEPOSITS, json={"amount": 100}, headers={"Idempotency-Key": f"page-{index:08d}"}
        )

    seen: list[int] = []
    cursor: int | None = None
    for _ in range(4):
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["before"] = cursor
        page = client.get(ENTRIES, params=params).json()
        seen.extend(entry["id"] for entry in page["entries"])
        cursor = page["next_before"]
        if cursor is None:
            break

    assert cursor is None, "the walk should have reached the last page"
    assert len(seen) == 5
    assert len(set(seen)) == 5, "a row was returned on two different pages"
    assert seen == sorted(seen, reverse=True), "entries must stay newest first across pages"


def test_the_last_page_reports_no_cursor(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    """The off-by-one that would make a client loop forever.

    Exactly `limit` rows remaining must report `next_before: null`, not a cursor
    onto an empty page.
    """
    _signed_in(make_cat, as_identity, balance=0, handle="dahlia")

    for index in range(2):
        client.post(
            DEPOSITS, json={"amount": 100}, headers={"Idempotency-Key": f"exact-{index:08d}"}
        )

    page = client.get(ENTRIES, params={"limit": 2}).json()

    assert len(page["entries"]) == 2
    assert page["next_before"] is None


@pytest.mark.parametrize("limit", [0, -1, 101], ids=["zero", "negative", "over-the-cap"])
def test_an_out_of_range_page_size_is_refused(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity, limit: int
) -> None:
    _signed_in(make_cat, as_identity, handle="dahlia")

    response = client.get(ENTRIES, params={"limit": limit})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_a_statement_with_no_movements_is_an_empty_page_and_not_a_404(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    _signed_in(make_cat, as_identity, handle="lotus")

    response = client.get(ENTRIES)

    assert response.status_code == 200
    assert response.json() == {"entries": [], "next_before": None}


# -- the recipient picker ---------------------------------------------------


def test_the_directory_lists_other_cats_without_balances(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    make_cat(handle="milo", balance=999)
    make_cat(handle="lotus")
    _signed_in(make_cat, as_identity, handle="dahlia")

    response = client.get(CATS)

    assert response.status_code == 200, response.text
    listed = response.json()
    assert [cat["handle"] for cat in listed] == ["lotus", "milo"]
    # A directory reporting balances would tell every cat who is worth robbing.
    assert all("balance" not in cat for cat in listed)


def test_the_directory_excludes_the_caller_and_the_treasury(
    client: TestClient, make_cat: MakeCat, as_identity: AsIdentity
) -> None:
    make_cat(handle="milo")
    _signed_in(make_cat, as_identity, handle="dahlia")

    handles = [cat["handle"] for cat in client.get(CATS).json()]

    assert "dahlia" not in handles, "offering a self transfer invites the error it refuses"
    assert TREASURY_HANDLE not in handles


def test_the_directory_needs_a_cat_and_not_just_a_token(
    client: TestClient, as_identity: AsIdentity
) -> None:
    """Depends on `CurrentCatDep`, so an identity cannot enumerate cats before
    it has onboarded."""
    as_identity()

    response = client.get(CATS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cat_not_onboarded"
