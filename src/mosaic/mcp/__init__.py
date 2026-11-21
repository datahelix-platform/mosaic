"""Mosaic MCP (Model Context Protocol) transport layer (optional ``mcp``
extra, ADR-0009 / issue #182).

A fourth transport alongside REST/GraphQL/CLI, giving any LLM-driven
client one shared, validated way to query Mosaic without hand-maintaining
a capability copy (ADR-0009's Context — the three-way capability-manifest
duplication between Mosaic, Exon, and Aperture this boundary exists to
close). Like the GraphQL layer, this is a THIN wrapper: resources reshape
the shared type/capability model (:mod:`mosaic.core.schema_typing`); no
business logic lives here.

The heavy lifting depends on the ``mcp`` package, which ships in the
optional ``mcp`` extra. Public entry points below import lazily and fail
with an actionable message when the extra is not installed.

**Transport**: Streamable HTTP, mounted onto the same FastAPI app REST/
GraphQL already run in (``mosaic serve --mcp``), not stdio. ADR-0009's
Notes section hedges stdio as "the likely default," but that reading is
architecturally incompatible with the Decision text's actual requirement
— "sharing the same MosaicClient/SchemaRegistry every other transport
already uses" — since stdio runs as a wholly separate client-invoked
process with no access to a running FastAPI app's state at all. Streamable
HTTP is the only transport that can share app state, so it is what is
implemented here; the stdio hedge is non-binding per the ADR's own text.

Public API:

- :func:`create_mcp_app` — ``MosaicClient`` → ``(MCPServer, ASGI app)``
- :func:`mcp_available` — feature probe for callers (CLI, serve)
- :data:`MCP_EXTRA_HINT` — the actionable install-hint message
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mosaic.core.client import MosaicClient

MCP_EXTRA_HINT = (
    "The MCP transport requires the optional 'mcp' extra. "
    "Install it with: pip install 'datahelix-mosaic[mcp]'"
)

__all__ = [
    "MCP_EXTRA_HINT",
    "create_mcp_app",
    "mcp_available",
]


def mcp_available() -> bool:
    """Return True when the ``mcp`` extra (the MCP Python SDK) is importable."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def _require_mcp() -> None:
    """Raise a clear ImportError when the ``mcp`` package is missing."""
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise ImportError(MCP_EXTRA_HINT) from exc


def create_mcp_app(mosaic_client: "MosaicClient") -> tuple[Any, Any]:
    """Build the MCP server and its mountable ASGI app for one deployment.

    Returns ``(mcp_server, asgi_app)``. The caller must mount ``asgi_app``
    (e.g. ``fastapi_app.mount("/mcp", asgi_app)``) AND wire
    ``mcp_server.session_manager.run()`` into the host app's own lifespan
    — Starlette does not run a mounted sub-application's lifespan, so the
    session manager's background task group never starts otherwise (see
    ``mosaic.serve.create_default_app``, which does both).

    Raises:
        ImportError: If the ``mcp`` extra is not installed.
        mosaic.core.exceptions.ConfigError: If the client carries no
            ``SchemaRegistry`` (both resources are schema-derived).
    """
    _require_mcp()
    from mosaic.mcp.router import create_mcp_app as _create

    return _create(mosaic_client)
