"""Schema router for the Mosaic REST API.

Exposes the loaded LinkML schema (via :class:`SchemaRegistry`) as JSON.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request

router = APIRouter(prefix="/schemas", tags=["schemas"])


async def get_client(request: Request):
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return None


def _describe_class(registry, class_name: str) -> dict[str, Any]:
    cls = registry.get_class(class_name)
    return {
        "name": class_name,
        "description": cls.description if cls is not None else None,
        "abstract": bool(cls.abstract) if cls is not None else False,
        "is_a": cls.is_a if cls is not None else None,
        "fields": [
            {
                "name": slot.name,
                "range": slot.range,
                "required": bool(slot.required),
                "identifier": bool(slot.identifier),
                "multivalued": bool(slot.multivalued),
            }
            for slot in registry.induced_slots(class_name)
        ],
    }


@router.get("")
async def list_schemas(
    request: Request,
) -> list[dict[str, Any]]:
    client = await get_client(request)
    if client is None or client._registry is None:
        return []
    registry = client._registry
    return [_describe_class(registry, name) for name in registry.class_names()]


@router.get("/{schema_name}")
async def get_schema(
    schema_name: str = Path(..., description="Class name"),
    request: Request = None,
) -> dict[str, Any]:
    client = await get_client(request)
    if client is None or client._registry is None or not client._registry.has_class(
        schema_name
    ):
        raise HTTPException(
            status_code=404, detail=f"Schema not found: {schema_name}"
        )
    return _describe_class(client._registry, schema_name)


@router.get("/{entity_type}/references")
async def get_schema_references(
    entity_type: str = Path(..., description="Entity type name"),
    request: Request = None,
) -> dict[str, Any]:
    client = await get_client(request)
    if client is None or client._registry is None or not client._registry.has_class(
        entity_type
    ):
        raise HTTPException(
            status_code=404, detail=f"Schema not found: {entity_type}"
        )
    return {
        "entity_type": entity_type,
        "references": client.schema_references(entity_type),
    }
