"""Whether this process can reach a correctly migrated database.

By the usual taxonomy this is a **readiness** check, not a liveness one: it
touches a dependency and it can fail. There is no separate liveness endpoint,
and that is now a deliberate trade rather than an absence.

Render is pointed at this path, so it also gates deploys: traffic does not move
to a new instance until this passes, which means a build that cannot read the
database never replaces one that can. A dependency-free probe would be a weaker
gate, waving through exactly that. The cost is the other direction: a database
that is merely having a bad minute fails a deploy of code that is fine, and a
hotfix cannot ship while it is degraded.

Two limits worth knowing before trusting it. It proves the treasury row from
migration 0002 exists, so a **later** migration that was never applied leaves
this green while routes 500; it catches an unmigrated database, not drift. And
`connect_timeout` is 10s, so an unreachable database makes each probe hang that
long, which a platform may report as a timeout rather than as a database
problem.
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
