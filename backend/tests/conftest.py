"""Test fixtures.

Tests that need a database are marked `db` and skip with a clear message when
the database cannot be reached, so `make all` on a fresh clone before
backend/.env exists reports skips rather than a wall of red.

That skip is also the most dangerous thing in this file, so read `_unreachable`
before widening it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NoReturn

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from meowpay import config
from meowpay.config import BACKEND_ROOT
from meowpay.session import build_engine, build_migration_engine

SKIP_REASON = (
    "The database is not reachable. Set DATABASE_URL in backend/.env (copy "
    "backend/.env.example), and check the Supabase project is not paused: free "
    "projects pause after 7 days of inactivity."
)


def _answered(exc: Exception) -> bool:
    """Did a Postgres speaking server reply, or did nothing reply at all?

    This distinction is the whole safety of the skip below, so it is worth being
    exact about. Every test in the suite depends on the `database` fixture, and a
    skip in a session-scoped fixture is cached and replayed, so getting this
    wrong turns a completely broken setup into 20-odd skips and exit code 0.

    A server that answered and refused us says so in one of two ways, and they
    are not interchangeable:

    - A QUERY error carries a SQLSTATE. Permission denied, too many connections,
      a bad statement.
    - A CONNECTION error does NOT. psycopg does not populate `sqlstate` for the
      startup handshake, so `FATAL: password authentication failed` arrives with
      `sqlstate=None`, exactly like a DNS failure. Measured, after an earlier
      version of this function let a wrong password skip the entire suite.

    So the connection case is discriminated on the Postgres severity marker,
    which is present only when a server relayed an ErrorResponse. That is a
    string check and it is not elegant, but it is the only signal the protocol
    gives here, and the alternative is the failure mode this whole file exists
    to prevent.
    """
    if not isinstance(exc, OperationalError):
        return True
    if getattr(getattr(exc, "orig", None), "sqlstate", None) is not None:
        return True
    return "FATAL:" in str(getattr(exc, "orig", exc))


def _unreachable(exc: Exception) -> NoReturn:
    """Skip if nothing answered, raise if something answered and refused.

    A wrong password, a bad username shape, a missing privilege or an exhausted
    connection pool are all configuration failures. Reporting them as "the
    database is not reachable" would hide a broken setup behind a green run.
    """
    if _answered(exc):
        raise RuntimeError(
            "The database answered and refused the connection rather than being "
            "unreachable, so the suite fails rather than skipping. Check the password, "
            "the 'postgres.<project-ref>' username shape and the pooler port in "
            f"DATABASE_URL. Underlying error: {exc.__class__.__name__}"
        ) from exc
    if config.require_database():
        raise RuntimeError(f"MEOWPAY_REQUIRE_DB is set and {SKIP_REASON}") from exc
    pytest.skip(f"{SKIP_REASON} ({exc.__class__.__name__})")


def _admin_url() -> str:
    """The same server, but the maintenance database.

    CREATE DATABASE cannot run against the database being created, and it
    cannot run inside a transaction either, hence AUTOCOMMIT below. Parsed
    properly rather than split on "/", which would fold a query string such as
    ?sslmode=require into the database name.
    """
    # render_as_string, not str(): SQLAlchemy's __str__ masks the password.
    return (
        make_url(config.test_database_url())
        .set(database="postgres")
        .render_as_string(hide_password=False)
    )


def _test_db_name() -> str:
    """The database the suite owns, having checked that it owns it.

    The drops below use WITH (FORCE), which terminates live sessions rather than
    refusing. A one-character typo pointing this at the application database
    would therefore succeed in destroying it, and on a hosted project that is
    real data rather than a disposable container. Refusing anything that is not
    clearly a test database costs one line.
    """
    name = make_url(config.test_database_url()).database
    if not name or not name.endswith("_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL must name a database ending in '_test', got {name!r}. "
            "The suite drops this database, so it refuses to run against anything else."
        )
    app_name = make_url(config.database_url()).database
    if name == app_name:
        raise RuntimeError(
            f"TEST_DATABASE_URL and DATABASE_URL both name {name!r}. The suite drops the "
            "test database, so this would destroy the application's data."
        )
    return name


@pytest.fixture(scope="session")
def database() -> Iterator[None]:
    """Create a throwaway test database and migrate it to head.

    A whole database rather than a schema inside the shared one. Supabase allows
    it: the `postgres` role has CREATEDB, the pooler routes to the new database,
    and DROP ... WITH (FORCE) works even against a live session. Measured, not
    assumed. The isolation is worth having, because it means no routing mistake
    anywhere in this file can reach the application's rows.

    Migrated with alembic rather than Base.metadata.create_all, because the
    migrations are what a reviewer actually runs. create_all would let the
    models and the migrations drift without any test noticing.
    """
    try:
        name = _test_db_name()
    except RuntimeError:
        raise
    except Exception as exc:  # a missing DATABASE_URL lands here
        _unreachable(exc)

    admin = create_engine(
        _admin_url(), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 10}
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        _unreachable(exc)
    finally:
        # dispose in a finally, so the skip path does not leak the engine.
        admin.dispose()

    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))

    # build_migration_engine rather than build_engine: it creates the schema, and
    # build_engine refuses to hand back a connection whose search_path did not
    # land, which on an unmigrated database is every connection.
    engine = build_migration_engine(config.test_database_url())
    with engine.begin() as connection:
        alembic_cfg.attributes["connection"] = connection
        alembic_cfg.attributes["db_schema"] = config.db_schema()
        command.upgrade(alembic_cfg, "head")
    engine.dispose()

    yield

    admin = create_engine(
        _admin_url(), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 10}
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database: None) -> Iterator[Engine]:
    # Two connections, hard capped. The `connection` fixture below is strictly
    # serial, so the cap documents that assumption and leaves the rest of the
    # pooler's budget for the concurrency suite.
    eng = build_engine(config.test_database_url(), pool_size=2, max_overflow=0)
    yield eng
    eng.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """One connection per test, rolled back at the end.

    Everything a test writes lives inside an outer transaction that is never
    committed, so tests cannot see each other's data and nothing needs
    truncating between them. The concurrency tests cannot use this, because
    threads need their own connections and have to really commit to be visible
    to one another.
    """
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


@pytest.fixture
def sessions_factory(connection: Connection) -> sessionmaker[Session]:
    """A session factory bound to the rolled-back connection.

    join_transaction_mode="create_savepoint" lets the code under test call
    commit() for real while the outer transaction still owns the rollback.
    """
    return sessionmaker(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
