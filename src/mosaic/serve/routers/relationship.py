"""Relationship router for Mosaic API.

The ``/entities/{id}/relationships`` routes were stubs that stored and read
nothing; they answer 501 Not Implemented until they are implemented through
the SDK or removed (mosaic#291).
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel

router = APIRouter(prefix="/entities", tags=["relationships"])


_NOT_IMPLEMENTED = (
    "REST relationship routes are not implemented (mosaic#291). Use the SDK's "
    "MosaicClient.relationships (relate, unrelate, find_relationships), or "
    "write reference slots with PUT /entities/{entity_type}/{entity_id}, "
    "GraphQL update<Type> or MosaicClient.update, and read them with "
    "MosaicClient.get or GraphQL reference fields."
)


def _not_implemented() -> HTTPException:
    """501 for the stub relationship routes (#290 review, #291).

    They stored and read nothing: answering 200 would be a silent success,
    the class of bug #289 fixes.
    """
    return HTTPException(status_code=501, detail=_NOT_IMPLEMENTED)


class RelationshipRequest(BaseModel):
    """Request body for creating a relationship."""

    target_entity_id: str
    relationship_type: str


@router.get("/{entity_id}/relationships", status_code=501)
async def get_entity_relationships(entity_id: str) -> dict[str, Any]:
    """Not implemented: 501 (mosaic#291). See ``_NOT_IMPLEMENTED``."""
    raise _not_implemented()


@router.post("/{entity_id}/relationships", status_code=501)
async def create_relationship(
    entity_id: str, body: RelationshipRequest
) -> dict[str, Any]:
    """Not implemented: 501 (mosaic#291). See ``_NOT_IMPLEMENTED``."""
    raise _not_implemented()


@router.delete("/{entity_id}/relationships/{rel_id}", status_code=501)
async def delete_relationship(
    entity_id: str,
    rel_id: str = Path(..., description="Relationship ID"),
) -> dict[str, Any]:
    """Not implemented: 501 (mosaic#291). See ``_NOT_IMPLEMENTED``."""
    raise _not_implemented()
