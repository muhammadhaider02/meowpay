"""Every refusal this service makes on purpose.

One exception per reason a request can be turned away: the ledger's rejections,
the ways a caller can fail to be identified, and the ways onboarding can refuse a
handle. Each carries a stable `code` the frontend branches on and the status the
API maps it to. The status lives here rather than in the route so there is one
place where "insufficient funds is a 422" is written down, and so a caller with
no HTTP in front of it, the seed script or a future worker, gets the same answer.

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


class AppError(Exception):
    """Base for every refusal this service makes on purpose.

    One base so there is one exception handler and one place the error envelope
    is built. Starlette walks an exception's __mro__ when looking for a handler,
    so registering this one covers every subclass below it.
    """

    code: ClassVar[str] = "error"
    status: ClassVar[int] = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LedgerError(AppError):
    """Base for every rejection the ledger makes."""

    code: ClassVar[str] = "ledger_error"
    status: ClassVar[int] = 422


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

    `meowpay.ledger` sets lock_timeout on its own transaction, so a party under
    heavy contention gives up rather than hanging the request. That is retryable
    and 503 says so, where the 500 it would otherwise become says the opposite.
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


# -- identity --------------------------------------------------------------
#
# Named AccessToken* rather than InvalidTokenError and ExpiredSignatureError,
# which are what PyJWT exports. Identically named locals would shadow them in
# any module that imports both, and produce an `except` clause that silently
# catches the wrong hierarchy.


class AuthError(AppError):
    """Base for every refusal to identify a caller."""

    code: ClassVar[str] = "unauthenticated"
    status: ClassVar[int] = 401


class MissingCredentialsError(AuthError):
    """No Authorization header, or not a Bearer token."""

    def __init__(self) -> None:
        super().__init__("Authentication required.")


class AccessTokenInvalidError(AuthError):
    """Every verification failure except expiry.

    Deliberately one code for all of them. Distinguishing a bad signature from a
    bad issuer from an unknown key on the wire would hand an attacker an oracle
    for probing what the server checks. The precise reason goes to the log,
    keyed by the request id.
    """

    code = "invalid_token"

    def __init__(self) -> None:
        super().__init__("The access token is not valid.")


class AccessTokenExpiredError(AuthError):
    """Separate from invalid only because the client acts on it differently.

    A frontend can refresh its session and retry once. That retry is only safe
    because every money-moving request carries an idempotency key.
    """

    code = "token_expired"

    def __init__(self) -> None:
        super().__init__("The access token has expired.")


class IdentityUnavailableError(AuthError):
    """The auth provider could not be reached, or is misconfigured.

    503 and NOT 401. This is our infrastructure failing, not the caller's
    credential being bad. A 401 here would bounce every signed-in user to the
    login screen during a provider blip, and the natural client reaction, sign
    out and retry, makes the blip worse.
    """

    code = "auth_unavailable"
    status = 503

    def __init__(self) -> None:
        super().__init__("Cannot verify credentials right now. Try again shortly.")


class CatNotOnboardedError(AuthError):
    """A valid token whose subject has no cat.

    403 and NOT 401. A 401 means "re-authenticate", and re-authenticating
    produces an identical token, so the client would be in a sign-in loop that
    can never resolve. 403 is "understood, and refused until you do something
    different", which is exactly the situation: create a cat.

    Raised by the dependency rather than by each route, because forgetting it in
    one route is a security bug.
    """

    code = "cat_not_onboarded"
    status = 403

    def __init__(self) -> None:
        super().__init__("This account has no cat yet.")


# -- onboarding ------------------------------------------------------------


class HandleInvalidError(AppError):
    """Mirrors ck_cats_handle_shape, and covers reserved handles too.

    Reserved handles return this and never HandleTakenError, so nothing leaks
    about which rows exist.
    """

    code = "handle_invalid"
    status = 422

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "A handle is 3 to 32 characters of lowercase letters, digits and underscores."
        )


class DisplayNameInvalidError(AppError):
    """A display name carrying characters Postgres cannot store, or should not.

    A NUL byte is the sharp one: psycopg raises DataError before the statement
    is even sent, so without this it is a 500 on a value the caller typed.
    """

    code = "display_name_invalid"
    status = 422

    def __init__(self) -> None:
        super().__init__("A display name cannot contain control characters.")


class HandleTakenError(AppError):
    """Another cat already holds this handle."""

    code = "handle_taken"
    status = 409

    def __init__(self, handle: str) -> None:
        super().__init__(f"The handle {handle!r} is taken.")


class CatAlreadyExistsError(AppError):
    """This identity already has a cat, under a different handle.

    Onboarding is not a rename endpoint. Calling it again with the SAME handle
    is a 200 rather than an error, so a client that fires it on every page load
    is safe.
    """

    code = "cat_already_exists"
    status = 409

    def __init__(self, handle: str) -> None:
        super().__init__(f"This account already has a cat, {handle!r}.")


class ValidationError(AppError):
    """A malformed request body, rendered into our envelope.

    FastAPI's own RequestValidationError returns {"detail": [...]}, which is not
    the shape the frontend branches on. Registering a handler that raises this
    instead keeps one error contract across the whole API.
    """

    code = "validation_error"
    status = 422
