"""Pydantic wire shapes.

These are what crosses the HTTP boundary. The SQLAlchemy tables live in
`meowpay.models`; nothing here ever touches the database.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel


class HealthStatus(enum.StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class HealthResponse(BaseModel):
    status: HealthStatus
    version: str
    database: HealthStatus


class ErrorDetail(BaseModel):
    # A stable machine-readable string. The frontend branches on this, never on
    # the HTTP status, so a status can change without breaking a client.
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
