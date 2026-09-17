"""Environment configuration.

Plain os.getenv with lazy validation. require_env raises at the point of use
rather than at import, so a missing variable names itself in the traceback of
the thing that actually needed it.

`database_url()` does not hand back what it was given. It rewrites two things
silently, the driver and a missing sslmode, and refuses three outright: the
transaction pooler port, an sslmode that permits plaintext, and a bare
`postgres` username against the pooler. Each refusal is a copy-paste mistake
whose symptom otherwise points somewhere else entirely.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(BACKEND_ROOT / ".env")

# Supabase's transaction pooler. Refused rather than supported: see _normalise.
TRANSACTION_POOLER_PORT = 6543

# The throwaway database the suite creates and drops. Never the app's.
TEST_DATABASE_NAME = "meowpay_test"

# Where the app's tables live. Not `public`, which is what Supabase exposes
# through PostgREST to anyone holding the publishable key.
DEFAULT_SCHEMA = "meowpay"


def require_env(name: str) -> str:
    """Return an environment variable, or raise explaining how to set it."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy backend/.env.example to backend/.env and fill it in."
        )
    return value


def optional_env(name: str, default: str) -> str:
    """Return an environment variable, falling back to a default.

    An empty value counts as unset, so a blank line in .env behaves the same as
    an absent one.
    """
    return os.getenv(name) or default


def _normalise(url: str) -> str:
    """Make a pasted Supabase connection string safe to hand to SQLAlchemy.

    Parsed with make_url rather than split on punctuation, because a generated
    password contains anything and `str()` on a URL masks it.
    """
    parsed = make_url(url)

    # 1. The dashboard gives `postgresql://`. SQLAlchemy resolves a bare
    #    `postgresql` to psycopg2, which is not a dependency, and the resulting
    #    ModuleNotFoundError reads as a broken install rather than a bad URL.
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")

    # 2. The transaction pooler breaks three things at once and all of them are
    #    silent: prepared statements become intermittent 42P05/26000 errors under
    #    load only, session level settings stop holding, and connections are
    #    multiplexed so the concurrency suite's one-backend-per-thread assertion
    #    fails. One digit separates the two ports, so this is a refusal and not a
    #    comment.
    if parsed.port == TRANSACTION_POOLER_PORT:
        raise RuntimeError(
            f"DATABASE_URL points at port {TRANSACTION_POOLER_PORT}, the Supabase transaction "
            "pooler. This backend is a long lived process and needs the session pooler on port "
            "5432, which gives one server connection per client connection. Copy the 'Session "
            "pooler' string from the Supabase dashboard under Connect."
        )

    # 3. This URL carries a real password over the public internet. libpq
    #    defaults to `prefer`, which falls back to plaintext without saying so.
    #    Adding `require` where it is absent can only tighten, so it is silent.
    #    Downgrading it is a refusal.
    query = dict(parsed.query)
    sslmode = query.get("sslmode")

    # A repeated key parses as a tuple rather than a string, and libpq honours
    # the last occurrence. Collapsing to that value first is what stops
    # `?sslmode=require&sslmode=disable` slipping past the refusal below as a
    # value matching neither branch. Lowercased for the same reason: `Disable`
    # disables just as thoroughly as `disable`.
    if isinstance(sslmode, tuple):
        sslmode = sslmode[-1] if sslmode else None
    if sslmode is not None:
        sslmode = sslmode.lower()

    if sslmode in ("disable", "allow", "prefer"):
        raise RuntimeError(
            f"DATABASE_URL sets sslmode={sslmode}, which permits an unencrypted connection. "
            "The password and every balance travel over this link. Use sslmode=require."
        )

    query["sslmode"] = sslmode or "require"
    parsed = parsed.set(query=query)

    # 4. A plain `postgres` username against the pooler produces Supavisor's
    #    "Tenant or user not found", which names neither the username nor the
    #    project and is not a diagnosable error.
    host = parsed.host or ""
    if "pooler.supabase.com" in host and "." not in (parsed.username or ""):
        raise RuntimeError(
            f"DATABASE_URL uses username {parsed.username!r} against the Supabase pooler, which "
            "expects 'postgres.<project-ref>'. Copy the connection string from the dashboard "
            "rather than assembling it."
        )

    return parsed.render_as_string(hide_password=False)


def database_url() -> str:
    """The application database, normalised.

    Required. There is deliberately no fallback: a default would let a
    misconfigured deployment connect to nothing and report it as a database
    outage rather than a missing variable.
    """
    return _normalise(require_env("DATABASE_URL"))


