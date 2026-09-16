"""Access token verification.

No database and no network, so these run on a machine with neither. The verifier
takes its key source as a constructor argument precisely so this file can build a
real ES256 key pair, sign real tokens with it and exercise every failure path
without touching Supabase or monkeypatching PyJWT.

This is where the test budget belongs. It is the only code in the service where a
bug means unauthenticated access to everybody's money, and most of these failures
are invisible to any functional test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import PyJWKSet
from jwt.algorithms import ECAlgorithm

from meowpay.auth import Claims, TokenVerifier
from meowpay.errors import (
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    IdentityUnavailableError,
)

ISSUER = "https://project.supabase.co/auth/v1"
AUDIENCE = "authenticated"
KID = "test-key-1"


@pytest.fixture(scope="module")
def signing_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(scope="module")
def key_set(signing_key: ec.EllipticCurvePrivateKey) -> PyJWKSet:
    jwk = ECAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return PyJWKSet.from_dict({"keys": [jwk]})


class _KeySet:
    """Stands in for PyJWKClient, resolving a kid out of a local key set."""

    def __init__(self, keys: PyJWKSet) -> None:
        self._keys = keys
        self.lookups = 0

    def get_signing_key_from_jwt(self, token: str) -> Any:
        self.lookups += 1
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._keys.keys:
            if key.key_id == kid:
                return key
        raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid}")


class _Unreachable:
    """Stands in for a key set that cannot be fetched at all.

    Raises what PyJWKClient actually raises, and that is the whole value of it.
    The builtin ConnectionError would also reach the 503 branch, so a stub using
    it would pass while production returned 401 on every network failure. A stub
    that raises something the real thing cannot raise proves nothing.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any:
        raise jwt.PyJWKClientConnectionError('Fail to fetch data from the url, err: "dns failure"')


