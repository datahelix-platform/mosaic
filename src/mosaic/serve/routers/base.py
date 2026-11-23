"""Base router helpers for Mosaic API.

Mosaic holds zero authn/authz (issue #54 Part A) — ``PassThroughAuthMiddleware``
(``mosaic.core.middleware``) extracts an actor for provenance only. The bearer-
token gate this module used to provide (``require_auth``/``create_base_router``)
validated nothing and is removed.
"""

import logging
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def get_client_from_request(request: Request) -> Any:
    """Get the MosaicClient from request state.

    Args:
        request: FastAPI request object.

    Returns:
        MosaicClient instance.
    """
    if hasattr(request.app.state, "mosaic_client"):
        return request.app.state.mosaic_client
    return None


def resolve_entity_type_or_404(client: Any, entity_id: str) -> str:
    """The stored class of ``entity_id``, or a 404 when the id is unknown.

    For routes addressed by id alone (``/entities/{id}/...``): they call the
    SDK with the row's real class, as ``DELETE /entities/{id}`` does. Since
    #289 a kind-specific SDK call refuses a class the row isn't, so a
    placeholder type such as ``"entity"`` would be rejected.
    """
    entity_type = client.resolve_type(entity_id)
    if entity_type is None:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")
    return entity_type
