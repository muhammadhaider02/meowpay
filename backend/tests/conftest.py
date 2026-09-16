"""Test fixtures.

Tests that need a database are marked `db` and skip with a clear message when
the database cannot be reached, so `make all` on a fresh clone before
backend/.env exists reports skips rather than a wall of red.

That skip is also the most dangerous thing in this file, so read `_unreachable`
before widening it.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import NoReturn

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, insert, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from meowpay import config
from meowpay.api.app import create_app
from meowpay.api.deps import claims, sessions
from meowpay.auth import Claims
from meowpay.config import BACKEND_ROOT
from meowpay.constants import TREASURY_CAT_ID
from meowpay.ledger import Ledger
from meowpay.models import Cat
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


# -- the ledger ------------------------------------------------------------


@pytest.fixture
def ledger(sessions_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(sessions_factory)


@pytest.fixture
def make_cat(sessions_factory: sessionmaker[Session]) -> Callable[..., uuid.UUID]:
    """Create an ordinary cat.

    auth_user_id is not optional. ck_cats_only_system_lacks_auth_user says
    is_system = (auth_user_id IS NULL), so a cat created without one is a system
    account, and ck_cats_only_the_sentinel_is_system then rejects it under a name
    that has nothing to do with identity. Written down here once so a fixture
    that forgets it does not fail confusingly.

    A random uuid is not a lie. There is deliberately no foreign key to
    auth.users, so as far as this database is concerned a cat's identity is a
    uuid and nothing more. That is exactly why the ledger suite can run without
    an auth server anywhere near it.

    The handle has to satisfy ck_cats_handle_shape and be lowercase. Hex off the
    uuid does both and cannot collide across tests.
    """

    def _make(
        balance: int = 0,
        handle: str | None = None,
        auth_user_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        cat_id = uuid.uuid4()
        with sessions_factory.begin() as session:
            session.execute(
                insert(Cat).values(
                    id=cat_id,
                    handle=handle or f"cat_{cat_id.hex[:12]}",
                    display_name="Test Cat",
                    auth_user_id=auth_user_id or uuid.uuid4(),
                    balance=balance,
                )
            )
        return cat_id

    return _make


# -- the HTTP surface ------------------------------------------------------


@pytest.fixture
def app(sessions_factory: sessionmaker[Session]) -> FastAPI:
    """An app whose database is the throwaway test one, not the application's.

    Overriding the dependency is the whole reason routes reach the session
    factory through `deps.sessions` instead of importing it. Without this the
    endpoint would resolve the process-wide engine built from DATABASE_URL and
    quietly read real data.
    """
    application = create_app()
    application.dependency_overrides[sessions] = lambda: sessions_factory
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """raise_server_exceptions=False lets the app's own error middleware produce
    the response, which is what makes the envelope assertable rather than having
    the exception propagate into the test.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def as_identity(app: FastAPI) -> Callable[..., Claims]:
    """Present a verified token, without a token.

    Overrides `deps.claims` and leaves `get_current_cat` real, so the parts that
    matter still run against the database: the cat lookup, the 403 when there is
    no cat, and the treasury being unreachable. Only the cryptography is stubbed,
    and that has its own suite in test_auth_tokens.py which uses no database.
    """

    def _as(auth_user_id: uuid.UUID | None = None) -> Claims:
        identity = Claims(
            auth_user_id=auth_user_id or uuid.uuid4(),
            email="test@meowpay.test",
            session_id=str(uuid.uuid4()),
        )
        app.dependency_overrides[claims] = lambda: identity
        return identity

    return _as


# -- concurrency -----------------------------------------------------------
#
# The fixtures above cannot be used for these. `sessions_factory` binds every
# session to ONE connection inside an outer transaction that is rolled back, so
# ten threads would share one Postgres backend, interleave statements on a
# connection that is not thread safe, and never actually race. Threads need their
# own connections and have to really commit to be visible to each other, which
# means real cleanup instead of rollback.

CONCURRENCY_THREADS = 8

# The warm barrier is reached only after a thread has opened a connection, which
# against a remote database means a TCP connect, a TLS handshake, pooler auth and
# the connect listener's own round trips, for every thread at once. Measured at
# roughly a second per connection, so fifteen was uncomfortably close.
WARM_TIMEOUT_SECONDS = 30
# The go barrier is reached milliseconds later with every connection already
# warm, so a long wait there is a genuine hang and the tight bound is the point.
GO_TIMEOUT_SECONDS = 15
# Covers one full lock_timeout wait plus a settle, with wide margin. Not a
# correctness knob: it is the "something is actually wedged" backstop.
RESULT_TIMEOUT_SECONDS = 60


