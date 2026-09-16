"""Pydantic wire shapes.

These are what crosses the HTTP boundary. The SQLAlchemy tables live in
`meowpay.models`; nothing here ever touches the database.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# The one import from models, and it is an enum rather than a table. Redefining
# it here would be a second definition of a value that is CHECK constrained in
# the database, which is the drift this file has no way to detect.
from meowpay.models import MovementKind


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


class MeResponse(BaseModel):
    """The signed-in cat, balance included.

    The only endpoint that reports a balance. It is read outside any lock, so it
    is true as of the read and not a moment longer; a client must never make a
    spending decision on it. The ledger rechecks funds under a row lock, which is
    the only check that counts.
    """

    id: uuid.UUID
    handle: str
    display_name: str
    balance: int


class MovementRequestBase(BaseModel):
    """Shared config, not a shared field.

    `strict` and `extra="forbid"` for the reason `OnboardRequest` gives: a body
    carrying a field we do not read should be a loud 422, because the field the
    caller thought mattered probably did.

    `amount` is deliberately NOT bounded here. `ledger._check_amount` mirrors
    `ck_transfers_amount_positive` and `ck_transfers_amount_within_cap`, and a
    second bound in this file is a second thing to forget when the constraint
    moves. Strict mode still rejects a float, a numeric string and a bool, which
    is the part Pydantic is genuinely better at.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    amount: int


class TransferRequest(MovementRequestBase):
    # The recipient is named, never given as an id. A uuid in a request body
    # invites a client to hold one, and the handle is what the sender actually
    # typed. It is normalised with the same function onboarding uses.
    to_handle: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
    ]


class DepositRequest(MovementRequestBase):
    """No recipient field.

    A deposit credits the caller, resolved from the token. Accepting a target
    here would let anyone mint treats into anyone else's account.
    """


class MovementResponse(BaseModel):
    """A settled movement, and the receipt for it.

    `replayed` is the honest part of the contract. A repeated call with the same
    idempotency key returns 200 with this true and moves nothing.
    """

    id: uuid.UUID
    kind: MovementKind
    amount: int
    idempotency_key: str

    # The caller's balance the instant this movement settled: the sender for a
    # transfer, the recipient for a deposit.
    #
    # On a replay this is the HISTORICAL value read back out of the ledger line,
    # not the balance now. A client repeating a week-old request gets a week-old
    # number, which is why this is not called `balance` and why `GET /api/v1/me`
    # is the only place a current balance comes from.
    balance_after: int

    created_at: datetime
    replayed: bool


class EntryResponse(BaseModel):
    """One line of the double-entry ledger, from one cat's point of view.

    `amount` is signed: negative debits the cat, positive credits it. The
    counterparty is denormalized onto the row, so reading a statement needs no
    join back through `transfers`.
    """

    id: int
    transfer_id: uuid.UUID
    kind: MovementKind
    amount: int
    balance_after: int
    counterparty_handle: str
    counterparty_display_name: str
    created_at: datetime


class EntryPage(BaseModel):
    """Keyset pagination, not offset.

    `entries.id` is a BIGSERIAL and the index is on `(cat_id, id DESC)`, so
    `id < :before` is a range scan whatever the page depth. OFFSET would make
    page fifty read fifty pages, and it would also skip or repeat rows when a new
    movement lands between requests, which is guaranteed here because the thing
    being paginated is a live money feed.
    """

    entries: list[EntryResponse]

    # The cursor for the next page, or null when this is the last one. The client
    # passes it back as `before` and never constructs it.
    next_before: int | None = None


class ErrorDetail(BaseModel):
    # A stable machine-readable string. The frontend branches on this, never on
    # the HTTP status, so a status can change without breaking a client.
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
