"""GraphQL type rendering for the shared LinkML type model.

Builds Strawberry object types, input types, page (pagination) types,
and enums for every exposed entity class. *What* to expose — the class
set, each slot's scalar/enum/reference classification, relationship
targets, enum values, system vs. user fields — comes from the shared
type model in :mod:`mosaic.core.schema_typing` (issue #47), the same
model the typed SDK renders. This module only decides *how* that model
renders into GraphQL:

- One object type per exposed entity class (``build_type_model``).
- Slots map to GraphQL fields with scalar/enum/list typing.
- Reference slots (``SlotKind.REFERENCE``) emit ONE field: the resolved
  relationship (``donor: Donor``), traversed via a per-request DataLoader.
  The raw stored id is a hidden ``strawberry.Private`` carrier the resolver
  reads — not an exposed GraphQL field (edge-only; ADR-0005). Physical
  identifiers never cross the API boundary. A reference whose target has no
  generated type (abstract/polymorphic base) retains a raw ``*_id`` field as
  an interim (polymorphic reference resolution is future work).
- System fields (``id``, ``is_available``) come from the entity table;
  temporal fields (``schema_typing.TEMPORAL_FIELDS`` plus ``version``
  and ``superseded_by``) are computed at read time from the provenance
  log by the SDK (sec9 §9.7) and exposed read-only.

No business logic lives here: conversion between SDK envelopes
(``client.get`` / ``client.query`` dicts) and GraphQL instances is pure
shape mapping.
"""

from __future__ import annotations

import enum
import keyword
import re
import warnings
from dataclasses import dataclass, field as dc_field
from typing import Any, NewType, Optional

import strawberry
from strawberry.dataloader import DataLoader
from strawberry.scalars import JSON
from strawberry.types import Info

# INFRASTRUCTURE_CLASSES is re-exported for callers/tests: the single
# definition lives in the schema-typing core (issue #47), so the
# GraphQL, typed-client, and OpenAPI surfaces cannot drift.
from mosaic.core.schema_typing import (
    INFRASTRUCTURE_CLASSES,  # noqa: F401  (re-export)
    TEMPORAL_FIELDS,
    EntityTypeModel,
    SlotKind,
    SlotModel,
    build_type_model,
)
from mosaic.linkml_bridge import SchemaRegistry

# ISO-8601 passthrough scalars. Mosaic stores temporal values as ISO
# strings (SQLite TEXT columns); these scalars document the format
# without forcing a parse/serialize round-trip in the transport.
ISODateTime = strawberry.scalar(
    NewType("ISODateTime", str),
    name="DateTime",
    description="ISO-8601 datetime string (UTC).",
    serialize=str,
    parse_value=str,
)
ISODate = strawberry.scalar(
    NewType("ISODate", str),
    name="Date",
    description="ISO-8601 date string.",
    serialize=str,
    parse_value=str,
)
ISOTime = strawberry.scalar(
    NewType("ISOTime", str),
    name="Time",
    description="ISO-8601 time string.",
    serialize=str,
    parse_value=str,
)

# LinkML scalar range → GraphQL (Python) type. This is pure rendering:
# the *classification* of a slot as scalar comes from the type model
# (SlotKind.SCALAR); this map only picks the GraphQL type for a scalar
# range string. Unknown ranges (including references to classes the
# typing core does not expose, e.g. infrastructure classes) fall back
# to String.
SCALAR_RANGE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "float": float,
    "double": float,
    "decimal": float,
    "boolean": bool,
    "date": ISODate,
    "datetime": ISODateTime,
    "time": ISOTime,
    "uri": str,
    "uriorcurie": str,
    "curie": str,
    "ncname": str,
    "objectidentifier": str,
    "nodeidentifier": str,
}

# Read-only computed fields added to every generated entity type. The
# temporal names come from the schema-typing core (sec9 §9.7: computed
# from ProvenanceRecord at read time — never stored on entity tables);
# ``version`` and ``superseded_by`` are SDK envelope fields with the
# same read-only nature.
_TEMPORAL_FIELD_TYPES: dict[str, Any] = {
    "created_at": Optional[ISODateTime],
    "updated_at": Optional[ISODateTime],
    "schema_version": Optional[str],
    "created_by": Optional[str],
    "updated_by": Optional[str],
}
COMPUTED_FIELDS: list[tuple[str, Any]] = [
    ("version", Optional[int]),
    *((name, _TEMPORAL_FIELD_TYPES[name]) for name in TEMPORAL_FIELDS),
    ("superseded_by", Optional[strawberry.ID]),
]


#: System field on a concrete parent's object type naming the row's concrete
#: class (``entityType`` in GraphQL; ADR-0014 Decision 1).
ENTITY_TYPE_FIELD = "entity_type"


def snake_case(name: str) -> str:
    """``DNASample`` → ``dna_sample`` (same regex as ``default_accessor``)."""
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def camel_case(name: str) -> str:
    """``create_sample`` → ``createSample`` (GraphQL field convention)."""
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _safe_attr(name: str) -> str:
    """Coerce a LinkML slot name into a valid Python attribute name."""
    attr = re.sub(r"\W", "_", name)
    if not attr or attr[0].isdigit():
        attr = f"f_{attr}"
    if keyword.iskeyword(attr):
        attr = f"{attr}_"
    return attr


def _enum_member_name(value: str) -> str:
    member = re.sub(r"\W", "_", value)
    if not member or member[0].isdigit():
        member = f"V_{member}"
    if keyword.iskeyword(member):
        member = f"{member}_"
    return member


