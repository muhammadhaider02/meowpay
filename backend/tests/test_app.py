"""Wiring: what is mounted, and whether the schema for it can be built.

No database and no network, which is a promise this file has to work to keep.
Entering a `TestClient` context runs the lifespan, and the lifespan resolves
`DATABASE_URL` and `SUPABASE_URL`, so both are supplied here as well formed
fakes. Nothing dials them: the lifespan validates the URL string and never
opens a connection, which is the property that lets these run on a machine with
no database and anywhere no secrets are available.

The fake connection string is deliberately shaped like a real Supabase pooler
URL. A value that would be refused by `config._normalise` would make these tests
fail for a reason that has nothing to do with what they assert.

The schema test is not ceremony. Every other suite drives routes through
`TestClient`, which never asks for `/openapi.json`, so a response model FastAPI
cannot describe would leave the whole suite green and break `/docs` in
production. That is the one page a reviewer opens first.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meowpay.api.app import create_app

# Never connected to. Shaped to pass every refusal in config._normalise so that
# a failure here is always about wiring and never about the fixture.
FAKE_DATABASE_URL = "postgresql://postgres.abcdefghijklmnop:hunter2@aws-0-eu-west-2.pooler.supabase.com:5432/postgres"
FAKE_SUPABASE_URL = "https://abcdefghijklmnop.supabase.co"


@pytest.fixture(autouse=True)
def _configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set unconditionally, so these tests behave the same everywhere.

    Reading the developer's real `.env` when one happens to exist would make the
    suite pass locally for a reason a clean checkout does not have.
    """
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    monkeypatch.setenv("SUPABASE_URL", FAKE_SUPABASE_URL)
    # Pinned too, because the startup log is asserted on below and a developer
    # with a real deployment origin in .env would otherwise get a red test about
    # CORS inside a test about password redaction.
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")


# Path, method. The money-moving pair is the reason this list is written out
# rather than derived: a route silently failing to register is the failure this
# catches, and deriving the expectation from the app would assert nothing.
EXPECTED = {
    ("/health", "get"),
    ("/api/v1/cats", "get"),
    ("/api/v1/cats", "post"),
    ("/api/v1/me", "get"),
    ("/api/v1/me/entries", "get"),
    ("/api/v1/transfers", "post"),
    ("/api/v1/deposits", "post"),
}


def _spec() -> dict[str, Any]:
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200, response.text
        spec: dict[str, Any] = response.json()
        return spec


def test_the_openapi_schema_can_be_generated() -> None:
    """A 500 here is a broken `/docs`, and no other test would notice."""
    assert _spec()["info"]["title"] == "MeowPay API"


def test_every_endpoint_is_mounted_where_it_is_documented() -> None:
    mounted = {
        (path, method) for path, operations in _spec()["paths"].items() for method in operations
    }

    assert mounted == EXPECTED, (
        f"missing: {sorted(EXPECTED - mounted)}, unexpected: {sorted(mounted - EXPECTED)}"
    )


def test_a_malformed_connection_string_stops_the_app_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of resolving configuration at startup.

    Without this the refusal still exists, but it fires on whichever request
    first needed a database, which on a platform where the log is all you can
    see reads as an application bug rather than a wrong variable.
    """
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL.replace(":5432/", ":6543/"))

    with pytest.raises(RuntimeError, match="transaction pooler"), TestClient(create_app()):
        pass


def test_the_startup_log_names_the_database_and_not_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A credential that reaches a log aggregator is there for ever."""
    with caplog.at_level(logging.INFO, logger="meowpay.api.app"), TestClient(create_app()):
        pass

    logged = caplog.text

    # Every value the lifespan resolves is named, because a value that is
    # resolved but not logged can stop being resolved without anyone noticing.
    assert "aws-0-eu-west-2.pooler.supabase.com" in logged
    assert FAKE_SUPABASE_URL in logged
    assert "localhost:3000" in logged

    assert "hunter2" not in logged


def test_health_is_mounted_outside_the_version_prefix() -> None:
    """So a deploy probe does not move when the API version does."""
    assert "/health" in _spec()["paths"]
    assert "/api/v1/health" not in _spec()["paths"]
