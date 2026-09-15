"""The FastAPI application factory and the server entry point."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from meowpay.api.middleware import add_middleware
from meowpay.api.routes import api_router
from meowpay.api.routes.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="MeowPay API",
        description="A digital wallet for cats. Humans top it up, cats send each other treats.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    add_middleware(app)

    # Health sits at the root, unversioned, because a liveness probe should not
    # move when the API version does.
    app.include_router(health_router)
    app.include_router(api_router)

    return app


def serve() -> None:
    """Entry point for the `meowpay-api` command. Pass --reload for hot reload."""
    import sys

    import uvicorn

    from meowpay import config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    # Handed to uvicorn as an import string with factory=True rather than as an
    # app object, because --reload needs something it can re-import.
    uvicorn.run(
        "meowpay.api.app:create_app",
        factory=True,
        host=config.optional_env("API_HOST", "127.0.0.1"),
        port=int(config.optional_env("API_PORT", "8000")),
        reload="--reload" in sys.argv[1:],
    )
