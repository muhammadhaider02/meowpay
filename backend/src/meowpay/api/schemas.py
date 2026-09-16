"""Pydantic wire shapes.

These are what crosses the HTTP boundary. The SQLAlchemy tables live in
`meowpay.models`; nothing here ever touches the database.
"""

from __future__ import annotations

import enum
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class HealthStatus(enum.StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class HealthResponse(BaseModel):
    status: HealthStatus
    version: str
    database: HealthStatus


class OnboardRequest(BaseModel):
    """The one thing a new cat chooses for itself.

    strict and extra="forbid" for the same reason every request model here has
    them: a body carrying a field we do not read should be a loud 422 and not a
    silent no-op, because the field a caller thought mattered probably did.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    # Validated here for shape and again in the route against the shared regex,
    # which is also what the CHECK constraint is built from. Pydantic bounds the
    # length so a megabyte handle never reaches a regex.
    handle: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32)]
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]


class CatResponse(BaseModel):
    """A cat, as the API describes one.

    Deliberately no balance. `GET /api/v1/me` is the one place a balance is
    reported, so there is one place that can report a stale one.
    """

    id: uuid.UUID
    handle: str
    display_name: str


class ErrorDetail(BaseModel):
    # A stable machine-readable string. The frontend branches on this, never on
    # the HTTP status, so a status can change without breaking a client.
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
