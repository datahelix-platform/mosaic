"""Search router for Mosaic API.

Provides endpoints for full-text search of entities, composed with the
list surface (issue #157): search takes the same field-filter query
params as ``GET /entities`` and returns the same paginated envelope.
"""

from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from mosaic.core.client import MosaicClient
from mosaic.serve.routers.entity import _parse_field_filters

router = APIRouter(prefix="/search", tags=["search"])

#: Query params consumed by ``search_entities`` itself — everything else
#: on the request is treated as an entity-field filter (the same sec4
#: §4.3 convention as the list endpoint).
_RESERVED_SEARCH_PARAMS = {
    "entity_type",
    "q",
    "limit",
    "offset",
    "filter_mode",
    "order_by",
    "order_dir",
}


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


@router.get("")
async def search_entities(
    request: Request,
    entity_type: str = Query(..., description="Entity type to search"),
    q: str = Query(..., description="Search query"),
    limit: int = Query(100, ge=0, le=1000, description="Maximum results (0 = none)"),
    offset: int = Query(0, ge=0, description="Results to skip"),
    filter_mode: str = Query("and", description="Filter composition: 'and' or 'or'"),
    order_by: Optional[str] = Query(
        None,
        description=(
            "Stored slot to order by (ADR-0007); omitted, results come "
            "back in FTS rank order"
        ),
    ),
    order_dir: str = Query("asc", description="Sort direction: 'asc' or 'desc'"),
) -> dict[str, Any]:
    """Full-text search composed with the list surface (issue #157).

    Any query parameter that is not reserved is treated as an
    entity-field filter, exactly like ``GET /entities`` (a repeated field
    is a same-field OR). Returns the same paginated envelope as the list
    endpoint; ``total`` honors both the FTS match set and the composed
    filters. Results come back in FTS rank order unless ``order_by`` is
    given (rank-precedence rule).
    """
    client = await get_client(request)

    result = client.search(
        entity_type=entity_type,
        query=q,
        limit=limit,
        offset=offset,
        filters=_parse_field_filters(request, _RESERVED_SEARCH_PARAMS),
        filter_mode=filter_mode,
        order_by=order_by,
        order_dir=order_dir,
    )

    return {
        "items": result.items,
        "total": result.total,
        "limit": result.limit,
        "offset": result.offset,
    }
