"""GA4GH DRS v1 router for Mosaic API.

Read-only implementation of the GA4GH Data Repository Service (DRS) v1 spec.
Resolves Mosaic entity UUIDs to DRS objects using the entity's `uri` field.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/ga4gh/drs/v1", tags=["drs"])


async def get_client(request: Request):
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return None


def _scheme_from_uri(uri: str) -> str:
    """Derive a DRS access method type from a URI scheme."""
    if "://" in uri:
        return uri.split("://")[0]
    return "https"


@router.get("/objects/{object_id}")
async def get_drs_object(
    object_id: str,
    request: Request,
) -> dict[str, Any]:
    """Resolve a Mosaic entity UUID to a GA4GH DRS v1 object.

    Args:
        object_id: Mosaic entity UUID.
        request: FastAPI request object.

    Returns:
        GA4GH DRS v1 object representation.

    Raises:
        HTTPException 404: If entity not found, unavailable, or has no `uri` field.
    """
    client = await get_client(request)

    if client is None or client.storage is None:
        raise HTTPException(status_code=404, detail="Object not found")

    raw = client.storage.read(object_id)
    if raw is None or not raw.is_available:
        raise HTTPException(status_code=404, detail="Object not found")

    uri = raw.data.get("uri") if isinstance(raw.data, dict) else None
    if not uri:
        raise HTTPException(status_code=404, detail="Object has no URI")

    # Use client.get() for provenance-derived timestamps.
    try:
        entity = client.get(entity_type=raw.entity_type, entity_id=object_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Object not found")

    scheme = _scheme_from_uri(uri)

    return {
        "id": object_id,
        "name": f"{raw.entity_type}/{object_id}",
        "self_uri": f"drs://localhost/{object_id}",
        "size": None,
        "created_time": entity.get("created_at"),
        "updated_time": entity.get("updated_at"),
        "checksums": [],
        "access_methods": [
            {
                "type": scheme,
                "access_url": {"url": uri},
            }
        ],
    }
