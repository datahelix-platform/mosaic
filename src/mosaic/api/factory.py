"""FastAPI application factory for Mosaic."""

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from mosaic.api.exceptions import EntityNotFoundError
from mosaic.api.schemas import ErrorResponse
from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import (
    AdapterError,
    ConfigError,
    EntityAlreadySupersededError,
    EntityTypeConflictError,
    IngestionError,
    MosaicError,
    ProvenanceIntegrityError,
    SchemaError,
    SearchCapabilityError,
    TemporalQueryError,
    ValidationError as MosaicValidationError,
    ValidationFailed,
    ValidationFailure,
)
from mosaic.core.middleware import ACTOR_HEADER, PassThroughAuthMiddleware

logger = logging.getLogger(__name__)

# Net-new SDK exception → (HTTP status, error title) mapping (sec4 §4.3).
#
# This table covers the MosaicError subclasses that did NOT already have a
# dedicated handler on main (EntityNotFoundError→404, the two validation
# errors→422, and the tier-tagged ValidationFailed envelope are registered
# separately below and are intentionally absent here). Anything not listed
# here (recipe errors, migration errors, ...) falls through to the
# ``MosaicError`` fallback: a named 500 carrying the SDK message rather than
# an anonymous "Internal Server Error".
#
# Starlette resolves exception handlers by walking ``type(exc).__mro__``, so
# a handler registered for a subclass always wins over the ``MosaicError``
# fallback (and the generic ``Exception`` handler) regardless of
# registration order — most-specific wins.
#
# Status choices follow sec4 §4.3: ``ConfigError`` (e.g. adapter conflict)
# is a 409, ``AdapterError`` (storage failure) is a named 500. Supersession
# conflicts map to 409 (mirrors the GraphQL ``ALREADY_SUPERSEDED`` code);
# ingestion / search-capability / temporal-query / schema errors are
# client-side 400s; provenance-integrity faults are loud, named 500s.
_MOSAIC_EXCEPTION_STATUS: list[tuple[type[MosaicError], int, str]] = [
    # Conflict — the request collides with current entity/config state.
    (EntityAlreadySupersededError, 409, "Entity Already Superseded"),
    (EntityTypeConflictError, 409, "Entity Type Conflict"),
    (ConfigError, 409, "Configuration Error"),
    # Semantic validation failure surfaced outside the ValidationFailed path.
    (ValidationFailure, 422, "Validation Failed"),
    # Client-side errors — the request referenced something invalid.
    (IngestionError, 400, "Ingestion Error"),
    (SearchCapabilityError, 400, "Search Capability Error"),
    (TemporalQueryError, 400, "Temporal Query Error"),
    (SchemaError, 400, "Schema Error"),
    # Server-side errors — loud, named, and logged.
    (AdapterError, 500, "Storage Adapter Error"),
    (ProvenanceIntegrityError, 500, "Provenance Integrity Error"),
]


