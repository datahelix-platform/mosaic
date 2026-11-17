"""TUI SDK backend — wraps the public ``MosaicClient`` API for direct local mode.

Business logic stays in the SDK: this adapter only calls public
``MosaicClient`` methods (``query``, ``get``, ``history``, ``create``,
``replace``, ``set_availability_bulk``, ``search``) and the
``SchemaRegistry`` introspection surface. All synchronous SDK calls are
dispatched via ``asyncio.to_thread()`` so Textual's event loop never blocks.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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

#: How many rows a substring filter will scan before paginating client-side.
_FILTER_SCAN_LIMIT = 1000


def _resolve_db_path(db_path: str | Path | None) -> Path:
    """Resolve the SQLite database path.

    Priority: explicit *db_path* argument > ``config.json`` in cwd >
    ``data/mosaic.db`` (the CLI convention) when it exists > the legacy
    ``data/hippo.db`` / ``hippo.db`` when they exist (ADR-0004) >
    ``mosaic.db``.
    """
    if db_path is not None:
        return Path(db_path)

    config_file = Path("config.json")
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
            candidate = cfg.get("db_path") or cfg.get("database_url")
            if candidate:
                return Path(candidate)
        except (json.JSONDecodeError, OSError):
            pass

    for candidate in (
        Path("data/mosaic.db"),
        Path("data/hippo.db"),  # legacy (ADR-0004)
        Path("hippo.db"),  # legacy (ADR-0004)
    ):
        if candidate.exists():
            return candidate

    return Path("mosaic.db")


def _resolve_schema_path(schema_path: str | Path | None) -> Path | None:
    """Resolve the LinkML schema directory/file for the registry.

    Priority: explicit *schema_path* argument > ``schemas/`` in cwd (the
    CLI convention) when it exists > ``None`` (bundled ``hippo_core`` only).
    """
    if schema_path is not None:
        return Path(schema_path)
    cli_default = Path("schemas")
    if cli_default.is_dir():
        return cli_default
    return None


def _resolve_validators_path(validators_path: str | Path | None) -> Path | None:
    """Resolve the CEL validators file for the write pipeline.

    Priority: explicit *validators_path* argument > ``validators_path`` in
    ``config.json`` (skipped when ``validation_enabled`` is ``false``) >
    ``None``. ``None`` means no CEL business-rule validators are loaded;
    schema-level validation via the registry still applies regardless.
    """
    if validators_path is not None:
        return Path(validators_path)

    config_file = Path("config.json")
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if cfg.get("validation_enabled", True) is False:
            return None
        candidate = cfg.get("validators_path")
        if candidate:
            return Path(candidate)
    return None


def _resolve_storage_backend(storage_backend: str | None) -> str:
    """Resolve the storage backend name.

    Priority: explicit *storage_backend* argument > ``storage_backend`` in
    ``config.json`` > ``"sqlite"``. Lets the TUI honour a deployment's
    configured backend (e.g. PostgreSQL) rather than assuming SQLite.
    """
    if storage_backend is not None:
        return storage_backend

    config_file = Path("config.json")
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
        except (json.JSONDecodeError, OSError):
            return "sqlite"
        candidate = cfg.get("storage_backend")
        if candidate:
            return str(candidate)
    return "sqlite"


class SDKBackend:
    """TUIBackend implementation that uses ``MosaicClient`` directly.

    Constructs the client through :func:`mosaic.core.factory.create_client` —
    the same config-driven factory the CLI and ``mosaic serve`` use — so the
    TUI opens a deployment exactly as the other transports do.

    Args:
        db_path: Path/URL of the database. If omitted, falls back to
            ``config.json`` in the cwd, then ``data/mosaic.db``, then
            the legacy ``data/hippo.db`` / ``hippo.db``, then
            ``mosaic.db``.
        schema_path: Path to a LinkML schema file or directory. If omitted,
            falls back to ``schemas/`` in the cwd, then the bundled
            ``hippo_core`` schema (framework classes only).
        validators_path: Path to a CEL ``validators.yaml`` for the write
            pipeline. If omitted, falls back to ``config.json``'s
            ``validators_path`` (unless ``validation_enabled`` is false),
            then ``None``. Mirrors how a deployment configures the SDK so
            TUI writes honour the same business-rule validators.
        storage_backend: Storage backend name. If omitted, falls back to
            ``config.json``'s ``storage_backend``, then ``"sqlite"``.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        schema_path: str | Path | None = None,
        validators_path: str | Path | None = None,
        storage_backend: str | None = None,
    ) -> None:
        self._db_path = _resolve_db_path(db_path)
        self._schema_path = _resolve_schema_path(schema_path)
        self._validators_path = _resolve_validators_path(validators_path)
        self._storage_backend = _resolve_storage_backend(storage_backend)
        self._client: Any = None  # lazy-initialized on first use
        self._schema_view: SchemaView | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Lazy-create and return the configured MosaicClient."""
        if self._client is None:
            from mosaic.core.factory import create_client

            try:
                self._client = create_client(
                    storage_backend=self._storage_backend,
                    database_url=str(self._db_path),
                    schema_path=self._schema_path,
                    validators_path=self._validators_path,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced as BackendError
                raise BackendError(
                    f"Could not open Mosaic instance at {self._db_path}: {exc}"
                ) from exc
        return self._client

    def _entity_type_names(self) -> list[str]:
        """Concrete, user-facing entity classes from the merged schema."""
        client = self._get_client()
        registry = client.registry
        if registry is None:
            return []
        names: list[str] = []
        for name in registry.class_names():
            if name in CORE_INFRA_CLASSES:
                continue
            cls = registry.get_class(name)
            if cls is None or cls.abstract:
                continue
            names.append(name)
        return sorted(names)

    @staticmethod
    def _matches_substring(item: dict[str, Any], lowered: str) -> bool:
        """Case-insensitive substring match over id + user data."""
        if lowered in str(item.get("id", "")).lower():
            return True
        return lowered in json.dumps(item.get("data", {}), default=str).lower()

    # ------------------------------------------------------------------
    # Synchronous workers (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _connection_info_sync(self) -> ConnectionInfo:
        target = str(self._db_path)
        schema_desc = (
            str(self._schema_path) if self._schema_path else "bundled hippo_core"
        )
        try:
            client = self._get_client()
            n_types = len(self._entity_type_names())
            del client
            return ConnectionInfo(
                mode="sdk",
                target=target,
                ok=True,
                detail=f"schema: {schema_desc} ({n_types} entity types)",
            )
        except BackendError as exc:
            return ConnectionInfo(mode="sdk", target=target, ok=False, detail=exc.message)

    def _list_entity_types_sync(self) -> list[EntityTypeSummary]:
        client = self._get_client()
        registry = client.registry
        summaries: list[EntityTypeSummary] = []
        for name in self._entity_type_names():
            try:
                # Type-exact: a concrete parent's query spans its subclasses
                # (ADR-0014 Decision 1), which would count each subclass row
                # twice across the sidebar and disagree with status().
                count = client.query(
                    entity_type=name, limit=1, include_subclasses=False
                ).total
            except Exception:  # noqa: BLE001 — count failure must not kill sidebar
                count = 0
            cls = registry.get_class(name) if registry is not None else None
            description = getattr(cls, "description", None) if cls else None
            summaries.append(
                EntityTypeSummary(name=name, count=count, description=description)
            )
        return summaries

    def _list_entities_sync(
        self, entity_type: str, page: int, filter_text: str
    ) -> PagedResult:
        client = self._get_client()
        filter_text = filter_text.strip()

        try:
            if filter_text and "=" in filter_text:
                field_name, _, value = filter_text.partition("=")
                return self._query_entities_sync(
                    entity_type,
                    [{"field": field_name.strip(), "value": value.strip()}],
                    "and",
                    page,
                )

            if filter_text:
                result = client.query(entity_type=entity_type, limit=_FILTER_SCAN_LIMIT)
                lowered = filter_text.lower()
                matched = [
                    item
                    for item in result.items
                    if self._matches_substring(item, lowered)
                ]
                total = len(matched)
                page, total_pages = compute_paging(total, page)
                offset = (page - 1) * PAGE_SIZE
                return PagedResult(
                    items=matched[offset : offset + PAGE_SIZE],
                    page=page,
                    total_pages=total_pages,
                    total_items=total,
                )

            return self._query_entities_sync(entity_type, None, "and", page)
        except BackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Query failed for {entity_type}: {exc}") from exc

    def _query_entities_sync(
        self,
        entity_type: str,
        filters: list[dict[str, Any]] | None,
        filter_mode: str,
        page: int,
    ) -> PagedResult:
        client = self._get_client()
        try:
            offset = max(0, (page - 1) * PAGE_SIZE)
            result = client.query(
                entity_type=entity_type,
                filters=filters,
                limit=PAGE_SIZE,
                offset=offset,
                filter_mode=filter_mode,
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Query failed for {entity_type}: {exc}") from exc

        page, total_pages = compute_paging(result.total, page)
        return PagedResult(
            items=list(result.items),
            page=page,
            total_pages=total_pages,
            total_items=result.total,
        )

    def _search_entities_sync(
        self, entity_type: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        client = self._get_client()
        try:
            return list(
                client.search(
                    entity_type=entity_type, query=query, limit=limit
                ).items
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Search failed for {entity_type}: {exc}") from exc

    def _get_entity_sync(self, entity_type: str, entity_id: str) -> EntityDetail:
        client = self._get_client()
        try:
            record = client.get(
                entity_type=entity_type,
                entity_id=entity_id,
                include_unavailable=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendError(
                f"Could not load {entity_type} {entity_id}: {exc}"
            ) from exc
        return record_to_detail(record, entity_type, self._get_schema_sync())

    def _get_schema_sync(self) -> SchemaView:
        if self._schema_view is not None:
            return self._schema_view

        client = self._get_client()
        registry = client.registry
        if registry is None:
            self._schema_view = SchemaView()
            return self._schema_view

        all_enums = registry.schema_view.all_enums()
        entity_types: list[EntityTypeSchema] = []
        relationships: list[RelationshipDeclaration] = []

        for name in self._entity_type_names():
            cls = registry.get_class(name)
            indexed = {slot.name for slot, _partial in registry.indexed_slots(name)}
            fields: list[FieldInfo] = []
            for slot in registry.induced_slots(name):
                rng = slot.range or "string"
                ref_target = (
                    rng
                    if registry.has_class(rng) and rng not in CORE_INFRA_CLASSES
                    else None
                )
                enum_values: list[str] | None = None
                enum_def = all_enums.get(rng)
                if enum_def is not None:
                    enum_values = list(enum_def.permissible_values.keys())
                fields.append(
                    FieldInfo(
                        name=slot.name,
                        field_type=rng,
                        required=bool(slot.required),
                        indexed=slot.name in indexed,
                        ref_target=ref_target,
                        multivalued=bool(slot.multivalued),
                        identifier=bool(slot.identifier),
                        enum_values=enum_values,
                        description=slot.description,
                    )
                )
            entity_types.append(
                EntityTypeSchema(
                    name=name,
                    fields=fields,
                    description=getattr(cls, "description", None),
                )
            )
            for slot_name, target in registry.reference_slots(name):
                if target in CORE_INFRA_CLASSES:
                    continue
                relationships.append(
                    RelationshipDeclaration(
                        source_type=name,
                        relationship_name=slot_name,
                        target_type=target,
                    )
                )

        self._schema_view = SchemaView(
            entity_types=entity_types, relationships=relationships
        )
        return self._schema_view

    def _get_provenance_sync(
        self, entity_type: str, entity_id: str
    ) -> list[ProvenanceEvent]:
        client = self._get_client()
        try:
            history = client.history(entity_id)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(
                f"Could not load provenance for {entity_id}: {exc}"
            ) from exc

        events: list[ProvenanceEvent] = []
        for record in reversed(history):  # newest first
            events.append(
                ProvenanceEvent(
                    event_type=record.get("operation_type", "unknown"),
                    timestamp=record.get("timestamp", ""),
                    actor=record.get("user_id") or "",
                    diff=record.get("state_snapshot") or {},
                )
            )
        return events

    def _create_entity_sync(self, entity_type: str, data: dict[str, Any]) -> str:
        client = self._get_client()
        try:
            result = client.create(entity_type=entity_type, data=data)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Create failed: {exc}") from exc
        self._invalidate_schema_dependent_caches()
        return str(result.get("id", ""))

    def _update_entity_sync(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        client = self._get_client()
        try:
            client.replace(entity_type=entity_type, entity_id=entity_id, data=data)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Update failed: {exc}") from exc

    def _set_availability_sync(
        self,
        entity_type: str,
        entity_id: str,
        is_available: bool,
        reason: str | None,
    ) -> None:
        client = self._get_client()
        try:
            result = client.set_availability_bulk(
                entity_type=entity_type,
                entity_ids=[entity_id],
                is_available=is_available,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Availability change failed: {exc}") from exc

        failures = result.get("failures") or []
        if failures:
            raise BackendError(
                f"Availability change failed: {failures[0].get('error', 'unknown')}"
            )

    def _invalidate_schema_dependent_caches(self) -> None:
        """Hook for cache invalidation after writes (schema is static here)."""

    # ------------------------------------------------------------------
    # TUIBackend protocol implementation
    # ------------------------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(supports_filters=True, supports_fts=True)

    async def connection_info(self) -> ConnectionInfo:
        return await asyncio.to_thread(self._connection_info_sync)

    async def list_entity_types(self) -> list[EntityTypeSummary]:
        return await asyncio.to_thread(self._list_entity_types_sync)

    async def list_entities(
        self,
        entity_type: str,
        page: int = 1,
        filter_text: str = "",
    ) -> PagedResult:
        return await asyncio.to_thread(
            self._list_entities_sync, entity_type, page, filter_text
        )

    async def query_entities(
        self,
        entity_type: str,
        filters: list[dict[str, Any]] | None = None,
        filter_mode: str = "and",
        page: int = 1,
    ) -> PagedResult:
        return await asyncio.to_thread(
            self._query_entities_sync, entity_type, filters, filter_mode, page
        )

    async def search_entities(
        self, entity_type: str, query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._search_entities_sync, entity_type, query, limit
        )

    async def get_entity(self, entity_type: str, entity_id: str) -> EntityDetail:
        return await asyncio.to_thread(self._get_entity_sync, entity_type, entity_id)

    async def get_schema(self) -> SchemaView:
        return await asyncio.to_thread(self._get_schema_sync)

    async def get_provenance(
        self, entity_type: str, entity_id: str
    ) -> list[ProvenanceEvent]:
        return await asyncio.to_thread(
            self._get_provenance_sync, entity_type, entity_id
        )

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._create_entity_sync, entity_type, data)

    async def update_entity(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        await asyncio.to_thread(
            self._update_entity_sync, entity_type, entity_id, data
        )

    async def set_availability(
        self,
        entity_type: str,
        entity_id: str,
        is_available: bool,
        reason: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._set_availability_sync, entity_type, entity_id, is_available, reason
        )