@pytest.fixture(scope="session")
def committing_engine(database: None) -> Iterator[Engine]:
    """An engine whose sessions really commit, one connection per thread.

    pool_size is raised to match the thread count and overflow is switched off,
    so every thread provably gets its own backend or the test fails.
    """
    # max_overflow=0 makes pool_size a hard cap. Without it SQLAlchemy opens up
    # to 10 more connections past pool_size, so a starved pool would still hand
    # out enough connections and the guard in `race` would never fire.
    eng = build_engine(config.test_database_url(), pool_size=CONCURRENCY_THREADS, max_overflow=0)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def committing_sessions(committing_engine: Engine) -> sessionmaker[Session]:
    """Bound to the ENGINE, not a connection, so each session gets its own backend."""
    return sessionmaker(committing_engine, expire_on_commit=False)


@pytest.fixture
def committed_tables(committing_engine: Engine) -> Iterator[None]:
    """Wipe before and after, since these tests really commit.

    Before as well as after, so a test that died halfway does not poison the next.
    `cats` is not truncated: migration 0002 put the treasury there and every
    deposit depends on it, so its balance is reset instead. `transfers` and
    `entries` truncate together because entries references transfers with
    ON DELETE RESTRICT.

    This runs against the throwaway database, not the application's. That is the
    whole of the protection and it is why a separate database was worth keeping:
    no routing mistake in this file can reach real rows, because they are not in
    this database at all. `_test_db_name()` is what guarantees which database
    this is.
    """

    def wipe() -> None:
        with committing_engine.begin() as conn:
            conn.execute(text("TRUNCATE entries, transfers RESTART IDENTITY"))
            conn.execute(text("DELETE FROM cats WHERE id <> :t"), {"t": TREASURY_CAT_ID})
            conn.execute(text("UPDATE cats SET balance = 0 WHERE id = :t"), {"t": TREASURY_CAT_ID})

    wipe()
    yield
    wipe()


@pytest.fixture
def race() -> Callable[..., list[object]]:
    """Run `work` on N threads that provably overlap.

    Four things make this a race rather than a loop, and each answers a way this
    kind of test silently stops testing anything.

    1. Two barriers. `warm` holds every thread while it has a session open, `go`
       releases them into the ledger at the same instant. Without them the OS
       serialises by start time and the slowest thread runs alone.
    2. The backend pid is recorded WHILE the warm barrier holds every session
       open, so N distinct pids proves N Postgres sessions existed at once.
       Collecting them afterwards would prove nothing: one connection reused N
       times reports one pid N times.
    3. If the pool cannot supply N connections the warm barrier times out and the
       test fails with BrokenBarrierError. That is the point. A pool that
       serialises the threads turns this into a slow serial test that still
       passes.
    4. Connections are warm before the measured phase, so no thread spends its
       first milliseconds on a handshake while the others are already settling.

    Exceptions are returned rather than raised, because "five succeeded and three
    hit insufficient funds" is a correct outcome for several of these tests.

    The pid assertion now does double duty. Through a transaction pooler,
    connections are multiplexed and the pids would repeat, so this also fails
    loudly if DATABASE_URL ever names the wrong port. If it does fire, the answer
    is never to quietly lower CONCURRENCY_THREADS: that silently weakens six
    tests and leaves them passing.
    """

    def _race(
        sessions: sessionmaker[Session],
        work: Callable[[int], object],
        *,
        threads: int = CONCURRENCY_THREADS,
    ) -> list[object]:
        warm = threading.Barrier(threads, timeout=WARM_TIMEOUT_SECONDS)
        go = threading.Barrier(threads, timeout=GO_TIMEOUT_SECONDS)
        pids: list[int] = []
        guard = threading.Lock()

        def runner(index: int) -> object:
            with sessions() as session:
                pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
                with guard:
                    pids.append(pid)
                # Release the read transaction that autobegan above before
                # blocking. Holding one open across the barrier would sit idle in
                # transaction for as long as the slowest thread takes to arrive.
                session.rollback()
                warm.wait()
            go.wait()
            return work(index)

        outcomes: list[object] = []
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(runner, i) for i in range(threads)]
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=RESULT_TIMEOUT_SECONDS))
                except Exception as exc:
                    outcomes.append(exc)

        assert len(set(pids)) == threads, (
            f"{len(set(pids))} distinct Postgres backends for {threads} threads. "
            "The pool serialised them, so this test did not actually race."
        )
        return outcomes

    return _race
