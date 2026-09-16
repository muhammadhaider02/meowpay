"""Demo data, for `make seed`.

Three cats with uneven balances, one of them deliberately empty so an
insufficient-funds rejection can be demonstrated without editing data first.

Funded **through the ledger**, never by writing `cats.balance`. That is the point
of the script existing at all: the first command a reviewer runs obeys the same
invariant every endpoint does, so the seeded cats have a real ledger behind their
balances and reconciliation passes on a freshly seeded database.

Re-runnable, and each of the three steps is independently idempotent, so a crash
part way through is repaired by running it again rather than by hand.

Creates Supabase auth users too, because a cat cannot exist without an identity:
`ck_cats_only_system_lacks_auth_user` makes a cat with no `auth_user_id` a system
account, which `ck_cats_only_the_sentinel_is_system` then rejects. That same pair
of constraints is why there is no linking step here. An unlinked ordinary cat is
impossible, so a cat that exists is already linked and `UPDATE cats SET
auth_user_id` is a statement this codebase never needs.

GoTrue is reached over HTTP and never by querying `auth.users` directly, even
though that would be one exact query. Reading it would hard-code the assumption
that the application database IS the auth database, which the deliberate absence
of a foreign key exists to avoid.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meowpay import config
from meowpay.ledger import Ledger
from meowpay.models import Cat
from meowpay.session import get_sessions

logger = logging.getLogger(__name__)

PASSWORD = config.optional_env("SEED_PASSWORD", "treats123")

# .test is reserved by RFC 2606 and can never resolve, so a misconfigured SMTP
# server cannot deliver a sign-up mail to somebody's real inbox.
EMAIL_DOMAIN = config.optional_env("SEED_EMAIL_DOMAIN", "meowpay.test")

# handle, display name, starting balance. Handles satisfy ck_cats_handle_shape
# and are already lowercase for ck_cats_handle_is_lowercase.
SEED_CATS: list[tuple[str, str, int]] = [
    ("dahlia", "Dahlia", 1500),
    ("milo", "Milo", 300),
    # Starts empty on purpose, so a rejected transfer can be shown live.
    ("lotus", "Lotus", 0),
]

TIMEOUT = httpx.Timeout(10.0)


def _admin(client: httpx.Client, method: str, path: str, **kwargs: object) -> httpx.Response:
    key = config.supabase_secret_key()
    return client.request(
        method,
        f"{config.supabase_url()}/auth/v1{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        **kwargs,  # type: ignore[arg-type]
    )


def _find_user(client: httpx.Client, email: str) -> str | None:
    """Look up an existing auth user by email.

    The documented admin list surface is pagination only, with no email filter,
    so this is O(users). Acceptable only because this is a seed script against a
    demo project. Capped and loud rather than silently giving up, because
    "not found" here would make the caller try to create a user that exists and
    loop.
    """
    for page in range(1, 11):
        response = _admin(
            client, "GET", "/admin/users", params={"page": page, "per_page": 1000}
        )
        response.raise_for_status()
        users = response.json().get("users", [])
        if not users:
            return None
        for user in users:
            # `.get(key, "")` only defaults when the key is ABSENT. GoTrue
            # returns "email": null for phone-only, anonymous and some SSO
            # users, so the default never fires and .lower() raises. One
            # such user anywhere in the project would break every recovery.
            if (user.get("email") or "").lower() == email.lower():
                return str(user["id"])
    raise RuntimeError(
        f"Could not find {email} after 10 pages of auth users. Refusing to guess."
    )


def _ensure_auth_user(client: httpx.Client, handle: str, display_name: str) -> str:
    """Create the auth user, or find the one already there.

    `email_confirm` is what lets `make seed` work with no SMTP configured at all.
    Without it the user exists but cannot sign in, which is a demo that looks
    fine and is broken.
    """
    email = f"{handle}@{EMAIL_DOMAIN}"
    response = _admin(
        client,
        "POST",
        "/admin/users",
        json={
            "email": email,
            "password": PASSWORD,
            "email_confirm": True,
            "user_metadata": {"handle": handle, "display_name": display_name},
        },
    )

    if response.is_success:
        created = response.json()
        if not isinstance(created, dict) or "id" not in created:
            raise RuntimeError(f"GoTrue returned an unexpected body for {handle}: {created!r}")
        return str(created["id"])

    # A gateway can answer with an HTML error page, and older GoTrue versions
    # answer with a different JSON shape. Neither should become a traceback.
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    reason = str(
        body.get("error_code") or body.get("msg") or body.get("error_description") or ""
    ).lower()

    # Phrase matching, because GoTrue has said this at least three ways:
    # error_code "email_exists", msg "User already registered", and msg "Email
    # address already in use". Matching only "exist" missed two of the three.
    already_exists = any(
        phrase in reason for phrase in ("exist", "already registered", "already in use")
    )
    if response.status_code in (400, 422, 409) and already_exists:
        found = _find_user(client, email)
        if found is None:
            raise RuntimeError(f"GoTrue says {email} exists but it is not in the user list.")
        return found

    if "weak_password" in reason:
        raise RuntimeError(
            "Supabase rejected the seed password as too weak. Set SEED_PASSWORD, or "
            "relax the password policy under Authentication, Sign In / Providers."
        )

    raise RuntimeError(f"Could not create the auth user for {handle}: {response.status_code} {body}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sessions = get_sessions()
    ledger = Ledger(sessions)

    # Fail here rather than half way through, and name the variable. A seed that
    # silently created cats nobody could sign in as would be worse than one that
    # refuses to start.
    config.supabase_secret_key()
    config.supabase_url()

    ids: dict[str, uuid.UUID] = {}

    with httpx.Client(timeout=TIMEOUT) as client:
        for handle, display_name, _ in SEED_CATS:
            # Resolve the identity FIRST, and key everything off it.
            #
            # Checking the handle first and skipping looks equivalent and funds
            # the wrong cat. These handles are not reserved, so anyone can claim
            # `milo` through POST /api/v1/cats, and the deposit below would then
            # resolve to that stranger's cat and credit it out of the treasury.
            # The idempotency key is scoped to the owner, so it would be a
            # one-off mint into an account this script never created.
            #
            # `_ensure_auth_user` is keyed on a deterministic email and is
            # idempotent, so calling it every run costs one request and removes
            # the whole class of bug.
            auth_user_id = uuid.UUID(_ensure_auth_user(client, handle, display_name))

            with sessions() as session:
                mine = session.execute(
                    select(Cat.id, Cat.handle).where(Cat.auth_user_id == auth_user_id)
                ).one_or_none()

            if mine is not None:
                if mine.handle != handle:
                    raise RuntimeError(
                        f"The auth user for {handle!r} already owns a cat called "
                        f"{mine.handle!r}. Refusing to guess which one to fund."
                    )
                ids[handle] = mine.id
                continue

            # No cat for this identity. Is the handle free?
            with sessions() as session:
                clash = session.scalar(select(Cat.id).where(Cat.handle == handle))
            if clash is not None:
                raise RuntimeError(
                    f"The handle {handle!r} is held by a cat this seed did not create. "
                    f"Refusing to fund it. Rename or remove it, then re-run."
                )

            with sessions.begin() as session:
                session.execute(
                    pg_insert(Cat)
                    .values(
                        id=uuid.uuid4(),
                        handle=handle,
                        display_name=display_name,
                        auth_user_id=auth_user_id,
                    )
                    # No conflict target, the same as the onboarding route.
                    # Naming `handle` alone leaves uq_cats_auth_user_id
                    # uncovered, so an identity that already owns a differently
                    # named cat aborts the whole run with an IntegrityError.
                    .on_conflict_do_nothing()
                )

            with sessions() as session:
                ids[handle] = session.execute(
                    select(Cat.id).where(Cat.auth_user_id == auth_user_id)
                ).scalar_one()

    for handle, _, balance in SEED_CATS:
        if balance == 0:
            continue
        ledger.deposit(
            to_cat_id=ids[handle],
            amount=balance,
            # Deterministic, so re-running replays instead of double-funding.
            # At least 8 characters, per ck_transfers_idempotency_key_shape.
            idempotency_key=f"seed-v1-{handle}",
        )

    # Read back rather than echoing SEED_CATS. On a re-run nothing is inserted
    # and the deposits replay, so printing the configured numbers would report a
    # state that may not be true after a demo transfer has moved treats around.
    with sessions() as session:
        final = session.execute(
            select(Cat.handle, Cat.balance).where(~Cat.is_system).order_by(Cat.handle)
        ).all()

    logger.info("Sign in with the email, not the handle. Password for every cat: %s", PASSWORD)
    for handle, balance in final:
        logger.info("  %-8s %-24s %6d treats", handle, f"{handle}@{EMAIL_DOMAIN}", balance)


if __name__ == "__main__":
    main()
