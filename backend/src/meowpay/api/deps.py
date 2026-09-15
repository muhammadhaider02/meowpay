"""FastAPI dependencies.

The session factory is reached through a dependency rather than imported
directly, so a test can override it onto its own throwaway database. Importing
`get_sessions()` inside a route would bind the route to the process-wide engine
built from DATABASE_URL, and no amount of fixture work could redirect it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from meowpay.session import get_sessions


def sessions() -> sessionmaker[Session]:
    return get_sessions()


Sessions = Annotated[sessionmaker[Session], Depends(sessions)]
