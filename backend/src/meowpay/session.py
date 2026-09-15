"""The engine and the session factory."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from meowpay import config

# Session level nets. Coarse on purpose: these exist so nothing runs forever,
# not to enforce anything. The one setting the transfer path actually depends on
# is lock_timeout, and it lives in meowpay.ledger, applied per transaction. See
# the note in _apply_session_settings below.
SESSION_STATEMENT_TIMEOUT = "30s"
SESSION_IDLE_IN_TRANSACTION_TIMEOUT = "60s"


def _apply_session_settings(dbapi_connection: Any, schema: str) -> None:
    """Put a new connection into the schema, with the session nets applied.

    Two things here are load bearing and neither is obvious.

    The settings are issued here rather than through libpq's startup `options`,
    which is where they used to live. Supavisor parses the startup packet for its
    own tenant routing and does not forward arbitrary settings to the backend it
    owns. Measured against the real project: connecting through the session
    pooler with `-c lock_timeout=3s` in connect_args and then asking the server
    gives back `0`. The connection succeeds, nothing errors and the setting is
    simply absent.

    And `autocommit` is toggled on around the SET. psycopg connects with
    autocommit off, so a bare SET opens a transaction, and the pool issues a
    ROLLBACK every time a connection is returned, which reverts it. The handler
    would work for the first checkout and then silently stop, leaving every
    later statement on that connection resolving against the wrong schema. This
    is SQLAlchemy's own documented idiom for exactly this reason.
    """
    previous = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        cursor = dbapi_connection.cursor()
        try:
            # Identifier, so it cannot be bound as a parameter. config.db_schema()
            # is ours rather than a caller's, and the quoting keeps it inert.
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute(f"SET statement_timeout = '{SESSION_STATEMENT_TIMEOUT}'")
            cursor.execute(
                "SET idle_in_transaction_session_timeout = "
                f"'{SESSION_IDLE_IN_TRANSACTION_TIMEOUT}'"
            )
            # public is deliberately NOT on the path, so verify we landed
            # somewhere. current_schema() returns the first schema on the path
            # that exists, so a missing or misspelled schema comes back NULL and
            # this fires as a connection failure, which is what it is. Without
            # it the same mistake is a successful query against the wrong rows.
            cursor.execute("SELECT current_schema()")
            landed = cursor.fetchone()[0]
            if landed != schema:
                raise RuntimeError(
                    f"search_path was set to {schema!r} but current_schema() is {landed!r}. "
                    f"The schema probably does not exist yet. Run `make migrate`."
                )
        finally:
            cursor.close()
    finally:
        dbapi_connection.autocommit = previous


def _register_schema_listener(engine: Engine, schema: str) -> None:
    # insert=True so this runs ahead of any other connect handler. `connect`
    # rather than `first_connect`, which fires once per engine: pool_pre_ping
    # reconnects would otherwise come back with a default search_path.
    @event.listens_for(engine, "connect", insert=True)
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        _apply_session_settings(dbapi_connection, schema)


def build_engine(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    schema: str | None = None,
) -> Engine:
    """Build an engine with the settings the transfer path depends on.

    isolation_level is pinned rather than inherited. Under REPEATABLE READ the
    idempotent replay silently breaks: the re-read runs against the
    transaction's original snapshot, so it cannot see the row the winning
    duplicate just committed, and the replay finds nothing. REPEATABLE READ also
    turns every lock wait on a concurrently-updated row into a 40001 failure
    instead of a block, which would force a retry loop around every transfer.

    pool_size and max_overflow are parameters because the concurrency tests need
    a hard cap they can reason about. A race test whose threads queue on the
    pool never actually races, and still passes.
    """
    engine = create_engine(
        url,
        isolation_level="READ COMMITTED",
        pool_size=pool_size,
        # Exposed so the concurrency tests can set it to 0 and make pool_size a
        # HARD cap. With the default of 10, pool_size is only a soft target and
        # overflow connections are opened past it, which would let a test that
        # means to starve the pool quietly succeed instead.
        max_overflow=max_overflow,
        pool_pre_ping=True,
        # Supavisor drops idle client connections. Recycling first turns most of
        # what pool_pre_ping would otherwise catch into a non-event, which
        # matters because a pre_ping costs a round trip and this link is remote.
        pool_recycle=300,
        connect_args={
            # libpq defaults connect_timeout to 0, meaning infinite. Without
            # this, a host that drops packets rather than refusing (a firewall,
            # a dead node, a typo in DATABASE_URL) hangs the request forever,
            # and /health never gets to report 503.
            #
            # Ten rather than five: this is a WAN link with a TLS handshake and
            # a pooler hop, against an instance that may be waking. A timeout
            # too tight produces a connection error with no SQLSTATE, which the
            # test suite correctly reads as "unreachable" and skips on. So a
            # tight connect_timeout is itself a way to get a green run that
            # tested nothing.
            "connect_timeout": 10,
            # psycopg's default. Stated rather than inherited because it is only
            # safe in SESSION mode: the transaction pooler hands the same client
            # a different backend between transactions, and a named statement
            # prepared on one is 42P05 or 26000 on the next. config.database_url
            # refuses port 6543 so that cannot happen, and this comment is why
            # that refusal exists.
            "prepare_threshold": 5,
        },
    )
    _register_schema_listener(engine, schema or config.db_schema())
    return engine


def build_migration_engine(url: str, *, schema: str | None = None) -> Engine:
    """An engine for Alembic and for schema setup and teardown.

    Differs from build_engine in three ways, each for its own reason. NullPool
    because it is a one-shot and a lingering connection blocks DROP DATABASE.
    prepare_threshold=None because nothing here runs five times, so preparing
    can only cost, and DDL churns plans anyway. No statement_timeout, because a
    CREATE INDEX on a cold instance over a WAN link can outlast the app's net
    and would then fail as 57014, which reads as an outage rather than a limit
    somebody set.
    """
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 10, "prepare_threshold": None},
    )

    target = schema or config.db_schema()

    # Not the app's listener: this engine is used to CREATE the schema, so it
    # cannot require the schema to already exist.
    @event.listens_for(engine, "connect", insert=True)
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{target}"')
                cursor.execute(f'SET search_path TO "{target}"')
            finally:
                cursor.close()
        finally:
            dbapi_connection.autocommit = previous

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return build_engine(config.database_url())


@lru_cache(maxsize=1)
def get_sessions() -> sessionmaker[Session]:
    """The application session factory.

    expire_on_commit=False so results built inside a transaction stay readable
    after it commits. Without it, touching any attribute after the block exits
    triggers a refresh against a closed session.
    """
    return sessionmaker(get_engine(), expire_on_commit=False)