#: (input attr, storage op) pairs for the generated filter operator inputs
#: (ADR-0006 increment 2). The resolvers' where-walker iterates this same
#: table, so attr↔op mapping lives once.
FILTER_OP_ATTRS: list[tuple[str, str]] = [
    ("eq", "eq"),
    ("neq", "neq"),
    ("in_", "in"),
    ("gt", "gt"),
    ("gte", "gte"),
    ("lt", "lt"),
    ("lte", "lte"),
    ("contains", "contains"),
    ("is_null", "is_null"),
]


@dataclass
class SlotSpec:
    """How one type-model slot renders onto the generated GraphQL surface."""

    slot_name: str  # LinkML slot name == storage data key
    attr_name: str  # Python attribute on the generated type
    kind: str  # "scalar" | "enum" | "reference"
    multivalued: bool
    required: bool
    has_default: bool  # LinkML ifabsent present
    target_class: Optional[str] = None  # for kind == "reference"
    resolvable: bool = False  # reference target has a generated type
    resolved_attr: Optional[str] = None  # resolver field name for references
    enum_cls: Optional[type] = None  # for kind == "enum"
    scalar_type: Any = None  # for kind == "scalar"
    description: Optional[str] = None
    #: Base LinkML scalar range (typeof chains resolved) for kind ==
    #: "scalar" — drives the per-slot filter-operator set (ADR-0006).
    base_range: Optional[str] = None
    #: ADR-0011: for kind == "reference", the forward FK slot on
    #: ``target_class`` this virtual reverse edge derives from. Such a
    #: slot is read-only — omitted from Create/Update inputs.
    inverse_of: Optional[str] = None


@dataclass
class EntityGraphQLInfo:
    """Everything the resolvers need to serve one entity class."""

    class_name: str
    singular_name: str  # GraphQL query name, snake form (e.g. "sample")
    plural_name: str  # list-query name, from the accessor convention
    model: Optional[EntityTypeModel] = None  # shared type model entry
    slots: list[SlotSpec] = dc_field(default_factory=list)
    computed_fields: list[str] = dc_field(default_factory=list)
    gql_type: Any = None
    page_type: Any = None
    create_input: Any = None
    update_input: Any = None
    create_specs: list[SlotSpec] = dc_field(default_factory=list)
    update_specs: list[SlotSpec] = dc_field(default_factory=list)
    #: Generated ``<Class>Filter`` input (ADR-0006 increment 2) and its
    #: (input attr name, slot spec) pairs — the resolvers' where-walker
    #: reads these to translate a filter input into the SDK tree.
    filter_input: Any = None
    filter_fields: list[tuple[str, SlotSpec]] = dc_field(default_factory=list)
    #: Relationship-predicate edges on the filter input (ADR-0006
    #: M5a/M5b): (input attr name, slot spec, target EntityGraphQLInfo,
    #: multivalued) tuples — to-one edges nest the target's filter,
    #: to-many edges nest the some/none quantifier object.
    filter_edges: list = dc_field(default_factory=list)
    #: Generated ``<Class>OrderField`` enum (ADR-0007 increment 3): the
    #: orderable stored columns. Member values are LinkML slot names.
    order_field_enum: Any = None
    #: Exposed concrete subclasses when this class is a *concrete parent*
    #: (ADR-0014 Decision 1): its collection and every reference ranged on
    #: it may return their rows, rendered as this type, so the type carries
    #: the non-null ``entityType`` system field naming each row's class.
    exposed_subclasses: list[str] = dc_field(default_factory=list)

    def filterable_slot_names(self) -> list[str]:
        """Slot names a list query's ``filters.field`` can match on.

        Multivalued references are excluded: they live in the relationships
        table rather than a column of the entity's own table (ADR-0002), so
        no column predicate can ever match them.
        """
        return [
            spec.slot_name
            for spec in self.slots
            if not (spec.kind == "reference" and spec.multivalued)
        ]

    def is_computed_field(self, field: str) -> bool:
        """True when ``field`` names a read-time computed field.

        Matched under either spelling, since these are exposed on the type
        (as camelCase) but are not columns of it.
        """
        return any(
            field in (name, camel_case(name)) for name in self.computed_fields
        )

    def resolve_filter_field(self, field: str) -> Optional[SlotSpec]:
        """Find the slot a ``filters.field`` value addresses (issue #149).

        Filters reach the storage layer, which is keyed by LinkML slot name
        (snake_case); the generated GraphQL type exposes those same slots
        under camelCase, and a reference slot under its resolved edge name.
        All of those spellings resolve here so a filter built from ordinary
        GraphQL introspection matches the intended column instead of
        silently matching nothing. ``None`` = unrecognized.
        """
        # Exact slot names first, so a slot can never be shadowed by another
        # slot's alias.
        for spec in self.slots:
            if field == spec.slot_name:
                return spec
        for spec in self.slots:
            aliases = {camel_case(spec.slot_name)}
            if spec.resolved_attr is not None:
                aliases |= {spec.resolved_attr, camel_case(spec.resolved_attr)}
            if field in aliases:
                return spec
        return None


