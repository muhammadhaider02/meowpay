"""Moving treats: the two endpoints that reach the ledger.

Both are thin. Every rule about what may move, in what order, under which locks,
lives in `meowpay.ledger`, and every rejection it raises is already a typed error
carrying its own `code` and HTTP status. So these routes resolve the caller,
resolve the recipient, and hand over. There is deliberately no error translation
here: a `try/except` around `ledger.transfer` would be a second place where the
status code for insufficient funds is decided.

The sender is always the token holder and never a request field. That is the
single most important line in this module: a `from_handle` in the body would let
anyone spend anyone's treats, and no constraint in the database would stop it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response, status
from sqlalchemy import select

from meowpay.api.deps import CurrentCatDep, LedgerDep, Sessions
from meowpay.api.routes.cats import normalise_handle
from meowpay.api.schemas import DepositRequest, MovementResponse, TransferRequest
from meowpay.errors import IdempotencyKeyInvalidError, RecipientNotFoundError
from meowpay.ledger import Settlement, check_idempotency_key
from meowpay.models import Cat

router = APIRouter(tags=["movements"])

# The header name is fixed by convention, not by us. Stripe, and everyone who
# copied Stripe, spells it exactly this way.
IDEMPOTENCY_HEADER = "Idempotency-Key"

IdempotencyKey = Annotated[
    list[str] | None,
    Header(
        alias=IDEMPOTENCY_HEADER,
        description=(
            "Required, exactly once. 8 to 255 characters. Generate it once per "
            "intent, not per attempt: reusing it makes a retry safe, "
            "regenerating it on retry makes a double spend."
        ),
    ),
]

# 200 is a real outcome of both routes and the decorators can only declare one
# status, so it is named here or a client generated from the schema has no
# replay case at all.
REPLAY_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"model": MovementResponse, "description": "Replayed. Nothing moved."}
}


def _require_idempotency_key(supplied: list[str] | None) -> str:
    """Exactly one, well formed, before anything reaches the database.

    Declared as a list rather than a `str` because FastAPI gives a bare `str`
    only the FIRST value of a repeated header and discards the rest in silence.
    A client that sent two keys would settle under one and retry under the
    other, which is a double spend assembled out of a header nobody looked at.

    An intermediary that folds duplicates into one comma-joined value per RFC
    9110 is indistinguishable from a caller who sent that string deliberately,
    so it is accepted as an ordinary key. Rejecting the repeat is what makes
    that case rare rather than routine.

    Refused here rather than by marking the header required, so the answer stays
    inside our envelope: FastAPI's own missing-header rejection is a
    `validation_error`, and a client branching on `code` should not have to know
    that one came from Pydantic.

    The shape is checked here too, by the ledger's own validator. That is the
    same function rather than a second definition, and calling it now is what
    keeps a malformed key from first checking out a pool connection in
    `_resolve_recipient` and coming back as a 404 about the recipient.
    """
    if not supplied:
        raise IdempotencyKeyInvalidError(f"the {IDEMPOTENCY_HEADER} header is required")
    if len(supplied) > 1:
        raise IdempotencyKeyInvalidError(
            f"the {IDEMPOTENCY_HEADER} header was sent {len(supplied)} times, so which "
            "movement is being retried is ambiguous"
        )

    key = supplied[0]
    check_idempotency_key(key)
    return key


def _receipt(settlement: Settlement, response: Response) -> MovementResponse:
    """200 on replay, 201 on a movement that actually happened.

    The status code is the only place the distinction is free. A client that
    ignores it still gets `replayed` in the body, and a client that ignores both
    still gets the right answer, which is the whole point of idempotency.
    """
    if settlement.replayed:
        response.status_code = status.HTTP_200_OK

    return MovementResponse(
        id=settlement.transfer_id,
        kind=settlement.kind,
        amount=settlement.amount,
        idempotency_key=settlement.idempotency_key,
        balance_after=settlement.owner_balance_after,
        created_at=settlement.created_at,
        replayed=settlement.replayed,
    )


def _resolve_recipient(factory: Sessions, to_handle: str) -> uuid.UUID:
    """Handle to id, refusing anything the sender could not legitimately name.

    Normalised through the same function onboarding uses, so `Dahlia` reaches
    `dahlia` and a reserved handle is refused as `handle_invalid` rather than
    confirming whether that row exists.

    The `~is_system` filter is redundant and deliberate, the same way the one in
    `get_current_cat` is. The treasury is already unreachable here because its
    handle carries the reserved prefix and never survives normalisation, and
    `Ledger.transfer` refuses it a third time. No test can reach this clause, so
    it is a comment rather than a guard; it is kept so that renaming the treasury
    or relaxing the reserved prefix fails somewhere loud instead of opening a
    path to the one row allowed to go negative.

    Racy by nature: the cat can be deleted between this read and the settlement.
    That is fine. `_settle` rechecks existence under the row lock and raises the
    same error, so the race closes where it has to.

    Note what this ordering costs, because it is not free. A replay is reached
    only after the recipient resolves, so in principle retrying a settled
    transfer whose recipient had since vanished would answer 404 rather than
    replaying it. That cannot currently happen: a settled movement writes an
    entry row for the recipient, `entries.counterparty_cat_id` is a foreign key
    with ON DELETE RESTRICT, and no endpoint renames a handle. So the recipient
    of a settled movement cannot be removed or renamed by anything the API
    offers. Adding either would make this reachable, and the fix then is to look
    the replay up by `(owner_cat_id, key)` before resolving anything.
    """
    handle = normalise_handle(to_handle)

    with factory() as session:
        recipient_id = session.scalar(select(Cat.id).where(Cat.handle == handle, ~Cat.is_system))

    if recipient_id is None:
        raise RecipientNotFoundError(f"handle {handle!r}")
    return recipient_id


@router.post(
    "/transfers",
    response_model=MovementResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REPLAY_RESPONSE,
    summary="Send treats to another cat",
)
def send_treats(
    body: TransferRequest,
    cat: CurrentCatDep,
    ledger: LedgerDep,
    factory: Sessions,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> MovementResponse:
    """Settle a transfer from the signed-in cat.

    A self transfer is refused by the ledger rather than here, even though the
    handle is in hand and the check would be one line. The ledger's version
    compares ids after both handles have resolved, so it also catches the case
    where a cat reaches itself under a handle it has since changed.
    """
    key = _require_idempotency_key(idempotency_key)
    to_cat_id = _resolve_recipient(factory, body.to_handle)

    settlement = ledger.transfer(
        from_cat_id=cat.id,
        to_cat_id=to_cat_id,
        amount=body.amount,
        idempotency_key=key,
    )
    return _receipt(settlement, response)


@router.post(
    "/deposits",
    response_model=MovementResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REPLAY_RESPONSE,
    summary="Top up the signed-in cat from the treasury",
)
def top_up(
    body: DepositRequest,
    cat: CurrentCatDep,
    ledger: LedgerDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> MovementResponse:
    """Credit the caller, debiting the treasury by the same amount.

    Deliberately open, with no per-deposit cap beyond the ledger's own ceiling
    and no payment step. This endpoint stands in for a payment rail that is out
    of scope, and pretending otherwise with a fake card form would be a worse
    kind of dishonest than saying so in the README.

    The recipient is the token holder. There is no target field to supply.
    """
    key = _require_idempotency_key(idempotency_key)

    settlement = ledger.deposit(
        to_cat_id=cat.id,
        amount=body.amount,
        idempotency_key=key,
    )
    return _receipt(settlement, response)
