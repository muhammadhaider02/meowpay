"""Turning a refusal into the error envelope.

Why these are exception handlers while the 500 net is middleware, which looks
inconsistent and is not:

Starlette holds handlers registered for a SPECIFIC exception class in
`ExceptionMiddleware`, the innermost wrapper around the router. So a response
built here travels back out through `CORSMiddleware` and picks up its headers.
A handler registered for bare `Exception` is different: Starlette hands that to
`ServerErrorMiddleware`, the outermost layer of all, OUTSIDE CORS. Its response
would carry no `Access-Control-Allow-Origin`, the browser would block it, and
the frontend could not read the `code` it branches on. That is why
`UnhandledErrorMiddleware` is middleware and has to stay that way.

Resulting order:

    RequestID -> CORS -> UnhandledError -> ExceptionMiddleware(AppError) -> route
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meowpay.api.middleware import request_id_ctx
from meowpay.api.schemas import ErrorDetail, ErrorResponse
from meowpay.errors import AppError

logger = logging.getLogger(__name__)


def _envelope(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=request_id_ctx.get() or None)
    )
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


def add_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        # RFC 6750: any 401 from a bearer-protected resource says how to
        # authenticate. Without it a client cannot tell "your token is bad" from
        # "this endpoint is broken".
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        if exc.status >= 500:
            logger.error("%s: %s", type(exc).__name__, exc.message)
        return _envelope(exc.status, exc.code, exc.message, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own handler returns {"detail": [...]}, which is not the shape
        # anything else here returns. One error contract across the whole API
        # matters more than the field-level detail, which is reconstructible from
        # the log and is mostly noise to a client that cannot act on it.
        logger.info("request validation failed: %s", exc.errors())
        return _envelope(422, "validation_error", "The request body is not valid.")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers 404 and 405, which FastAPI raises directly. Same envelope, so a
        # typo in a path and a rejected transfer are parsed the same way.
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return _envelope(exc.status_code, code, str(exc.detail), dict(exc.headers or {}) or None)
