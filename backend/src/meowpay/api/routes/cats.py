"""Onboarding: turning a Supabase identity into a cat.

Depends on a verified token and NOT on `get_current_cat`, because by definition
the caller has no cat row yet. That split is the whole reason `deps.claims`
exists separately.

Not a trigger on `auth.users`, which is Supabase's own documented pattern for
this. Four reasons, strongest first. A trigger cannot invent a validated handle
without trusting client-supplied `raw_user_meta_data`. A trigger error aborts the
signup transaction itself, so a duplicate handle would make sign-up fail with
GoTrue's opaque "Database error saving new user" and the user's identity would
never be created, leaving them unable to tell a taken handle from an outage.
A SECURITY DEFINER function on a table Alembic does not reflect is invisible to
`alembic check`, so it can drift or be dropped unnoticed. And it would be a
second, elevated, invisible writer of a money table, which is the one thing this
codebase claims does not exist.

The cost of doing it here is a window where an identity exists with no cat. That
is handled with a first-class 403 rather than pretended away.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meowpay.api.deps import ClaimsDep, Sessions
from meowpay.api.schemas import CatResponse, OnboardRequest
from meowpay.constants import HANDLE_REGEX, RESERVED_HANDLE_PREFIX
from meowpay.errors import (
    CatAlreadyExistsError,
    DisplayNameInvalidError,
    HandleInvalidError,
    HandleTakenError,
)
from meowpay.models import Cat

router = APIRouter(prefix="/cats", tags=["cats"])


def normalise_handle(raw: str) -> str:
    """Normalise and validate, in an order that matters.

    ASCII is checked BEFORE lowercasing, so a handle with accented characters
    gets an error naming the allowed characters rather than an opaque pattern
    failure after the characters have already been mangled.

    `.lower()` and never `.casefold()`. Casefold maps some characters to
    sequences, so it would turn one rejected character into two accepted ones,
    which is a surprise nobody wants in an identifier.

    No NFKC normalisation. It maps fullwidth forms onto ASCII, which would let
    two visually distinct sign-ups collide on one handle.

    `re.fullmatch` and never `re.match` with anchors. Python's `$` also matches
    before a trailing newline and Postgres's does not, so `re.match(r"^...$")`
    accepts "dahlia\\n" and hands it to a CHECK constraint that refuses it,
    turning a user error into a 500.

    Input arrives already stripped, because `OnboardRequest` does that at the
    boundary. Stripping again here would be harmless and would also make the
    paragraph above untrue: a trailing newline could then never reach the regex,
    the two calls really would be interchangeable, and swapping one for the other
    would break nothing that any test could see. A guard nothing can reach is not
    a guard.
    """
    handle = raw
    if not handle.isascii():
        raise HandleInvalidError(
            "A handle uses lowercase letters a to z, digits and underscores only."
        )

    handle = handle.lower()
    if not re.fullmatch(HANDLE_REGEX, handle):
        raise HandleInvalidError()

    # Reserved handles are refused as invalid and never as taken, so the response
    # does not confirm which rows exist.
    if handle.startswith(RESERVED_HANDLE_PREFIX):
        raise HandleInvalidError(f"Handles starting with {RESERVED_HANDLE_PREFIX!r} are reserved.")

    return handle


def normalise_display_name(raw: str) -> str:
    """Refuse control characters.

    `handle` gets the shared regex; this column had only a length bound, which
    is the same class of gap in the field nobody thought to check. A NUL byte
    makes psycopg raise DataError before the statement even leaves the process,
    so it arrives as a 500 on a value the caller typed. The rest of the C0 range
    is storable and has no business in a display name.

    Input arrives already stripped, the same as the handle.
    """
    if any(char < " " or char == "\x7f" for char in raw):
        raise DisplayNameInvalidError()
    return raw


@router.post(
    "",
    response_model=CatResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create the cat for the signed-in account",
)
def onboard(
    body: OnboardRequest,
    identity: ClaimsDep,
    factory: Sessions,
    response: Response,
) -> CatResponse:
    """201 on create, 200 when the same call is repeated.

    Repeating with the same handle is deliberately NOT an error. A frontend that
    fires this on every load of the onboarding page is then safe, and the
    201-versus-200 split still tells a careful client which happened.
    """
    handle = normalise_handle(body.handle)
    display_name = normalise_display_name(body.display_name)

    with factory.begin() as session:
        created = session.execute(
            pg_insert(Cat)
            .values(
                id=uuid.uuid4(),
                handle=handle,
                display_name=display_name,
                auth_user_id=identity.auth_user_id,
            )
            # No conflict target: this has to cover BOTH uq_cats_handle and
            # uq_cats_auth_user_id, and naming one would let the other raise.
            #
            # DO NOTHING rather than catching IntegrityError, for the reason the
            # ledger gives: a caught IntegrityError leaves the transaction in
            # 25P02 and makes the follow-up SELECT impossible. And never
            # DO UPDATE, which would let a second call re-point auth_user_id at a
            # different identity.
            .on_conflict_do_nothing()
            .returning(Cat.id, Cat.handle, Cat.display_name)
        ).first()

        if created is not None:
            return CatResponse(
                id=created.id, handle=created.handle, display_name=created.display_name
            )

        # Something conflicted. Whose row is it?
        #
        # ON CONFLICT DO NOTHING waits on a conflicting in-flight insert rather
        # than skipping past it, so by the time this runs the winner has
        # committed and this read sees it. That is what makes two simultaneous
        # sign-ups on one handle resolve to one 201 and one 409 rather than two
        # 201s or a 500.
        existing = session.execute(
            select(Cat.id, Cat.handle, Cat.display_name).where(
                Cat.auth_user_id == identity.auth_user_id
            )
        ).one_or_none()

    if existing is None:
        # No cat for this identity, so the collision was on the handle.
        raise HandleTakenError(handle)

    if existing.handle != handle:
        # This identity has a cat already, under a different name. Onboarding is
        # not a rename endpoint.
        raise CatAlreadyExistsError(existing.handle)

    response.status_code = status.HTTP_200_OK
    return CatResponse(
        id=existing.id, handle=existing.handle, display_name=existing.display_name
    )
