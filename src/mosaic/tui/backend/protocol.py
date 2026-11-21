"""TUI backend protocol — data transfer objects and TUIBackend abstract interface.

The TUI is a *consumer* of Mosaic: every view talks to a ``TUIBackend`` and
never to ``MosaicClient`` or HTTP directly. Both backends translate their
native record shapes into the small DTO vocabulary below so that views are
completely backend-agnostic (SDK-first principle, sec2).
"""

from __future__ import annotations

import math
import typing
from dataclasses import dataclass, field
from typing import Any

#: Default page size used by both backends for entity listings.
PAGE_SIZE = 20

#: System fields surfaced on every entity detail, in display order.
#: ``id``/``is_available`` live on the entity table; the temporal fields
#: (``created_at``, ``updated_at``, ``schema_version``, …) are computed at
#: read time from the provenance log (sec9 §9.7).
SYSTEM_FIELDS: tuple[str, ...] = (
    "id",
    "is_available",
    "version",
    "created_at",
    "updated_at",
    "schema_version",
    "created_by",
    "updated_by",
    "superseded_by",
)

#: mosaic_core infrastructure classes that are concrete (non-abstract) but are
#: not user-facing entity types; hidden from sidebar/schema listings.
CORE_INFRA_CLASSES: frozenset[str] = frozenset(
    {
        "ProvenanceRecord",
        "Process",
        "Validator",
        "ReferenceLoader",
        "ExternalID",
        # ExternalReference is a structured VALUE type (issue #48) —
        # stored inline on entity slots, never an entity of its own.
        "ExternalReference",
    }
)

#: Entity lifecycle status values (mosaic_core ``Status`` enum). Transitions
#: flip ``is_available`` and record the driver in provenance — there are no
#: hard deletes.
STATUS_VALUES: tuple[str, ...] = (
    "active",
    "archived",
    "superseded",
    "deleted",
    "distributed",
    "removed",
)

