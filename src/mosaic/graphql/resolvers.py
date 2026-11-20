"""Query/Mutation generation and SDK delegation for the GraphQL transport.

Every resolver is a THIN wrapper over ``MosaicClient`` — the same
SDK-first contract the REST routers follow (sec2 §2.7, sec4 §4.1).
Resolvers never touch storage adapters or validation pipelines
directly; they translate GraphQL arguments to SDK calls and SDK
envelopes/errors back to GraphQL shapes.

Error mapping: SDK validation failures surface as structured GraphQL
errors with ``extensions.code = "VALIDATION_FAILED"`` and, when the
sec9 §9.9 envelope is available, the tier-annotated ``failures`` list.
Missing entities map to ``extensions.code = "NOT_FOUND"``.
"""

from __future__ import annotations

import enum
import os
from typing import Annotated, Any, Optional

import strawberry
from graphql import GraphQLError
from strawberry.extensions import QueryDepthLimiter
from strawberry.scalars import JSON
from strawberry.tools import create_type
from strawberry.types import Info

from mosaic.core.converse_query_spec import EXON_URL_ENV, run_converse_turn
from mosaic.core.converse_query_spec import exon_timeout as _exon_timeout
from mosaic.core.exceptions import (
    EntityAlreadySupersededError,
    EntityNotFoundError,
    EntityTypeConflictError,
    ValidationError as MosaicValidationError,
    ValidationFailed,
    ValidationFailure,
)
from mosaic.core.schema_typing import (
    EntityTypeModel,
    FamilyTypeModel,
    build_capability_manifest,
)
from mosaic.core.storage import has_relationship_predicate
from mosaic.core.validation.validators import WriteOperation
from mosaic.graphql import DEFAULT_MAX_QUERY_DEPTH
from mosaic.graphql.schema_builder import (
    FILTER_OP_ATTRS,
    EntityGraphQLInfo,
    GraphQLTypeBuilder,
    ISODateTime,
    SlotSpec,
    camel_case,
)
from mosaic.linkml_bridge import SchemaRegistry


@strawberry.enum(description="How multiple filters compose (SDK filter_mode).")
class FilterMode(enum.Enum):
    AND = "and"
    OR = "or"


@strawberry.enum(
    description=(
        "Predicate applied to a filter's field/value (SDK filter op). "
        "Which operators a given slot supports depends on its kind/range "
        "(ADR-0006): comparisons (GT/GTE/LT/LTE) on numeric and temporal "
        "slots, CONTAINS (case-insensitive substring) on string slots, "
        "IS_NULL everywhere. An operator outside a slot's set is a coded "
        "error, never a silently-wrong result (issue #129)."
    )
)
class FilterOp(enum.Enum):
    EQ = "eq"
    IN = "in"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"
    IS_NULL = "is_null"


#: Base LinkML ranges whose values order meaningfully — comparison
#: operators push down as typed SQL predicates for these (ADR-0006).
_ORDERED_RANGES = frozenset(
    {"integer", "float", "double", "decimal", "date", "datetime", "time"}
)


@strawberry.enum(description="Sort direction for orderBy (ADR-0007).")
class OrderDirection(enum.Enum):
    ASC = "asc"
    DESC = "desc"


@strawberry.type(
    description=(
        "One facet bucket: a stored value of the requested field and how "
        "many matching entities carry it (ADR-0007). Entities with no "
        "stored value are not counted — query absence with IS_NULL. "
        "Availability-consistent: buckets sum over exactly the entities "
        "the list query would return under the same filters."
    )
)
class FacetCount:
    value: JSON
    count: int


@strawberry.type(
    description=(
        "Min/max of a field under a filter, for range facets (ADR-0007). "
        "Both null when no matching entity has a stored value. "
        "Availability-consistent with the list surface."
    )
)
class FieldRange:
    min: Optional[JSON]
    max: Optional[JSON]


def _allowed_filter_ops(spec: "SlotSpec") -> set[str]:
    """Operator set a slot's kind/range supports (ADR-0006).

    Multivalued reference slots never reach here (rejected earlier as
    UNFILTERABLE_FIELD until M5b lands).
    """
    if spec.kind == "reference":
        # Single-valued reference: the stored value is the target UUID.
        return {"eq", "neq", "in", "is_null"}
    if spec.multivalued:
        # Inline multivalued slot (JSON-array column). Membership
        # ('contains') is deferred — ADR-0006 open note.
        return {"eq", "is_null"}
    if spec.kind == "enum":
        return {"eq", "neq", "in", "is_null"}
    if spec.scalar_type is JSON:
        # Structured inline value (issue #48): whole-value equality only.
        return {"eq", "in", "is_null"}
    base = spec.base_range or "string"
    if base in _ORDERED_RANGES:
        return {"eq", "neq", "in", "gt", "gte", "lt", "lte", "is_null"}
    if base == "boolean":
        return {"eq", "neq", "is_null"}
    # Strings and other text-like scalars.
    return {"eq", "neq", "in", "contains", "is_null"}


@strawberry.input(
    description=(
        "Field/value filter (SDK query filter). ``field`` is a LinkML slot "
        "name (snake_case, as listed by `mosaicSchema`); the equivalent "
        "camelCase spelling exposed on the entity type is also accepted. An "
        "unrecognized name is an error, never an empty result (issue #149). "
        "``op`` defaults to EQ (equality); IN treats ``value`` as a list and "
        "matches when the field is a member of it (issue #102). Comparison "
        "and null-test operators (ADR-0006): GT/GTE/LT/LTE on numeric and "
        "temporal slots, CONTAINS (case-insensitive substring) on string "
        "slots, NEQ on comparable slots, IS_NULL (boolean ``value``; true "
        "matches entities with no stored value) everywhere. ``value: null`` "
        "is rejected — absence is asked with IS_NULL, never with EQ null."
    )
)
class FilterInput:
    field: str
    value: JSON
    op: FilterOp = FilterOp.EQ


@strawberry.type(
    description=(
        "One provenance log entry for an entity (append-only audit "
        "trail; see sec6/sec9 §9.6)."
    )
)
class ProvenanceEntry:
    operation_id: Optional[strawberry.ID]
    entity_id: Optional[strawberry.ID]
    entity_type: Optional[str]
    operation: Optional[str]
    timestamp: Optional[ISODateTime]
    actor_id: Optional[str]
    patch: Optional[JSON]


@strawberry.type(
    description=(
        "Result of an availability transition. Mosaic never hard-deletes; "
        "lifecycle status drivers (active/archived/superseded/deleted/"
        "distributed/removed) are recorded in provenance (sec3, sec9 §9.5)."
    )
)
class AvailabilityResult:
    entity_id: strawberry.ID
    is_available: bool


@strawberry.type(description="One per-entity failure from a bulk availability change.")
class BulkAvailabilityFailure:
    entity_id: strawberry.ID
    error: str


@strawberry.type(
    description=(
        "Summary of a bulk availability change (mirrors REST "
        "POST /entities/{type}/bulk-availability): per-record error "
        "isolation — failures never roll back sibling successes."
    )
)
class BulkAvailabilityResult:
    total: int
    succeeded: int
    failed: int
    successes: list[AvailabilityResult]
    failures: list[BulkAvailabilityFailure]


@strawberry.type(
    description=(
        "Entity resolved from an external reference (system, value) pair "
        "via the mosaic_external_xref reverse-lookup index (issue #48). "
        "Generic envelope shape — the matching entity type is only known "
        "at query time, so `data` carries the typed payload as JSON; use "
        "`entityType` with the per-type queries for typed traversal."
    )
)
class XrefMatch:
    entity_id: strawberry.ID
    entity_type: str
    data: JSON
    version: Optional[int]
    created_at: Optional[ISODateTime]
    updated_at: Optional[ISODateTime]


@strawberry.type(
    description=(
        "An entity that references the queried entity through a "
        "relationships-table-backed multivalued slot (ADR-0002) — the "
        "reverse of forward multivalued-reference resolution (issue "
        "#146). Generic envelope shape — the referencing entity's type "
        "is only known at query time, so `data` carries the typed "
        "payload as JSON; use `entityType` with the per-type queries "
        "for typed traversal."
    )
)
class RelatedEntity:
    entity_id: strawberry.ID
    entity_type: str
    relationship_type: str
    data: JSON
    version: Optional[int]
    created_at: Optional[ISODateTime]
    updated_at: Optional[ISODateTime]


