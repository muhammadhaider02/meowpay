"""FastAPI dependencies.

The session factory, the ledger and the token verifier are reached through
dependencies rather than imported directly, so a test can override them onto its
own throwaway database or its own signing key. Importing `get_sessions()` inside
a route would bind the route to the process-wide engine built from DATABASE_URL,
and no amount of fixture work could redirect it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from meowpay.auth import Claims, CurrentCat, TokenVerifier, get_verifier
from meowpay.errors import CatNotOnboardedError, MissingCredentialsError
from meowpay.ledger import Ledger
from meowpay.models import Cat
from meowpay.session import get_sessions


def sessions() -> sessionmaker[Session]:
    return get_sessions()


Sessions = Annotated[sessionmaker[Session], Depends(sessions)]


def ledger(factory: Sessions) -> Ledger:
    """The transfer service, over whichever session factory is in force."""
    return Ledger(factory)


LedgerDep = Annotated[Ledger, Depends(ledger)]


def verifier() -> TokenVerifier:
    return get_verifier()


VerifierDep = Annotated[TokenVerifier, Depends(verifier)]

# auto_error=False is load bearing. With the default, HTTPBearer raises its own
# HTTPException, a 403 for a missing header rather than a 401, carrying a
# {"detail": ...} body that is not the error envelope the frontend branches on.
# Returning None instead lets us raise our own error into our own shape.
# Registering it at all is what gives /docs an Authorize button.
_bearer = HTTPBearer(auto_error=False)


def claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    token_verifier: VerifierDep,
) -> Claims:
    """A verified token, and nothing more.

    Separate from `get_current_cat` because onboarding needs a verified caller
    and by definition has no cat row yet, so it can never depend on one.

    A plain `def` on purpose, never `async def`. The verifier's key fetch is
    blocking, so Starlette must be free to run this in the threadpool. As an
    `async def` it would block the event loop for the whole fetch every time the
    key cache expires.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise MissingCredentialsError()
    # Through the dependency, not `get_verifier()` directly. Calling it directly
    # works and makes `VerifierDep` a lie: an override would silently do nothing
    # and the test would quietly exercise the real process-wide verifier against
    # the real project.
    return token_verifier.verify(credentials.credentials)


ClaimsDep = Annotated[Claims, Depends(claims)]


def get_current_cat(identity: ClaimsDep, factory: Sessions) -> CurrentCat:
    """Resolve a verified token to the cat it owns.

    Opens its own short read session and returns plain values. Two reasons, and
    the usual one about SQLAlchemy autobegin is not among them, because `Ledger`
    takes a sessionmaker and builds a fresh Session per movement:

    1. `Ledger`'s contract. It owns its transaction boundaries precisely so a
       caller cannot commit around it and drop the row locks halfway through. A
       request-scoped Session here would create constant pressure to hand it in,
       and the day someone does, the idempotency argument silently breaks.
    2. Connection lifetime. A request-scoped session holds a pooled connection
       and an open read transaction for the whole request, so under load the pool
       starves on requests that are only waiting on the ledger.
    """
    with factory() as session:
        row = session.execute(
            select(Cat.id, Cat.handle, Cat.display_name, Cat.auth_user_id).where(
                Cat.auth_user_id == identity.auth_user_id,
                # Redundant: ck_cats_only_system_lacks_auth_user guarantees the
                # treasury's auth_user_id is NULL, and NULL is never equal to
                # anything. Kept so that relaxing that constraint fails a test
                # rather than opening a hole.
                ~Cat.is_system,
            )
        ).one_or_none()

    if row is None:
        # 403 and not 401: the token is fine, so re-authenticating would return
        # an identical one and the client would loop forever.
        raise CatNotOnboardedError()

    return CurrentCat(
        id=row.id,
        handle=row.handle,
        display_name=row.display_name,
        auth_user_id=row.auth_user_id,
    )


CurrentCatDep = Annotated[CurrentCat, Depends(get_current_cat)]
