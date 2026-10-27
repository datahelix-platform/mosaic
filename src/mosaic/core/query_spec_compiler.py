"""Compiles a validated :class:`~mosaic.core.query_spec.QuerySpec` into
``MosaicClient.query``'s argument shape (ADR-0009 decision 3, issue #183's
remaining half).

**Precondition: the caller validates first.** This module trusts its
input — it does not re-derive anything :func:`~mosaic.core.query_spec.
validate_query_spec` already checked (anchor/slot/op/edge legality, enum
values, the asOf+relationship-predicate exclusion, the single-sort-column
cap). ``execute_query_spec`` (the MCP tool built on this) validates
unconditionally before ever calling :func:`compile_query_spec` — see its
docstring for why that ordering is load-bearing, not just tidy.

Compiles ONLY to ``client.query()``'s ``where``/``order_by``/``as_of``
shape, plus ``fields`` — the anchor-owned ``columns`` selection (issue #215)
that ``MosaicClient.query(fields=...)`` projects. No ``facet_counts``/
``field_range``/``search`` compilation here — ``QuerySpec`` (ADR-0035) has no
representation for those yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mosaic.core.query_spec import (
    CriteriaGroup,
    Criterion,
    FieldCondition,
    QuerySpec,
    RelatedCondition,
)
from mosaic.core.schema_typing import EntityCapability


@dataclass(frozen=True)
class CompiledQuery:
    """``MosaicClient.query()``'s keyword arguments, pre-filled."""

    entity_type: str
    where: Optional[dict[str, Any]]
    as_of: Optional[str]
    order_by: Optional[str]
    order_dir: str
    #: Anchor slots to keep in each envelope's ``data``; ``None`` = full envelopes.
    fields: Optional[tuple[str, ...]] = None


def _compile_field_condition(cond: FieldCondition) -> dict[str, Any]:
    return {"field": cond.slot, "op": cond.op, "value": cond.value}


def _identifier_slot_name(entity: EntityCapability) -> str:
    for f in entity.fields:
        if f.slot.identifier:
            return f.slot.name
    return "id"  # pragma: no cover - every exposed class carries one


def _compile_related_condition(
    cond: RelatedCondition, entity: EntityCapability, manifest: dict[str, EntityCapability]
) -> dict[str, Any]:
    field = entity.fields_by_name[cond.edge]
    target = manifest[field.slot.target_class]
    if cond.criteria:
        compiled = [_compile_field_condition(c) for c in cond.criteria]
        inner = compiled[0] if len(compiled) == 1 else {"and": compiled}
    else:
        # No sub-criteria: "has/has no related record at all" — a
        # trivially-true predicate on the target's own identifier stands
        # in for "exists, full stop" (the storage layer's `where` node
        # requires a non-empty `where`; see normalize_where's docstring).
        inner = {"field": _identifier_slot_name(target), "op": "is_null", "value": False}

    if field.slot.multivalued:
        # To-many: the storage layer's quantifier is explicit either way.
        return {"edge": cond.edge, "quantifier": cond.quantifier, "where": inner}
    # To-one: the storage layer has no quantifier for this shape at all —
    # the bare {"edge", "where"} node already means "some" ("target
    # exists, is available, and satisfies the tree" per normalize_where's
    # docstring); negating it is exactly "none".
    node = {"edge": cond.edge, "where": inner}
    return {"not": node} if cond.quantifier == "none" else node


def _compile_criterion(
    c: Criterion, entity: EntityCapability, manifest: dict[str, EntityCapability]
) -> dict[str, Any]:
    if isinstance(c, FieldCondition):
        return _compile_field_condition(c)
    if isinstance(c, RelatedCondition):
        return _compile_related_condition(c, entity, manifest)
    if isinstance(c, CriteriaGroup):
        return _compile_criteria(c.criteria, c.mode, entity, manifest)
    raise TypeError(f"unknown criterion type: {type(c).__name__}")  # pragma: no cover


def _compile_criteria(
    criteria: tuple[Criterion, ...],
    mode: str,
    entity: EntityCapability,
    manifest: dict[str, EntityCapability],
) -> Optional[dict[str, Any]]:
    if not criteria:
        return None
    compiled = [_compile_criterion(c, entity, manifest) for c in criteria]
    return compiled[0] if len(compiled) == 1 else {mode.lower(): compiled}


def compile_query_spec(
    spec: QuerySpec, manifest: dict[str, EntityCapability]
) -> CompiledQuery:
    """Compile ``spec`` into ``MosaicClient.query()``'s keyword arguments.

    Callers MUST validate ``spec`` against the same ``manifest`` first
    (:func:`mosaic.core.query_spec.validate_query_spec`) — this function
    assumes every anchor/slot/op/edge/enum-value is already known-good and
    raises no coded errors of its own; a ``KeyError`` here on an
    unvalidated spec is a caller bug, not a query-time condition.
    """
    anchor = manifest[spec.anchor]
    where = _compile_criteria(spec.criteria, spec.mode, anchor, manifest)
    sort = spec.sort[0] if spec.sort else None
    return CompiledQuery(
        entity_type=spec.anchor,
        where=where,
        as_of=spec.as_of,
        order_by=sort.slot if sort else None,
        order_dir=sort.direction if sort else "asc",
        # Order-preserving dedupe: a repeated column is one column.
        fields=tuple(dict.fromkeys(c.slot for c in spec.columns)) or None,
    )
