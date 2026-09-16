"""Wiring: what is mounted, and whether the schema for it can be built.

No database and no network. `create_app()` reads CORS origins from the
environment and nothing else, so these run anywhere.

The schema test is not ceremony. Every other suite drives routes through
`TestClient`, which never asks for `/openapi.json`, so a response model FastAPI
cannot describe would leave the whole suite green and break `/docs` in
production. That is the one page a reviewer opens first.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from meowpay.api.app import create_app

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


def test_health_is_mounted_outside_the_version_prefix() -> None:
    """So a liveness probe does not move when the API version does."""
    assert "/health" in _spec()["paths"]
    assert "/api/v1/health" not in _spec()["paths"]