def create_app(
    routers: Optional[list] = None,
    mosaic_client: Optional[MosaicClient] = None,
    title: str = "Mosaic API",
    version: Optional[str] = None,
    description: str = (
        "A runtime for LinkML schemas. Generates a typed SDK, REST API, "
        "append-only provenance store, and dynamic validation from a "
        "schema definition."
    ),
    docs_url: str = "/docs",
    redoc_url: str = "/redoc",
    openapi_url: str = "/openapi.json",
    cors_allow_origins: Optional[list[str]] = None,
) -> FastAPI:
    """Create and configure a FastAPI application.

    Args:
        routers: Optional list of APIRouter instances to mount.
                 Defaults to empty list.
        mosaic_client: Optional MosaicClient instance for the application.
        title: API title for OpenAPI documentation.
        version: API version for OpenAPI documentation. Defaults to the
            installed Mosaic package version.
        description: API description for OpenAPI documentation.
        docs_url: URL path for Swagger UI documentation.
        redoc_url: URL path for ReDoc documentation.
        openapi_url: URL path for OpenAPI schema.
        cors_allow_origins: Origins allowed to make cross-origin requests
            (REST, GraphQL, and MCP all share this app, so this covers all
            three transports). Off by default — an empty/``None`` list adds
            no CORS middleware at all, so a deployment stays exactly as
            locked-down as it is today unless an operator opts in with an
            explicit origin list (issue #207; no ``["*"]`` default since
            write mutations live on this same surface).

    Returns:
        Configured FastAPI application instance.
    """
    if version is None:
        from mosaic import __version__

        version = __version__

    app = FastAPI(
        title=title,
        version=version,
        description=description,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    if mosaic_client:
        app.state.mosaic_client = mosaic_client

    app.add_exception_handler(
        RequestValidationError,
        lambda request, exc: JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="Validation Error",
                detail=str(exc),
            ).model_dump(),
        ),
    )

    app.add_exception_handler(
        ValidationError,
        lambda request, exc: JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="Validation Error",
                detail=str(exc),
            ).model_dump(),
        ),
    )

    app.add_exception_handler(
        EntityNotFoundError,
        lambda request, exc: JSONResponse(
            status_code=404,
            content=ErrorResponse(
                error="Entity Not Found",
                detail=exc.message,
            ).model_dump(),
        ),
    )

    app.add_exception_handler(
        MosaicValidationError,
        lambda request, exc: JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="Validation Error",
                detail=exc.message,
            ).model_dump(),
        ),
    )

    # sec4 §4.5: an unparseable updated_since watermark surfaces as a 400
    # Temporal Query Error — now covered by the MosaicError→status table
    # above (TemporalQueryError, 400), landed via #64. No separate handler
    # needed here.

    # sec9 §9.9: ValidationFailed carries the full tier-tagged envelope.
    # REST response body includes `passed`, `failures[].tier`,
    # `failures[].rule`, `failures[].field`, `failures[].message`,
    # `failures[].details`. 400 for request-shape issues surfaced
    # through this path; 422 for semantic (CEL / Python) failures.
    def _validation_failed_handler(request: Request, exc: ValidationFailed):
        envelope: dict[str, Any] = {
            "error": "Validation Failed",
            "detail": exc.message,
            "passed": False,
            "failures": [],
        }
        result = getattr(exc, "result", None)
        if result is not None and hasattr(result, "to_envelope"):
            envelope.update(result.to_envelope())
        # 422 for semantic validation failures (sec9 §9.9 boundary rules).
        return JSONResponse(status_code=422, content=envelope)

    app.add_exception_handler(ValidationFailed, _validation_failed_handler)

    # Map the remaining SDK exception hierarchy to meaningful HTTP statuses
    # so clients can distinguish causes (sec4 §4.3). Without these, every
    # un-handled SDK error collapses to the anonymous generic 500.
    def _make_mosaic_handler(status_code: int, title: str):
        async def _handler(request: Request, exc: MosaicError) -> JSONResponse:
            if status_code >= 500:
                logger.exception("%s: %s", title, exc)
            return JSONResponse(
                status_code=status_code,
                content=ErrorResponse(
                    error=title,
                    detail=exc.message,
                ).model_dump(),
            )

        return _handler

    for exc_class, status_code, title in _MOSAIC_EXCEPTION_STATUS:
        app.add_exception_handler(
            exc_class, _make_mosaic_handler(status_code, title)
        )

    # Fallback for any other MosaicError subclass (recipe / migration /
    # cache-integrity / orchestration errors, ...): a named 500 carrying the
    # SDK message instead of the anonymous generic handler. Sits beneath the
    # specific handlers (most-specific MRO match wins) but above the bare
    # Exception handler, so non-Mosaic errors still get the opaque 500.
    async def _mosaic_error_fallback(request: Request, exc: MosaicError):
        logger.exception("Unhandled Mosaic error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error=type(exc).__name__,
                detail=exc.message,
            ).model_dump(),
        )

    app.add_exception_handler(MosaicError, _mosaic_error_fallback)

    async def generic_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="Internal Server Error",
                detail="An unexpected error occurred",
            ).model_dump(),
        )

    app.add_exception_handler(Exception, generic_exception_handler)

    app.add_middleware(PassThroughAuthMiddleware)

    if cors_allow_origins:
        # Added after PassThroughAuthMiddleware: Starlette's add_middleware
        # prepends, so the most-recently-added middleware ends up outermost.
        # CORS must sit outside auth so a preflight OPTIONS request gets its
        # access-control-allow-* headers before PassThroughAuthMiddleware
        # gets a chance to 401 it.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_allow_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["content-type", ACTOR_HEADER],
        )

    if routers:
        for router in routers:
            app.include_router(router)

    # When the app is built around a schema-bearing client (as `mosaic serve`
    # does via the core factory), enrich the generated OpenAPI document with
    # LinkML-derived per-entity-type components (issue #46). Apps without a
    # client/registry keep the default document.
    registry = getattr(mosaic_client, "registry", None) if mosaic_client else None
    if registry is not None:
        from mosaic.api.openapi import install_typed_openapi

        install_typed_openapi(app, registry)

    return app
