"""The FastAPI application factory and the server entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from meowpay import config
from meowpay.api.errors import add_exception_handlers
from meowpay.api.middleware import add_middleware
from meowpay.api.routes import api_router
from meowpay.api.routes.health import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    """Resolve the configuration once, loudly, before the first request.

    Every getter here is lazy by design, so without this a deployment missing a
    variable boots green and fails one request at a time, with the reason buried
    in whichever endpoint happened to need it first. On a platform where the log
    is the only thing you can see, that is the difference between a named error
    and an afternoon.

    **This deliberately opens no connection.** Validating the URL string rather
    than dialling it separates two failures that deserve opposite answers: a URL
    naming the transaction pooler is never going to work and should kill the
    boot, while a database that is merely unreachable is transient and should
    leave the service up so `/health` can report it honestly. Connecting here
    would collapse the second into the first and turn a blip into a failed
    deploy.

    Note what it does not catch. A well formed URL pointing at the wrong project,
    or carrying a rotated password, passes every check and is caught later by
    `/health`. The value here is narrow and worth having: the three refusals in
    `config._normalise` are copy-paste shapes, and naming one in the first line
    of the log beats discovering it through a health check that only says it
    failed.
    """
    database = config.database_summary()
    supabase = config.supabase_url()
    origins = config.cors_origins()

    # These lines reach the deployed log because `serve()` configures the root
    # logger in the process that runs it. Under `--reload` uvicorn re-imports
    # the app in a child where `serve()` never runs, so the root logger has no
    # handler and INFO is dropped: the summary is a production diagnostic, not a
    # local one. A refusal still surfaces either way, because that is an
    # exception rather than a log line.

    logger.info("MeowPay API starting")
    logger.info("  database: %s", database)
    logger.info("  supabase: %s", supabase)
    logger.info("  cors origins: %s", ", ".join(origins))

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="MeowPay API",
        description="A digital wallet for cats. Humans top it up, cats send each other treats.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    add_middleware(app)
    # After the middleware, and registered for specific classes so the
    # responses travel back out through CORS. See api/errors.py.
    add_exception_handlers(app)

    # Health sits at the root, unversioned, because a deploy probe should not
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