class _UnusableKeySet:
    """Fetched something, and it is not a key set.

    What a project still on a shared HS256 secret publishes: the endpoint
    answers, with no keys in it.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any:
        raise jwt.PyJWKSetError("The JWK Set did not contain any keys")


class _Flaky:
    """Fails once, then works. A one second blip."""

    def __init__(self, keys: PyJWKSet) -> None:
        self._real = _KeySet(keys)
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise jwt.PyJWKClientConnectionError("transient")
        return self._real.get_signing_key_from_jwt(token)


@pytest.fixture
def source(key_set: PyJWKSet) -> _KeySet:
    return _KeySet(key_set)


@pytest.fixture
def verifier(source: _KeySet) -> TokenVerifier:
    return TokenVerifier(source, issuer=ISSUER, audience=AUDIENCE)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 900,
        "role": "authenticated",
        "email": "milo@meowpay.test",
        "session_id": str(uuid.uuid4()),
        "is_anonymous": False,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not ...}


def _sign(key: ec.EllipticCurvePrivateKey, payload: dict[str, Any], kid: str = KID) -> str:
    return jwt.encode(payload, key, algorithm="ES256", headers={"kid": kid})


# -- the happy path --------------------------------------------------------


def test_a_valid_token_resolves_to_its_subject(
    verifier: TokenVerifier, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    subject = uuid.uuid4()
    claims = verifier.verify(_sign(signing_key, _claims(sub=str(subject))))

    assert isinstance(claims, Claims)
    assert claims.auth_user_id == subject
    assert claims.email == "milo@meowpay.test"


# -- the one that matters most ---------------------------------------------


def test_an_hs256_token_signed_with_the_public_key_is_refused(
    verifier: TokenVerifier, key_set: PyJWKSet
) -> None:
    """Algorithm confusion. A bug here is total authentication bypass.

    The key set is public by design, so an attacker can fetch the ES256 public
    key, use its raw bytes as an HMAC secret and mint an HS256 token. If HS256
    were ever in the accepted algorithm list alongside a key resolved from the
    key set, that token would verify and the attacker would be any cat they
    chose.

    It must fail even though the attacker used a key the verifier legitimately
    holds, and nothing else in the suite would notice if it did not.

    Forged by hand, and verified by hand, because PyJWT refuses an asymmetric key
    as an HMAC secret on both encode and decode. That refusal is a second layer
    underneath our algorithm list and is worth knowing about, but it is PyJWT's
    guarantee and not ours: it would not survive swapping libraries. The
    algorithm list is what this codebase controls, and
    test_a_verifier_in_asymmetric_mode_refuses_a_valid_hs256_token is the case
    where the list is the only thing standing in the way, because a plain string
    secret does not trip PyJWT's key-type check.
    """
    # The PEM is what an attacker actually has: it is what the key set publishes
    # and what every JWT library hands back.
    public_pem = key_set.keys[0].key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = b64(json.dumps(_claims()).encode())
    signing_input = header + b"." + payload
    signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    # Sanity: the forgery is real. Checked by hand, because PyJWT will not use a
    # PEM as an HMAC secret even to verify one.
    expected = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    assert hmac.compare_digest(signature, expected)

    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(forged)


def test_alg_none_is_refused(verifier: TokenVerifier) -> None:
    unsigned = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": KID})

    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(unsigned)


def test_a_verifier_in_asymmetric_mode_refuses_a_valid_hs256_token(
    verifier: TokenVerifier,
) -> None:
    """Even a properly signed HS256 token, with a secret the verifier never saw."""
    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(jwt.encode(_claims(), "some-shared-secret", algorithm="HS256"))


def test_a_token_signed_by_a_different_key_with_the_same_kid_is_refused(
    verifier: TokenVerifier,
) -> None:
    """A matching kid must not be mistaken for a matching signature."""
    impostor = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(_sign(impostor, _claims()))


# -- claim validation ------------------------------------------------------


def test_an_expired_token_says_so_distinctly(
    verifier: TokenVerifier, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    """Its own code, because the client acts on it: refresh, then retry once."""
    now = int(time.time())
    expired = _sign(signing_key, _claims(iat=now - 1800, exp=now - 60))

    with pytest.raises(AccessTokenExpiredError) as caught:
        verifier.verify(expired)

    assert caught.value.code == "token_expired"
    assert caught.value.status == 401


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"iss": "https://evil.example/auth/v1"}, "wrong issuer"),
        ({"aud": "anon"}, "wrong audience"),
        ({"sub": ...}, "no subject"),
        ({"sub": "not-a-uuid"}, "subject is not a uuid"),
        ({"exp": ...}, "no expiry"),
        ({"iat": ...}, "no issued-at"),
        ({"role": "anon"}, "wrong role"),
        ({"role": ...}, "no role"),
        ({"is_anonymous": True}, "anonymous user"),
    ],
)
def test_a_token_with_bad_claims_is_refused(
    verifier: TokenVerifier,
    signing_key: ec.EllipticCurvePrivateKey,
    overrides: dict[str, Any],
    why: str,
) -> None:
    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(_sign(signing_key, _claims(**overrides)))


@pytest.mark.parametrize(
    "token", ["", "garbage", "two.segments", "a.b.c", "Bearer something"], ids=repr
)
def test_a_malformed_token_is_refused(verifier: TokenVerifier, token: str) -> None:
    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(token)


# -- key resolution --------------------------------------------------------


def test_an_unknown_kid_is_refused(
    verifier: TokenVerifier, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    """A key set we read successfully, which does not contain this kid.

    Theirs, not ours, so 401. Contrast with an unreachable key set below.

    There is deliberately no cache of recently-missed kids here. PyJWKClient
    refetches the key set when a kid misses, which is what makes key rotation
    work, and caps that itself with `cooldown_duration`. A cache in this class
    would be keyed on an attacker-controlled header with nothing bounding it, and
    would have to tell a genuine miss from a fetch failure.
    """
    token = _sign(signing_key, _claims(), kid="nobody-has-this-kid")

    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(token)


def test_the_real_client_is_built_with_a_refetch_cooldown() -> None:
    """The amplification guard, asserted where it actually lives.

    Without a cooldown, an attacker sending random kids forces one outbound
    request to the auth provider per inbound request.
    """
    import inspect

    from meowpay import auth

    assert "cooldown_duration" in inspect.signature(auth.PyJWKClient.__init__).parameters
    assert "cooldown_duration=UNKNOWN_KID_COOLDOWN_SECONDS" in inspect.getsource(
        auth.get_verifier
    )


def test_the_per_key_cache_is_off_so_a_revoked_key_stops_working() -> None:
    """cache_keys=True would make revocation permanently ineffective.

    It adds a per-kid lru_cache with NO time based expiry, sitting in FRONT of
    the key set cache that `lifespan` governs. A signing key revoked at the
    provider would go on being honoured until the process restarted or sixteen
    other kids evicted it, and no action on the Supabase side could stop it.
    """
    import inspect

    from meowpay import auth

    source = inspect.getsource(auth.get_verifier)
    assert "cache_keys=False" in source
    assert "cache_keys=True" not in source


# -- ours versus theirs ----------------------------------------------------


def test_an_unreachable_key_set_is_unavailable_and_not_unauthorized() -> None:
    """503, not 401. Our infrastructure failing is not the caller's problem.

    A 401 here bounces every signed-in user to the login screen during a
    provider blip, and the client's natural reaction, sign out and retry, makes
    the blip worse.

    The stub raises PyJWKClientConnectionError, which is what PyJWKClient really
    raises for DNS failure, TLS failure, a connect timeout and a 5xx. It is also
    a SUBCLASS of PyJWKClientError, so an `except` chain that catches the parent
    first swallows it and returns 401. That ordering is the entire point of this
    test.
    """
    verifier = TokenVerifier(_Unreachable(), issuer=ISSUER, audience=AUDIENCE)

    with pytest.raises(IdentityUnavailableError) as caught:
        verifier.verify(jwt.encode({"sub": "x"}, "k", algorithm="HS256", headers={"kid": "k1"}))

    assert caught.value.status == 503
    assert caught.value.code == "auth_unavailable"


def test_a_key_set_with_no_keys_is_unavailable_and_not_unauthorized() -> None:
    """The shape a project still on a shared HS256 secret publishes.

    The endpoint answers and contains nothing usable. That is a misconfiguration
    on our side, so it must not tell every user their credential is bad.
    """
    verifier = TokenVerifier(_UnusableKeySet(), issuer=ISSUER, audience=AUDIENCE)

    with pytest.raises(IdentityUnavailableError):
        verifier.verify(jwt.encode({"sub": "x"}, "k", algorithm="HS256", headers={"kid": "k1"}))


def test_a_blip_does_not_outlive_itself(
    key_set: PyJWKSet, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    """Recovery must be immediate, with no cached memory of the failure.

    A cache of missed kids, written on a FETCH failure rather than only on a
    genuine miss, poisons itself with the one kid that matters and converts a one
    second blip into a total outage for as long as the entry lives.
    """
    flaky = _Flaky(key_set)
    verifier = TokenVerifier(flaky, issuer=ISSUER, audience=AUDIENCE)
    token = _sign(signing_key, _claims())

    with pytest.raises(IdentityUnavailableError):
        verifier.verify(token)

    # The very next call, with the source healthy again.
    assert verifier.verify(token).auth_user_id
    assert flaky.calls == 2


# -- clocks ----------------------------------------------------------------


@pytest.mark.parametrize("skew", [1, 5, 25], ids=lambda s: f"+{s}s")
def test_a_token_from_a_slightly_fast_auth_server_is_accepted(
    verifier: TokenVerifier, signing_key: ec.EllipticCurvePrivateKey, skew: int
) -> None:
    """`iat` is stamped on the provider's clock, not ours.

    PyJWT rejects a token whose `iat` is in the future, and leeway guards that as
    well as `exp`. With no leeway, one second of positive skew fails every
    authentication in the service and reports it as `invalid_token`, which tells
    the client to re-authenticate, which mints a token with the same problem.
    """
    now = int(time.time())
    ahead = _sign(signing_key, _claims(iat=now + skew, exp=now + skew + 900))

    assert verifier.verify(ahead).auth_user_id


def test_a_token_from_an_implausibly_fast_clock_is_still_refused(
    verifier: TokenVerifier, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    """Leeway is tolerance, not a blank cheque."""
    now = int(time.time())
    far_ahead = _sign(signing_key, _claims(iat=now + 3600, exp=now + 7200))

    with pytest.raises(AccessTokenInvalidError):
        verifier.verify(far_ahead)
