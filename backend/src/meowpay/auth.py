"""Verifying a Supabase access token, and resolving it to a cat.

Knows nothing about HTTP. The FastAPI wiring lives in `meowpay.api.deps`, so
this module can be unit tested with a locally generated key and no network.

Asymmetric (ES256) rather than the legacy shared HS256 secret. With a shared
secret this service would hold the key GoTrue MINTS with, so anyone who could
read the deployment environment could forge a token for any cat. With an
asymmetric key it holds only the public half, so a full compromise of this
service still cannot mint a session. That asymmetry is the whole argument for a
system whose only job is refusing to move money without authorization.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from meowpay import config
from meowpay.errors import (
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    IdentityUnavailableError,
)

logger = logging.getLogger(__name__)

# Tokens are only ever verified with these. Never with the algorithm named in
# the token's own header. See TokenVerifier.verify for the attack that prevents.
ASYMMETRIC_ALGORITHMS = ["ES256", "RS256"]

# Tolerance on `iat` and `nbf`, not a revocation window.
#
# Zero looks stricter and is a self-inflicted outage. `iat` is stamped on the
# auth provider's clock, not ours, and PyJWT rejects a token whose `iat` is even
# one second in the future. NTP does not guarantee sub-second agreement between
# two providers, so leeway=0 means a second of positive skew fails every
# authentication in the service, reported as `invalid_token`, which tells the
# client to re-authenticate, which mints a token with the same problem. An
# unbreakable loop from a clock.
#
# This does NOT meaningfully widen revocation: `exp` bounds that, and 30 seconds
# against a 900 second token is noise.
CLOCK_LEEWAY_SECONDS = 30

# PyJWKClient refetches the key set whenever a `kid` misses, which is correct for
# key rotation and would otherwise be a free amplification vector: an attacker
# sending random kids could force one outbound request per request. PyJWT caps
# that itself, refusing to refetch more than once per cooldown, so this needs no
# cache of its own. Do not add one: the key would be an attacker-controlled
# header with nothing bounding it, expiry would race, and it would have to tell a
# genuine miss from a fetch failure or cache the real kid and outlive the blip.
UNKNOWN_KID_COOLDOWN_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Claims:
    """What a verified token asserts. Only `auth_user_id` is identity."""

    auth_user_id: uuid.UUID
    email: str | None
    session_id: str | None


@dataclass(frozen=True, slots=True)
class CurrentCat:
    """The caller, as plain values.

    Frozen and slotted for the same reason `Settlement` is: no ORM instance
    escapes its session, so reading an attribute after that session closes cannot
    lazy-load or raise DetachedInstanceError.

    Deliberately no `balance`. The dependency that builds this has already
    committed and closed its transaction by the time a route runs, so any balance
    here is stale before it is used, and a route that trusted it would make a
    money decision on a value no lock covered. `Ledger._settle` re-reads balances
    under FOR NO KEY UPDATE and that must stay the only balance anyone acts on.
    """

    id: uuid.UUID
    handle: str
    display_name: str
    auth_user_id: uuid.UUID


class SigningKeySource(Protocol):
    """What the verifier needs from a key provider.

    A Protocol rather than PyJWKClient directly, so tests can hand in a locally
    built key set and exercise every failure path without HTTP. This is the same
    injection seam `deps.sessions` exists for.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class TokenVerifier:
    """Turns a bearer token into Claims, or raises.

    Constructed with its key source rather than reaching for one, so the fourteen
    unit tests around it need no network and no monkeypatching.
    """

    def __init__(
        self,
        key_source: SigningKeySource,
        *,
        issuer: str,
        audience: str,
        algorithms: list[str] | None = None,
    ) -> None:
        self._keys = key_source
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms or ASYMMETRIC_ALGORITHMS)

    def verify(self, token: str) -> Claims:
        if not token:
            raise AccessTokenInvalidError()

        key = self._signing_key(token)

        try:
            payload = jwt.decode(
                token,
                key,
                # From this verifier's construction, NEVER from the token
                # header.
                #
                # The attack this prevents: the JWKS endpoint is public, so an
                # attacker downloads the ES256 public key, uses its raw bytes as
                # an HMAC secret, and mints an HS256 token. If HS256 were in this
                # list alongside a JWKS-resolved key, that token would verify and
                # the attacker would be any cat they chose. One algorithm family
                # per process removes it by construction rather than by care.
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                # See CLOCK_LEEWAY_SECONDS. This guards `iat` and `nbf`, not
                # just `exp`, and zero here is an outage waiting on a clock.
                leeway=CLOCK_LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AccessTokenExpiredError() from exc
        except jwt.PyJWTError as exc:
            # The precise reason goes to the log and never to the wire, where it
            # would be an oracle for probing what the server checks.
            logger.info("rejected access token: %s: %s", type(exc).__name__, exc)
            raise AccessTokenInvalidError() from exc

        return self._claims(payload)

    def _signing_key(self, token: str) -> Any:
        """Resolve the key this token claims to be signed with.

        The ordering of these clauses is the whole of the method, and getting it
        wrong is silent. PyJWT's hierarchy is:

            PyJWKClientConnectionError -> PyJWKClientError -> PyJWTError
            PyJWKSetError ----------------------------------> PyJWTError

        So catching `PyJWKClientError` first, which looks like the correct
        specific-before-broad ordering, swallows **every network failure**:
        `fetch_data` wraps URLError and TimeoutError into the connection
        subclass. DNS failure, TLS failure, connect timeout and a 5xx would all
        arrive as a 401 telling the user their credential is bad, which signs
        them out during a provider blip and makes the blip worse. That is exactly
        the failure IdentityUnavailableError exists to prevent, and only a
        deliberately ordered `except` chain prevents it.

        The distinction that matters is not "which library class" but "did we get
        a key set at all":

        - could not fetch one, or fetched something unusable -> ours, 503
        - fetched one fine and this kid is not in it          -> theirs, 401
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            logger.info("malformed token header: %s", exc)
            raise AccessTokenInvalidError() from exc

        try:
            return self._keys.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as exc:
            # MUST precede PyJWKClientError, which is its parent.
            logger.warning("could not reach the token key set: %s", exc)
            raise IdentityUnavailableError() from exc
        except jwt.PyJWKSetError as exc:
            # Fetched, and unusable. Either the project is still on a shared
            # HS256 secret, in which case the endpoint publishes no keys at all,
            # or something returned a page that is not a key set. Both are our
            # misconfiguration and neither is the caller's fault.
            logger.error("the token key set is unusable: %s", exc)
            raise IdentityUnavailableError() from exc
        except jwt.PyJWKClientError as exc:
            # A key set we successfully read, which does not contain this kid.
            logger.info("no signing key for kid %r: %s", kid, exc)
            raise AccessTokenInvalidError() from exc
        except jwt.PyJWTError as exc:
            logger.info("could not select a signing key: %s", exc)
            raise AccessTokenInvalidError() from exc
        except Exception as exc:
            # A key source that is not PyJWKClient, or a failure mode PyJWT does
            # not wrap. Unknown, so it is ours rather than the caller's.
            logger.warning("token key lookup failed: %s", exc)
            raise IdentityUnavailableError() from exc

    def _claims(self, payload: dict[str, Any]) -> Claims:
        # A non-UUID sub would reach `WHERE auth_user_id = :sub` and become a
        # DataError and a 500. Rejected here instead.
        try:
            subject = uuid.UUID(str(payload["sub"]))
        except (ValueError, TypeError) as exc:
            logger.info("token subject is not a uuid")
            raise AccessTokenInvalidError() from exc

        if payload.get("role") != "authenticated":
            logger.info("token role is %r, not authenticated", payload.get("role"))
            raise AccessTokenInvalidError()

        # An anonymous Supabase user has no verified contact of any kind and must
        # never own a money account.
        if payload.get("is_anonymous"):
            logger.info("anonymous token refused")
            raise AccessTokenInvalidError()

        email = payload.get("email")
        return Claims(
            auth_user_id=subject,
            # Carried for logging only. Identity is `sub` and nothing else: an
            # email can be changed at the provider and reused.
            email=str(email) if email else None,
            session_id=str(payload["session_id"]) if payload.get("session_id") else None,
        )


@lru_cache(maxsize=1)
def get_verifier() -> TokenVerifier:
    """The process-wide verifier.

    Built lazily rather than at startup, so a brief provider outage does not
    prevent the service from booting into a restart loop.

    PyJWKClient is requests-backed and BLOCKING, which is why the FastAPI
    dependency that calls this must be a plain `def` and never `async def`.
    An async dependency would block the event loop for the whole fetch every time
    the key cache expires.
    """
    try:
        issuer = config.jwt_issuer()
        client = PyJWKClient(
            config.jwks_url(),
            # cache_keys=False, which is PyJWT's default, and deliberate.
            # Setting it True adds a per-kid lru_cache with NO time based
            # expiry: a key is evicted only when 16 other kids push it out. A
            # signing key revoked at the provider would go on being honoured
            # here until the process restarted, and the lifespan below would not
            # help because that governs the key SET cache which this tier sits
            # in front of. The set cache alone gives the caching we want, with a
            # TTL.
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=config.jwks_cache_seconds(),
            max_cached_keys=16,
            timeout=config.jwks_timeout_seconds(),
            # Caps forced refetches after an unknown kid to one per cooldown, so
            # an attacker sending random kids cannot amplify into the provider.
            cooldown_duration=UNKNOWN_KID_COOLDOWN_SECONDS,
        )
    except (RuntimeError, ValueError, jwt.PyJWKClientError) as exc:
        # A misconfigured server is a 503 and not a 401, and /health keeps
        # working either way, which is what you want when debugging a bad
        # deploy. Three shapes reach here and all of them are ours rather than
        # the caller's: a missing SUPABASE_URL (RuntimeError), a non-numeric
        # timeout or cache setting (ValueError), and a SUPABASE_URL with no
        # scheme or a lifespan of zero (PyJWKClientError). The scheme one is the
        # likeliest copy-paste mistake there is.
        logger.error("identity is not configured: %s", exc)
        raise IdentityUnavailableError() from exc

    return TokenVerifier(client, issuer=issuer, audience=config.jwt_audience())