def database_summary() -> str:
    """The connection, with the password removed, safe to put in a log.

    Everywhere else in this module renders with `hide_password=False`, because
    the point there is to hand a working URL to a driver. This is the one place
    that exists to be read by a human, so it is the one place that must not
    carry the credential: a startup line naming the host, port and database is
    what makes a misconfigured deploy diagnosable from a dashboard log, and a
    startup line carrying the password is a credential in a log aggregator for
    ever.

    Built from the parsed components rather than by masking the rendered
    string, so there is no pattern for an unusual password to slip past. The
    credential is never in the value at all.
    """
    parsed = make_url(database_url())
    return f"{parsed.host}:{parsed.port or 5432}/{parsed.database}"


def test_database_url() -> str:
    """The throwaway database the suite creates, migrates and drops.

    Defaults to the application URL with the database name swapped, so there is
    one credential to configure rather than two that can drift. The suite
    refuses any name not ending in `_test` before it runs a single DROP.
    """
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        return _normalise(override)
    return (
        make_url(database_url())
        .set(database=TEST_DATABASE_NAME)
        .render_as_string(hide_password=False)
    )


def db_schema() -> str:
    """The schema the application's tables live in.

    Not `public`. Supabase runs PostgREST over `public` and the publishable key
    ships in the browser bundle, so a table there is reachable by a PATCH that
    never touches the ledger. A schema PostgREST does not expose has no such
    resource to address.

    Models stay schema-unqualified and this is applied through `search_path`
    instead. Setting it on the metadata would make autogenerate compare
    qualified models against the connection's default schema, which is permanent
    `alembic check` drift.
    """
    return optional_env("DB_SCHEMA", DEFAULT_SCHEMA)


def require_database() -> bool:
    """Whether an unreachable database is an error rather than a skip.

    Off by default, because a developer without the variables set should get a
    clear skip. On in CI, where a skipped suite is indistinguishable from a
    passing one and a paused free-tier Supabase project would produce exactly
    that.
    """
    return os.getenv("MEOWPAY_REQUIRE_DB", "").strip().lower() in ("1", "true", "yes")


def supabase_url() -> str:
    """The Supabase project, without a trailing slash.

    Public: it ships in the browser bundle anyway. Required, because a verifier
    that cannot name its issuer cannot verify anything, and guessing would mean
    accepting tokens from an issuer we did not choose.
    """
    return require_env("SUPABASE_URL").rstrip("/")


def jwks_url() -> str:
    """Where the token signing keys live.

    Returns no keys at all if the project is still on a shared HS256 secret,
    which is the loud failure we want rather than a quiet fallback.
    """
    return optional_env("SUPABASE_JWKS_URL", f"{supabase_url()}/auth/v1/.well-known/jwks.json")


def jwt_issuer() -> str:
    return optional_env("SUPABASE_JWT_ISSUER", f"{supabase_url()}/auth/v1")


def jwt_audience() -> str:
    return optional_env("SUPABASE_JWT_AUDIENCE", "authenticated")


def jwks_cache_seconds() -> int:
    """How long a fetched key set is trusted. Matches Supabase's own edge cache."""
    return int(optional_env("SUPABASE_JWKS_CACHE_SECONDS", "600"))


def jwks_timeout_seconds() -> float:
    """PyJWKClient defaults to 30, which is 30 seconds of a hung request."""
    return float(optional_env("SUPABASE_JWKS_TIMEOUT_SECONDS", "3"))


def supabase_secret_key() -> str:
    """The service role key, for the GoTrue admin API.

    Read by `meowpay-seed` and by nothing else. It bypasses every row level
    security policy, so it must NOT be set on the API service: the API has no use
    for it and its presence there is a standing privilege escalation risk.
    """
    return require_env("SUPABASE_SECRET_KEY")


def cors_origins() -> list[str]:
    """Origins allowed to call the API from a browser.

    The access token travels in an Authorization header rather than a cookie, so
    these are not credentialed requests and a wildcard would not fail open the
    way it would with cookies. The explicit list stays anyway: it is free blast
    radius control, and it documents which deployments exist.
    """
    # 127.0.0.1 alongside localhost, because a reviewer who opens the frontend
    # by IP would otherwise get a CORS failure on every preflight.
    raw = optional_env("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins:
        raise RuntimeError(
            "CORS_ORIGINS cannot be '*'. List the frontend origins explicitly, so that a "
            "misconfiguration is a refusal to start rather than every site on the internet "
            "being able to call this API."
        )
    return origins
