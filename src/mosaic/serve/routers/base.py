"""Base router helpers for Mosaic API.

Mosaic holds zero authn/authz (issue #54 Part A) — ``PassThroughAuthMiddleware``
(``mosaic.core.middleware``) extracts an actor for provenance only. The bearer-
token gate this module used to provide (``require_auth``/``create_base_router``)
validated nothing and is removed.
"""

import logging
from typing import Any

from fastapi import Request

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
