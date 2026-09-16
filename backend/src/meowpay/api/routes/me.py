"""The signed-in cat: its balance, and its statement.

`CurrentCat` deliberately carries no balance, because the dependency's read
transaction has closed by the time a route runs and any balance it carried would
be stale before use. So the balance is read here, in the request that reports it,
and reported nowhere else.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import aliased

from meowpay.api.deps import CurrentCatDep, Sessions
from meowpay.api.schemas import EntryPage, EntryResponse, MeResponse
from meowpay.errors import CatNotOnboardedError
from meowpay.models import Cat, Entry

router = APIRouter(prefix="/me", tags=["me"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@router.get("", response_model=MeResponse, summary="The signed-in cat and its balance")
def me(cat: CurrentCatDep, factory: Sessions) -> MeResponse:
    """Read the balance fresh, rather than trusting the dependency.

    A second query on a request that already did one, and worth it: this is the
    only endpoint that reports a balance, so it is the only place that can report
    a wrong one.
    """
    with factory() as session:
        balance = session.scalar(select(Cat.balance).where(Cat.id == cat.id))

    if balance is None:
        # The cat resolved a moment ago and is gone now. Deleting a cat is not
        # something this API offers, so this is either a manual intervention or a
        # bug, and either way the honest answer is the one the dependency would
        # have given a moment later.
        raise CatNotOnboardedError()

    return MeResponse(
        id=cat.id,
        handle=cat.handle,
        display_name=cat.display_name,
        balance=balance,
    )


@router.get("/entries", response_model=EntryPage, summary="The signed-in cat's statement")
def entries(
    cat: CurrentCatDep,
    factory: Sessions,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    before: Annotated[int | None, Query(gt=0, description="Cursor from `next_before`.")] = None,
) -> EntryPage:
    """Newest first, keyset paginated on the entry id.

    `WHERE cat_id = :id AND id < :before ORDER BY id DESC` is exactly the shape
    of `ix_entries_cat_id_id`, so depth costs nothing.

    The counterparty is joined rather than returned as an id, because a statement
    listing uuids is not a statement anyone can read. It is an inner join and
    that is safe: `entries.counterparty_cat_id` is a foreign key with ON DELETE
    RESTRICT, so the row it names cannot disappear.

    One row more than asked for is fetched, and the extra is dropped. That is how
    `next_before` knows whether a further page exists without a second COUNT,
    which on a live feed would be both slower and capable of disagreeing with the
    page it describes.
    """
    counterparty = aliased(Cat)

    statement = (
        select(
            Entry.id,
            Entry.transfer_id,
            Entry.kind,
            Entry.amount,
            Entry.balance_after,
            Entry.created_at,
            counterparty.handle.label("counterparty_handle"),
            counterparty.display_name.label("counterparty_display_name"),
        )
        .join(counterparty, counterparty.id == Entry.counterparty_cat_id)
        .where(Entry.cat_id == cat.id)
    )

    if before is not None:
        statement = statement.where(Entry.id < before)

    statement = statement.order_by(Entry.id.desc()).limit(limit + 1)

    with factory() as session:
        rows = session.execute(statement).all()

    has_more = len(rows) > limit
    page = rows[:limit]

    return EntryPage(
        entries=[
            EntryResponse(
                id=row.id,
                transfer_id=row.transfer_id,
                kind=row.kind,
                amount=row.amount,
                balance_after=row.balance_after,
                counterparty_handle=row.counterparty_handle,
                counterparty_display_name=row.counterparty_display_name,
                created_at=row.created_at,
            )
            for row in page
        ],
        next_before=page[-1].id if has_more and page else None,
    )
