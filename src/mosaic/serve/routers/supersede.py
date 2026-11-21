"""Supersede router for Mosaic API.

Provides endpoints for managing entity supersession.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from mosaic.api.exceptions import EntityNotFoundError
from mosaic.core.client import MosaicClient

router = APIRouter(prefix="/entities", tags=["supersede"])


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


class SupersedeRequest(BaseModel):
    """Request body for superseding an entity."""

    old_external_id: str
    new_external_id: str
    source_system: str = "default"


@router.post("/{entity_id}/supersede")
async def supersede_entity(
    entity_id: str,
    request: Request,
    body: SupersedeRequest,
) -> dict[str, Any]:
    """Supersede an entity's external ID with a new one.

    Args:
        entity_id: The ID of the entity.
        request: FastAPI request object.
        body: Supersession request with old and new external IDs.

    Returns:
        New external ID record.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    try:
        result = client.supersede(
            entity_id=entity_id,
            old_external_id=body.old_external_id,
            new_external_id=body.new_external_id,
            source_system=body.source_system,
        )
        return result
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{entity_id}/superseded")
async def get_superseded_entities(
    entity_id: str,
    request: Request,
) -> list[dict[str, Any]]:
    """Get superseded external IDs for an entity.

    Args:
        entity_id: The ID of the entity.
        request: FastAPI request object.

    Returns:
        List of superseded external IDs.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    try:
        external_ids = client.list_external_ids(
            entity_id=entity_id,
            include_superseded=True,
        )
        superseded = [eid for eid in external_ids if eid.get("superseded_at")]
        return superseded
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
