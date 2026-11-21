"""Health check and system status routers for Mosaic API."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from mosaic import __version__
from mosaic.core.client import MosaicClient

router = APIRouter(tags=["health"])


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state.

    Args:
        request: FastAPI request object.

    Returns:
        MosaicClient instance.
    """
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


@router.get("/health")
async def health_check() -> dict:
    """Health check endpoint.

    Unauthenticated liveness probe — reports nothing about stored data.

    Returns:
        Health status information.
    """
    return {"status": "healthy", "service": "mosaic"}


@router.get("/status")
async def system_status(
    request: Request,
) -> dict[str, Any]:
    """System status endpoint (sec4 system endpoints).

    Reports the Mosaic version, active storage adapter, schema version,
    declared entity types, per-type entity counts, and adapter
    capability declarations. Authenticated — unlike ``/health``, the
    response describes the deployment's data.

    Args:
        request: FastAPI request object.

    Returns:
        Status summary from :meth:`MosaicClient.status`.
    """
    client = await get_client(request)
    return client.status()


@router.get("/")
async def root() -> dict:
    """Root endpoint.

    Returns:
        API information.
    """
    return {
        "service": "Mosaic API",
        "version": __version__,
        "docs": "/docs",
    }
