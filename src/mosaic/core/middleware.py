"""Authentication middleware for Mosaic."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mosaic.core.context import current_actor


ACTOR_HEADER = "x-datahelix-actor"
ACTOR_PREFIX = "actor:"


@dataclass
class RequestContext:
    """Request context for actor identification."""

    actor_id: str = ""
    raw_header: Optional[str] = None


class AuthMiddleware(ABC):
    """Abstract base class for authentication middleware.

    Subclasses must implement the get_request_context method to provide
    custom authentication logic.
    """

    @abstractmethod
    def get_request_context(self, request: Request) -> RequestContext:
        """Extract authentication context from the request.

        Args:
            request: The incoming HTTP request.

        Returns:
            RequestContext with actor identification.
        """
        pass


class PassThroughAuthMiddleware:
    """Pass-through authentication middleware that extracts actor from X-DataHelix-Actor header.

    Header format: "actor:<identifier>"

    Behaviors:
    - Valid format: extracts identifier into request context
    - No header: continues with empty actor identifier
    - Invalid format (not starting with "actor:"): returns 401
    - Empty identifier after "actor:": returns 401
    - Multiple headers: uses first value
    """

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        raw_header = request.headers.get(ACTOR_HEADER)

        if raw_header is None:
            context = RequestContext(actor_id="", raw_header=None)
        elif not raw_header.startswith(ACTOR_PREFIX):
            response = JSONResponse(
                status_code=401,
                content={"error": "Invalid X-DataHelix-Actor header format"},
            )
            await response(scope, receive, send)
            return
        else:
            actor_id = raw_header[len(ACTOR_PREFIX) :]
            if not actor_id or not actor_id.strip():
                response = JSONResponse(
                    status_code=401,
                    content={"error": "Empty actor identifier in X-DataHelix-Actor header"},
                )
                await response(scope, receive, send)
                return
            context = RequestContext(actor_id=actor_id, raw_header=raw_header)

        request.state.hippo_context = context

        # Propagate actor into the ContextVar so provenance writes in
        # downstream SDK calls pick it up without explicit threading.
        # Use context.actor_id so we're always referencing the resolved value.
        token = current_actor.set(context.actor_id if context.actor_id else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_actor.reset(token)


def create_auth_middleware() -> type[PassThroughAuthMiddleware]:
    """Create the default authentication middleware class.

    Returns:
        PassThroughAuthMiddleware class for use with app.add_middleware().
    """
    return PassThroughAuthMiddleware