@strawberry.type(
    description=(
        "One cross-class search hit (issue #158): a heterogeneous JSON "
        "envelope — the matching entity's type is only known at query "
        "time, so `data` carries the typed payload as JSON; use "
        "`entityType` with the per-type queries for typed traversal. "
        "`score` is the FTS rank, normalized to [0, 1] per index "
        "(comparable in relative terms across classes)."
    )
)
class SearchHit:
    entity_id: strawberry.ID
    entity_type: str
    score: float
    data: JSON
    version: Optional[int]
    created_at: Optional[ISODateTime]
    updated_at: Optional[ISODateTime]


@strawberry.type(
    description=(
        "One node of a neighborhood subgraph (issue #158) — the same "
        "heterogeneous JSON envelope as SearchHit/XrefMatch."
    )
)
class GraphNode:
    entity_id: strawberry.ID
    entity_type: Optional[str]
    data: JSON
    created_at: Optional[ISODateTime]
    updated_at: Optional[ISODateTime]


@strawberry.type(
    description=(
        "One directed edge of a neighborhood subgraph. `type` is the "
        "referencing slot / relationship type; `edgeSource` names the "
        "store it came from — LINK_TABLE (relationship-backed multivalued "
        "references, ADR-0002) or COLUMN (single-valued reference slots)."
    )
)
class GraphEdge:
    source: strawberry.ID
    target: strawberry.ID
    type: str
    edge_source: str


@strawberry.type(
    description=(
        "A depth-bounded subgraph around one entity (issue #158): "
        "renderable without further queries. `edgeSources` names the edge "
        "stores covered by this response; `notices` discloses any bound "
        "hit (node budget) or scoped-out coverage (column edges under "
        "asOf) — the graph is never silently partial."
    )
)
class NeighborhoodGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    edge_sources: list[str]
    notices: list[str]


@strawberry.type(description="Result of a supersede operation.")
class SupersedeResult:
    entity_id: strawberry.ID
    superseded_by: strawberry.ID


@strawberry.type(
    description=(
        "Entity-level supersession state (mirrors REST GET "
        "/entities/{id}/superseded). `supersededBy` is the direct "
        "replacement (null when the entity is current); `chain` follows "
        "replacement links forward to the terminal replacement."
    )
)
class SupersessionInfo:
    entity_id: strawberry.ID
    superseded_by: Optional[strawberry.ID]
    chain: list[strawberry.ID]


# ---------------------------------------------------------------------------
# Batch unit-of-work (issue #84): whole-set dry-run validation + atomic
# multi-entity write. Cross-type, so these are root mutations with generic
# JSON payloads (each entity's typed shape is only known at runtime).
# ---------------------------------------------------------------------------


@strawberry.input(description="One entity in a batch validate/write request.")
class BatchEntityInput:
    entity_type: str
    data: JSON
    operation: str = "insert"


@strawberry.input(
    description=(
        "One intra-batch relationship edge, created after the entities "
        "within the same atomic transaction. Source/target may reference "
        "entities created earlier in the same batch."
    )
)
class BatchRelationshipInput:
    source_id: strawberry.ID
    target_id: strawberry.ID
    relationship_type: str
    metadata: Optional[JSON] = None


@strawberry.type(description="One tier-annotated validation failure (sec9 §9.9).")
class ValidationFailureType:
    tier: str
    rule: str
    message: str
    field: Optional[str] = None
    details: Optional[JSON] = None


@strawberry.type(description="Per-entity validation outcome within a batch.")
class BatchEntityValidation:
    entity_id: Optional[strawberry.ID]
    passed: bool
    failures: list[ValidationFailureType]


@strawberry.type(
    description=(
        "Whole-set dry-run validation result (MosaicClient.validate_batch); "
        "aggregated per-entity, no writes."
    )
)
class BatchValidationGraphQLResult:
    passed: bool
    results: list[BatchEntityValidation]


@strawberry.type(
    description=(
        "Result of an atomic multi-entity write (MosaicClient.batch_put). "
        "The set commits all-or-nothing; `entities`/`relationships` carry the "
        "written (or, on a dry run, planned) payloads as JSON."
    )
)
class BatchWriteGraphQLResult:
    committed: bool
    dry_run: bool
    validation: BatchValidationGraphQLResult
    entities: list[JSON]
    relationships: list[JSON]


# ---------------------------------------------------------------------------
# converseQuerySpec (issue #205): the GraphQL surface for the `converse_
# query_spec` MCP tool (issue #186, ADR-0010) — registered only when
# MOSAIC_EXON_URL is set (see build_mutation_type below), the same gate the
# MCP tool uses. This is a thin transport wrapper: run_converse_turn (
# mosaic.core.converse_query_spec) owns every bit of the planning
# delegation, the ADR-0010 relay terms, the in-process re-validation of
# every candidate spec, and the suspend/recompute semantics -- nothing
# here reimplements any of it.
# ---------------------------------------------------------------------------


@strawberry.type(
    description=(
        "One turn in a converseQuerySpec conversation (issue #186/#205). "
        "`status` is one of 'proposal' (an updated, Mosaic-validated "
        "QuerySpec plus a plain-language restatement of what it now "
        "means — see `query_spec`), 'clarification' (a question back, no "
        "spec change), 'suspended' (recomputed away by an edit to an "
        "earlier turn — see ConverseResult.suspended_turn_ids), or "
        "'error' (the configured planning service was unreachable, timed "
        "out, or returned something that failed Mosaic's own "
        "re-validation; nothing was applied). `id` is null ONLY for an "
        "'error' turn — an error is not a conversational step that can be "
        "rewound or edited via `editTurnId`, so it is never assigned one."
    )
)
class ConversationTurn:
    id: Annotated[
        Optional[strawberry.ID],
        strawberry.field(
            description=(
                "Null ONLY for an 'error' turn (see `status`) — an error "
                "is not a conversational step, so it is never assigned an "
                "id and can never be the target of `editTurnId`. Every "
                "other status ('proposal', 'clarification', 'suspended') "
                "always carries one. A client can key off `status == "
                "\"error\"` instead of an id's absence."
            )
        ),
    ]
    utterance: str
    status: str
    message: str
    query_spec: Optional[JSON]


@strawberry.input(
    description=(
        "One turn of prior conversation state, passed back into "
        "converseQuerySpec's `turns` argument exactly as it was received "
        "from an earlier ConverseResult — mirrors ConversationTurn field "
        "for field. Always a real prior turn (proposal/clarification/"
        "suspended); an 'error' turn is never part of the authoritative "
        "conversation, so it never belongs in this list."
    )
)
class ConversationTurnInput:
    id: strawberry.ID
    utterance: str
    status: str
    message: str
    query_spec: Optional[JSON] = None


@strawberry.type(
    description=(
        "converseQuerySpec's result envelope (issue #186/#205, ADR-0010). "
        "`turns` is ALWAYS the full conversation so far, including on the "
        "error path — a deliberate GraphQL-side choice (issue #205): the "
        "in-process handler this wraps omits its own `turns` on every "
        "failure (and on a success from a planning service that predates "
        "the field), meaning 'nothing changed, keep what you had' — but a "
        "GraphQL client cannot distinguish an omitted field the way that "
        "handler's own caller can, and this field is non-null. So "
        "whenever the handler omits `turns`, converseQuerySpec fills it "
        "in with the caller's OWN input `turns` unchanged (never an empty "
        "list unless the caller's input `turns` was itself empty, and "
        "never absent)."
    )
)
class ConverseResult:
    turn: ConversationTurn
    turns: list[ConversationTurn]
    suspended_turn_ids: list[strawberry.ID]


def _turn_input_to_dict(turn: ConversationTurnInput) -> dict[str, Any]:
    return {
        "id": turn.id,
        "utterance": turn.utterance,
        "status": turn.status,
        "query_spec": turn.query_spec,
        "message": turn.message,
    }


def _turn_dict_to_graphql(turn: dict[str, Any]) -> ConversationTurn:
    return ConversationTurn(
        id=turn.get("id"),
        utterance=turn["utterance"],
        status=turn["status"],
        message=turn["message"],
        query_spec=turn.get("query_spec"),
    )


