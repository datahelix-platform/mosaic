"""QuerySpec (ADR-0009 / issue #183): the canonical typed cross-class query
artifact this boundary accepts, as normatively defined by Aperture's
ADR-0035 (``design/decisions/ADR-0035-cross-class-queries-typed-queryspec.md``
in the ``aperture`` repo) and its shipped implementation
(``web/src/query/querySpec.ts``).

This module is the Python side of that same artifact: parsing (structural,
schema-independent — mirrors the TS ``validateQuerySpecShape`` shape guard)
and validation (semantic, capability-manifest-driven — mirrors the TS
``validateQuerySpec``, ported to run server-side where the capability
manifest (:mod:`mosaic.core.schema_typing`) is available in-process with no
introspection round-trip).

**Scope note (issue #183, validator-only increment):** this module parses
and validates. It does not compile a validated ``QuerySpec`` into GraphQL
``where:``/aggregation arguments (the executor) and registers no MCP tool —
both are a separate increment (#182 sequencing), gated on #185's auth
decision landing first. Nothing here is reachable from any network surface.

**Shape decisions, and why:** ADR-0035's normative artifact also specifies
``asOf``, ``sort``, and ``columns``, beyond the flat MVP subset
(``v``/``anchor``/``mode``/``criteria``) the current Aperture TS code emits.
The dividing line used here is not "ratified ADR text vs. current code" —
it is *what Mosaic's ``where:`` surface can actually compile today*:

- Nested ``CriteriaGroup``s ARE accepted (depth-capped at 3, per ADR-0035):
  Mosaic's generated ``<Class>Filter`` already carries ``and``/``or``/``not``
  combinators at arbitrary depth (ADR-0006).
- ``asOf`` IS accepted, with the same hard mutual-exclusion against any
  relationship predicate anywhere in the tree that Mosaic's storage layer
  already enforces (``has_relationship_predicate``, ADR-0001).
- ``sort`` IS accepted, checked against the manifest's per-field
  ``orderable`` flag (ADR-0007's ``<Class>OrderField``) — but capped at
  ONE entry: Mosaic's query surface (``MosaicClient.query``'s single
  ``order_by``/``order_dir`` pair, mirrored by GraphQL's single-valued
  ``orderBy`` argument) has no multi-column sort to compile a second
  entry against.
- ``columns`` IS accepted (ADR-0009's 2026-10-02 and 2026-10-27 notes;
  issues #215 and #236): a list of ``{"path": [...], "mode"?: ...}``
  ColumnSpecs. A single-element path names a field of the anchor (one row
  per anchor, unchanged since #215). A longer path follows **1–2 reference
  hops** — forward, or an ``inverse_of`` reverse — and ends in a slot of the
  class it reaches, all spelled as LinkML slot names exactly like
  ``criteria``. A to-many hop (at most one per path) needs ``mode``:
  ``count`` / ``joinIds`` keep one row per anchor, ``explode`` makes one row
  per (anchor x member) and is allowed through at most one link per spec
  (Aperture ADR-0041's cap). ``COLUMNS_NOT_SUPPORTED`` (coded, not-a-crash)
  now refuses only depth > 2, a to-many hop without ``mode``, a second
  to-many hop on one path, and a second exploded link.

**Op spelling:** ADR-0035's prose writes ``isNull``; both real
implementations (Aperture's shipped ``QueryOp`` type and this module's
``FilterOp``) use ``is_null``. Code wins — do not "fix" this back to the
prose spelling.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Literal, Optional, Union

from mosaic.core.schema_typing import EntityCapability, FilterOp, SlotKind

#: ADR-0035's normative nesting cap on CriteriaGroup depth.
MAX_CRITERIA_DEPTH = 3

_VALID_QUANTIFIERS = frozenset({"some", "none"})
_VALID_MODES = frozenset({"AND", "OR"})


@dataclass(frozen=True)
class FieldCondition:
    """``{slot, op, value}`` — a leaf condition on one field of the entity
    it is evaluated against (the anchor at the top level; a RelatedCondition's
    target entity when nested under one)."""

    slot: str
    op: str
    value: Any = None
    kind: Literal["field"] = "field"


@dataclass(frozen=True)
class RelatedCondition:
    """``{edge, quantifier, criteria}`` — quantified relationship predicate.
    ``criteria`` holds on the SAME related record, flat/AND (matches both
    ADR-0035's prose and the current TS implementation — no nested groups
    inside a RelatedCondition)."""

    edge: str
    quantifier: str
    criteria: tuple[FieldCondition, ...] = ()
    kind: Literal["related"] = "related"


@dataclass(frozen=True)
class CriteriaGroup:
    """Nested ``{mode, criteria}`` boolean group (ADR-0035, depth-capped)."""

    mode: str
    criteria: tuple["Criterion", ...] = ()
    kind: Literal["group"] = "group"


Criterion = Union[FieldCondition, RelatedCondition, CriteriaGroup]


@dataclass(frozen=True)
class SortField:
    """One ``order_by`` field on the anchor (ADR-0007)."""

    slot: str
    direction: str = "asc"  # "asc" | "desc"


#: Reference hops a ``columns`` path may follow (ADR-0009 note 2026-10-27,
#: matching Aperture ADR-0041's client cap): ``[hop, hop, leaf]`` at most.
MAX_COLUMN_HOPS = 2

#: How a to-many hop resolves (ADR-0041's vocabulary). Only ``explode``
#: changes the grain.
COLUMN_MODES = ("count", "joinIds", "explode")


@dataclass(frozen=True)
class ColumnSpec:
    """One ``columns`` entry (ADR-0035/ADR-0041).

    ``path`` is the anchor slot alone (anchor-owned, #215) or up to
    :data:`MAX_COLUMN_HOPS` reference hops then a leaf slot (#236). ``mode``
    is set only when a hop is to-many — whether one is depends on the
    schema, so that is :func:`validate_query_spec`'s check, not the parser's.
    """

    path: tuple[str, ...]
    mode: Optional[str] = None

    @property
    def slot(self) -> str:
        """The anchor slot the path starts from (the whole column when the
        path has no hop)."""
        return self.path[0]

    @property
    def is_traversal(self) -> bool:
        return len(self.path) > 1

    @property
    def key(self) -> str:
        """The column's name in a result row: the path, dot-joined (the
        same ``pathKey`` Aperture's result table uses)."""
        return ".".join(self.path)


@dataclass(frozen=True)
class QuerySpec:
    """The parsed, not-yet-validated artifact (ADR-0009/ADR-0035)."""

    v: int
    anchor: str
    mode: str
    criteria: tuple[Criterion, ...] = ()
    as_of: Optional[str] = None
    sort: tuple[SortField, ...] = ()
    #: Empty = no selection (full envelopes, the historical behaviour).
    columns: tuple[ColumnSpec, ...] = ()


class QuerySpecShapeError(ValueError):
    """Structural problem with the raw input — schema-independent (mirrors
    the TS ``validateQuerySpecShape`` guard). Never raised for a problem
    that depends on what a specific deployment's schema supports; that is
    :func:`validate_query_spec`'s job."""

    def __init__(self, code: str, message: str, path: str = "$"):
        self.code = code
        self.path = path
        super().__init__(f"{path}: {message}")


def _require(cond: bool, code: str, message: str, path: str) -> None:
    if not cond:
        raise QuerySpecShapeError(code, message, path)


def _parse_field_condition(raw: Any, path: str) -> FieldCondition:
    _require(isinstance(raw, dict), "INVALID_QUERYSPEC_SHAPE", "must be an object", path)
    _require(isinstance(raw.get("slot"), str), "INVALID_QUERYSPEC_SHAPE", "'slot' must be a string", f"{path}.slot")
    _require(isinstance(raw.get("op"), str), "INVALID_QUERYSPEC_SHAPE", "'op' must be a string", f"{path}.op")
    return FieldCondition(slot=raw["slot"], op=raw["op"], value=raw.get("value"))


def _parse_related_condition(raw: dict, path: str) -> RelatedCondition:
    _require(isinstance(raw.get("edge"), str), "INVALID_QUERYSPEC_SHAPE", "'edge' must be a string", f"{path}.edge")
    quantifier = raw.get("quantifier")
    _require(
        quantifier in _VALID_QUANTIFIERS,
        "INVALID_QUERYSPEC_SHAPE",
        f"'quantifier' must be one of {sorted(_VALID_QUANTIFIERS)}",
        f"{path}.quantifier",
    )
    raw_criteria = raw.get("criteria", [])
    _require(isinstance(raw_criteria, list), "INVALID_QUERYSPEC_SHAPE", "'criteria' must be a list", f"{path}.criteria")
    sub = tuple(
        _parse_field_condition(c, f"{path}.criteria[{i}]") for i, c in enumerate(raw_criteria)
    )
    return RelatedCondition(edge=raw["edge"], quantifier=quantifier, criteria=sub)


def _parse_criterion(raw: Any, path: str, depth: int) -> Criterion:
    _require(isinstance(raw, dict), "INVALID_QUERYSPEC_SHAPE", "must be an object", path)
    kind = raw.get("kind")
    if kind == "field":
        return _parse_field_condition(raw, path)
    if kind == "related":
        return _parse_related_condition(raw, path)
    if kind == "group":
        return _parse_criteria_group(raw, path, depth)
    raise QuerySpecShapeError(
        "INVALID_QUERYSPEC_SHAPE",
        f"'kind' must be one of 'field', 'related', 'group' (got {kind!r})",
        f"{path}.kind",
    )


def _parse_criteria_group(raw: dict, path: str, depth: int) -> CriteriaGroup:
    _require(
        depth <= MAX_CRITERIA_DEPTH,
        "DEPTH_EXCEEDED",
        f"criteria nesting exceeds ADR-0035's cap of {MAX_CRITERIA_DEPTH}",
        path,
    )
    mode = raw.get("mode")
    _require(mode in _VALID_MODES, "INVALID_QUERYSPEC_SHAPE", "'mode' must be 'AND' or 'OR'", f"{path}.mode")
    raw_criteria = raw.get("criteria", [])
    _require(isinstance(raw_criteria, list), "INVALID_QUERYSPEC_SHAPE", "'criteria' must be a list", f"{path}.criteria")
    sub = tuple(
        _parse_criterion(c, f"{path}.criteria[{i}]", depth + 1)
        for i, c in enumerate(raw_criteria)
    )
    return CriteriaGroup(mode=mode, criteria=sub)


def _parse_sort_field(raw: Any, path: str) -> SortField:
    _require(isinstance(raw, dict), "INVALID_QUERYSPEC_SHAPE", "must be an object", path)
    _require(isinstance(raw.get("slot"), str), "INVALID_QUERYSPEC_SHAPE", "'slot' must be a string", f"{path}.slot")
    direction = raw.get("direction", "asc")
    _require(direction in ("asc", "desc"), "INVALID_QUERYSPEC_SHAPE", "'direction' must be 'asc' or 'desc'", f"{path}.direction")
    return SortField(slot=raw["slot"], direction=direction)


#: Grain-choice spellings from ADR-0035 drafts that never shipped; named in
#: the error so a client that sends one learns the real key.
_LEGACY_GRAIN_KEYS = frozenset({"aggregate", "explode", "many"})


def _parse_column_spec(raw: Any, path: str) -> ColumnSpec:
    _require(isinstance(raw, dict), "INVALID_QUERYSPEC_SHAPE", "must be an object like {\"path\": [\"slot\"]}", path)
    seg = raw.get("path")
    _require(
        isinstance(seg, list) and len(seg) > 0 and all(isinstance(x, str) for x in seg),
        "INVALID_QUERYSPEC_SHAPE",
        "'path' must be a non-empty list of slot names",
        f"{path}.path",
    )
    if len(seg) > MAX_COLUMN_HOPS + 1:
        raise QuerySpecShapeError(
            "COLUMNS_NOT_SUPPORTED",
            f"column path {seg!r} follows {len(seg) - 1} reference hops; the "
            f"depth cap is {MAX_COLUMN_HOPS} hops (at most {MAX_COLUMN_HOPS + 1} "
            f"path elements: hop, hop, slot) — {seg[MAX_COLUMN_HOPS]!r} is one "
            "hop too many",
            f"{path}.path[{MAX_COLUMN_HOPS}]",
        )
    extra = sorted(set(raw) - {"path", "mode"})
    if extra:
        hint = (
            " — a to-many hop's grain is chosen with 'mode': "
            f"{' | '.join(repr(m) for m in COLUMN_MODES)}"
            if _LEGACY_GRAIN_KEYS & set(extra)
            else ""
        )
        raise QuerySpecShapeError(
            "INVALID_QUERYSPEC_SHAPE", f"unknown ColumnSpec key(s) {extra}{hint}", path
        )
    mode = raw.get("mode")
    if mode is not None:
        _require(
            mode in COLUMN_MODES,
            "INVALID_QUERYSPEC_SHAPE",
            f"'mode' must be one of {list(COLUMN_MODES)} (got {mode!r})",
            f"{path}.mode",
        )
        _require(
            len(seg) > 1,
            "INVALID_QUERYSPEC_SHAPE",
            f"'mode' chooses how a to-many HOP resolves, and {seg!r} has no hop "
            "(an anchor slot is returned as stored) — drop 'mode'",
            f"{path}.mode",
        )
    return ColumnSpec(path=tuple(seg), mode=mode)


def _parse_columns(raw_columns: list) -> tuple[ColumnSpec, ...]:
    """Parse every ColumnSpec, dropping exact repeats (a repeated column is
    one column) and refusing one path asked for in two different modes —
    a result row names its columns by path, so the two would collide."""
    out: dict[tuple[str, ...], ColumnSpec] = {}
    for i, raw in enumerate(raw_columns):
        col = _parse_column_spec(raw, f"$.columns[{i}]")
        seen = out.get(col.path)
        if seen is None:
            out[col.path] = col
            continue
        _require(
            seen.mode == col.mode,
            "INVALID_QUERYSPEC_SHAPE",
            f"column path {list(col.path)!r} is asked for twice, as mode "
            f"{seen.mode!r} and {col.mode!r}; a row names its columns by path, "
            "so pick one",
            f"$.columns[{i}].mode",
        )
    return tuple(out.values())


def parse_query_spec(raw: Any) -> QuerySpec:
    """Parse a raw (JSON-decoded) object into a :class:`QuerySpec`.

    Structural only — every check here is schema-independent (a malformed
    QuerySpec is malformed on every Mosaic deployment). Raises
    :class:`QuerySpecShapeError`. Schema-dependent legality (does this
    anchor/slot/op/edge/enum-value/sort-field exist and is it usable) is
    :func:`validate_query_spec`'s job, run separately against a specific
    deployment's capability manifest.
    """
    _require(isinstance(raw, dict), "INVALID_QUERYSPEC_SHAPE", "QuerySpec must be an object", "$")
    _require(raw.get("v") == 1, "INVALID_QUERYSPEC_SHAPE", "'v' must be 1", "$.v")
    _require(isinstance(raw.get("anchor"), str), "INVALID_QUERYSPEC_SHAPE", "'anchor' must be a string", "$.anchor")
    mode = raw.get("mode")
    _require(mode in _VALID_MODES, "INVALID_QUERYSPEC_SHAPE", "'mode' must be 'AND' or 'OR'", "$.mode")
    raw_columns = raw.get("columns")
    if raw_columns is None:
        raw_columns = []
    _require(isinstance(raw_columns, list), "INVALID_QUERYSPEC_SHAPE", "'columns' must be a list", "$.columns")
    columns = _parse_columns(raw_columns)
    raw_criteria = raw.get("criteria", [])
    _require(isinstance(raw_criteria, list), "INVALID_QUERYSPEC_SHAPE", "'criteria' must be a list", "$.criteria")
    criteria = tuple(
        _parse_criterion(c, f"$.criteria[{i}]", depth=1) for i, c in enumerate(raw_criteria)
    )
    as_of = raw.get("asOf") or raw.get("as_of")
    if as_of is not None:
        _require(isinstance(as_of, str), "INVALID_QUERYSPEC_SHAPE", "'asOf' must be an ISO-8601 string", "$.asOf")
    raw_sort = raw.get("sort") or []
    _require(isinstance(raw_sort, list), "INVALID_QUERYSPEC_SHAPE", "'sort' must be a list", "$.sort")
    sort = tuple(_parse_sort_field(s, f"$.sort[{i}]") for i, s in enumerate(raw_sort))
    return QuerySpec(v=1, anchor=raw["anchor"], mode=mode, criteria=criteria, as_of=as_of, sort=sort, columns=columns)


# ---------------------------------------------------------------------------
# Validation: total, capability-manifest-driven (mirrors the TS
# validateQuerySpec, ported server-side per ADR-0009 decision 5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuerySpecError:
    """One actionable validation failure (ADR-0009 item 3: "specific and
    actionable per-criterion... load-bearing, not cosmetic"). ``code`` is a
    fixed vocabulary a generic MCP client can branch on; ``message`` names
    the offending slot/op/edge and, for enum/op mismatches, the valid set."""

    code: str
    message: str
    path: str


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[QuerySpecError, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def _contains_related_condition(criteria: tuple[Criterion, ...]) -> bool:
    for c in criteria:
        if isinstance(c, RelatedCondition):
            return True
        if isinstance(c, CriteriaGroup) and _contains_related_condition(c.criteria):
            return True
    return False


def _validate_field_condition(
    cond: FieldCondition, entity: EntityCapability, path: str, errors: list[QuerySpecError]
) -> None:
    field = entity.fields_by_name.get(cond.slot)
    if field is None:
        errors.append(
            QuerySpecError(
                "UNKNOWN_SLOT",
                f"{entity.class_name!r} has no field {cond.slot!r}. "
                f"Known fields: {sorted(entity.fields_by_name)}",
                f"{path}.slot",
            )
        )
        return
    try:
        op = FilterOp(cond.op)
    except ValueError:
        errors.append(
            QuerySpecError(
                "INVALID_QUERYSPEC_SHAPE",
                f"unknown operator {cond.op!r}. Valid operators: "
                f"{sorted(o.value for o in FilterOp)}",
                f"{path}.op",
            )
        )
        return
    if op not in field.filter_ops:
        if field.predicate:
            errors.append(
                QuerySpecError(
                    "UNSUPPORTED_OP",
                    f"{entity.class_name}.{cond.slot} is a relationship — filter "
                    f"it with a RelatedCondition (edge={cond.slot!r}), not a "
                    f"FilterOp",
                    f"{path}.op",
                )
            )
        else:
            errors.append(
                QuerySpecError(
                    "UNSUPPORTED_OP",
                    f"{entity.class_name}.{cond.slot} does not support "
                    f"{cond.op!r}. Supported: {sorted(o.value for o in field.filter_ops)}",
                    f"{path}.op",
                )
            )
        return
    value_ok = True
    if op is FilterOp.IN and not isinstance(cond.value, list):
        errors.append(QuerySpecError("INVALID_VALUE_TYPE", "'in' requires a list value", f"{path}.value"))
        value_ok = False
    elif op is FilterOp.IS_NULL and not isinstance(cond.value, bool):
        errors.append(QuerySpecError("INVALID_VALUE_TYPE", "'is_null' requires a boolean value", f"{path}.value"))
        value_ok = False
    elif op not in (FilterOp.IS_NULL,) and cond.value is None:
        errors.append(QuerySpecError("MISSING_VALUE", f"{cond.slot!r} needs a 'value' for {cond.op!r}", f"{path}.value"))
        value_ok = False
    if value_ok and field.slot.kind is SlotKind.ENUM and cond.value is not None:
        values = cond.value if op is FilterOp.IN else [cond.value]
        bad = [v for v in values if isinstance(v, str) and v not in field.slot.enum_values]
        if bad:
            errors.append(
                QuerySpecError(
                    "INVALID_ENUM_VALUE",
                    f"{entity.class_name}.{cond.slot} does not accept {bad!r}. "
                    f"Valid values: {list(field.slot.enum_values)}",
                    f"{path}.value",
                )
            )


def _validate_related_condition(
    cond: RelatedCondition,
    entity: EntityCapability,
    manifest: dict[str, EntityCapability],
    path: str,
    errors: list[QuerySpecError],
) -> None:
    field = entity.fields_by_name.get(cond.edge)
    if field is None or field.slot.kind is not SlotKind.REFERENCE:
        errors.append(
            QuerySpecError(
                "UNKNOWN_EDGE",
                f"{entity.class_name!r} has no relationship {cond.edge!r}. "
                f"Known relationships: "
                f"{sorted(f.slot.name for f in entity.fields if f.slot.kind is SlotKind.REFERENCE)}",
                f"{path}.edge",
            )
        )
        return
    if not field.predicate:
        errors.append(
            QuerySpecError(
                "NOT_PREDICATE_FILTERABLE",
                f"{entity.class_name}.{cond.edge} targets a type not exposed "
                f"on this deployment — cannot filter through it",
                f"{path}.edge",
            )
        )
        return
    target = manifest.get(field.slot.target_class)
    if target is None:
        errors.append(
            QuerySpecError("NOT_PREDICATE_FILTERABLE", f"target type {field.slot.target_class!r} is not exposed", f"{path}.edge")
        )
        return
    for i, sub in enumerate(cond.criteria):
        _validate_field_condition(sub, target, f"{path}.criteria[{i}]", errors)


def _validate_criteria(
    criteria: tuple[Criterion, ...],
    entity: EntityCapability,
    manifest: dict[str, EntityCapability],
    path: str,
    errors: list[QuerySpecError],
) -> None:
    for i, c in enumerate(criteria):
        cpath = f"{path}[{i}]"
        if isinstance(c, FieldCondition):
            _validate_field_condition(c, entity, cpath, errors)
        elif isinstance(c, RelatedCondition):
            _validate_related_condition(c, entity, manifest, cpath, errors)
        elif isinstance(c, CriteriaGroup):
            _validate_criteria(c.criteria, entity, manifest, f"{cpath}.criteria", errors)


def _validate_column(
    col: ColumnSpec,
    anchor: EntityCapability,
    manifest: dict[str, EntityCapability],
    path: str,
    errors: list[QuerySpecError],
    as_of: Optional[str] = None,
) -> Optional[tuple[str, ...]]:
    """Validate one ColumnSpec hop by hop (issue #236).

    Returns the column's to-many LINK — the path up to and including its
    to-many hop, which is what an ``explode`` repeats rows over — or
    ``None`` when the column has no to-many hop or is invalid.
    """
    if not col.is_traversal:
        # Anchor-owned (#215): unchanged, including the error path.
        if col.slot not in anchor.fields_by_name:
            errors.append(
                QuerySpecError(
                    "UNKNOWN_SLOT",
                    f"{anchor.class_name!r} has no field {col.slot!r}. "
                    f"Known fields: {sorted(anchor.fields_by_name)}",
                    f"{path}.path",
                )
            )
        return None
    entity = anchor
    to_many: list[int] = []
    for j, hop in enumerate(col.path[:-1]):
        hop_path = f"{path}.path[{j}]"
        field = entity.fields_by_name.get(hop)
        if field is None:
            errors.append(
                QuerySpecError(
                    "UNKNOWN_SLOT",
                    f"{entity.class_name!r} has no field {hop!r} (hop {j + 1} "
                    f"of column path {list(col.path)!r}). Known references: "
                    f"{sorted(f.slot.name for f in entity.fields if f.slot.kind is SlotKind.REFERENCE)}",
                    hop_path,
                )
            )
            return None
        if field.slot.kind is not SlotKind.REFERENCE:
            errors.append(
                QuerySpecError(
                    "NOT_A_REFERENCE",
                    f"{entity.class_name}.{hop} is a {field.slot.kind.value} "
                    f"field, not a reference, so column path {list(col.path)!r} "
                    f"cannot follow it as hop {j + 1} — put {hop!r} last to "
                    "show its value",
                    hop_path,
                )
            )
            return None
        target = manifest.get(field.slot.target_class) if field.predicate else None
        if target is None:
            errors.append(
                QuerySpecError(
                    "NOT_PREDICATE_FILTERABLE",
                    f"{entity.class_name}.{hop} targets "
                    f"{field.slot.target_class!r}, which is not exposed on this "
                    f"deployment — column path {list(col.path)!r} cannot "
                    "traverse it",
                    hop_path,
                )
            )
            return None
        if field.slot.inverse_of is not None and as_of is not None:
            # As-of reconstruction rebuilds an entity's stored slots, not
            # the virtual reverse lists derived from other entities' FKs
            # (ADR-0011), so the hop would read as empty — refuse it rather
            # than answer wrongly. Forward hops reconstruct and stay legal.
            errors.append(
                QuerySpecError(
                    "ASOF_REVERSE_HOP_UNSUPPORTED",
                    f"{entity.class_name}.{hop} is a reverse edge (inverse of "
                    f"{field.slot.target_class}.{field.slot.inverse_of}), and "
                    "'asOf' cannot reconstruct a reverse edge — drop 'asOf', or "
                    f"anchor on {field.slot.target_class} and follow "
                    f"{field.slot.inverse_of!r} forward instead",
                    hop_path,
                )
            )
            return None
        if field.slot.multivalued:
            to_many.append(j)
        entity = target
    leaf = col.path[-1]
    if leaf not in entity.fields_by_name:
        errors.append(
            QuerySpecError(
                "UNKNOWN_SLOT",
                f"{entity.class_name!r} has no field {leaf!r} (the leaf of "
                f"column path {list(col.path)!r}). Known fields: "
                f"{sorted(entity.fields_by_name)}",
                f"{path}.path[{len(col.path) - 1}]",
            )
        )
        return None
    if len(to_many) > 1:
        second = to_many[1]
        errors.append(
            QuerySpecError(
                "COLUMNS_NOT_SUPPORTED",
                f"column path {list(col.path)!r} has two to-many hops "
                f"({col.path[to_many[0]]!r} and {col.path[second]!r}); at most "
                "one hop on a path may be to-many",
                f"{path}.path[{second}]",
            )
        )
        return None
    if not to_many:
        if col.mode is not None:
            errors.append(
                QuerySpecError(
                    "INVALID_QUERYSPEC_SHAPE",
                    f"column path {list(col.path)!r} has no to-many hop (every "
                    "hop is a single reference), so it resolves to one value "
                    f"per row — drop 'mode' ({col.mode!r})",
                    f"{path}.mode",
                )
            )
        return None
    many = to_many[0]
    if col.mode is None:
        errors.append(
            QuerySpecError(
                "COLUMNS_NOT_SUPPORTED",
                f"hop {col.path[many]!r} of column path {list(col.path)!r} is "
                "to-many, so the column needs 'mode': 'count' or 'joinIds' "
                "(one row per anchor) or 'explode' (one row per anchor x "
                "member)",
                f"{path}.path[{many}]",
            )
        )
        return None
    return tuple(col.path[: many + 1])


def validate_query_spec(
    spec: QuerySpec, manifest: dict[str, EntityCapability]
) -> ValidationResult:
    """Total, capability-manifest-driven validation (ADR-0009 item 3).

    Every anchor/slot/op/edge/enum-value/sort-field is checked against what
    this specific deployment's schema actually advertises. A ``QuerySpec``
    that validates cleanly compiles to a query the ``where:``/aggregation
    surface accepts.
    """
    errors: list[QuerySpecError] = []
    anchor = manifest.get(spec.anchor)
    if anchor is None:
        return ValidationResult(
            errors=(
                QuerySpecError(
                    "UNKNOWN_ANCHOR",
                    f"unknown anchor {spec.anchor!r}. Known types: {sorted(manifest)}",
                    "$.anchor",
                ),
            )
        )
    _validate_criteria(spec.criteria, anchor, manifest, "$.criteria", errors)
    if spec.as_of is not None and _contains_related_condition(spec.criteria):
        errors.append(
            QuerySpecError(
                "ASOF_RELATIONSHIP_FILTER_UNSUPPORTED",
                "'asOf' cannot combine with a RelatedCondition anywhere in "
                "'criteria' (ADR-0001)",
                "$.asOf",
            )
        )
    if len(spec.sort) > 1:
        errors.append(
            QuerySpecError(
                "MULTI_COLUMN_SORT_UNSUPPORTED",
                "Mosaic's query surface takes a single order_by/order_dir "
                "pair (matches GraphQL's single-valued orderBy argument) — "
                "'sort' must carry at most one field, not "
                f"{len(spec.sort)}",
                "$.sort",
            )
        )
    exploded_link: Optional[tuple[str, ...]] = None
    for i, col in enumerate(spec.columns):
        link = _validate_column(
            col, anchor, manifest, f"$.columns[{i}]", errors, as_of=spec.as_of
        )
        if link is None or col.mode != "explode":
            continue
        if exploded_link is None:
            exploded_link = link
        elif link != exploded_link:
            errors.append(
                QuerySpecError(
                    "COLUMNS_NOT_SUPPORTED",
                    f"column {list(col.path)!r} explodes through "
                    f"{'.'.join(link)!r}, but {'.'.join(exploded_link)!r} is "
                    "already exploded; at most one link may explode per spec "
                    "(two would be a cartesian product) — use 'count' or "
                    "'joinIds' for this one",
                    f"$.columns[{i}].mode",
                )
            )
    for i, s in enumerate(spec.sort):
        field = anchor.fields_by_name.get(s.slot)
        if field is None:
            errors.append(
                QuerySpecError(
                    "UNKNOWN_SLOT",
                    f"{anchor.class_name!r} has no field {s.slot!r}",
                    f"$.sort[{i}].slot",
                )
            )
        elif not field.orderable:
            errors.append(
                QuerySpecError(
                    "UNORDERABLE_SORT_FIELD",
                    f"{anchor.class_name}.{s.slot} is not orderable "
                    f"(multivalued, reference, or structured fields have no "
                    f"meaningful order)",
                    f"$.sort[{i}].slot",
                )
            )
    return ValidationResult(errors=tuple(errors))
