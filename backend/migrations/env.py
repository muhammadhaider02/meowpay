from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

# Importing the models is what registers every table on Base.metadata. Without
# it, autogenerate sees an empty metadata and cheerfully writes a migration that
# drops the whole schema.
import meowpay.models  # noqa: F401
from meowpay import config as app_config
from meowpay.db import Base
from meowpay.session import build_migration_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return app_config.database_url()


def _schema() -> str:
    # Tests migrate a throwaway database and hand their schema in alongside the
    # connection, so one migration file serves both.
    injected = config.attributes.get("db_schema")
    return injected if injected is not None else app_config.db_schema()


# version_table_schema is deliberately NOT set.
#
# It looks like the right thing to do once the tables live in a named schema,
# and it is actively wrong here. search_path already makes that schema the
# connection's default, so reflection reports alembic_version unqualified.
# Declaring it schema-qualified as well makes autogenerate compare a table it
# believes is in `meowpay` against one it found in the default schema, conclude
# they are different tables, and emit `remove_table('alembic_version')` on every
# run. `alembic check` then fails permanently.
#
# search_path is what puts the version table in the right place, and it is set
# on every connection by build_migration_engine before any migration runs.


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Keep autogenerate inside our own schema.

    Dormant while include_schemas is False, which it is: Alembic only consults
    this for schemas when include_schemas is on. It is here as insurance, because
    turning that flag on without this would let autogenerate see Supabase's
    auth, storage, realtime and vault schemas and write a migration that drops
    them.
    """
    if type_ == "schema":
        return name in (None, _schema())
    return True


def _configure_and_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests hand in a live connection so a throwaway database can be migrated
    # without building a second engine.
    injected = config.attributes.get("connection")
    if injected is not None:
        _configure_and_run(injected)
        return

    # build_migration_engine creates the schema if it is absent and puts the
    # connection into it. The migrations themselves name no schema, which is
    # what lets one file serve the app database and the test database.
    engine = build_migration_engine(_url(), schema=_schema())
    try:
        with engine.connect() as connection:
            _configure_and_run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
