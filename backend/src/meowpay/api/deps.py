"""FastAPI dependencies.

The session factory and the ledger are reached through dependencies rather than
imported directly, so a test can override them onto its own throwaway database.
Importing `get_sessions()` inside a route would bind the route to the
process-wide engine built from DATABASE_URL, and no amount of fixture work could
redirect it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from meowpay.ledger import Ledger
from meowpay.session import get_sessions


def sessions() -> sessionmaker[Session]:
    return get_sessions()


Sessions = Annotated[sessionmaker[Session], Depends(sessions)]


def ledger(factory: Sessions) -> Ledger:
    """The transfer service, over whichever session factory is in force."""
    return Ledger(factory)


LedgerDep = Annotated[Ledger, Depends(ledger)]
