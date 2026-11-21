"""Ingest router for Mosaic API.

Provides endpoints for creating entities.
"""

import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ValidationError

from mosaic.api.exceptions import EntityNotFoundError
from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import (
    EntityNotFoundError as CoreEntityNotFoundError,
    ValidationError as MosaicValidationError,
    ValidationFailure,
)
from mosaic.core.validation.validators import WriteOperation

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _operations_from_body(entities: Any) -> list[WriteOperation]:
    """Build ``WriteOperation``s from a request's ``entities`` array.

    Raises ``HTTPException`` (422) on a malformed item — each entity needs
    ``entity_type`` and ``data``; ``operation`` defaults to ``insert``.
    """
    if not isinstance(entities, list) or not entities:
        raise HTTPException(
            status_code=422, detail="Missing required field: entities (non-empty list)"
        )
    ops: list[WriteOperation] = []
    for i, item in enumerate(entities):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=422, detail=f"entities[{i}] must be an object"
            )
        entity_type = item.get("entity_type")
        data = item.get("data")
        if not entity_type:
            raise HTTPException(
                status_code=422, detail=f"entities[{i}]: missing entity_type"
            )
        if not data:
            raise HTTPException(status_code=422, detail=f"entities[{i}]: missing data")
        ops.append(
            WriteOperation(
                operation=item.get("operation", "insert"),
                entity_type=entity_type,
                data=data,
            )
        )
    return ops


async def get_client(request: Request) -> MosaicClient:
    """Get the MosaicClient from request state."""
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return MosaicClient()


class IngestRequest(BaseModel):
    """Request body for entity ingestion."""

    entity_type: str
    data: dict[str, Any]


@router.post("")
async def ingest_entity(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Create a new entity.

    Args:
        request: FastAPI request object.
        body: Entity data containing entity_type and data fields.

    Returns:
        Created entity with generated ID and timestamps.

    Raises:
        HTTPException: If validation fails or entity type is missing.
    """
    if not body:
        raise HTTPException(status_code=400, detail="Invalid JSON format")

    entity_type = body.get("entity_type")
    data = body.get("data", {})

    if not entity_type:
        raise HTTPException(
            status_code=422, detail="Missing required field: entity_type"
        )

    if not data:
        raise HTTPException(status_code=422, detail="Missing required field: data")

    client = await get_client(request)

    try:
        result = client.create(entity_type=entity_type, data=data)
        return result
    except ValidationFailure as e:
        raise HTTPException(status_code=422, detail=str(e))
    except MosaicValidationError as e:
        # Adapter-level integrity violations (e.g. XrefUniquenessError on
        # a mosaic_external_xref slot) follow the standard validation
        # error path — same 422 shape as schema validation failures.
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON format: {str(e)}")


@router.post("/validate")
async def validate_batch_endpoint(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Whole-set dry-run validation (no writes) — issue #84 increment 1.

    Body: ``{"entities": [{"entity_type", "data", "operation"?}, ...]}``.
    Always returns 200 with the sec9 §9.9 batch envelope
    (``{"passed", "results": [{"entity_id", "passed", "failures"}]}``);
    inspect ``passed`` rather than the HTTP status — this endpoint reports,
    it does not write.
    """
    ops = _operations_from_body(body.get("entities"))
    client = await get_client(request)
    return client.validate_batch(ops).to_envelope()


@router.post("/batch")
async def ingest_batch(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Atomic multi-entity write (batch unit-of-work) — issue #84 increment 2.

    Body: ``{"entities": [...], "relationships"?: [...], "dry_run"?: bool}``.
    The whole set is validated, then committed all-or-nothing. Returns the
    batch result (``committed``/``dry_run``/``validation``/``entities``/
    ``relationships``). A validation failure returns **422** with the same
    body shape (nothing was written); a dry run returns **200** with the plan.
    """
    ops = _operations_from_body(body.get("entities"))
    relationships = body.get("relationships") or None
    dry_run = bool(body.get("dry_run", False))
    client = await get_client(request)

    try:
        result = client.batch_put(ops, relationships=relationships, dry_run=dry_run)
    except (ValidationFailure, MosaicValidationError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except (CoreEntityNotFoundError, EntityNotFoundError) as e:
        # e.g. a relationship references an entity neither in the batch nor
        # already persisted; the whole set was rolled back.
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    payload = {
        "committed": result.committed,
        "dry_run": result.dry_run,
        "validation": result.validation.to_envelope(),
        "entities": result.entities,
        "relationships": result.relationships,
    }
    # Validation failure (not a dry run) → nothing written; surface as 422
    # carrying the same structured body so callers get the per-entity failures.
    if not result.committed and not result.dry_run:
        raise HTTPException(status_code=422, detail=payload)
    return payload