def _make_converse_query_spec_resolver(exon_endpoint: str, timeout: float):
    """Builds the converseQuerySpec resolver bound to the endpoint/timeout
    read ONCE at schema-build time (mirrors the MCP tool: the whole
    delegate configuration has one lifecycle, fixed at mount/build time,
    not re-read per request)."""

    def resolver(
        info: Info,
        utterance: str,
        query_spec: Optional[JSON] = None,
        turns: Optional[list[ConversationTurnInput]] = None,
        edit_turn_id: Optional[strawberry.ID] = None,
    ) -> ConverseResult:
        client = _client(info)
        manifest = build_capability_manifest(client.registry)
        input_turns = [_turn_input_to_dict(t) for t in (turns or [])]
        result = run_converse_turn(
            manifest=manifest,
            exon_endpoint=exon_endpoint,
            timeout=timeout,
            utterance=utterance,
            query_spec=query_spec,
            turns=input_turns,
            edit_turn_id=str(edit_turn_id) if edit_turn_id is not None else None,
        )
        # The design decision this field's docstring describes: when the
        # handler omits `turns` (every failure path, and a legacy-planner
        # success), echo back the caller's own input unchanged rather than
        # `[]` or leaving the field absent (GraphQL's `turns` is non-null).
        turns_out = result.get("turns")
        if turns_out is None:
            turns_out = input_turns
        return ConverseResult(
            turn=_turn_dict_to_graphql(result["turn"]),
            turns=[_turn_dict_to_graphql(t) for t in turns_out],
            suspended_turn_ids=list(result.get("suspended_turn_ids") or []),
        )

    resolver.__name__ = "converse_query_spec"
    resolver.__doc__ = (
        "Build a QuerySpec conversationally, one turn at a time (issue "
        "#186/#205): send the user's new utterance plus the conversation "
        "so far, get back a single turn. A turn is either a 'proposal' "
        "(an updated, Mosaic-validated QuerySpec plus a plain-language "
        "restatement of what it now means) or a 'clarification' (a "
        "question back, no spec change), or 'error' if the planning "
        "service is unreachable or returned something invalid. Pass "
        "'editTurnId' to redo an EARLIER turn with a new utterance -- "
        "turns after it are recomputed, and any that no longer make "
        "sense come back in 'suspendedTurnIds' for the user to re-prompt "
        "rather than being silently dropped. Stateless: the caller owns "
        "the turn list and must pass it back each call; nothing is "
        "persisted here. ALWAYS replace your turn list with the "
        "returned 'turns' -- it is the authoritative conversation after "
        "the call (see ConverseResult.turns for what happens on error). "
        "This never executes anything -- hand a returned proposal to an "
        "execute mutation/query only on an explicit user action. Present "
        "only when this deployment has MOSAIC_EXON_URL configured."
    )
    return resolver


# ---------------------------------------------------------------------------
# Mosaic schema introspection (the LinkML type model — distinct from
# GraphQL's own __schema introspection; mirrors REST GET /schemas and
# GET /schemas/{type}/references).
# ---------------------------------------------------------------------------


@strawberry.type(
    description=(
        "One slot of an entity type, as classified by the shared LinkML "
        "type model (mosaic.core.schema_typing)."
    )
)
class MosaicSlotInfo:
    name: str
    kind: str  # scalar | enum | reference
    range: str  # raw LinkML range
    role: str  # user | system
    required: bool
    multivalued: bool
    identifier: bool
    has_default: bool
    description: Optional[str]
    target_entity_type: Optional[str]  # set when kind == reference
    enum_name: Optional[str]
    enum_values: list[str]
    is_external_xref: bool
    #: ADR-0011: set when this reference is a virtual reverse edge — names
    #: the forward FK slot on ``target_entity_type`` it derives from.
    inverse_of: Optional[str] = None


@strawberry.type(
    description=(
        "One relationship of an entity type (mirrors REST GET "
        "/schemas/{type}/references)."
    )
)
class MosaicReferenceInfo:
    field: str
    target_entity_type: str


@strawberry.type(
    description=(
        "One exposed entity type from the deployment's merged LinkML "
        "schema (mirrors REST GET /schemas). This is Mosaic's *domain* "
        "schema introspection — the LinkML type model the GraphQL "
        "surface itself is generated from."
    )
)
class MosaicEntityTypeInfo:
    name: str
    accessor_name: str
    description: Optional[str]
    fields: list[MosaicSlotInfo]
    relationships: list[MosaicReferenceInfo]
    #: Class hierarchy (ADR-0014 Decision 4, #228): ``abstract`` marks a
    #: family — an abstract class whose rows are its ``kinds``' rows; a
    #: family's ``fields`` are the ones every kind shares.
    abstract: bool = False
    parent: Optional[str] = None
    kinds: list[str] = strawberry.field(default_factory=list)


def _entity_type_info(
    model: EntityTypeModel, builder: Optional[GraphQLTypeBuilder] = None
) -> MosaicEntityTypeInfo:
    family = isinstance(model, FamilyTypeModel)
    parent: Optional[str] = None
    kinds: list[str] = []
    if family:
        parent = model.parent
        kinds = list(model.kinds)
    elif builder is not None:
        listed = set(builder.entities) | set(builder.families)
        ancestors = builder.registry.schema_view.class_ancestors(model.class_name) or []
        parent = next(
            (a for a in ancestors if a != model.class_name and a in listed), None
        )
        closure = [
            k for k in builder.registry.concrete_closure(model.class_name)
            if k in builder.entities
        ]
        kinds = closure if len(closure) > 1 else []
    return MosaicEntityTypeInfo(
        abstract=family,
        parent=parent,
        kinds=kinds,
        name=model.class_name,
        accessor_name=model.accessor_name,
        description=model.description,
        fields=[
            MosaicSlotInfo(
                name=slot.name,
                kind=slot.kind.value,
                range=slot.range,
                role=slot.role.value,
                required=slot.required,
                multivalued=slot.multivalued,
                identifier=slot.identifier,
                has_default=slot.has_default,
                description=slot.description,
                target_entity_type=slot.target_class,
                enum_name=slot.enum_name,
                enum_values=list(slot.enum_values),
                is_external_xref=slot.is_external_xref,
                inverse_of=slot.inverse_of,
            )
            for slot in model.fields
        ],
        relationships=[
            MosaicReferenceInfo(
                field=slot.name,
                target_entity_type=slot.target_class or slot.range,
            )
            for slot in model.relationships
        ],
    )


def _client(info: Info) -> Any:
    return info.context["client"]


def _builder(info: Info) -> GraphQLTypeBuilder:
    return info.context["builder"]


def _as_graphql_error(exc: Exception) -> GraphQLError:
    """Map SDK exceptions onto structured GraphQL errors."""
    message = getattr(exc, "message", str(exc))
    if isinstance(exc, ValidationFailed):
        extensions: dict[str, Any] = {"code": "VALIDATION_FAILED"}
        result = getattr(exc, "result", None)
        if result is not None and hasattr(result, "to_envelope"):
            extensions.update(result.to_envelope())
        return GraphQLError(message, extensions=extensions)
    if isinstance(exc, ValidationFailure):
        return GraphQLError(
            message,
            extensions={
                "code": "VALIDATION_FAILED",
                "rule_id": exc.rule_id,
                "entity_type": exc.entity_type,
                "entity_id": exc.entity_id,
            },
        )
    if isinstance(exc, MosaicValidationError):
        return GraphQLError(message, extensions={"code": "VALIDATION_FAILED"})
    if isinstance(exc, EntityAlreadySupersededError):
        return GraphQLError(message, extensions={"code": "ALREADY_SUPERSEDED"})
    if isinstance(exc, EntityTypeConflictError):
        return GraphQLError(
            message,
            extensions={
                "code": "ENTITY_TYPE_CONFLICT",
                "entity_id": exc.entity_id,
                "requested_entity_type": exc.requested_entity_type,
                "existing_entity_type": exc.existing_entity_type,
            },
        )
    if isinstance(exc, EntityNotFoundError):
        return GraphQLError(message, extensions={"code": "NOT_FOUND"})
    return GraphQLError(message, extensions={"code": "INTERNAL_ERROR"})


# ---------------------------------------------------------------------------
# Query resolvers
# ---------------------------------------------------------------------------


