"""Request correlation, the unhandled-error net, and CORS."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from meowpay import config
from meowpay.api.schemas import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

# Read by the error net below and, once transfers land, by the settlement log
# line, so one id ties a client's failed request to the server log entry that
# explains it.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")

Dispatch = Callable[[Request], Awaitable[Response]]


def _clean(supplied: str | None) -> str:
    """Accept a caller-supplied id only if it is a real UUID.

    Anything else is replaced rather than echoed, so a caller cannot forge log
    lines or push an unbounded string into every response header.
    """
    try:
        return str(uuid.UUID(supplied)) if supplied else str(uuid.uuid4())
    except ValueError:
        return str(uuid.uuid4())


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Dispatch) -> Response:
        request_id = _clean(request.headers.get("X-Request-ID"))
        request_id_ctx.set(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class UnhandledErrorMiddleware(BaseHTTPMiddleware):
    """Turn any uncaught exception into the standard error envelope.

    This is middleware rather than `@app.exception_handler(Exception)` on
    purpose. Starlette does not install a bare-Exception handler on the normal
    handler chain: it hands it to ServerErrorMiddleware, which is the outermost
    layer of all, outside CORS. A 500 produced there carries no
    Access-Control-Allow-Origin, so the browser blocks it and the frontend sees
    an opaque network error instead of the error code it is supposed to branch
    on. Registering here, inside CORS, means the envelope actually reaches the
    client that needs it.
    """

    async def dispatch(self, request: Request, call_next: Dispatch) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            request_id = request_id_ctx.get("")
            logger.error(
                "Unhandled exception on %s %s (request_id=%s)",
                request.method,
                request.url.path,
                request_id,
                exc_info=exc,
            )
            # Deliberately opaque. An unexpected failure in a money service must
            # not leak a traceback, a SQL fragment or a connection string. The
            # request id is what ties this response to the log line with the
            # detail.
            return JSONResponse(
                status_code=500,
                content=ErrorResponse(
                    error=ErrorDetail(
                        code="internal_error",
                        message="An unexpected error occurred.",
                        request_id=request_id,
                    )
                ).model_dump(),
            )


def add_middleware(app: FastAPI) -> None:
    """Install the middleware stack.

    Order matters and reads backwards: Starlette runs the LAST one added as the
    outermost layer. The result is

        RequestID  ->  CORS  ->  UnhandledError  ->  the route

    so every response including a 500 gets both a correlation id and CORS
    headers on the way back out.
    """
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # An explicit origin list, never a wildcard. The access token travels in
        # an httpOnly cookie, and with credentials enabled Starlette echoes the
        # request origin back rather than sending "*", so a wildcard would fail
        # open and allow every site on the internet. config.cors_origins()
        # refuses "*" outright for that reason.
        allow_origins=config.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Without this the browser hides X-Request-ID from cross-origin
        # JavaScript, making the correlation id unreadable by the client it
        # exists for.
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIDMiddleware)
