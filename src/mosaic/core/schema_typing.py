"""Shared, transport-agnostic type model derived from a LinkML schema.

Single source of truth for "how a LinkML schema maps to a typed surface",
consumed by every transport so they cannot drift:

- the typed SDK (Pydantic accessors) — :mod:`mosaic.core.typed_client`
- the GraphQL transport (Strawberry types) — ``mosaic.graphql`` (issue #45)
- the OpenAPI/JSON-Schema transport — REST (issue #46)

This module answers the questions all three previously answered separately:
which classes are exposed, how each slot's range classifies (scalar / enum /
class-reference), the relationship targets, enum values, and which fields are
system vs. computed-temporal. Each transport renders this model into its own
type system; the classification lives here once.

No transport types and no business logic live here — only a normalized view
over a :class:`~mosaic.linkml_bridge.SchemaRegistry`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field as dc_field
from typing import Any, Optional

from mosaic.linkml_bridge import (
    HIPPO_EXTERNAL_XREF,
    VALUE_TYPE_CLASSES,
    SchemaRegistry,
    annotation_value,
    class_accessor_name,
)

#: hippo_core framework classes that transports never expose as domain
#: entities. ``Entity`` is abstract; the rest are system concerns. This is
#: the single definition — transports import it rather than re-listing it.
INFRASTRUCTURE_CLASSES: frozenset[str] = frozenset(
    {
        "Entity",
        "ProvenanceRecord",
        "Process",
        "Validator",
        "ReferenceLoader",
    }
)

#: ``from_schema`` of the framework classes named in INFRASTRUCTURE_CLASSES.
HIPPO_CORE_SCHEMA_URI = "https://w3id.org/hippo/hippo_core"


def is_infrastructure_class(registry: SchemaRegistry, name: str) -> bool:
    """True if ``name`` is one of ``hippo_core``'s framework classes.

    Matches by identity (the class's defining schema), not bare name, so a
    domain schema that defines its own ``Process`` keeps it as an ordinary
    entity class.
    """
    if name not in INFRASTRUCTURE_CLASSES:
        return False
    cls = registry.get_class(name)
    return cls is not None and str(cls.from_schema) == HIPPO_CORE_SCHEMA_URI


# VALUE_TYPE_CLASSES (imported above) is the framework-baseline value-type
# set, re-exported here for backward compatibility. As of issue #90 the
# authoritative, per-schema set is computed from the schema via
# ``SchemaRegistry.value_type_classes()`` (identifier-less, non-tree-root
# classes — stored inline on the slot that ranges them, no id/lifecycle).
# Slots ranged against a value type classify as ``SlotKind.STRUCTURED`` and
# are not exposed as entity types. Detection logic lives in
# ``mosaic.linkml_bridge``.

#: System fields stored on the entity table (present as induced slots).
SYSTEM_FIELDS: frozenset[str] = frozenset({"id", "is_available"})

#: Read-time fields computed from the provenance log (sec9 §9.7) — not stored
#: slots. Transports expose them read-only; they are not part of an entity's
#: ``fields`` (which derive from induced slots).
TEMPORAL_FIELDS: tuple[str, ...] = (
    "created_at",
    "updated_at",
    "schema_version",
    "created_by",
    "updated_by",
)


class SlotKind(enum.Enum):
    """How a slot's range classifies for type rendering."""

    SCALAR = "scalar"  # string/integer/float/boolean/date/datetime/...
    ENUM = "enum"  # range is a LinkML enum
    REFERENCE = "reference"  # range is another (non-infrastructure) class
    STRUCTURED = "structured"  # range is an inline value type (issue #48)


class FieldRole(enum.Enum):
    """Where a field comes from / how transports should treat it."""

    USER = "user"  # domain slot, writable
    SYSTEM = "system"  # id / is_available — stored, read-only


@dataclass(frozen=True)
class SlotModel:
    """Normalized view of one induced slot on an entity class."""

    name: str
    kind: SlotKind
    range: str  # raw LinkML range string
    role: FieldRole = FieldRole.USER
    required: bool = False
    multivalued: bool = False
    identifier: bool = False
    has_default: bool = False  # LinkML ``ifabsent`` present
    description: Optional[str] = None
    target_class: Optional[str] = None  # set when kind is REFERENCE/STRUCTURED
    enum_name: Optional[str] = None  # set when kind is ENUM
    enum_values: tuple[str, ...] = ()
    #: ``hippo_external_xref`` annotation present (issue #48): the slot's
    #: ``(system, value)`` pairs are reverse-lookup keys with global
    #: uniqueness among available entities. Only meaningful on
    #: STRUCTURED slots ranged against ``ExternalReference``.
    is_external_xref: bool = False
    #: LinkML ``inverse`` on a multivalued reference (ADR-0011): this slot is
    #: a *virtual* reverse edge over ``<target_class>.<inverse_of>``'s stored
    #: FK column — no storage of its own, ignored on write, hydrated /
    #: filtered / counted through the forward slot. ``None`` for every
    #: ordinary (stored) slot.
    inverse_of: Optional[str] = None


@dataclass(frozen=True)
class EntityTypeModel:
    """Normalized view of one concrete, exposed entity class."""

    class_name: str
    accessor_name: str  # canonical plural accessor (shared naming)
    description: Optional[str] = None
    fields: tuple[SlotModel, ...] = dc_field(default_factory=tuple)

    @property
    def user_fields(self) -> tuple[SlotModel, ...]:
        return tuple(f for f in self.fields if f.role is FieldRole.USER)

    @property
    def system_fields(self) -> tuple[SlotModel, ...]:
        return tuple(f for f in self.fields if f.role is FieldRole.SYSTEM)

    @property
    def relationships(self) -> tuple[SlotModel, ...]:
        return tuple(f for f in self.fields if f.kind is SlotKind.REFERENCE)


def exposed_class_names(registry: SchemaRegistry) -> list[str]:
    """Return the concrete, non-infrastructure entity classes, sorted.

    Replaces the duplicated "skip infrastructure + skip abstract" selection
    that lived in both the typed client and the GraphQL builder.
    """
    sv = registry.schema_view
    value_types = registry.value_type_classes()
    names: list[str] = []
    for name in registry.class_names():
        if is_infrastructure_class(registry, name) or name in value_types:
            continue
        cls = sv.get_class(name)
        if cls is None or cls.abstract:
            continue
        names.append(name)
    return sorted(names)


def _classify_slot(slot: Any, registry: SchemaRegistry, enums: dict[str, Any]) -> SlotModel:
    rng = slot.range or "string"
    name = slot.name
    role = FieldRole.SYSTEM if name in SYSTEM_FIELDS else FieldRole.USER

    kind = SlotKind.SCALAR
    target_class: Optional[str] = None
    enum_name: Optional[str] = None
    enum_values: tuple[str, ...] = ()

    if rng in enums:
        kind = SlotKind.ENUM
        enum_name = rng
        enum_values = tuple(enums[rng].permissible_values.keys())
    elif rng in registry.value_type_classes():
        # Inline structured value (issue #48 / #90): the stored value is the
        # object itself (JSON), not a UUID reference to another entity.
        kind = SlotKind.STRUCTURED
        target_class = rng
    elif registry.has_class(rng) and not is_infrastructure_class(registry, rng):
        kind = SlotKind.REFERENCE
        target_class = rng

    return SlotModel(
        name=name,
        kind=kind,
        range=rng,
        role=role,
        required=bool(slot.required),
        multivalued=bool(slot.multivalued),
        identifier=bool(slot.identifier),
        has_default=getattr(slot, "ifabsent", None) is not None,
        description=slot.description,
        target_class=target_class,
        enum_name=enum_name,
        enum_values=enum_values,
        is_external_xref=bool(annotation_value(slot, HIPPO_EXTERNAL_XREF)),
        # Only the multivalued, derived side is virtual (ADR-0011); a
        # single-valued slot carrying ``inverse`` is the stored FK. A reverse
        # the registry synthesized from such a forward declaration (ADR-0012)
        # is already a multivalued slot by the time it reaches here.
        inverse_of=(
            str(slot.inverse)
            if kind is SlotKind.REFERENCE and slot.multivalued and getattr(slot, "inverse", None)
            else None
        ),
    )


def build_type_model(registry: SchemaRegistry) -> dict[str, EntityTypeModel]:
    """Build the normalized type model for every exposed entity class.

    Returns a mapping ``class_name -> EntityTypeModel``. Reference slots
    pointing at non-exposed classes keep their ``target_class`` so callers can
    decide how to render a dangling reference.
    """
    sv = registry.schema_view
    enums = sv.all_enums()
    model: dict[str, EntityTypeModel] = {}

    for name in exposed_class_names(registry):
        cls = sv.get_class(name)
        fields = tuple(
            _classify_slot(slot, registry, enums)
            for slot in registry.induced_slots(name)
        )
        model[name] = EntityTypeModel(
            class_name=name,
            accessor_name=class_accessor_name(name, cls),
            description=getattr(cls, "description", None),
            fields=fields,
        )
    return model


# ---------------------------------------------------------------------------
# Capability manifest (ADR-0009 / issue #181): what the ``where:``-shaped
# query boundary supports per field, on top of the type model above. Feeds
# the MCP capability resource (issue #182) and the QuerySpec validator
# (issue #183) so neither has to re-derive this from raw introspection.
# ---------------------------------------------------------------------------


class FilterOp(enum.Enum):
    """Operators the ``where:`` contract may accept for a field (ADR-0006)."""

    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"
    IS_NULL = "is_null"


#: Base LinkML scalar ranges whose values order meaningfully — comparison
#: operators and fieldRange min/max are only defined for these (ADR-0006/
#: ADR-0007). Mirrors ``mosaic.graphql.resolvers._ORDERED_RANGES``; the two
#: classify the same thing through two transports and must not drift.
ORDERED_BASE_RANGES: frozenset[str] = frozenset(
    {"integer", "float", "double", "decimal", "date", "datetime", "time"}
)


def _filter_ops_for_slot(
    slot: SlotModel, registry: SchemaRegistry
) -> tuple[FilterOp, ...]:
    """Operators the ``where:`` contract accepts directly on this field.

    This mirrors ``mosaic.graphql.schema_builder``'s ``_filter_ops_input_for``
    — the generated ``<Type>Filter`` inputs actually served over ``where:`` —
    not ``mosaic.graphql.resolvers._allowed_filter_ops``, which serves the
    older flat ``filters:`` list argument and additionally allows
    eq/neq/in/is_null directly on a single-valued reference's stored UUID.

    A ``REFERENCE`` slot returns no direct filter ops here: since ADR-0006
    M5a/M5b landed, references are filtered through the relationship
    predicate (the nested edge under the slot's name, or a some/none
    quantifier when multivalued — see ``predicate`` on ``FieldCapability``),
    never through a FilterOp on the field itself. That is a capability, not
    an omission — callers must not read an empty ``filter_ops`` tuple on a
    reference field as "unfilterable."
    """
    if slot.kind is SlotKind.REFERENCE:
        return ()
    if slot.multivalued:
        return (FilterOp.EQ, FilterOp.IS_NULL)
    if slot.kind is SlotKind.ENUM:
        return (FilterOp.EQ, FilterOp.NEQ, FilterOp.IN, FilterOp.IS_NULL)
    if slot.kind is SlotKind.STRUCTURED:
        return (FilterOp.EQ, FilterOp.IN, FilterOp.IS_NULL)
    base = registry.base_scalar_range(slot.range)
    if base in ORDERED_BASE_RANGES:
        return (
            FilterOp.EQ,
            FilterOp.NEQ,
            FilterOp.IN,
            FilterOp.GT,
            FilterOp.GTE,
            FilterOp.LT,
            FilterOp.LTE,
            FilterOp.IS_NULL,
        )
    if base == "boolean":
        return (FilterOp.EQ, FilterOp.NEQ, FilterOp.IS_NULL)
    return (FilterOp.EQ, FilterOp.NEQ, FilterOp.IN, FilterOp.CONTAINS, FilterOp.IS_NULL)


@dataclass(frozen=True)
class FieldCapability:
    """What one field supports on the ``where:``-shaped query boundary.

    ``orderable`` and ``aggregatable`` currently share one condition (single-
    valued scalar or enum) — kept as separate flags because they answer
    separate questions (``orderBy`` vs. ``facetCounts``) that happen to
    coincide today, not because they are the same capability.
    """

    slot: SlotModel
    filter_ops: tuple[FilterOp, ...]
    #: Relationship-predicate filterable: a nested edge filter under this
    #: slot's name (to-one, ADR-0006 M5a) or a some/none quantifier over it
    #: (to-many, M5b). False for a dangling reference to a non-exposed class.
    predicate: bool
    orderable: bool  # legal `orderBy` field
    aggregatable: bool  # legal `facetCounts` field
    range_queryable: bool  # legal `fieldRange` (min/max) field
    searchable: bool  # carries a `hippo_search` annotation


@dataclass(frozen=True)
class EntityCapability:
    """Per-entity capability manifest: ``EntityTypeModel`` plus what the
    ``where:``/aggregation/search surface supports for each field."""

    class_name: str
    accessor_name: str
    description: Optional[str]
    fields: tuple[FieldCapability, ...]
    #: Whether `search{Plural}` returns anything for this entity. The root
    #: field itself is always mounted (issue #181 survey finding) but
    #: silently returns an empty result with no FTS-annotated slot — callers
    #: must read this flag, not field presence, to know if search works.
    search_available: bool

    @property
    def fields_by_name(self) -> dict[str, FieldCapability]:
        return {f.slot.name: f for f in self.fields}


def build_capability_manifest(
    registry: SchemaRegistry,
) -> dict[str, EntityCapability]:
    """Build the ``where:``/aggregation/search capability manifest for every
    exposed entity class, on top of :func:`build_type_model`.

    Returns a mapping ``class_name -> EntityCapability``.
    """
    type_model = build_type_model(registry)
    exposed = set(type_model)
    manifest: dict[str, EntityCapability] = {}

    for class_name, entity in type_model.items():
        searchable_names = {
            slot.name for slot, _mode in registry.searchable_slots(class_name)
        }
        fields = []
        for slot in entity.fields:
            if slot.kind is SlotKind.REFERENCE:
                filter_ops: tuple[FilterOp, ...] = ()
                predicate = slot.target_class in exposed
                orderable = aggregatable = range_queryable = False
            else:
                filter_ops = _filter_ops_for_slot(slot, registry)
                predicate = False
                orderable = aggregatable = slot.kind in (
                    SlotKind.SCALAR,
                    SlotKind.ENUM,
                ) and not slot.multivalued
                range_queryable = (
                    aggregatable
                    and slot.kind is SlotKind.SCALAR
                    and registry.base_scalar_range(slot.range) in ORDERED_BASE_RANGES
                )
            fields.append(
                FieldCapability(
                    slot=slot,
                    filter_ops=filter_ops,
                    predicate=predicate,
                    orderable=orderable,
                    aggregatable=aggregatable,
                    range_queryable=range_queryable,
                    searchable=slot.name in searchable_names,
                )
            )
        manifest[class_name] = EntityCapability(
            class_name=entity.class_name,
            accessor_name=entity.accessor_name,
            description=entity.description,
            fields=tuple(fields),
            search_available=bool(searchable_names),
        )
    return manifest
