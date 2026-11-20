"""History router for Mosaic API.

Provides endpoints for querying entity history/provenance.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from mosaic.api.exceptions import EntityNotFoundError
from mosaic.core.client import MosaicClient

router = APIRouter(prefix="/entities", tags=["history"])


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


@router.get("/{entity_id}/history")
async def get_entity_history(
    entity_id: str,
    request: Request,
) -> list[dict[str, Any]]:
    """Get the change history for an entity.

    Args:
        entity_id: The ID of the entity.
        request: FastAPI request object.

    Returns:
        List of history records.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    try:
        history = client.history(entity_id)
        return history
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