def get_entity_loader(context: Any, class_name: str) -> DataLoader:
    """Per-request, per-entity-type DataLoader (batched relationship reads).

    A single GraphQL request resolving N relationship fields of the same
    target type issues ONE ``client.query`` (OR-composed id filters; one
    storage round-trip + one batched temporal aggregation) instead of N
    ``client.get`` calls.
    """
    loaders = context.setdefault("entity_loaders", {})
    if class_name not in loaders:
        client = context["client"]

        async def load_fn(keys: list[str]) -> list[Optional[dict[str, Any]]]:
            paginated = client.query(
                entity_type=class_name,
                filters=[{"field": "id", "value": key} for key in keys],
                filter_mode="or",
            )
            by_id = {item["id"]: item for item in paginated.items}
            return [by_id.get(key) for key in keys]

        loaders[class_name] = DataLoader(load_fn=load_fn)
    return loaders[class_name]


class GraphQLTypeBuilder:
    """Renders the shared type model into the Strawberry type universe.

    Two-pass generation: bare classes are created first so cyclic and
    self-referential relationships annotate with real class objects (no
    string forward references), then slots/resolvers are attached and
    the classes are decorated with ``strawberry.type``.
    """

    def __init__(self, registry: SchemaRegistry) -> None:
        self._registry = registry
        self.type_model: dict[str, EntityTypeModel] = {}
        self.entities: dict[str, EntityGraphQLInfo] = {}
        self.enums: dict[str, type] = {}
        self._built = False

    # -- public surface ----------------------------------------------------

    def build(self) -> "GraphQLTypeBuilder":
        """Generate all types. Idempotent."""
        if self._built:
            return self
        self.type_model = build_type_model(self._registry)
        self._select_entities()
        self._build_enums()
        self._build_object_types()
        self._build_page_types()
        self._build_input_types()
        self._build_filter_input_types()
        self._build_order_enums()
        self._built = True
        return self

    def instance_from_envelope(
        self, class_name: str, envelope: dict[str, Any]
    ) -> Any:
        """SDK entity envelope (``client.get``/``client.query`` item) →
        generated GraphQL type instance. Pure shape mapping.
        """
        entity = self.entities[class_name]
        data = envelope.get("data") or {}
        kwargs: dict[str, Any] = {}

        for spec in entity.slots:
            if spec.slot_name == "id":
                kwargs[spec.attr_name] = envelope.get("id", data.get("id"))
                continue
            if spec.slot_name == "is_available":
                kwargs[spec.attr_name] = bool(data.get("is_available", True))
                continue
            raw = data.get(spec.slot_name)
            kwargs[spec.attr_name] = self._convert_out(spec, raw)

        for name in entity.computed_fields:
            kwargs[name] = envelope.get(name)

        if entity.exposed_subclasses:
            # The row's concrete class (ADR-0014 Decision 1): a subclass row
            # reached through a concrete parent renders as the parent type.
            kwargs[ENTITY_TYPE_FIELD] = str(
                envelope.get("entity_type") or class_name
            )

        return entity.gql_type(**kwargs)

    def input_to_dict(self, class_name: str, data: Any, mode: str) -> dict[str, Any]:
        """Generated input instance → plain dict for ``MosaicClient`` writes.

        UNSET fields are dropped (absent from the write payload); enum
        members are flattened to their LinkML string values.
        """
        entity = self.entities[class_name]
        specs = entity.create_specs if mode == "create" else entity.update_specs
        out: dict[str, Any] = {}
        for spec in specs:
            value = getattr(data, spec.attr_name, strawberry.UNSET)
            if value is strawberry.UNSET or value is None:
                continue
            if spec.kind == "enum":
                if spec.multivalued:
                    value = [getattr(v, "value", v) for v in value]
                else:
                    value = getattr(value, "value", value)
            elif spec.kind == "reference" or spec.scalar_type in (
                ISODate,
                ISODateTime,
                ISOTime,
            ):
                value = [str(v) for v in value] if spec.multivalued else str(value)
            out[spec.slot_name] = value
        return out

    # -- generation passes -------------------------------------------------

    def _select_entities(self) -> None:
        # The exposed class set is the type model's key set — the same
        # selection the typed client renders (exposed_class_names).
        for class_name, model in self.type_model.items():
            self.entities[class_name] = EntityGraphQLInfo(
                class_name=class_name,
                singular_name=snake_case(class_name),
                plural_name=model.accessor_name,
                model=model,
            )
        for class_name, entity in self.entities.items():
            entity.exposed_subclasses = [
                c for c in self._registry.concrete_closure(class_name)
                if c != class_name and c in self.entities
            ]

    def _build_enums(self) -> None:
        # Enum membership comes from the type model's slot classification
        # (SlotKind.ENUM + enum_values). The enum *description* is
        # rendering metadata the model does not carry, so it is looked
        # up from the schema view.
        enum_defs = self._registry.schema_view.all_enums() or {}
        for model in self.type_model.values():
            for slot in model.fields:
                if slot.kind is not SlotKind.ENUM or slot.enum_name in self.enums:
                    continue
                members = {
                    _enum_member_name(str(value)): str(value)
                    for value in slot.enum_values
                }
                if not members:
                    continue
                py_enum = enum.Enum(slot.enum_name, members)  # type: ignore[misc]
                enum_def = enum_defs.get(slot.enum_name)
                self.enums[slot.enum_name] = strawberry.enum(  # type: ignore[assignment]
                    py_enum,
                    name=slot.enum_name,
                    description=(
                        enum_def.description if enum_def is not None else None
                    )
                    or None,
                )

    def _slot_spec(self, slot: SlotModel) -> SlotSpec:
        """Render one type-model slot into a :class:`SlotSpec`.

        Reference resolvability is provisional here; final collision
        checks against sibling attribute names happen in
        :meth:`_build_object_types`.
        """
        attr = _safe_attr(slot.name)

        if slot.kind is SlotKind.ENUM and slot.enum_name in self.enums:
            return SlotSpec(
                slot_name=slot.name,
                attr_name=attr,
                kind="enum",
                multivalued=slot.multivalued,
                required=slot.required,
                has_default=slot.has_default,
                enum_cls=self.enums[slot.enum_name],
                description=slot.description or None,
            )

        if slot.kind is SlotKind.STRUCTURED:
            # Inline value type (issue #48, e.g. ExternalReference): the
            # stored value is the structured object itself, not a UUID —
            # rendered as a JSON passthrough scalar in both directions.
            description = f"Inline {slot.target_class or slot.range} value."
            if slot.is_external_xref:
                description += (
                    " Reverse-lookup key (hippo_external_xref): "
                    "(system, value) is globally unique among available "
                    "entities; see the findByXref query."
                )
            return SlotSpec(
                slot_name=slot.name,
                attr_name=attr,
                kind="scalar",
                multivalued=slot.multivalued,
                required=slot.required,
                has_default=slot.has_default,
                scalar_type=JSON,
                description=description,
            )

        if slot.kind is SlotKind.REFERENCE:
            # Relationship slot. Stored value is a UUID (sec9 §9.5). The
            # raw field keeps/derives an ``*_id`` name; the resolved field
            # uses the natural slot name when the target has a generated
            # type, enabling graph traversal in one query.
            resolvable = slot.target_class in self.entities
            if slot.multivalued:
                raw = slot.name if slot.name.endswith("_ids") else f"{slot.name}_ids"
                resolved = (
                    f"{slot.name[: -len('_ids')]}s"  # sample_ids -> samples
                    if slot.name.endswith("_ids")
                    else slot.name
                )
            else:
                raw = slot.name if slot.name.endswith("_id") else f"{slot.name}_id"
                resolved = (
                    slot.name[: -len("_id")]
                    if slot.name.endswith("_id")
                    else slot.name
                )
            raw_attr = _safe_attr(raw)
            resolved_attr = _safe_attr(resolved)
            if resolved_attr == raw_attr:
                resolvable = False  # name collision — raw ID field only
            return SlotSpec(
                slot_name=slot.name,
                attr_name=raw_attr,
                kind="reference",
                multivalued=slot.multivalued,
                required=slot.required,
                has_default=slot.has_default,
                target_class=slot.target_class,
                resolvable=resolvable,
                resolved_attr=resolved_attr if resolvable else None,
                description=slot.description or None,
                inverse_of=slot.inverse_of,
            )

        # SlotKind.SCALAR — including an ENUM whose definition has no
        # permissible values (no GraphQL enum can be built for it).
        scalar = SCALAR_RANGE_MAP.get(slot.range, str)
        return SlotSpec(
            slot_name=slot.name,
            attr_name=attr,
            kind="scalar",
            multivalued=slot.multivalued,
            required=slot.required,
            has_default=slot.has_default,
            scalar_type=scalar,
            description=slot.description or None,
            base_range=self._registry.base_scalar_range(slot.range),
        )

    def _output_annotation(self, spec: SlotSpec) -> Any:
        if spec.slot_name == "id":
            return strawberry.ID
        if spec.slot_name == "is_available":
            return bool
        if spec.kind == "reference":
            if spec.resolvable:
                # Edge-only (ADR-0005): the raw foreign-key id is a hidden
                # carrier the resolved-field resolver reads — never an exposed
                # GraphQL field. Only the resolved relationship is in the schema,
                # so a physical id never crosses the API boundary.
                carrier: Any = list[str] if spec.multivalued else str
                return strawberry.Private[Optional[carrier]]
            # Unresolvable target (abstract/polymorphic base with no generated
            # type): retain the raw *_id field so the reference isn't lost.
            # See _build_object_types for the rationale + build-time warning.
            base: Any = strawberry.ID
        elif spec.kind == "enum":
            base = spec.enum_cls
        else:
            base = spec.scalar_type
        if spec.multivalued:
            return Optional[list[base]]
        if spec.required and not spec.has_default:
            return base
        return Optional[base]

    def _build_object_types(self) -> None:
        # Pass 1 — bare classes so relationship annotations can use real
        # class objects (handles cycles and self-references).
        bare: dict[str, type] = {
            name: type(name, (), {}) for name in self.entities
        }

        # Pass 2 — attach fields and decorate.
        for class_name, entity in self.entities.items():
            cls = bare[class_name]
            annotations: dict[str, Any] = {}
            model = entity.model
            assert model is not None

            specs = [self._slot_spec(slot) for slot in model.fields]
            # Edge-only reference emission (ADR-0005): every reference renders
            # as its resolved relationship field; the raw id is a hidden carrier
            # (see _output_annotation). Two cases can't produce an edge and are
            # handled explicitly rather than silently downgraded to a raw-id
            # field:
            #   * a resolved-field NAME COLLISION with a sibling → build error
            #     (fail loud; the schema is ambiguous);
            #   * a target with NO generated type (abstract/polymorphic base)
            #     → retain the raw *_id field as an interim, and warn.
            used = {s.attr_name for s in specs}
            for spec in specs:
                if spec.kind != "reference":
                    continue
                if spec.resolvable:
                    if spec.resolved_attr in used:
                        raise ValueError(
                            f"{class_name}.{spec.slot_name}: the resolved "
                            f"relationship field '{spec.resolved_attr}' collides "
                            f"with another field on {class_name}. Rename the slot "
                            f"to disambiguate (edge-only reference emission, "
                            f"ADR-0005 — no silent raw-id fallback)."
                        )
                    used.add(spec.resolved_attr)  # type: ignore[arg-type]
                    if spec.multivalued:
                        count_attr = f"{spec.resolved_attr}_count"
                        if count_attr in used:
                            raise ValueError(
                                f"{class_name}.{spec.slot_name}: the cardinality "
                                f"field '{count_attr}' collides with another "
                                f"field on {class_name}. Rename the slot to "
                                f"disambiguate (issue #132)."
                            )
                        used.add(count_attr)
                elif spec.target_class in self.entities:
                    # resolvable was cleared for a name degeneracy even though
                    # the target IS exposed — same ambiguity, fail loud.
                    raise ValueError(
                        f"{class_name}.{spec.slot_name}: cannot derive a distinct "
                        f"resolved relationship field name for reference target "
                        f"'{spec.target_class}' (edge-only, ADR-0005). Rename the "
                        f"slot to disambiguate."
                    )
                else:
                    # Abstract/polymorphic base (ADR-0003) has no generated
                    # object type to resolve to. Keep the raw *_id field as an
                    # interim so the reference is not dropped; edge-only covers
                    # resolvable references, and interface/union-typed
                    # polymorphic references are future work.
                    warnings.warn(
                        f"{class_name}.{spec.slot_name}: reference target "
                        f"'{spec.target_class}' has no generated GraphQL type "
                        f"(abstract/polymorphic base); retaining the raw "
                        f"'{spec.attr_name}' id field as an interim "
                        f"(edge-only emission, ADR-0005).",
                        stacklevel=2,
                    )

            for spec in specs:
                entity.slots.append(spec)
                annotations[spec.attr_name] = self._output_annotation(spec)
                setattr(cls, spec.attr_name, None)
                if spec.kind == "reference" and spec.resolvable:
                    target = bare[spec.target_class]  # type: ignore[index]
                    resolver = self._make_reference_resolver(spec, target)
                    setattr(
                        cls,
                        spec.resolved_attr,  # type: ignore[arg-type]
                        strawberry.field(
                            resolver=resolver,
                            description=(
                                f"Resolved {spec.target_class} for "
                                f"`{spec.slot_name}` (graph traversal; "
                                f"batched per request)."
                            ),
                        ),
                    )
                    if spec.multivalued:
                        count_attr = f"{spec.resolved_attr}_count"
                        count_resolver = self._make_reference_count_resolver(
                            spec, class_name
                        )
                        annotations[count_attr] = int
                        setattr(
                            cls,
                            count_attr,
                            strawberry.field(
                                resolver=count_resolver,
                                description=(
                                    f"Cardinality of `{spec.slot_name}` "
                                    f"without resolving its member objects "
                                    f"— a single indexed COUNT(*) over the "
                                    f"relationship edges (issue #132)."
                                ),
                            ),
                        )

            slot_names = {s.slot_name for s in entity.slots}
            for name, annotation in COMPUTED_FIELDS:
                if name in slot_names:
                    continue
                annotations[name] = annotation
                setattr(cls, name, None)
                entity.computed_fields.append(name)

            if entity.exposed_subclasses:
                if ENTITY_TYPE_FIELD in slot_names:
                    raise ValueError(
                        f"{class_name}.{ENTITY_TYPE_FIELD}: the slot collides "
                        f"with the entityType system field a concrete parent "
                        f"carries (ADR-0014 Decision 1). Rename the slot."
                    )
                annotations[ENTITY_TYPE_FIELD] = str
                setattr(
                    cls,
                    ENTITY_TYPE_FIELD,
                    strawberry.field(
                        default=class_name,
                        description=(
                            f"Concrete class of this row: {class_name} or "
                            f"one of its subclasses "
                            f"({', '.join(entity.exposed_subclasses)}), whose "
                            f"rows this type also renders (ADR-0014). Query "
                            f"the subclass's own collection for its slots."
                        ),
                    ),
                )

            cls.__annotations__ = annotations
            entity.gql_type = strawberry.type(
                cls, description=model.description or None
            )

    def _make_reference_resolver(self, spec: SlotSpec, target: type) -> Any:
        builder = self
        target_class: str = spec.target_class  # type: ignore[assignment]
        raw_attr = spec.attr_name

        if spec.multivalued:

            async def resolver(self, info: Info):  # type: ignore[no-untyped-def]
                ids = getattr(self, raw_attr) or []
                loader = get_entity_loader(info.context, target_class)
                envelopes = await loader.load_many([str(i) for i in ids])
                return [
                    builder.instance_from_envelope(target_class, env)
                    for env in envelopes
                    if env is not None
                ]

            resolver.__annotations__["return"] = list[target]  # type: ignore[valid-type]
        else:

            async def resolver(self, info: Info):  # type: ignore[no-untyped-def]
                ref_id = getattr(self, raw_attr)
                if not ref_id:
                    return None
                loader = get_entity_loader(info.context, target_class)
                envelope = await loader.load(str(ref_id))
                if envelope is None:
                    return None
                return builder.instance_from_envelope(target_class, envelope)

            resolver.__annotations__["return"] = Optional[target]
        return resolver

    def _make_reference_count_resolver(self, spec: SlotSpec, class_name: str) -> Any:
        edge = spec.slot_name

        async def resolver(self, info: Info) -> int:  # type: ignore[no-untyped-def]
            client = info.context["client"]
            return client.count_relationship(class_name, str(self.id), edge)

        return resolver

    def _build_page_types(self) -> None:
        for entity in self.entities.values():
            page_name = f"{entity.class_name}Page"
            page = type(page_name, (), {})
            page.__annotations__ = {
                "items": list[entity.gql_type],  # type: ignore[name-defined]
                "total": int,
                "limit": int,
                "offset": int,
            }
            entity.page_type = strawberry.type(
                page,
                description=(
                    f"Offset-paginated {entity.class_name} result set "
                    f"(mirrors the SDK's PaginatedResult)."
                ),
            )

    def _input_annotation(self, spec: SlotSpec, force_optional: bool) -> Any:
        if spec.kind == "reference":
            base: Any = strawberry.ID
        elif spec.kind == "enum":
            base = spec.enum_cls
        else:
            base = spec.scalar_type
        if spec.multivalued:
            base = list[base]
        required = spec.required and not spec.has_default and not force_optional
        # ``id`` is SDK-assigned on create; never required on inputs.
        if spec.slot_name == "id":
            required = False
        return base if required else Optional[base]

    def _is_input_required(self, spec: SlotSpec, force_optional: bool) -> bool:
        if force_optional or spec.slot_name == "id":
            return False
        return spec.required and not spec.has_default

    def _build_one_input(
        self, entity: EntityGraphQLInfo, suffix: str, force_optional: bool
    ) -> tuple[Any, list[SlotSpec]]:
        cls = type(f"{entity.class_name}{suffix}", (), {})
        annotations: dict[str, Any] = {}
        specs: list[SlotSpec] = []
        # Inputs use the ORIGINAL slot name for every field (including
        # references — callers pass the target UUID under the slot name)
        # because the dict handed to MosaicClient keys on slot names.
        ordered = sorted(
            entity.slots,
            key=lambda s: not self._is_input_required(s, force_optional),
        )
        for slot_spec in ordered:
            if slot_spec.inverse_of is not None:
                # ADR-0011: a virtual reverse edge is derived from the
                # forward FK on the target class — never writable here.
                continue
            input_spec = SlotSpec(
                slot_name=slot_spec.slot_name,
                attr_name=_safe_attr(slot_spec.slot_name),
                kind=slot_spec.kind,
                multivalued=slot_spec.multivalued,
                required=self._is_input_required(slot_spec, force_optional),
                has_default=slot_spec.has_default,
                target_class=slot_spec.target_class,
                enum_cls=slot_spec.enum_cls,
                scalar_type=slot_spec.scalar_type,
            )
            specs.append(input_spec)
            annotations[input_spec.attr_name] = self._input_annotation(
                slot_spec, force_optional
            )
            if not input_spec.required:
                setattr(cls, input_spec.attr_name, strawberry.UNSET)
        cls.__annotations__ = annotations
        return (
            strawberry.input(
                cls,
                description=(
                    f"{'Create' if suffix == 'CreateInput' else 'Update'} "
                    f"payload for {entity.class_name}. Relationship fields "
                    f"take the target entity UUID."
                ),
            ),
            specs,
        )

    def _build_input_types(self) -> None:
        for entity in self.entities.values():
            entity.create_input, entity.create_specs = self._build_one_input(
                entity, "CreateInput", force_optional=False
            )
            entity.update_input, entity.update_specs = self._build_one_input(
                entity, "UpdateInput", force_optional=True
            )

    # -- typed filter inputs (ADR-0006 increment 2) --------------------------

    def _build_ops_input(
        self, name: str, value_type: Any, ops: tuple[str, ...], doc: str
    ) -> type:
        """Build one shared per-kind operator input (e.g. StringFilterOps).

        Fields are the operators the kind/range supports — introspection is
        the capability contract (ADR-0006): a consumer reads exactly which
        predicates a slot takes off the schema, no side-channel.
        """
        cls = type(name, (), {})
        annotations: dict[str, Any] = {}
        for attr, op in FILTER_OP_ATTRS:
            if op not in ops:
                continue
            if op == "is_null":
                annotations[attr] = Optional[bool]
                setattr(
                    cls,
                    attr,
                    strawberry.field(
                        default=strawberry.UNSET,
                        description=(
                            "true matches entities with no stored value "
                            "for the field; false the complement."
                        ),
                    ),
                )
            elif op == "in":
                annotations[attr] = Optional[list[value_type]]
                setattr(
                    cls,
                    attr,
                    strawberry.field(
                        name="in",
                        default=strawberry.UNSET,
                        description="Matches when the field is any listed value.",
                    ),
                )
            else:
                annotations[attr] = Optional[value_type]
                setattr(cls, attr, strawberry.UNSET)
        cls.__annotations__ = annotations
        return strawberry.input(cls, description=doc)

    def _build_filter_input_types(self) -> None:
        """Generate the shared operator inputs and per-class ``<Class>Filter``
        inputs with ``and``/``or``/``not`` combinators (ADR-0006 inc. 2).

        Reference slots are deliberately absent until the relationship
        predicates land (M5a adds to-one nesting under the edge name, M5b
        adds ``some``/``none`` quantifiers) — adding them later is additive;
        exposing a placeholder now and retyping it would break consumers.
        """
        comparison_ops = ("eq", "neq", "in", "gt", "gte", "lt", "lte", "is_null")
        self._string_filter_ops = self._build_ops_input(
            "StringFilterOps",
            str,
            ("eq", "neq", "in", "contains", "is_null"),
            "Operators on string-ranged slots. `contains` is "
            "case-insensitive substring; `%`/`_` are literals.",
        )
        self._int_filter_ops = self._build_ops_input(
            "IntFilterOps", int, comparison_ops,
            "Operators on integer-ranged slots (typed comparisons).",
        )
        self._float_filter_ops = self._build_ops_input(
            "FloatFilterOps", float, comparison_ops,
            "Operators on float/double/decimal-ranged slots.",
        )
        self._boolean_filter_ops = self._build_ops_input(
            "BooleanFilterOps", bool, ("eq", "neq", "is_null"),
            "Operators on boolean-ranged slots.",
        )
        self._date_filter_ops = self._build_ops_input(
            "DateFilterOps", ISODate, comparison_ops,
            "Operators on date-ranged slots (ISO-8601 ordering).",
        )
        self._datetime_filter_ops = self._build_ops_input(
            "DateTimeFilterOps", ISODateTime, comparison_ops,
            "Operators on datetime-ranged slots (ISO-8601 ordering).",
        )
        self._time_filter_ops = self._build_ops_input(
            "TimeFilterOps", ISOTime, comparison_ops,
            "Operators on time-ranged slots (ISO-8601 ordering).",
        )
        self._json_filter_ops = self._build_ops_input(
            "JsonFilterOps", JSON, ("eq", "in", "is_null"),
            "Operators on inline structured-value slots: whole-value "
            "equality/membership only.",
        )
        self._list_filter_ops = self._build_ops_input(
            "ListFilterOps", JSON, ("eq", "is_null"),
            "Operators on inline multivalued slots: whole-list equality "
            "only (membership testing is deferred — ADR-0006).",
        )
        self._scalar_filter_ops_by_base: dict[str, type] = {
            "integer": self._int_filter_ops,
            "float": self._float_filter_ops,
            "double": self._float_filter_ops,
            "decimal": self._float_filter_ops,
            "boolean": self._boolean_filter_ops,
            "date": self._date_filter_ops,
            "datetime": self._datetime_filter_ops,
            "time": self._time_filter_ops,
        }
        self._enum_filter_ops: dict[type, type] = {
            enum_cls: self._build_ops_input(
                f"{enum_name}FilterOps",
                enum_cls,
                ("eq", "neq", "in", "is_null"),
                f"Operators on {enum_name}-ranged slots.",
            )
            for enum_name, enum_cls in self.enums.items()
        }

        # Pass 1 — bare filter classes, so the and/or/not combinators can
        # self-reference (and M5a can later cross-reference target filters)
        # with real class objects.
        bare: dict[str, type] = {
            name: type(f"{name}Filter", (), {}) for name in self.entities
        }

        # Per-target quantifier inputs for to-many edges (ADR-0006 M5b):
        # {some: <Target>Filter, none: <Target>Filter}, shared by every
        # multivalued edge with the same target. Built lazily against the
        # bare classes so recursion stays safe.
        quantifier_inputs: dict[str, type] = {}

        def _quantifier_input(target: str) -> type:
            if target not in quantifier_inputs:
                qcls = type(f"{target}EdgeQuantifiers", (), {})
                qcls.__annotations__ = {
                    "some": Optional[bare[target]],
                    "none": Optional[bare[target]],
                }
                setattr(
                    qcls,
                    "some",
                    strawberry.field(
                        default=strawberry.UNSET,
                        description=(
                            f"Matches entities with AT LEAST ONE live edge "
                            f"to an available {target} satisfying this "
                            f"filter."
                        ),
                    ),
                )
                setattr(
                    qcls,
                    "none",
                    strawberry.field(
                        default=strawberry.UNSET,
                        description=(
                            f"Matches entities with NO live edge to an "
                            f"available {target} satisfying this filter "
                            f"(entities with no edges at all match)."
                        ),
                    ),
                )
                quantifier_inputs[target] = strawberry.input(
                    qcls,
                    description=(
                        f"some/none quantifiers over a multivalued "
                        f"reference edge targeting {target} (ADR-0006 "
                        f"M5b). Both set AND together."
                    ),
                )
            return quantifier_inputs[target]

        # Pass 2 — attach per-slot operator fields + combinators, decorate.
        for class_name, entity in self.entities.items():
            cls = bare[class_name]
            annotations: dict[str, Any] = {}
            fields: list[tuple[str, SlotSpec]] = []
            edges: list = []
            for spec in entity.slots:
                if spec.kind == "reference":
                    # Relationship predicates (ADR-0006 M5a/M5b): a to-one
                    # edge nests the TARGET's filter under the resolved
                    # edge name — `where: {donor: {age: {gt: 60}}}` (one
                    # correlated EXISTS on the FK column); a to-many
                    # (relationship-backed multivalued) edge nests the
                    # some/none quantifier object —
                    # `where: {samples: {some: {...}}}` (EXISTS/NOT EXISTS
                    # over the link table). Bare classes make the
                    # cross-references safe.
                    if spec.target_class not in bare:
                        continue  # target type not exposed: no filter to nest
                    attr = spec.resolved_attr or spec.attr_name
                    if attr in annotations or attr in {"and_", "or_", "not_"}:
                        warnings.warn(
                            f"{class_name}.{spec.slot_name}: edge name "
                            f"collides on {class_name}Filter; omitted."
                        )
                        continue
                    if spec.multivalued:
                        annotations[attr] = Optional[
                            _quantifier_input(spec.target_class)
                        ]
                        description = (
                            f"Quantified predicate over the "
                            f"{spec.slot_name} edges (ADR-0006 M5b): some "
                            f"= at least one linked {spec.target_class} "
                            f"matches; none = no linked "
                            f"{spec.target_class} matches. Not combinable "
                            f"with asOf."
                        )
                    else:
                        annotations[attr] = Optional[bare[spec.target_class]]
                        description = (
                            f"Matches entities whose {spec.slot_name} "
                            f"target exists, is available, and satisfies "
                            f"this {spec.target_class} filter (ADR-0006 "
                            f"M5a). Not combinable with asOf."
                        )
                    setattr(
                        cls,
                        attr,
                        strawberry.field(
                            default=strawberry.UNSET,
                            description=description,
                        ),
                    )
                    edges.append(
                        (
                            attr,
                            spec,
                            self.entities[spec.target_class],
                            spec.multivalued,
                        )
                    )
                    continue
                ops_input = self._filter_ops_input_for(spec)
                if ops_input is None:
                    continue
                attr = spec.attr_name
                if attr in {"and_", "or_", "not_"}:
                    warnings.warn(
                        f"{class_name}.{spec.slot_name}: slot collides with "
                        f"a filter combinator name; omitted from "
                        f"{class_name}Filter."
                    )
                    continue
                annotations[attr] = Optional[ops_input]
                setattr(
                    cls,
                    attr,
                    strawberry.field(
                        default=strawberry.UNSET,
                        description=spec.description or None,
                    ),
                )
                fields.append((attr, spec))
            annotations["and_"] = Optional[list[cls]]
            setattr(
                cls,
                "and_",
                strawberry.field(
                    name="and",
                    default=strawberry.UNSET,
                    description="All sub-filters must match.",
                ),
            )
            annotations["or_"] = Optional[list[cls]]
            setattr(
                cls,
                "or_",
                strawberry.field(
                    name="or",
                    default=strawberry.UNSET,
                    description="At least one sub-filter must match.",
                ),
            )
            annotations["not_"] = Optional[cls]
            setattr(
                cls,
                "not_",
                strawberry.field(
                    name="not",
                    default=strawberry.UNSET,
                    description="Negates the sub-filter (two-valued: an "
                    "entity missing the field satisfies the negation).",
                ),
            )
            cls.__annotations__ = annotations
            entity.filter_fields = fields
            entity.filter_edges = edges
            entity.filter_input = strawberry.input(
                cls,
                description=(
                    f"Typed filter for {class_name} (ADR-0006). Slot fields "
                    f"and multiple operators within one field AND together; "
                    f"nest boolean structure with and/or/not. To-one "
                    f"reference edges nest the target type's filter under "
                    f"the edge name (M5a — one correlated EXISTS); "
                    f"multivalued reference edges nest some/none "
                    f"quantifiers over the target's filter (M5b — "
                    f"EXISTS/NOT EXISTS over the link table). Relationship "
                    f"predicates are not combinable with asOf. Composes "
                    f"with `filters:` by AND."
                ),
            )

    def _build_order_enums(self) -> None:
        """Generate per-class ``<Class>OrderField`` enums (ADR-0007 inc. 3).

        Members are the class's orderable stored columns: single-valued
        scalar and enum slots (including ``id``). Excluded: multivalued
        slots (JSON arrays), reference slots (UUIDs have no meaningful
        order), inline JSON slots, and the computed temporal fields —
        those are provenance-derived, not columns, and are never column
        sorts (ADR-0007's pinned constraint). Member values are LinkML
        slot names, so introspection doubles as the aggregation-field
        capability contract.
        """
        for entity in self.entities.values():
            # SCREAMING_SNAKE member names (GraphQL enum convention, like
            # FilterOp); values stay the LinkML slot names.
            members = {
                _enum_member_name(spec.slot_name).upper(): spec.slot_name
                for spec in entity.slots
                if not spec.multivalued
                and spec.kind != "reference"
                and spec.scalar_type is not JSON
            }
            if not members:
                continue  # unreachable in practice: `id` is always a slot
            py_enum = enum.Enum(  # type: ignore[misc]
                f"{entity.class_name}OrderField", members
            )
            entity.order_field_enum = strawberry.enum(
                py_enum,
                name=f"{entity.class_name}OrderField",
                description=(
                    f"Orderable stored columns of {entity.class_name} "
                    f"(ADR-0007). Computed temporal fields (createdAt/"
                    f"updatedAt) are provenance-derived, not columns, and "
                    f"are not orderable."
                ),
            )

    def _filter_ops_input_for(self, spec: SlotSpec) -> Optional[type]:
        """Pick the operator input for one slot — or None to omit the slot
        (references, until M5a/M5b land)."""
        if spec.kind == "reference":
            return None
        if spec.multivalued:
            return self._list_filter_ops
        if spec.kind == "enum":
            return self._enum_filter_ops.get(spec.enum_cls)
        if spec.scalar_type is JSON:
            return self._json_filter_ops
        base = spec.base_range or "string"
        return self._scalar_filter_ops_by_base.get(
            base, self._string_filter_ops
        )

    # -- conversion helpers --------------------------------------------------

    def _convert_out(self, spec: SlotSpec, raw: Any) -> Any:
        if raw is None:
            return [] if spec.multivalued and spec.required else None
        if spec.kind == "enum":
            enum_cls = spec.enum_cls
            if spec.multivalued:
                return [enum_cls(v) for v in raw]  # type: ignore[misc]
            return enum_cls(raw)  # type: ignore[misc]
        if spec.kind == "reference":
            if spec.multivalued:
                values = raw if isinstance(raw, list) else [raw]
                return [str(v) for v in values]
            return str(raw)
        if spec.multivalued and not isinstance(raw, list):
            return [raw]
        return raw
