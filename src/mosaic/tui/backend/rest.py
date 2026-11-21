"""TUI REST backend — ``httpx.AsyncClient`` adapter for a remote ``mosaic serve``.

Targets the real REST surface mounted by ``mosaic.serve.create_default_app``:

- ``GET /health`` — connection probe (unauthenticated)
- ``GET /schemas`` — LinkML class descriptions (name, abstract, fields)
- ``GET /entities?entity_type=&limit=&offset=`` — paginated listing
- ``GET /entities/{id}`` — single entity (system + temporal fields + data)
- ``GET /entities/{id}/history`` — provenance history (oldest first)
- ``GET /search?entity_type=&q=&limit=`` — FTS search
- ``POST /ingest`` — create entity
- ``PUT /entities/{entity_type}/{id}`` — full replace
- ``POST /entities/{entity_type}/bulk-availability`` — availability transitions

Read methods degrade gracefully (empty results + ``status_callback``);
write methods raise :class:`BackendError` with the server's error detail.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from mosaic.tui.backend.protocol import (
    PAGE_SIZE,
    CORE_INFRA_CLASSES,
    BackendCapabilities,
    BackendError,
    ConnectionInfo,
    EntityDetail,
    EntityTypeSchema,
    EntityTypeSummary,
    FieldInfo,
    PagedResult,
    ProvenanceEvent,
    RelationshipDeclaration,
    SchemaView,
    compute_paging,
    record_to_detail,
)

_DEFAULT_URL = "http://127.0.0.1:8000"
_DEFAULT_TOKEN = "dev-token"

#: How many rows a substring filter will fetch before paginating client-side.
_FILTER_SCAN_LIMIT = 1000


def _resolve_token(token: str | None) -> str:
    """Resolve the auth token.

    Priority: explicit *token* argument > ``MOSAIC_TUI_TOKEN`` env variable
    > ``dev-token``.
    """
    if token is not None:
        return token
    from mosaic.config.env import get_env

    env_token = get_env("TUI_TOKEN")
    if env_token:
        return env_token
    return _DEFAULT_TOKEN


def _error_message(response: Any) -> str:
    """Extract a human-readable message from an error response body."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — non-JSON error body
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error")
        if isinstance(detail, str) and detail:
            return detail
        failures = body.get("failures")
        if isinstance(failures, list) and failures:
            msgs = [str(f.get("message", f)) for f in failures if f]
            return "; ".join(msgs)
    return f"HTTP {response.status_code}"