def _make_get_resolver(builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(info: Info, id: strawberry.ID):  # noqa: A002 - GraphQL arg
        try:
            envelope = _client(info).get(class_name, str(id))
        except EntityNotFoundError:
            return None
        return _builder(info).instance_from_envelope(class_name, envelope)

    resolver.__name__ = entity.singular_name
    resolver.__doc__ = f"Fetch one {class_name} by UUID (null when absent)."
    resolver.__annotations__["return"] = Optional[entity.gql_type]
    return resolver


def _to_sdk_filters(
    entity: EntityGraphQLInfo, filters: Optional[list[FilterInput]]
) -> list[dict[str, Any]]:
    """Translate GraphQL filter inputs into SDK query filters (issue #149).

    Storage matches on LinkML slot names; the generated type exposes their
    camelCase spellings. Both resolve here, and a name that is neither is
    rejected — the storage layer's own response to an unknown column is to
    match zero rows, which is indistinguishable from a legitimately empty
    result.
    """
    out: list[dict[str, Any]] = []
    for f in filters or []:
        spec = entity.resolve_filter_field(f.field)
        if spec is None and entity.is_computed_field(f.field):
            raise GraphQLError(
                f"{entity.class_name}.{f.field} is computed at read time "
                f"rather than being a column of {entity.class_name} "
                f"(sec9 §9.7), so it cannot be filtered on. Temporal fields "
                f"come from the provenance log — use `asOf` for "
                f"transaction-time queries.",
                extensions={"code": "UNFILTERABLE_FIELD", "field": f.field},
            )
        if spec is None:
            raise GraphQLError(
                f"Unknown filter field {f.field!r} for {entity.class_name}. "
                f"Filters match on: "
                f"{', '.join(sorted(entity.filterable_slot_names()))}.",
                extensions={"code": "UNKNOWN_FILTER_FIELD", "field": f.field},
            )
        if spec.kind == "reference" and spec.multivalued:
            raise GraphQLError(
                f"{entity.class_name}.{spec.slot_name} is a multivalued "
                f"reference, stored as relationship edges rather than a "
                f"column (ADR-0002), so the flat `filters:` list cannot "
                f"address it. Quantify over it with the typed `where:` "
                f"filter instead — some/none under the edge name (ADR-0006 "
                f"M5b) — or use `relatedTo` for reverse lookups.",
                extensions={"code": "UNFILTERABLE_FIELD", "field": f.field},
            )
        allowed = _allowed_filter_ops(spec)
        if f.op.value not in allowed:
            raise GraphQLError(
                f"Operator {f.op.name} is not supported on "
                f"{entity.class_name}.{spec.slot_name} "
                f"(slot range {spec.base_range or spec.kind!s}). Supported "
                f"operators: "
                f"{', '.join(sorted(FilterOp(o).name for o in allowed))} "
                f"(ADR-0006).",
                extensions={
                    "code": "UNSUPPORTED_FILTER_OP",
                    "field": f.field,
                    "op": f.op.name,
                },
            )
        if f.op is FilterOp.IS_NULL:
            if not isinstance(f.value, bool):
                raise GraphQLError(
                    f"IS_NULL on {entity.class_name}.{spec.slot_name} takes "
                    f"a boolean value (true = match entities with no stored "
                    f"value), got {f.value!r}.",
                    extensions={
                        "code": "INVALID_FILTER_VALUE",
                        "field": f.field,
                    },
                )
        elif f.value is None:
            raise GraphQLError(
                f"Filter value on {entity.class_name}.{spec.slot_name} is "
                f"null. GraphQL null cannot distinguish 'explicit null' "
                f"from 'absent', so equality-with-null is rejected — ask "
                f"about absence with op: IS_NULL (ADR-0006).",
                extensions={"code": "INVALID_FILTER_VALUE", "field": f.field},
            )
        elif f.op is FilterOp.IN and not isinstance(f.value, list):
            raise GraphQLError(
                f"IN on {entity.class_name}.{spec.slot_name} takes a list "
                f"of candidate values, got {type(f.value).__name__}.",
                extensions={"code": "INVALID_FILTER_VALUE", "field": f.field},
            )
        out.append({"field": spec.slot_name, "value": f.value, "op": f.op.value})
    return out


#: Maximum nesting depth of a `where:` filter input (and/or/not levels).
#: The output-side QueryDepthLimiter does not see input nesting; without a
#: cap, the recursive <Type>Filter would accept arbitrarily deep trees.
MAX_WHERE_INPUT_DEPTH = 10


def _filter_value(v: Any) -> Any:
    """GraphQL input value → SDK filter value (enum members → raw values)."""
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, list):
        return [x.value if isinstance(x, enum.Enum) else x for x in v]
    return v


def _where_to_tree(
    entity: EntityGraphQLInfo,
    node: Any,
    depth: int = 1,
    *,
    allow_empty: bool = True,
) -> Optional[dict[str, Any]]:
    """Translate a generated ``<Type>Filter`` input into the SDK's boolean
    filter tree (ADR-0006 increment 2).

    Slot fields and multiple operators within one operator object AND
    together; ``and``/``or``/``not`` nest. Because the operator inputs are
    generated per slot kind/range, operator applicability is enforced by
    GraphQL validation itself — only value-shape problems (explicit nulls,
    empty objects) and depth need runtime checks here.
    """
    if depth > MAX_WHERE_INPUT_DEPTH:
        raise GraphQLError(
            f"`where` filter exceeds the maximum nesting depth "
            f"({MAX_WHERE_INPUT_DEPTH}).",
            extensions={"code": "FILTER_TOO_DEEP"},
        )
    parts: list[dict[str, Any]] = []

    for combinator, key in (("and_", "and"), ("or_", "or")):
        children_in = getattr(node, combinator, strawberry.UNSET)
        if children_in is strawberry.UNSET or children_in is None:
            continue
        if not children_in:
            raise GraphQLError(
                f"`where.{key}` requires a non-empty list of sub-filters.",
                extensions={"code": "INVALID_FILTER_VALUE"},
            )
        children = [
            _where_to_tree(entity, c, depth + 1, allow_empty=False)
            for c in children_in
        ]
        # A one-child and/or is the child itself.
        parts.append(children[0] if len(children) == 1 else {key: children})

    not_in = getattr(node, "not_", strawberry.UNSET)
    if not_in is not strawberry.UNSET and not_in is not None:
        parts.append(
            {"not": _where_to_tree(entity, not_in, depth + 1, allow_empty=False)}
        )

    # Relationship predicates (ADR-0006 M5a/M5b): a to-one edge nests the
    # target type's filter ({edge, where}); a to-many edge nests the
    # some/none quantifier object ({edge, quantifier, where}).
    for attr, spec, target, multivalued in entity.filter_edges:
        sub_in = getattr(node, attr, strawberry.UNSET)
        if sub_in is strawberry.UNSET or sub_in is None:
            continue
        if not multivalued:
            parts.append(
                {
                    "edge": spec.slot_name,
                    "where": _where_to_tree(
                        target, sub_in, depth + 1, allow_empty=False
                    ),
                }
            )
            continue
        emitted = False
        for quantifier in ("some", "none"):
            q_in = getattr(sub_in, quantifier, strawberry.UNSET)
            if q_in is strawberry.UNSET or q_in is None:
                continue
            emitted = True
            parts.append(
                {
                    "edge": spec.slot_name,
                    "quantifier": quantifier,
                    "where": _where_to_tree(
                        target, q_in, depth + 1, allow_empty=False
                    ),
                }
            )
        if not emitted:
            raise GraphQLError(
                f"`where.{attr}` is an empty quantifier object — set "
                f"`some` and/or `none` (ADR-0006 M5b).",
                extensions={
                    "code": "INVALID_FILTER_VALUE",
                    "field": spec.slot_name,
                },
            )

    for attr, spec in entity.filter_fields:
        ops_obj = getattr(node, attr, strawberry.UNSET)
        if ops_obj is strawberry.UNSET or ops_obj is None:
            continue
        leaves: list[dict[str, Any]] = []
        for op_attr, op in FILTER_OP_ATTRS:
            v = getattr(ops_obj, op_attr, strawberry.UNSET)
            if v is strawberry.UNSET:
                continue
            if v is None:
                raise GraphQLError(
                    f"`where.{attr}.{op_attr.rstrip('_')}` is null. GraphQL "
                    f"null cannot distinguish 'explicit null' from 'absent' "
                    f"— ask about absence with isNull (ADR-0006).",
                    extensions={
                        "code": "INVALID_FILTER_VALUE",
                        "field": spec.slot_name,
                    },
                )
            leaves.append(
                {"field": spec.slot_name, "op": op, "value": _filter_value(v)}
            )
        if not leaves:
            raise GraphQLError(
                f"`where.{attr}` is an empty operator object — set at least "
                f"one operator.",
                extensions={
                    "code": "INVALID_FILTER_VALUE",
                    "field": spec.slot_name,
                },
            )
        parts.extend(leaves)

    if not parts:
        if allow_empty:
            return None
        raise GraphQLError(
            "Empty filter object inside `and`/`or`/`not` — a sub-filter "
            "must set at least one field or combinator.",
            extensions={"code": "INVALID_FILTER_VALUE"},
        )
    return parts[0] if len(parts) == 1 else {"and": parts}


