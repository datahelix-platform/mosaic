"""ASGI mounting for the MCP transport.

Produces the Streamable HTTP ASGI app for :func:`mosaic.mcp.create_mcp_app`,
mounted onto the existing ``mosaic serve`` app at ``/mcp`` (see
``mosaic.serve.create_default_app`` and the ``mosaic serve --mcp`` flag).

Mosaic holds zero authn/authz (issue #54 Part A) — same posture as REST
and GraphQL, confirmed acceptable for this surface in #185.
"""

from __future__ import annotations

import os
from typing import Any

from mosaic.core.client import MosaicClient
from mosaic.mcp.server import create_mcp_server

#: Host header values the MCP transport will accept, beyond the loopback
#: defaults. Comma-separated; ``*`` disables the check entirely.
ALLOWED_HOSTS_ENV = "MOSAIC_MCP_ALLOWED_HOSTS"


def create_mcp_app(mosaic_client: MosaicClient) -> tuple[Any, Any]:
    """Build ``(mcp_server, asgi_app)`` for one Mosaic deployment.

    ``stateless_http``/``json_response`` are both True: the resources
    exposed so far (#182) carry no per-session state, so the simpler
    request/response shape is preferable to standing up SSE streaming
    machinery nothing here needs yet. A future stateful tool (if one ever
    needs session-scoped state) would revisit this.
    """
    mcp = create_mcp_server(mosaic_client)
    asgi_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(),
    )
    return mcp, asgi_app


def _transport_security():
    """Which ``Host`` headers the MCP transport accepts.

    The SDK enables DNS-rebinding protection by default and, given no
    settings, derives its allow-list from a ``host`` parameter that defaults
    to ``127.0.0.1``. Passing neither — as this module did until #211 — means
    the transport answers only requests whose ``Host`` is loopback, and
    returns ``421 Invalid Host header`` to everything else.

    That is invisible while every caller is a host process on the same
    machine, because they all address it as ``localhost``/``127.0.0.1``. It
    becomes a hard wall the moment the boundary is containerized: a sibling
    container reaching ``http://mosaic:8001/mcp`` sends ``Host: mosaic:8001``,
    and a client on the Docker host sends ``Host: host.docker.internal:…`` —
    both rejected, with an error that says nothing about the Host header
    unless you read the response body.

    The protection is worth keeping (it is what stops a malicious page in a
    browser from driving a localhost MCP server via DNS rebinding), so this
    does not disable it by default. It makes the allow-list configurable, with
    loopback still permitted so nothing about the single-machine case changes.

    ``MOSAIC_MCP_ALLOWED_HOSTS=*`` turns the check off, for a deployment that
    has decided its own network boundary is the control — the same judgement
    ``--cors-origin '*'`` already offers on the HTTP surface.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    configured = [h.strip() for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if h.strip()]
    if "*" in configured:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    # Loopback in both spellings, with and without a port, so the default
    # behaviour is exactly what it was before this became configurable.
    allowed = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", *configured]
    return TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=["*"])
