"""The ledger's rejections.

One exception per reason a movement can be refused. Each carries a stable `code`
the frontend branches on and the status the API maps it to. The status lives here
rather than in the route so there is one place where "insufficient funds is a 422"
is written down, and so a caller with no HTTP in front of it, the seed script or a
future worker, gets the same answer.

Everything here is a rejection the SERVICE makes, ahead of the database. The
CHECK constraints in `meowpay.models` are a backstop, not an error channel:
reaching one means the validation above it is wrong, so it should surface as an
unhandled IntegrityError and a 500 rather than be caught and dressed up as a user
error. `LedgerInvariantError` is the one deliberate exception, and it exists to
make an impossible state loud instead of silently wrong.

Nothing in this module imports FastAPI.
"""

from __future__ import annotations

import logging
import uuid
from typing import ClassVar

from meowpay.constants import MAX_AMOUNT


class LedgerError(Exception):
    """Base for every rejection the ledger makes."""

    code: ClassVar[str] = "ledger_error"
    status: ClassVar[int] = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AmountOutOfRangeError(LedgerError):
    """Pre-empts ck_transfers_amount_positive and ck_transfers_amount_within_cap."""

    code = "amount_out_of_range"

    def __init__(self, amount: object) -> None:
        # The amount is the caller's own input, so echoing a number back is
        # useful. Anything that is not an int is described by type only, so a
        # hostile body cannot push an arbitrary string into the response.
        shown = (
            amount
            if isinstance(amount, int) and not isinstance(amount, bool)
            else type(amount).__name__
        )
        super().__init__(
            f"Amount must be a whole number of treats between 1 and {MAX_AMOUNT}, got {shown}."
        )


class IdempotencyKeyInvalidError(LedgerError):
    """Pre-empts ck_transfers_idempotency_key_shape and the varchar(255) cast."""

    code = "idempotency_key_invalid"

    def __init__(self, reason: str) -> None:
        # Never echoes the key. It is caller-controlled and unbounded, so a 300KB
        # key would otherwise become a 300KB error body.
        super().__init__(f"Idempotency key is invalid: {reason}.")


class SelfTransferError(LedgerError):
    """Pre-empts ck_transfers_parties_differ."""

    code = "self_transfer"

    def __init__(self) -> None:
        super().__init__("A cat cannot send treats to itself.")


class TreasuryIsNotAPartyError(LedgerError):
    """Nothing in the schema stops this, so the service is the only guard.

    A transfer to the treasury would burn treats out of circulation through a path
    meant to move them, and a transfer from it would be minting. Deposits are the
    one sanctioned way the treasury moves treats.
    """

    code = "treasury_is_not_a_party"

    def __init__(self) -> None:
        super().__init__("The treasury cannot be a party to a transfer.")


class SenderNotFoundError(LedgerError):
    code = "sender_not_found"
    status = 404

    def __init__(self, cat_id: uuid.UUID) -> None:
        super().__init__(f"No cat with id {cat_id}.")


class RecipientNotFoundError(LedgerError):
    code = "recipient_not_found"
    status = 404

    def __init__(self, description: str) -> None:
        super().__init__(f"No cat matching {description}.")


class InsufficientFundsError(LedgerError):
    """The service's overdraft refusal. ck_cats_balance_non_negative is the backstop."""

    code = "insufficient_funds"

    def __init__(self, *, balance: int, amount: int) -> None:
        # Safe to disclose: the sender comes from the auth context and never from
        # a request body, so the only cat that can provoke this message is the one
        # whose balance it describes.
        super().__init__(f"Balance is {balance} treats, which is short of {amount}.")
        self.balance = balance
        self.amount = amount


class BalanceLimitExceededError(LedgerError):
    """Pre-empts ck_cats_balance_is_js_safe, in both directions.

    The upper bound catches a cat credited past what a browser can represent
    exactly. The lower bound catches the treasury, whose balance is the negative
    of everything in circulation and which therefore reaches the limit first.
    """

    code = "balance_limit_exceeded"

    def __init__(self) -> None:
        super().__init__(
            "This movement would push a balance outside the range that can be "
            "represented exactly. It has not been applied."
        )


class IdempotencyKeyReusedError(LedgerError):
    """Same owner, same key, different movement.

    409 rather than 422: the request is well formed and the problem is a conflict
    with state that already exists.
    """

    code = "idempotency_key_reused"
    status = 409

    def __init__(self) -> None:
        super().__init__(
            "This idempotency key has already been used for a different movement. "
            "Use a new key, or repeat the original request exactly."
        )


class LedgerBusyError(LedgerError):
    """lock_timeout fired (SQLSTATE 55P03).

    The engine sets lock_timeout=3s, so a party under heavy contention gives up
    rather than hanging the request. That is retryable and 503 says so, where the
    500 it would otherwise become says the opposite.
    """

    code = "ledger_busy"
    status = 503

    def __init__(self) -> None:
        super().__init__("The accounts involved are busy. Retry with the same idempotency key.")


class LedgerInvariantError(LedgerError):
    """An impossible state. Always a bug in the ledger, never a caller error.

    Raised when the idempotency claim conflicts but the replay lookup finds
    nothing. That combination means the lock no longer covers the uniqueness
    scope, which is the one property the claim-and-replay design rests on.

    Deliberately opaque on the wire and deliberately fatal to the request. The
    plausible cause is a genuine concurrent duplicate, and settling it twice is
    the exact bug this mechanism exists to prevent.
    """

    code = "internal_error"
    status = 500

    def __init__(self, detail: str) -> None:
        # The wire message is deliberately opaque, so the detail has to reach the
        # log here or it reaches nobody: the middleware logs exc_info, whose
        # traceback line is that same opaque message.
        logging.getLogger(__name__).error("ledger invariant violated: %s", detail)
        super().__init__("An unexpected error occurred.")
        self.detail = detail