def _make_list_resolver(builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(
        info: Info,
        filters: Optional[list[FilterInput]] = None,
        where=None,
        filter_mode: FilterMode = FilterMode.AND,
        limit: int = 100,
        offset: int = 0,
        as_of: Optional[str] = None,
        order_by=None,
        order_dir: OrderDirection = OrderDirection.ASC,
    ):
        ent = _builder(info).info_for(class_name)
        tree = (
            _where_to_tree(ent, where)
            if where is not None and where is not strawberry.UNSET
            else None
        )
        order_field = (
            order_by.value
            if order_by is not None and order_by is not strawberry.UNSET
            else None
        )
        if order_field is not None and as_of is not None:
            raise GraphQLError(
                "orderBy cannot be combined with asOf: ordering pushdown "
                "targets current-state storage; as-of reconstruction keeps "
                "its documented default ordering (ADR-0007).",
                extensions={"code": "ASOF_ORDERING_UNSUPPORTED"},
            )
        if as_of is not None and tree is not None and has_relationship_predicate(tree):
            raise GraphQLError(
                "Relationship predicates cannot be combined with asOf: "
                "cross-class temporal joins are out of scope for the "
                "as-of reconstruction path (ADR-0006 M5a / hippo#71).",
                extensions={"code": "ASOF_RELATIONSHIP_FILTER_UNSUPPORTED"},
            )
        try:
            paginated = _client(info).query(
                entity_type=class_name,
                filters=_to_sdk_filters(ent, filters),
                limit=limit,
                offset=offset,
                filter_mode=filter_mode.value,
                as_of=as_of,
                where=tree,
                order_by=order_field,
                order_dir=order_dir.value,
            )
        except MosaicValidationError as exc:
            raise _as_graphql_error(exc) from exc
        b = _builder(info)
        return entity.page_type(
            items=[
                b.instance_from_envelope(class_name, item)
                for item in paginated.items
            ],
            total=paginated.total,
            limit=paginated.limit,
            offset=paginated.offset,
        )

    resolver.__name__ = entity.plural_name
    resolver.__doc__ = (
        f"List {class_name} entities with typed filters and offset "
        f"pagination (mirrors MosaicClient.query). `where` is the typed "
        f"{class_name}Filter (per-slot operator objects + and/or/not "
        f"combinators — ADR-0006); the flat `filters` list (FilterOp per "
        f"entry) remains supported and composes with `where` by AND. "
        f"`orderBy` sorts on a stored column (ADR-0007: NULLs last, stable "
        f"id tiebreak, ordering and pagination pushed down to storage); "
        f"omitted, results keep the historical createdAt-ascending order. "
        f"Not combinable with `asOf`."
    )
    resolver.__annotations__["return"] = entity.page_type
    resolver.__annotations__["where"] = Optional[entity.filter_input]
    resolver.__annotations__["order_by"] = Optional[entity.order_field_enum]
    return resolver


def _resolve_aggregate_field(
    entity: EntityGraphQLInfo, field: str, *, ordered_only: bool = False
) -> "SlotSpec":
    """Validate an aggregation `field` argument (facetCounts/fieldRange) —
    the #149 discipline extended to aggregates: unknown or unaggregatable
    names are coded errors, never empty results."""
    spec = entity.resolve_filter_field(field)
    if spec is None and entity.is_computed_field(field):
        raise GraphQLError(
            f"{entity.class_name}.{field} is computed at read time from "
            f"the provenance log rather than being a column of "
            f"{entity.class_name} (sec9 §9.7), so it cannot be aggregated "
            f"on (ADR-0007).",
            extensions={"code": "UNAGGREGATABLE_FIELD", "field": field},
        )
    if spec is None:
        raise GraphQLError(
            f"Unknown aggregation field {field!r} for {entity.class_name}. "
            f"Aggregations run on: "
            f"{', '.join(sorted(entity.filterable_slot_names()))}.",
            extensions={"code": "UNKNOWN_AGGREGATION_FIELD", "field": field},
        )
    if spec.multivalued or (spec.kind == "scalar" and spec.scalar_type is JSON):
        raise GraphQLError(
            f"{entity.class_name}.{spec.slot_name} holds structured/"
            f"multivalued values, which have no scalar buckets or order, "
            f"so it cannot be aggregated on (ADR-0007).",
            extensions={
                "code": "UNAGGREGATABLE_FIELD",
                "field": field,
            },
        )
    if ordered_only:
        base = spec.base_range or "string"
        if spec.kind != "scalar" or base not in _ORDERED_RANGES:
            raise GraphQLError(
                f"fieldRange on {entity.class_name}.{spec.slot_name} is not "
                f"supported: min/max is defined for numeric and temporal "
                f"slots (slot range "
                f"{spec.base_range or spec.kind!s}) — ADR-0007.",
                extensions={
                    "code": "UNAGGREGATABLE_FIELD",
                    "field": field,
                },
            )
    return spec


def _make_count_resolver(builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(
        info: Info,
        filters: Optional[list[FilterInput]] = None,
        where=None,
        filter_mode: FilterMode = FilterMode.AND,
        as_of: Optional[str] = None,
    ) -> int:
        ent = _builder(info).info_for(class_name)
        tree = (
            _where_to_tree(ent, where)
            if where is not None and where is not strawberry.UNSET
            else None
        )
        if as_of is not None and tree is not None and has_relationship_predicate(tree):
            raise GraphQLError(
                "Relationship predicates cannot be combined with asOf: "
                "cross-class temporal joins are out of scope for the "
                "as-of reconstruction path (ADR-0006 M5a / hippo#71).",
                extensions={"code": "ASOF_RELATIONSHIP_FILTER_UNSUPPORTED"},
            )
        try:
            return _client(info).count(
                entity_type=class_name,
                filters=_to_sdk_filters(ent, filters),
                filter_mode=filter_mode.value,
                as_of=as_of,
                where=tree,
            )
        except MosaicValidationError as exc:
            raise _as_graphql_error(exc) from exc

    resolver.__name__ = f"{entity.plural_name}_count"
    resolver.__doc__ = (
        f"Count {class_name} entities matching the given filters without "
        f"materializing them — a COUNT(*) under the exact predicate the "
        f"list query uses, so it always equals the list's `total` "
        f"(availability-consistent; ADR-0007). Under `asOf`, the count is "
        f"over the reconstructed as-of match set."
    )
    resolver.__annotations__["where"] = Optional[entity.filter_input]
    return resolver


def _make_facet_counts_resolver(
    builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo
):
    class_name = entity.class_name

    def resolver(
        info: Info,
        field: str,
        filters: Optional[list[FilterInput]] = None,
        where=None,
        filter_mode: FilterMode = FilterMode.AND,
    ) -> list[FacetCount]:
        ent = _builder(info).info_for(class_name)
        spec = _resolve_aggregate_field(ent, field)
        tree = (
            _where_to_tree(ent, where)
            if where is not None and where is not strawberry.UNSET
            else None
        )
        try:
            buckets = _client(info).facet_counts(
                class_name,
                spec.slot_name,
                _to_sdk_filters(ent, filters),
                filter_mode.value,
                where=tree,
            )
        except MosaicValidationError as exc:
            raise _as_graphql_error(exc) from exc
        return [FacetCount(value=value, count=count) for value, count in buckets]

    resolver.__name__ = f"{entity.plural_name}_facet_counts"
    resolver.__doc__ = (
        f"Per-value counts of one {class_name} field under the given "
        f"filters, ordered by count descending then value (ADR-0007). "
        f"`field` is a LinkML slot name (camelCase also accepted). "
        f"Entities with no stored value are not counted (ask about absence "
        f"with IS_NULL). Availability-consistent with the list surface. "
        f"Current-state only: as-of aggregation is a later increment."
    )
    resolver.__annotations__["where"] = Optional[entity.filter_input]
    return resolver


def _make_field_range_resolver(
    builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo
):
    class_name = entity.class_name

    def resolver(
        info: Info,
        field: str,
        filters: Optional[list[FilterInput]] = None,
        where=None,
        filter_mode: FilterMode = FilterMode.AND,
    ) -> FieldRange:
        ent = _builder(info).info_for(class_name)
        spec = _resolve_aggregate_field(ent, field, ordered_only=True)
        tree = (
            _where_to_tree(ent, where)
            if where is not None and where is not strawberry.UNSET
            else None
        )
        try:
            lo, hi = _client(info).field_range(
                class_name,
                spec.slot_name,
                _to_sdk_filters(ent, filters),
                filter_mode.value,
                where=tree,
            )
        except MosaicValidationError as exc:
            raise _as_graphql_error(exc) from exc
        return FieldRange(min=lo, max=hi)

    resolver.__name__ = f"{entity.plural_name}_field_range"
    resolver.__doc__ = (
        f"Min/max of one numeric or temporal {class_name} field under the "
        f"given filters, for range facets (ADR-0007). Both null when no "
        f"matching entity has a stored value. Availability-consistent with "
        f"the list surface. Current-state only: as-of aggregation is a "
        f"later increment."
    )
    resolver.__annotations__["where"] = Optional[entity.filter_input]
    return resolver


def _make_search_resolver(builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(
        info: Info,
        q: str,
        filters: Optional[list[FilterInput]] = None,
        where=None,
        filter_mode: FilterMode = FilterMode.AND,
        limit: int = 100,
        offset: int = 0,
        order_by=None,
        order_dir: OrderDirection = OrderDirection.ASC,
    ):
        ent = _builder(info).info_for(class_name)
        tree = (
            _where_to_tree(ent, where)
            if where is not None and where is not strawberry.UNSET
            else None
        )
        order_field = (
            order_by.value
            if order_by is not None and order_by is not strawberry.UNSET
            else None
        )
        try:
            paginated = _client(info).search(
                entity_type=class_name,
                query=q,
                limit=limit,
                offset=offset,
                filters=_to_sdk_filters(ent, filters),
                filter_mode=filter_mode.value,
                where=tree,
                order_by=order_field,
                order_dir=order_dir.value,
            )
        except MosaicValidationError as exc:
            raise _as_graphql_error(exc) from exc
        b = _builder(info)
        return entity.page_type(
            items=[
                b.instance_from_envelope(class_name, item)
                for item in paginated.items
            ],
            total=paginated.total,
            limit=paginated.limit,
            offset=paginated.offset,
        )

    resolver.__name__ = f"search_{entity.plural_name}"
    resolver.__doc__ = (
        f"Full-text search over {class_name} entities, composed with the "
        f"list surface (issue #157): takes the same `filters`/`where`/"
        f"`filterMode` arguments as the list query and returns the same "
        f"{class_name}Page envelope (items/total/limit/offset). Results "
        f"come back in FTS rank order; an explicit `orderBy` overrides "
        f"rank (ADR-0007). `total` is the matching-and-filtered count, "
        f"bounded by the 1000-hit FTS budget. Requires the schema to "
        f"declare searchable slots for {class_name}."
    )
    resolver.__annotations__["return"] = entity.page_type
    resolver.__annotations__["where"] = Optional[entity.filter_input]
    resolver.__annotations__["order_by"] = Optional[entity.order_field_enum]
    return resolver


def _entity_history_resolver(info: Info, entity_id: strawberry.ID) -> list[ProvenanceEntry]:
    """Provenance history for any entity (oldest first)."""
    try:
        records = _client(info).history(str(entity_id))
    except EntityNotFoundError as exc:
        raise _as_graphql_error(exc) from exc
    return [
        ProvenanceEntry(
            operation_id=record.get("operation_id"),
            entity_id=record.get("entity_id"),
            entity_type=record.get("entity_type"),
            operation=record.get("operation_type"),
            timestamp=record.get("timestamp"),
            actor_id=record.get("user_id"),
            patch=record.get("state_snapshot"),
        )
        for record in records
    ]


def _superseded_by_resolver(info: Info, id: strawberry.ID) -> SupersessionInfo:  # noqa: A002
    """Entity-level supersession chain (mirrors REST GET /{id}/superseded).

    Resolves the entity's type from the id, then follows
    ``superseded_by`` links forward (reading superseded entities with
    ``include_unavailable``) until the terminal replacement.
    """
    client = _client(info)
    entity_id = str(id)
    entity_type = client.resolve_type(entity_id)
    if entity_type is None:
        raise GraphQLError(
            f"Entity not found: {entity_id}", extensions={"code": "NOT_FOUND"}
        )

    chain: list[strawberry.ID] = []
    seen = {entity_id}
    current_id, current_type = entity_id, entity_type
    while True:
        try:
            envelope = client.get(
                current_type, current_id, include_unavailable=True
            )
        except EntityNotFoundError as exc:
            if not chain:
                raise _as_graphql_error(exc) from exc
            break
        next_id = envelope.get("superseded_by")
        if not next_id or str(next_id) in seen:  # terminal (or defensive cycle stop)
            break
        next_id = str(next_id)
        chain.append(strawberry.ID(next_id))
        seen.add(next_id)
        next_type = client.resolve_type(next_id)
        if next_type is None:
            break
        current_id, current_type = next_id, next_type

    return SupersessionInfo(
        entity_id=id,
        superseded_by=chain[0] if chain else None,
        chain=chain,
    )


def _find_by_xref_resolver(
    info: Info, system: str, value: str
) -> Optional[XrefMatch]:
    """Reverse lookup over mosaic_external_xref-annotated slots.

    Thin delegation to ``MosaicClient.find_by_xref`` (mirrors REST
    ``GET /xref/{system}/{value}``). Null when no available entity holds
    the pair; (system, value) is globally unique among available
    entities, so at most one entity can match.
    """
    try:
        envelope = _client(info).find_by_xref(system, value)
    except NotImplementedError as exc:
        raise GraphQLError(
            str(exc), extensions={"code": "NOT_IMPLEMENTED"}
        ) from exc
    if envelope is None:
        return None
    return XrefMatch(
        entity_id=strawberry.ID(str(envelope.get("id"))),
        entity_type=str(envelope.get("entity_type")),
        data=envelope.get("data") or {},
        version=envelope.get("version"),
        created_at=envelope.get("created_at"),
        updated_at=envelope.get("updated_at"),
    )


def _related_to_resolver(
    info: Info,
    id: strawberry.ID,  # noqa: A002
    relationship_type: Optional[str] = None,
) -> list[RelatedEntity]:
    """Reverse lookup: entities referencing ``id`` via a relationships-
    table-backed multivalued slot (ADR-0002) — answers "what points at
    this entity" (issue #146), the reverse of forward multivalued-
    reference resolution which has no such symmetric path today.

    Thin delegation to ``RelationshipManager.find_relationships(target_id=...)``.
    ``relationship_type`` (the referencing slot name) narrows the result
    when the target is referenced by more than one slot/class; omitted,
    all referencing edges are returned.
    """
    client = _client(info)
    edges = client.relationships.find_relationships(
        target_id=str(id), relationship_type=relationship_type
    )
    if not edges:
        return []

    source_ids = [edge["source_id"] for edge in edges]
    types_by_id = client.resolve_types(source_ids)

    results = []
    for edge in edges:
        source_id = edge["source_id"]
        entity_type = types_by_id.get(source_id)
        if entity_type is None:
            continue  # dangling edge: source no longer resolvable
        try:
            envelope = client.get(entity_type, source_id)
        except EntityNotFoundError:
            continue
        results.append(
            RelatedEntity(
                entity_id=strawberry.ID(source_id),
                entity_type=entity_type,
                relationship_type=edge["relationship_type"],
                data=envelope.get("data") or {},
                version=envelope.get("version"),
                created_at=envelope.get("created_at"),
                updated_at=envelope.get("updated_at"),
            )
        )
    return results


def _search_all_resolver(info: Info, q: str, limit: int = 100) -> list[SearchHit]:
    """Cross-class ranked full-text search (issue #158): one request fans
    over every FTS-indexed class server-side, merged by normalized rank,
    materialized batched by type (no per-hit reads). The per-class search
    twins remain for typed results; this root returns heterogeneous JSON
    envelopes."""
    hits = _client(info).search_all(q, limit)
    return [
        SearchHit(
            entity_id=strawberry.ID(str(hit["id"])),
            entity_type=str(hit["entity_type"]),
            score=float(hit["score"]),
            data=hit.get("data") or {},
            version=hit.get("version"),
            created_at=hit.get("created_at"),
            updated_at=hit.get("updated_at"),
        )
        for hit in hits
    ]


def _neighbors_resolver(
    info: Info,
    id: strawberry.ID,  # noqa: A002 - GraphQL arg
    depth: int = 1,
    as_of: Optional[str] = None,
) -> NeighborhoodGraph:
    """The subgraph around one entity (issue #158): nodes + edges from BOTH
    edge stores — link-table relationship edges (both directions) and
    column-stored single-valued references (forward and reverse,
    schema-driven) — closing the ADR-0002 visibility gap. Depth-bounded
    (cap 5), 1000-node budget, batched-by-type materialization,
    availability parity with list queries. Under `asOf`, link-table edges
    replay from provenance and node states reconstruct at that time;
    column edges are disclosed as out of scope (hippo#71) via
    `edgeSources`/`notices` — never a silently partial temporal graph."""
    try:
        graph = _client(info).neighbors(str(id), depth, as_of=as_of)
    except EntityNotFoundError as exc:
        raise _as_graphql_error(exc) from exc
    return NeighborhoodGraph(
        nodes=[
            GraphNode(
                entity_id=strawberry.ID(str(n["entity_id"])),
                entity_type=n.get("entity_type"),
                data=n.get("data") or {},
                created_at=n.get("created_at"),
                updated_at=n.get("updated_at"),
            )
            for n in graph["nodes"]
        ],
        edges=[
            GraphEdge(
                source=strawberry.ID(str(e["source"])),
                target=strawberry.ID(str(e["target"])),
                type=e["type"],
                edge_source=e["edge_source"],
            )
            for e in graph["edges"]
        ],
        edge_sources=list(graph["edge_sources"]),
        notices=list(graph["notices"]),
    )


def _make_mosaic_schema_resolver(builder: GraphQLTypeBuilder):
    def resolver(info: Info) -> list[MosaicEntityTypeInfo]:
        b = _builder(info)
        models = {**b.type_model, **{n: f.model for n, f in b.families.items()}}
        return [_entity_type_info(model, b) for _, model in sorted(models.items())]

    resolver.__name__ = "mosaic_schema"
    resolver.__doc__ = (
        "The deployment's LinkML type model — every exposed entity type "
        "with its slots and relationships (mirrors REST GET /schemas). "
        "Distinct from GraphQL's own __schema introspection."
    )
    return resolver


def _make_mosaic_entity_type_resolver(builder: GraphQLTypeBuilder):
    def resolver(info: Info, name: str) -> Optional[MosaicEntityTypeInfo]:
        b = _builder(info)
        model = b.type_model.get(name)
        if model is None and name in b.families:
            model = b.families[name].model
        if model is None:
            return None
        return _entity_type_info(model, b)

    resolver.__name__ = "mosaic_entity_type"
    resolver.__doc__ = (
        "One exposed entity type from the LinkML type model, with its "
        "slots and relationships (mirrors REST GET /schemas/{name} and "
        "/schemas/{name}/references). Null for unknown names."
    )
    return resolver


# ---------------------------------------------------------------------------
# Mutation resolvers
# ---------------------------------------------------------------------------


def _refetch(info: Info, class_name: str, entity_id: str) -> Any:
    """Read back the full envelope (incl. computed temporal fields)."""
    envelope = _client(info).get(class_name, entity_id)
    return _builder(info).instance_from_envelope(class_name, envelope)


def _make_create_resolver(builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(info: Info, data):
        payload = _builder(info).input_to_dict(class_name, data, mode="create")
        try:
            created = _client(info).create(class_name, payload)
        except Exception as exc:
            # Validation failures get the structured VALIDATION_FAILED
            # envelope; anything else (e.g. adapter integrity errors on
            # dangling references) maps to INTERNAL_ERROR so callers
            # always see a coded GraphQL error.
            raise _as_graphql_error(exc) from exc
        return _refetch(info, class_name, created["id"])

    resolver.__name__ = f"create_{entity.singular_name}"
    resolver.__doc__ = (
        f"Create a {class_name}. The SDK assigns a UUID when `id` is "
        f"omitted and records a `create` provenance entry."
    )
    resolver.__annotations__["data"] = entity.create_input
    resolver.__annotations__["return"] = entity.gql_type
    return resolver


def _make_update_resolver(builder: GraphQLTypeBuilder, entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(info: Info, id: strawberry.ID, data):  # noqa: A002
        payload = _builder(info).input_to_dict(class_name, data, mode="update")
        # ``MosaicClient.update`` has full-replace semantics (PUT — sec4
        # §4.3): omitted slots are nulled in storage. GraphQL update
        # inputs are partial by convention, so compose the SDK's read
        # and write: merge the patch over the current stored data. Pure
        # envelope composition — the SDK remains the only reader/writer.
        try:
            existing = _client(info).get(class_name, str(id))
        except EntityNotFoundError as exc:
            raise _as_graphql_error(exc) from exc
        merged = {**(existing.get("data") or {}), **payload}
        try:
            _client(info).update(class_name, str(id), merged)
        except Exception as exc:
            raise _as_graphql_error(exc) from exc
        return _refetch(info, class_name, str(id))

    resolver.__name__ = f"update_{entity.singular_name}"
    resolver.__doc__ = (
        f"Partially update an existing {class_name} (provided fields are "
        f"merged over the stored data); records an `update` provenance "
        f"entry. Errors with NOT_FOUND when the id is unknown."
    )
    resolver.__annotations__["data"] = entity.update_input
    resolver.__annotations__["return"] = entity.gql_type
    return resolver


def _make_availability_resolver(entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(
        info: Info,
        id: strawberry.ID,  # noqa: A002
        is_available: bool,
        reason: Optional[str] = None,
    ) -> AvailabilityResult:
        result = _client(info).set_availability_bulk(
            entity_type=class_name,
            entity_ids=[str(id)],
            is_available=is_available,
            reason=reason,
        )
        if result["failed"]:
            error = result["failures"][0].get("error", "availability change failed")
            code = "NOT_FOUND" if "not found" in error.lower() else "AVAILABILITY_CHANGE_FAILED"
            raise GraphQLError(error, extensions={"code": code})
        return AvailabilityResult(entity_id=id, is_available=is_available)

    resolver.__name__ = f"set_{entity.singular_name}_availability"
    resolver.__doc__ = (
        f"Availability transition for a {class_name} — Mosaic's "
        f"no-hard-delete lifecycle. The transition and its reason are "
        f"recorded as an `availability_change` provenance entry."
    )
    return resolver


def _make_bulk_availability_resolver(entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(
        info: Info,
        ids: list[strawberry.ID],
        is_available: bool,
        reason: Optional[str] = None,
    ) -> BulkAvailabilityResult:
        result = _client(info).set_availability_bulk(
            entity_type=class_name,
            entity_ids=[str(i) for i in ids],
            is_available=is_available,
            reason=reason,
        )
        return BulkAvailabilityResult(
            total=result["total"],
            succeeded=result["succeeded"],
            failed=result["failed"],
            successes=[
                AvailabilityResult(
                    entity_id=item["id"], is_available=item["is_available"]
                )
                for item in result["successes"]
            ],
            failures=[
                BulkAvailabilityFailure(
                    entity_id=item["id"],
                    error=item.get("error", "availability change failed"),
                )
                for item in result["failures"]
            ],
        )

    resolver.__name__ = f"set_{entity.singular_name}_availability_bulk"
    resolver.__doc__ = (
        f"Bulk availability transition for {class_name} entities (mirrors "
        f"REST POST /entities/{{type}}/bulk-availability; wraps "
        f"MosaicClient.set_availability_bulk). Per-record error isolation: "
        f"failures are reported per id and never roll back sibling "
        f"successes."
    )
    return resolver


def _make_supersede_resolver(entity: EntityGraphQLInfo):
    class_name = entity.class_name

    def resolver(
        info: Info,
        id: strawberry.ID,  # noqa: A002
        replacement_id: strawberry.ID,
        reason: Optional[str] = None,
    ) -> SupersedeResult:
        try:
            _client(info).supersede_entity(
                str(id), str(replacement_id), reason=reason
            )
        except (EntityNotFoundError, EntityAlreadySupersededError) as exc:
            raise _as_graphql_error(exc) from exc
        return SupersedeResult(entity_id=id, superseded_by=replacement_id)

    resolver.__name__ = f"supersede_{entity.singular_name}"
    resolver.__doc__ = (
        f"Atomically supersede a {class_name} with a replacement entity "
        f"(marks the source unavailable; provenance carries the link)."
    )
    return resolver


# ---------------------------------------------------------------------------
# Batch unit-of-work resolvers (root mutations; issue #84)
# ---------------------------------------------------------------------------


def _ops_from_inputs(entities: list[BatchEntityInput]) -> list[WriteOperation]:
    return [
        WriteOperation(
            operation=e.operation or "insert",
            entity_type=e.entity_type,
            data=dict(e.data or {}),
        )
        for e in entities
    ]


def _gql_batch_validation(vr: Any) -> BatchValidationGraphQLResult:
    return BatchValidationGraphQLResult(
        passed=vr.is_valid,
        results=[
            BatchEntityValidation(
                entity_id=r.entity_id,
                passed=r.is_valid,
                failures=[
                    ValidationFailureType(
                        tier=f.tier,
                        rule=f.rule,
                        message=f.message,
                        field=f.field,
                        details=f.details or None,
                    )
                    for f in r.failures
                ],
            )
            for r in vr.results
        ],
    )


def _validate_batch_resolver(
    info: Info, entities: list[BatchEntityInput]
) -> BatchValidationGraphQLResult:
    """Whole-set dry-run validation (no writes; mirrors REST POST /ingest/validate)."""
    vr = _client(info).validate_batch(_ops_from_inputs(entities))
    return _gql_batch_validation(vr)


def _ingest_batch_resolver(
    info: Info,
    entities: list[BatchEntityInput],
    relationships: Optional[list[BatchRelationshipInput]] = None,
    dry_run: bool = False,
) -> BatchWriteGraphQLResult:
    """Atomic multi-entity write (mirrors REST POST /ingest/batch).

    The set commits all-or-nothing; relationships are created after the
    entities within the same transaction so intra-batch forward references
    resolve. SDK errors map to coded GraphQL errors via ``_as_graphql_error``.
    """
    ops = _ops_from_inputs(entities)
    rels = [
        {
            "source_id": str(r.source_id),
            "target_id": str(r.target_id),
            "relationship_type": r.relationship_type,
            "metadata": r.metadata,
        }
        for r in (relationships or [])
    ] or None
    try:
        result = _client(info).batch_put(ops, relationships=rels, dry_run=dry_run)
    except Exception as exc:
        raise _as_graphql_error(exc) from exc
    return BatchWriteGraphQLResult(
        committed=result.committed,
        dry_run=result.dry_run,
        validation=_gql_batch_validation(result.validation),
        entities=result.entities,
        relationships=result.relationships,
    )


# ---------------------------------------------------------------------------
# Schema assembly
# ---------------------------------------------------------------------------


def build_query_type(builder: GraphQLTypeBuilder) -> type:
    fields = []
    # Entities, then each family with its own read-only collection
    # (ADR-0014 Decision 3, #228): the same get/list/search/count/facet/
    # range surface; items render as their own kind.
    anchors = list(builder.entities.values()) + [
        builder.families[name] for name in builder.family_anchors
    ]
    for entity in anchors:
        fields.append(
            strawberry.field(
                resolver=_make_get_resolver(builder, entity),
                name=camel_case(entity.singular_name),
            )
        )
        fields.append(
            strawberry.field(
                resolver=_make_list_resolver(builder, entity),
                name=camel_case(entity.plural_name),
            )
        )
        fields.append(
            strawberry.field(
                resolver=_make_search_resolver(builder, entity),
                name=camel_case(f"search_{entity.plural_name}"),
            )
        )
        fields.append(
            strawberry.field(
                resolver=_make_count_resolver(builder, entity),
                name=camel_case(f"{entity.plural_name}_count"),
            )
        )
        fields.append(
            strawberry.field(
                resolver=_make_facet_counts_resolver(builder, entity),
                name=camel_case(f"{entity.plural_name}_facet_counts"),
            )
        )
        fields.append(
            strawberry.field(
                resolver=_make_field_range_resolver(builder, entity),
                name=camel_case(f"{entity.plural_name}_field_range"),
            )
        )
    fields.append(
        strawberry.field(resolver=_entity_history_resolver, name="entityHistory")
    )
    fields.append(
        strawberry.field(resolver=_superseded_by_resolver, name="supersededBy")
    )
    fields.append(
        strawberry.field(resolver=_find_by_xref_resolver, name="findByXref")
    )
    fields.append(
        strawberry.field(resolver=_related_to_resolver, name="relatedTo")
    )
    fields.append(
        strawberry.field(resolver=_search_all_resolver, name="searchAll")
    )
    fields.append(
        strawberry.field(resolver=_neighbors_resolver, name="neighbors")
    )
    fields.append(
        strawberry.field(
            resolver=_make_mosaic_schema_resolver(builder), name="mosaicSchema"
        )
    )
    fields.append(
        strawberry.field(
            resolver=_make_mosaic_entity_type_resolver(builder),
            name="mosaicEntityType",
        )
    )
    return create_type("Query", fields)


def build_mutation_type(builder: GraphQLTypeBuilder) -> type:
    fields = []
    for entity in builder.entities.values():
        singular = entity.singular_name
        fields.append(
            strawberry.mutation(
                resolver=_make_create_resolver(builder, entity),
                name=camel_case(f"create_{singular}"),
            )
        )
        fields.append(
            strawberry.mutation(
                resolver=_make_update_resolver(builder, entity),
                name=camel_case(f"update_{singular}"),
            )
        )
        fields.append(
            strawberry.mutation(
                resolver=_make_availability_resolver(entity),
                name=camel_case(f"set_{singular}_availability"),
            )
        )
        fields.append(
            strawberry.mutation(
                resolver=_make_bulk_availability_resolver(entity),
                name=camel_case(f"set_{singular}_availability_bulk"),
            )
        )
        fields.append(
            strawberry.mutation(
                resolver=_make_supersede_resolver(entity),
                name=camel_case(f"supersede_{singular}"),
            )
        )
    # Cross-type batch unit-of-work (issue #84) — root mutations, added once.
    fields.append(
        strawberry.mutation(resolver=_ingest_batch_resolver, name="ingestBatch")
    )
    fields.append(
        strawberry.mutation(resolver=_validate_batch_resolver, name="validateBatch")
    )
    # converseQuerySpec (issue #205) -- hand-authored, alongside the
    # codegen'd mutations above, exactly like mosaicSchema is hand-authored
    # alongside the codegen'd Query fields. Registered ONLY when
    # MOSAIC_EXON_URL is set (read once, at schema-build time, mirroring
    # the MCP tool's own mount-time gate in mosaic/mcp/server.py) -- an
    # unconfigured deployment does not advertise a mutation it cannot serve.
    exon_endpoint = os.environ.get(EXON_URL_ENV, "").strip()
    if exon_endpoint:
        fields.append(
            strawberry.mutation(
                resolver=_make_converse_query_spec_resolver(
                    exon_endpoint, _exon_timeout()
                ),
                name="converseQuerySpec",
            )
        )
    return create_type("Mutation", fields)


def build_graphql_schema(
    registry: SchemaRegistry,
    builder: Optional[GraphQLTypeBuilder] = None,
    max_query_depth: int = DEFAULT_MAX_QUERY_DEPTH,
) -> strawberry.Schema:
    """Autogenerate the full ``strawberry.Schema`` for a deployment.

    One call at startup: renders the shared LinkML type model and emits
    per-entity object/input/page types plus Query and Mutation roots.
    Pass an existing :class:`GraphQLTypeBuilder` to share the type
    universe with the router's context.

    ``max_query_depth`` bounds query nesting (strawberry's
    ``QueryDepthLimiter``; introspection fields are exempt so GraphiQL
    keeps working). Relationship fields make arbitrarily deep traversal
    expressible, so a depth cap is the transport's recursion guard.
    """
    builder = (builder or GraphQLTypeBuilder(registry)).build()
    if not builder.entities:
        raise ValueError(
            "GraphQL schema generation found no concrete entity classes "
            "in the merged LinkML schema; nothing to expose."
        )
    return strawberry.Schema(
        query=build_query_type(builder),
        mutation=build_mutation_type(builder),
        # Factory form so a fresh extension is constructed per request.
        extensions=[lambda: QueryDepthLimiter(max_depth=max_query_depth)],
    )
