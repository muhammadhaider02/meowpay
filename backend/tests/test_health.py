"""The health endpoint."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from meowpay.api.app import create_app
from meowpay.api.deps import sessions

pytestmark = pytest.mark.db


@pytest.fixture
def app(sessions_factory: sessionmaker[Session]) -> FastAPI:
    """An app whose database is the throwaway test one, not the dev database.

    Overriding the dependency is the whole reason routes reach the session
    factory through `deps.sessions` instead of importing it. Without this the
    endpoint would resolve the process-wide engine built from DATABASE_URL and
    quietly read dev data.
    """
    application = create_app()
    application.dependency_overrides[sessions] = lambda: sessions_factory
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Not entered as a context manager, so no lifespan runs.

    raise_server_exceptions=False lets the app's own error middleware produce
    the response, which is what makes the envelope assertable.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_health_is_healthy_when_the_database_is_migrated(client: TestClient) -> None:
    # Asserted unconditionally. An earlier version skipped on 503, which meant a
    # genuinely broken /health reported itself as the reviewer's Postgres being
    # down and the suite stayed green. The `db` marker and the conftest skip
    # already handle a missing database, so this can be unconditional.
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["database"] == "healthy"


def test_health_is_unhealthy_when_the_database_is_unreachable(
    app: FastAPI, client: TestClient
) -> None:
    """Health has to be able to fail, or it is not telling you anything.

    The factory resolves fine and blows up on use, which is what a real
    unreachable database does: the engine is built lazily and only discovers it
    cannot connect when a session actually opens.
    """

    def _unreachable() -> sessionmaker[Session]:
        def _connect(*args: object, **kwargs: object) -> object:
            raise OSError("connection refused")

        return _connect  # type: ignore[return-value]

    app.dependency_overrides[sessions] = _unreachable

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["database"] == "unhealthy"


def test_health_is_unhealthy_when_the_database_is_reachable_but_unmigrated(
    client: TestClient, connection: object, sessions_factory: sessionmaker[Session]
) -> None:
    """SELECT 1 would pass here. Reading the treasury is what catches it."""
    with sessions_factory() as session:
        session.execute(text("DELETE FROM cats"))
        session.commit()

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["database"] == "unhealthy"


def test_an_unexpected_failure_returns_the_envelope_with_cors_and_leaks_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meowpay.api.routes.health as health_module

    def _explode() -> str:
        raise RuntimeError("password=hunter2 in a connection string")

    monkeypatch.setattr(health_module, "_version", _explode)

    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    # The detail belongs in the server log, never in the response body.
    assert "hunter2" not in response.text

    # The error net sits inside CORS on purpose. A 500 raised from the outermost
    # layer carries no CORS headers, the browser blocks it, and the frontend
    # cannot read the code it is supposed to branch on or the id that would let
    # anyone find the log line.
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert error["request_id"]
    assert response.headers["x-request-id"] == error["request_id"]


def test_a_forged_request_id_is_replaced_rather_than_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "not a uuid " + "x" * 300})

    assert response.status_code == 200
    echoed = response.headers["x-request-id"]
    assert "x" * 300 not in echoed
    assert len(echoed) == 36
