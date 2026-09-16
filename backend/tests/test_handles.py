"""Handle and display name normalisation, called directly.

No database and no network, deliberately. These functions are reachable over
HTTP only through `OnboardRequest`, which strips whitespace and bounds length
before they ever run, so an endpoint test cannot reach several of their guards:
`re.fullmatch`, the `isascii` check and the reserved prefix can each be deleted
with every endpoint test still passing.

A guard no test can reach is not a guard, it is a comment.
"""

from __future__ import annotations

import re

import pytest

from meowpay.api.routes.cats import normalise_display_name, normalise_handle
from meowpay.constants import HANDLE_REGEX, RESERVED_HANDLE_PREFIX
from meowpay.errors import DisplayNameInvalidError, HandleInvalidError


def test_a_trailing_newline_is_refused_which_is_why_fullmatch_is_used() -> None:
    """The one case that separates `re.fullmatch` from `re.match` with anchors.

    Python's `$` also matches immediately before a trailing newline. Postgres's
    does not. So `re.match(r"^[a-z0-9_]{3,32}$", "dahlia\\n")` succeeds, hands
    "dahlia\\n" to a CHECK constraint that refuses it, and a user error becomes a
    500.
    """
    # The premise, asserted rather than assumed, so this test explains itself
    # when it fails.
    assert re.match(rf"^{HANDLE_REGEX}$", "dahlia\n"), "premise broken: `$` should match here"
    assert not re.fullmatch(HANDLE_REGEX, "dahlia\n")

    with pytest.raises(HandleInvalidError):
        normalise_handle("dahlia\n")


def test_surrounding_whitespace_is_refused_rather_than_silently_accepted() -> None:
    """These functions do not strip, and that is deliberate.

    `OnboardRequest` strips at the boundary, so over HTTP a pasted handle is
    still accepted. Stripping again here would make the newline guard above
    unreachable and therefore untestable.
    """
    for padded in ("  dahlia", "dahlia  ", "\tdahlia"):
        with pytest.raises(HandleInvalidError):
            normalise_handle(padded)


def test_non_ascii_is_refused_with_a_message_naming_what_is_allowed() -> None:
    """The `isascii` guard exists only for the message.

    The regex would refuse these anyway, so deleting the guard changes no status
    code and no endpoint test notices. What it changes is whether the user is
    told which characters are allowed, or handed an opaque pattern failure after
    their input has already been lowercased into something they did not type.
    """
    with pytest.raises(HandleInvalidError) as caught:
        normalise_handle("Ünicode")

    # "a to z" appears ONLY in the isascii branch. Asserting on "letters"
    # would pass against the default message too, so deleting the guard
    # would not fail this test.
    assert "a to z" in caught.value.message
    assert caught.value.code == "handle_invalid"


def test_uppercase_is_normalised_rather_than_refused() -> None:
    assert normalise_handle("DAHLIA") == "dahlia"
    assert normalise_handle("Dahlia_01") == "dahlia_01"


def test_lower_and_casefold_cannot_differ_here() -> None:
    """Recorded because the obvious comment about casefold would be wrong.

    `casefold` maps some characters to sequences, so in general it can turn one
    rejected character into two accepted ones. It cannot here: `isascii` runs
    first, and on ASCII input `lower` and `casefold` are identical. So swapping
    one for the other is unobservable, and no test can distinguish them.

    Worth an assertion rather than a claim in a docstring, because a reader who
    later moves the isascii check below the lowercasing would silently make the
    difference reachable again.
    """
    assert "ß".casefold() != "ß".lower()  # the general case is real
    for ascii_input in ("DAHLIA", "Dahlia_01", "X" * 32):
        assert ascii_input.lower() == ascii_input.casefold()

    # And the non-ASCII case never reaches the lowercasing at all.
    with pytest.raises(HandleInvalidError) as caught:
        normalise_handle("dahliaß")
    assert "a to z" in caught.value.message


def test_fullwidth_forms_are_not_normalised_onto_ascii() -> None:
    """No NFKC. It would let two visually distinct sign-ups collide on one handle."""
    with pytest.raises(HandleInvalidError):
        normalise_handle("ｄａｈｌｉａ")


@pytest.mark.parametrize("handle", ["ab", "x" * 33, "bad-handle", "bad.handle", "", "  "])
def test_a_malformed_handle_is_refused(handle: str) -> None:
    with pytest.raises(HandleInvalidError):
        normalise_handle(handle)


def test_a_reserved_handle_is_refused_as_invalid_and_not_as_taken() -> None:
    """The code matters, not the refusal.

    `handle_taken` would confirm a row exists. `handle_invalid` leaks nothing and
    gives the same answer whether or not the reserved cat is really there.
    """
    with pytest.raises(HandleInvalidError) as caught:
        normalise_handle(f"{RESERVED_HANDLE_PREFIX}support")

    assert caught.value.code == "handle_invalid"


@pytest.mark.parametrize(
    "name",
    ["Dahlia\x00", "Dah\x07lia", "Dahlia\x7f", "Dah\nlia", "Dah\tlia"],
    ids=["nul", "bell", "del", "newline", "tab"],
)
def test_a_display_name_with_control_characters_is_refused(name: str) -> None:
    """A NUL is the sharp one.

    psycopg raises DataError before the statement leaves the process, so without
    this guard it is a 500 on a value the caller typed. The rest of the C0 range
    is storable and simply has no business in a display name.
    """
    with pytest.raises(DisplayNameInvalidError) as caught:
        normalise_display_name(name)

    assert caught.value.status == 422
    assert caught.value.code == "display_name_invalid"


def test_an_ordinary_display_name_is_left_alone() -> None:
    assert normalise_display_name("Dahlia") == "Dahlia"
    # Not an identifier, so anything printable is fine.
    assert normalise_display_name("Dahlia 🐈 the Third") == "Dahlia 🐈 the Third"
