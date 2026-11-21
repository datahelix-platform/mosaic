"""Availability router for Mosaic API.

Provides endpoints for managing entity availability.
"""

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from mosaic.api.exceptions import EntityNotFoundError
from mosaic.core.client import MosaicClient

router = APIRouter(prefix="/entities", tags=["availability"])


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


class AvailabilityRequest(BaseModel):
    """Request body for availability change."""

    is_available: bool


class BulkAvailabilityRequest(BaseModel):
    """Request body for bulk availability change."""

    entity_ids: list[str]
    is_available: bool
    reason: Optional[str] = None


@router.get("/{entity_id}/availability")
async def get_entity_availability(
    entity_id: str,
    request: Request,
) -> dict[str, Any]:
    """Get the availability status of an entity.

    Args:
        entity_id: The ID of the entity.
        request: FastAPI request object.

    Returns:
        Availability information.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    try:
        entity = client.get(entity_type="entity", entity_id=entity_id)
        return {
            "entity_id": entity_id,
            "is_available": True,
        }
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entity_id}/availability")
async def set_entity_availability(
    entity_id: str,
    request: Request,
    body: AvailabilityRequest,
) -> dict[str, Any]:
    """Set the availability status of an entity.

    Args:
        entity_id: The ID of the entity.
        request: FastAPI request object.
        body: Availability request with is_available flag.

    Returns:
        Updated availability information.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    try:
        entity = client.get(entity_type="entity", entity_id=entity_id)

        if not body.is_available:
            client.delete(entity_type="entity", entity_id=entity_id)

        return {
            "entity_id": entity_id,
            "is_available": body.is_available,
        }
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entity_type}/bulk-availability")
async def bulk_availability(
    entity_type: str,
    request: Request,
    body: BulkAvailabilityRequest,
) -> dict[str, Any]:
    """Change availability status for multiple entities at once.

    Args:
        entity_type: The entity type.
        request: FastAPI request object.
        body: Bulk availability request with entity IDs and target status.

    Returns:
        Summary of successes and failures. Returns 207 on partial failure.
    """
    from starlette.responses import JSONResponse

    client = await get_client(request)

    result = client.set_availability_bulk(
        entity_type=entity_type,
        entity_ids=body.entity_ids,
        is_available=body.is_available,
        reason=body.reason,
    )

    status_code = 200 if result["failed"] == 0 else 207
    return JSONResponse(content=result, status_code=status_code)