class RESTBackend:
    """TUIBackend implementation that calls a running ``mosaic serve`` instance.

    Args:
        url: Base URL of the REST API server.
        token: Bearer token. Falls back to the ``MOSAIC_TUI_TOKEN`` env var, then
            ``dev-token``.
        status_callback: Optional ``callable(message: str)`` invoked on
            connection errors so the UI can display an error without crashing.
    """

    def __init__(
        self,
        url: str = _DEFAULT_URL,
        token: str | None = None,
        status_callback: Any = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = _resolve_token(token)
        self._status_callback = status_callback
        self._client: Any = None  # lazy httpx.AsyncClient
        self._schema_view: SchemaView | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Lazy-create and return an httpx.AsyncClient."""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self._url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10.0,
            )
        return self._client

    def _report_error(self, message: str) -> None:
        """Report a connection error via callback (if set) without crashing."""
        if self._status_callback is not None:
            try:
                self._status_callback(message)
            except Exception:  # noqa: BLE001 — callback must never crash reads
                pass

    async def _get_json(self, path: str) -> Any:
        """GET *path* and return parsed JSON.

        Returns ``None`` on failure after reporting the error — read views
        render an empty state rather than crashing.
        """
        import httpx

        try:
            client = self._get_client()
            response = await client.get(path)
            response.raise_for_status()
            return response.json()
        except httpx.ConnectError as exc:
            self._report_error(f"Connection failed: {self._url} — {exc}")
            return None
        except httpx.TimeoutException as exc:
            self._report_error(f"Request timed out: {self._url}{path} — {exc}")
            return None
        except httpx.HTTPStatusError as exc:
            self._report_error(
                f"HTTP {exc.response.status_code} from {self._url}{path}: "
                f"{_error_message(exc.response)}"
            )
            return None
        except Exception as exc:  # noqa: BLE001
            self._report_error(f"Unexpected error: {exc}")
            return None

    async def _send_json(self, method: str, path: str, payload: Any) -> Any:
        """Send a write request; raise :class:`BackendError` on any failure."""
        import httpx

        try:
            client = self._get_client()
            response = await client.request(method, path, json=payload)
        except httpx.ConnectError as exc:
            raise BackendError(f"Connection failed: {self._url}") from exc
        except httpx.TimeoutException as exc:
            raise BackendError(f"Request timed out: {self._url}{path}") from exc
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Request failed: {exc}") from exc

        if response.status_code >= 400:
            raise BackendError(_error_message(response))
        try:
            return response.json()
        except Exception:  # noqa: BLE001 — empty/non-JSON success body
            return None

    async def _cached_schema(self) -> SchemaView:
        if self._schema_view is None:
            self._schema_view = await self._fetch_schema()
        return self._schema_view

    async def _fetch_schema(self) -> SchemaView:
        data = await self._get_json("/schemas")
        if not isinstance(data, list):
            return SchemaView()

        class_names = {
            cls.get("name", "") for cls in data if isinstance(cls, dict)
        }
        entity_types: list[EntityTypeSchema] = []
        relationships: list[RelationshipDeclaration] = []

        for cls in data:
            if not isinstance(cls, dict):
                continue
            name = cls.get("name", "")
            if not name or cls.get("abstract") or name in CORE_INFRA_CLASSES:
                continue
            fields: list[FieldInfo] = []
            for f in cls.get("fields", []):
                rng = f.get("range") or "string"
                ref_target = (
                    rng
                    if rng in class_names and rng not in CORE_INFRA_CLASSES
                    else None
                )
                fields.append(
                    FieldInfo(
                        name=f.get("name", ""),
                        field_type=rng,
                        required=bool(f.get("required", False)),
                        indexed=bool(f.get("indexed", False)),
                        ref_target=ref_target,
                        multivalued=bool(f.get("multivalued", False)),
                        identifier=bool(f.get("identifier", False)),
                    )
                )
                if ref_target:
                    relationships.append(
                        RelationshipDeclaration(
                            source_type=name,
                            relationship_name=f.get("name", ""),
                            target_type=ref_target,
                        )
                    )
            entity_types.append(
                EntityTypeSchema(
                    name=name, fields=fields, description=cls.get("description")
                )
            )

        entity_types.sort(key=lambda et: et.name)
        return SchemaView(entity_types=entity_types, relationships=relationships)

    async def _list_page(
        self, entity_type: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch one page from ``GET /entities``; return ``(items, total)``."""
        path = (
            f"/entities?entity_type={urllib.parse.quote(entity_type)}"
            f"&limit={limit}&offset={offset}"
        )
        data = await self._get_json(path)
        if not isinstance(data, dict):
            return [], 0
        items = data.get("items", [])
        total = int(data.get("total", len(items)))
        return items, total

    @staticmethod
    def _matches_substring(item: dict[str, Any], lowered: str) -> bool:
        import json as _json

        if lowered in str(item.get("id", "")).lower():
            return True
        return lowered in _json.dumps(item.get("data", {}), default=str).lower()

    # ------------------------------------------------------------------
    # TUIBackend protocol implementation
    # ------------------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        # The REST entity listing does not expose field filters; FTS search
        # is available via /search.
        return BackendCapabilities(supports_filters=False, supports_fts=True)

    async def connection_info(self) -> ConnectionInfo:
        data = await self._get_json("/health")
        if isinstance(data, dict) and data.get("status") == "healthy":
            return ConnectionInfo(
                mode="rest", target=self._url, ok=True, detail="healthy"
            )
        return ConnectionInfo(
            mode="rest",
            target=self._url,
            ok=False,
            detail="server unreachable or unhealthy",
        )

    async def list_entity_types(self) -> list[EntityTypeSummary]:
        schema = await self._cached_schema()
        summaries: list[EntityTypeSummary] = []
        for et in schema.entity_types:
            _items, total = await self._list_page(et.name, limit=1, offset=0)
            summaries.append(
                EntityTypeSummary(name=et.name, count=total, description=et.description)
            )
        return summaries

    async def list_entities(
        self,
        entity_type: str,
        page: int = 1,
        filter_text: str = "",
    ) -> PagedResult:
        filter_text = filter_text.strip()
        if filter_text:
            items, _total = await self._list_page(
                entity_type, limit=_FILTER_SCAN_LIMIT, offset=0
            )
            lowered = filter_text.lower()
            matched = [i for i in items if self._matches_substring(i, lowered)]
            total = len(matched)
            page, total_pages = compute_paging(total, page)
            offset = (page - 1) * PAGE_SIZE
            return PagedResult(
                items=matched[offset : offset + PAGE_SIZE],
                page=page,
                total_pages=total_pages,
                total_items=total,
            )

        offset = max(0, (page - 1) * PAGE_SIZE)
        items, total = await self._list_page(entity_type, PAGE_SIZE, offset)
        page, total_pages = compute_paging(total, page)
        return PagedResult(
            items=items, page=page, total_pages=total_pages, total_items=total
        )

    async def query_entities(
        self,
        entity_type: str,
        filters: list[dict[str, Any]] | None = None,
        filter_mode: str = "and",
        page: int = 1,
    ) -> PagedResult:
        if filters:
            raise BackendError(
                "The REST API does not expose field filters; "
                "use full-text search or SDK mode."
            )
        return await self.list_entities(entity_type, page=page)

    async def search_entities(
        self, entity_type: str, query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        path = (
            f"/search?entity_type={urllib.parse.quote(entity_type)}"
            f"&q={urllib.parse.quote(query)}&limit={limit}"
        )
        data = await self._get_json(path)
        if isinstance(data, dict):
            # Paginated envelope (issue #157); items carry the entities.
            items = data.get("items")
            return items if isinstance(items, list) else []
        return data if isinstance(data, list) else []

    async def get_entity(self, entity_type: str, entity_id: str) -> EntityDetail:
        import httpx

        try:
            client = self._get_client()
            response = await client.get(f"/entities/{entity_id}")
            response.raise_for_status()
            record = response.json()
        except httpx.HTTPStatusError as exc:
            raise BackendError(
                f"Could not load {entity_type} {entity_id}: "
                f"{_error_message(exc.response)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise BackendError(
                f"Could not load {entity_type} {entity_id}: {exc}"
            ) from exc

        schema = await self._cached_schema()
        return record_to_detail(record, entity_type, schema)

    async def get_schema(self) -> SchemaView:
        self._schema_view = None  # explicit get_schema() always refreshes
        return await self._cached_schema()

    async def get_provenance(
        self, entity_type: str, entity_id: str
    ) -> list[ProvenanceEvent]:
        data = await self._get_json(f"/entities/{entity_id}/history")
        if not isinstance(data, list):
            return []
        events: list[ProvenanceEvent] = []
        for record in reversed(data):  # history is oldest-first
            events.append(
                ProvenanceEvent(
                    event_type=record.get(
                        "operation_type", record.get("event_type", "unknown")
                    ),
                    timestamp=record.get("timestamp", ""),
                    actor=record.get("user_id") or "",
                    diff=record.get("state_snapshot")
                    or record.get("diff")
                    or {},
                )
            )
        return events

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> str:
        result = await self._send_json(
            "POST", "/ingest", {"entity_type": entity_type, "data": data}
        )
        if isinstance(result, dict):
            return str(result.get("id", ""))
        return ""

    async def update_entity(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        await self._send_json("PUT", f"/entities/{entity_type}/{entity_id}", data)

    async def set_availability(
        self,
        entity_type: str,
        entity_id: str,
        is_available: bool,
        reason: str | None = None,
    ) -> None:
        result = await self._send_json(
            "POST",
            f"/entities/{entity_type}/bulk-availability",
            {
                "entity_ids": [entity_id],
                "is_available": is_available,
                "reason": reason,
            },
        )
        if isinstance(result, dict):
            failures = result.get("failures") or []
            if failures:
                raise BackendError(
                    f"Availability change failed: "
                    f"{failures[0].get('error', 'unknown')}"
                )

    async def aclose(self) -> None:
        """Close the underlying HTTP client (called on app shutdown)."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