#: Statuses that map to ``is_available = True``.
AVAILABLE_STATUSES: frozenset[str] = frozenset({"active"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """Raised by backends when an operation fails.

    Views catch this (and only this) to show error toasts / inline
    validation feedback, keeping transport details out of the UI.

    Args:
        message: Human-readable summary suitable for a toast.
        detail: Optional longer detail (e.g. validation failure list).
    """

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass
class ConnectionInfo:
    """Connection state shown in the status bar."""

    mode: str  # "sdk" | "rest"
    target: str  # db path or base URL
    ok: bool
    detail: str = ""


@dataclass
class BackendCapabilities:
    """What the backend's query surface supports.

    The Query view adapts to this: the SDK exposes field filters with
    and/or composition; the REST API exposes type listing + FTS search.
    """

    supports_filters: bool = False
    supports_fts: bool = False


@dataclass
class EntityTypeSummary:
    """Summary of an entity type shown in the sidebar."""

    name: str
    count: int
    description: str | None = None


@dataclass
class PagedResult:
    """Paginated list of entity records."""

    items: list[dict[str, Any]]
    page: int
    total_pages: int
    total_items: int = 0


@dataclass
class RelatedEntityRef:
    """Reference to a related entity."""

    relationship_name: str
    target_type: str
    target_id: str


@dataclass
class EntityDetail:
    """Full detail view of a single entity.

    ``fields`` carries system fields first (see :data:`SYSTEM_FIELDS`)
    followed by user-schema slots; ``data`` holds only the user slots so
    edit forms can prefill without filtering.
    """

    id: str
    entity_type: str
    fields: dict[str, Any]
    relationships: list[RelatedEntityRef] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldInfo:
    """Metadata about a single schema field (LinkML slot)."""

    name: str
    field_type: str
    required: bool = False
    indexed: bool = False
    ref_target: str | None = None  # set when the range is another class
    multivalued: bool = False
    identifier: bool = False
    enum_values: list[str] | None = None
    description: str | None = None


@dataclass
class RelationshipDeclaration:
    """A relationship declared in the schema (a class-ranged slot)."""

    source_type: str
    relationship_name: str
    target_type: str


@dataclass
class EntityTypeSchema:
    """Schema information for a single entity type."""

    name: str
    fields: list[FieldInfo] = field(default_factory=list)
    description: str | None = None


@dataclass
class SchemaView:
    """Full schema view returned by get_schema()."""

    entity_types: list[EntityTypeSchema] = field(default_factory=list)
    relationships: list[RelationshipDeclaration] = field(default_factory=list)

    def get_entity_type(self, name: str) -> EntityTypeSchema | None:
        """Return the entity type schema for *name*, or ``None``."""
        for et in self.entity_types:
            if et.name == name:
                return et
        return None


@dataclass
class ProvenanceEvent:
    """A single provenance event for an entity."""

    event_type: str
    timestamp: str
    actor: str = ""
    diff: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def compute_paging(total: int, page: int, page_size: int = PAGE_SIZE) -> tuple[int, int]:
    """Clamp *page* against *total* items; return ``(page, total_pages)``."""
    total_pages = max(1, math.ceil(total / page_size)) if page_size else 1
    return max(1, min(page, total_pages)), total_pages


def derive_relationships(
    schema: SchemaView | None,
    entity_type: str,
    data: dict[str, Any],
) -> list[RelatedEntityRef]:
    """Derive outbound relationship refs from class-ranged slot values.

    Mosaic's graph-shaped API stores references as UUID-valued slots whose
    LinkML range is another class. Both backends share this derivation so
    relationship navigation behaves identically in SDK and REST mode.
    """
    if schema is None:
        return []
    et = schema.get_entity_type(entity_type)
    if et is None:
        return []

    refs: list[RelatedEntityRef] = []
    for field_info in et.fields:
        if not field_info.ref_target:
            continue
        value = data.get(field_info.name)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, (str, int)) and str(v):
                refs.append(
                    RelatedEntityRef(
                        relationship_name=field_info.name,
                        target_type=field_info.ref_target,
                        target_id=str(v),
                    )
                )
    return refs


def record_to_detail(
    record: dict[str, Any],
    entity_type: str,
    schema: SchemaView | None = None,
) -> EntityDetail:
    """Convert a client/REST entity record into an :class:`EntityDetail`.

    Records have the shape returned by ``MosaicClient.get()`` /
    ``GET /entities/{id}``: top-level system + temporal fields plus a
    nested ``data`` dict of user slots.
    """
    data = dict(record.get("data") or {})
    fields: dict[str, Any] = {}
    for name in SYSTEM_FIELDS:
        if name == "is_available":
            fields[name] = record.get(name, True)
        elif name in record:
            fields[name] = record.get(name)
    fields.update(data)
    return EntityDetail(
        id=str(record.get("id", "")),
        entity_type=entity_type,
        fields=fields,
        relationships=derive_relationships(schema, entity_type, data),
        data=data,
    )


# ---------------------------------------------------------------------------
# TUIBackend protocol
# ---------------------------------------------------------------------------


class TUIBackend(typing.Protocol):
    """Protocol that all TUI backends must satisfy.

    Both ``SDKBackend`` and ``RESTBackend`` implement this interface so that
    views are completely backend-agnostic.

    Read methods degrade gracefully (empty results + status callback /
    raised :class:`BackendError` for single-entity reads); write methods
    raise :class:`BackendError` with a human-readable message on failure.
    """

    def capabilities(self) -> BackendCapabilities:
        """Describe what the backend's query surface supports."""
        ...

    async def connection_info(self) -> ConnectionInfo:
        """Probe the backend and report connection state."""
        ...

    async def list_entity_types(self) -> list[EntityTypeSummary]:
        """Return a summary of all entity types with their entity counts."""
        ...

    async def list_entities(
        self,
        entity_type: str,
        page: int = 1,
        filter_text: str = "",
    ) -> PagedResult:
        """Return a page of entities for the given type, optionally filtered.

        ``filter_text`` of the form ``field=value`` becomes an exact-match
        filter where supported; any other text is a substring match.
        """
        ...

    async def query_entities(
        self,
        entity_type: str,
        filters: list[dict[str, Any]] | None = None,
        filter_mode: str = "and",
        page: int = 1,
    ) -> PagedResult:
        """Run a structured query (field filters with and/or composition)."""
        ...

    async def search_entities(
        self, entity_type: str, query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Full-text search over ``mosaic_search``-annotated fields."""
        ...

    async def get_entity(self, entity_type: str, entity_id: str) -> EntityDetail:
        """Return full detail for a single entity (including unavailable ones)."""
        ...

    async def get_schema(self) -> SchemaView:
        """Return the full schema view (entity types + relationships)."""
        ...

    async def get_provenance(
        self, entity_type: str, entity_id: str
    ) -> list[ProvenanceEvent]:
        """Return the full provenance history for an entity, newest first."""
        ...

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> str:
        """Create an entity; return the new entity id."""
        ...

    async def update_entity(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        """Replace an existing entity's user fields (full update)."""
        ...

    async def set_availability(
        self,
        entity_type: str,
        entity_id: str,
        is_available: bool,
        reason: str | None = None,
    ) -> None:
        """Transition entity availability (no hard deletes — sec9 §9.5)."""
        ...
