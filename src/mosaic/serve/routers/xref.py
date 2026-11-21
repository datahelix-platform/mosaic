"""External-reference (xref) reverse-lookup router (issue #48).

Serves the generic reverse lookup over ``mosaic_external_xref``-annotated
``ExternalReference`` slots: ``GET /xref/{system}/{value}`` resolves a
``(system, value)`` pair to the single available entity that holds it.

An entity's external references themselves are ordinary slot data — they
travel inside the entity payload on the standard entity endpoints, so no
separate per-entity xref endpoint exists. This router replaces the
deprecated ``/external-ids`` endpoints backed by the ExternalID entity.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request

from mosaic.core.client import MosaicClient

router = APIRouter(tags=["external-references"])


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


@router.get(
    "/xref/{system}/{value}",
    summary="Resolve an external reference to its entity",
    description=(
        "Reverse lookup over `mosaic_external_xref`-annotated "
        "ExternalReference slots: returns the full envelope of the single "
        "AVAILABLE entity whose annotated slot carries the `(system, "
        "value)` pair. The pair is globally unique among available "
        "entities, so at most one entity can match. Replaces the "
        "deprecated `/external-ids/{id_type}/{external_id}` endpoint."
    ),
)
async def find_by_xref(
    system: str = Path(..., description="External system name (e.g. STARLIMS)"),
    value: str = Path(..., description="Identifier value in that system"),
    request: Request = None,
) -> dict[str, Any]:
    """Resolve ``(system, value)`` to the entity envelope holding it.

    Returns:
        The entity envelope (same shape as ``GET /entities/{id}``).

    Raises:
        HTTPException: 404 when no available entity holds the pair; 501
            when the storage adapter does not implement the xref index
            (PostgreSQL).
    """
    client = await get_client(request)

    try:
        envelope = client.find_by_xref(system, value)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    if envelope is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No available entity holds external reference "
                f"(system={system!r}, value={value!r})"
            ),
        )
    return envelope
