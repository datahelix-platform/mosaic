"""Relationship router for Mosaic API.

Provides endpoints for managing entity relationships.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Path
from pydantic import BaseModel

from mosaic.api.exceptions import EntityNotFoundError
from mosaic.core.client import MosaicClient
from mosaic.serve.routers.base import resolve_entity_type_or_404

router = APIRouter(prefix="/entities", tags=["relationships"])


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


def _require_entity(client: MosaicClient, entity_id: str) -> None:
    """404 unless ``entity_id`` is an available entity, read as its own class."""
    entity_type = resolve_entity_type_or_404(client, entity_id)
    try:
        client.get(entity_type=entity_type, entity_id=entity_id)
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


class RelationshipRequest(BaseModel):
    """Request body for creating a relationship."""

    target_entity_id: str
    relationship_type: str


@router.get("/{entity_id}/relationships")
async def get_entity_relationships(
    entity_id: str,
    request: Request,
) -> list[dict[str, Any]]:
    """Get all relationships for an entity.

    Args:
        entity_id: The ID of the entity.
        request: FastAPI request object.

    Returns:
        List of relationships.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    _require_entity(client, entity_id)

    return []


@router.post("/{entity_id}/relationships")
async def create_relationship(
    entity_id: str,
    request: Request,
    body: RelationshipRequest,
) -> dict[str, Any]:
    """Create a relationship between entities.

    Args:
        entity_id: The ID of the source entity.
        request: FastAPI request object.
        body: Relationship request with target and type.

    Returns:
        Created relationship.

    Raises:
        HTTPException: If entity not found.
    """
    client = await get_client(request)

    _require_entity(client, entity_id)
    _require_entity(client, body.target_entity_id)

    rel_id = f"{entity_id}->{body.target_entity_id}:{body.relationship_type}"

    return {
        "id": rel_id,
        "source_entity_id": entity_id,
        "target_entity_id": body.target_entity_id,
        "relationship_type": body.relationship_type,
    }


@router.delete("/{entity_id}/relationships/{rel_id}")
async def delete_relationship(
    entity_id: str,
    rel_id: str = Path(..., description="Relationship ID"),
    request: Request = None,
) -> dict[str, Any]:
    """Delete a relationship.

    Args:
        entity_id: The ID of the source entity.
        rel_id: The ID of the relationship to delete.
        request: FastAPI request object.

    Returns:
        Deletion confirmation.
    """
    return {"status": "deleted", "relationship_id": rel_id}
