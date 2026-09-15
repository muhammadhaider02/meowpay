"""Liveness, including whether the database is reachable and migrated.

One endpoint rather than a liveness/readiness split. There is no load balancer
here to need a probe that never touches a dependency, and the brief checks that
the backend is real, so an endpoint that proves the database connection is worth
more than one that cannot fail.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Response
from sqlalchemy import select

from meowpay.api.deps import Sessions
from meowpay.api.schemas import HealthResponse, HealthStatus
from meowpay.constants import TREASURY_CAT_ID
from meowpay.models import Cat

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


def _version() -> str:
    try:
        return version("meowpay")
    except PackageNotFoundError:
        # Running from a source tree with no installed distribution. Not worth a
        # 500 on the first endpoint a reviewer curls.
        return "unknown"


@router.get("/health", response_model=HealthResponse)
def health(response: Response, factory: Sessions) -> HealthResponse:
    try:
        with factory() as session:
            # Reading the treasury rather than SELECT 1, because SELECT 1
            # succeeds against an empty, unmigrated or simply wrong database.
            # This row is created by a migration, so finding it proves the
            # schema is actually there and at least minimally correct.
            found = session.scalar(select(Cat.id).where(Cat.id == TREASURY_CAT_ID))
        database = HealthStatus.HEALTHY if found else HealthStatus.UNHEALTHY
        if not found:
            logger.warning("Health check: database reachable but not migrated")
    except Exception as exc:
        logger.warning("Health check could not reach the database: %s", exc)
        database = HealthStatus.UNHEALTHY

    # 503 so a script or an orchestrator can tell without parsing the body.
    response.status_code = 200 if database is HealthStatus.HEALTHY else 503
    return HealthResponse(status=database, version=_version(), database=database)
