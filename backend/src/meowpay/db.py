"""The declarative base.

Deliberately separate from `session.py`. This module imports nothing
environmental, so importing a model never requires DATABASE_URL to be set and
Alembic can load the metadata without constructing an engine.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names. Without this, Postgres invents names for
# unnamed constraints, autogenerate diffs churn, and a test that wants to assert
# on a specific constraint has nothing stable to name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
