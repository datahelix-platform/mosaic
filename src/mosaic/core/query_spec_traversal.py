"""Server-side hydration of traversal ``columns`` (issue #236, ADR-0009 note
2026-10-27).

:func:`project_traversal` takes one page of anchor envelopes (read with full
``data``, so every hop slot is present) and a compiled
:class:`~mosaic.core.query_spec_compiler.TraversalProjection`, and returns
the result rows: each anchor envelope with ``data`` trimmed to the
anchor-owned columns and a ``columns`` object holding every column's value,
keyed by its dot-joined path. Semantics follow Aperture ADR-0041's
client-side flattener (``web/src/data/selection.ts``), which this replaces
server-side without changing the artifact:

- a to-one chain yields the leaf value (``None`` if any hop is unset or its
  target is unavailable);
- ``count`` yields the number of members of the to-many hop;
- ``joinIds`` yields the list of the members' leaf values, in member order,
  skipping members with no value (``["books", "id"]`` is the members' ids);
- ``explode`` yields one row per (anchor x member), the anchor repeated; an
  anchor with no members still gets one row, with ``None`` in the exploded
  columns, so an explode never silently becomes a filter. Every exploded
  column through the same link reads the same member. Members are *all*
  members — criteria select anchors, not members.

**Batched per hop, adapter-agnostic.** Every hop slot — a stored FK, a
relationships-backed multivalued reference, or a virtual ``inverse_of``
reverse — is already hydrated into ``data`` as ids by both storage adapters
(the reverse over the range's concrete closure, ``InverseSlot.tables``,
#224). So a hop is: collect the ids for the whole page, resolve their
concrete types in one round trip, then read each type's rows with one
``id in [...]`` query. Two round-trip groups per hop level at most, shared
across columns by a per-call cache — the GraphQL DataLoader discipline. A
member is read under the concrete class that actually holds it, never the
hop's declared range, so nothing here assumes a reference resolves only in
its range's own table.
"""

from __future__ import annotations

from typing import Any, Optional

from mosaic.core.query_spec_compiler import TraversalColumn, TraversalProjection


class _EntityCache:
    """Per-call id -> envelope cache with batched, type-resolved loads."""

    def __init__(self, client: Any, as_of: Optional[str]):
        self._client = client
        self._as_of = as_of
        self._by_id: dict[str, Optional[dict[str, Any]]] = {}

    def load(self, ids: list[str]) -> None:
        missing = list(dict.fromkeys(i for i in ids if i not in self._by_id))
        if not missing:
            return
        by_type: dict[str, list[str]] = {}
        for eid, etype in self._client.resolve_types(missing).items():
            by_type.setdefault(etype, []).append(eid)
        for etype, type_ids in by_type.items():
            page = self._client.query(
                entity_type=etype,
                where={"field": "id", "op": "in", "value": type_ids},
                as_of=self._as_of,
            )
            for item in page.items:
                self._by_id[item["id"]] = item
        # Unknown or unavailable ids resolve to nothing, and stay resolved.
        for eid in missing:
            self._by_id.setdefault(eid, None)

    def get(self, eid: Any) -> Optional[dict[str, Any]]:
        return self._by_id.get(eid) if isinstance(eid, str) else None


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _slot_value(entity: dict[str, Any], slot: str) -> Any:
    data = entity.get("data") or {}
    if slot in data:
        return data[slot]
    # ``id`` and the computed temporal fields live on the envelope.
    return entity.get(slot)


def _prefetch(
    cache: _EntityCache, anchors: list[dict[str, Any]], columns: tuple[TraversalColumn, ...]
) -> None:
    """Load every hop's targets, one batch per hop level across all columns."""
    frontier: list[tuple[TraversalColumn, dict[str, Any]]] = [
        (c, a) for c in columns if c.hops for a in anchors
    ]
    depth = 0
    while frontier:
        ids = [
            eid for col, ent in frontier for eid in _ids(_slot_value(ent, col.hops[depth].slot))
        ]
        cache.load(ids)
        nxt: list[tuple[TraversalColumn, dict[str, Any]]] = []
        for col, ent in frontier:
            if depth + 1 >= len(col.hops):
                continue
            for eid in _ids(_slot_value(ent, col.hops[depth].slot)):
                target = cache.get(eid)
                if target is not None:
                    nxt.append((col, target))
        frontier = nxt
        depth += 1


def _follow(cache: _EntityCache, entity: Optional[dict[str, Any]], hops) -> Optional[dict[str, Any]]:
    """Walk to-one hops; ``None`` once any is unset or unavailable."""
    for hop in hops:
        if entity is None:
            return None
        entity = cache.get(_slot_value(entity, hop.slot))
    return entity


def _members(cache: _EntityCache, anchor: dict[str, Any], col: TraversalColumn) -> list[dict[str, Any]]:
    """The resolved (available) members of ``col``'s to-many hop."""
    at = col.many_at
    holder = _follow(cache, anchor, col.hops[:at])
    if holder is None:
        return []
    out = []
    for eid in dict.fromkeys(_ids(_slot_value(holder, col.hops[at].slot))):
        member = cache.get(eid)
        if member is not None:
            out.append(member)
    return out


def _leaf_from_member(cache: _EntityCache, member: Optional[dict[str, Any]], col: TraversalColumn) -> Any:
    end = _follow(cache, member, col.hops[col.many_at + 1 :])
    return None if end is None else _slot_value(end, col.leaf)


def _anchor_grain_value(cache: _EntityCache, anchor: dict[str, Any], col: TraversalColumn) -> Any:
    if col.many_at is None:
        end = _follow(cache, anchor, col.hops)
        return None if end is None else _slot_value(end, col.leaf)
    members = _members(cache, anchor, col)
    if col.mode == "count":
        return len(members)
    values = [_leaf_from_member(cache, m, col) for m in members]
    return [v for v in values if v is not None]


def project_traversal(
    client: Any,
    items: list[dict[str, Any]],
    projection: TraversalProjection,
    *,
    as_of: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Hydrate ``projection`` for one page of anchor envelopes.

    ``items`` must carry full ``data`` (every hop slot). Returns
    ``(rows, grain)``; ``grain`` is ``None`` unless a column explodes, else
    ``{"anchor": <anchors on the page>, "rows": <rows>, "edge": <the
    exploded to-many slot>}``. Referenced entities are read under ``as_of``
    when given, so a traversal sees the same point in time as its anchors.
    """
    cache = _EntityCache(client, as_of)
    _prefetch(cache, items, projection.columns)
    keep = set(projection.anchor_fields)
    exploded = projection.explode
    link = exploded.link if exploded else None

    rows: list[dict[str, Any]] = []
    for anchor in items:
        base = {**anchor, "data": {k: v for k, v in (anchor.get("data") or {}).items() if k in keep}}
        fixed = {
            c.key: _anchor_grain_value(cache, anchor, c)
            for c in projection.columns
            if not (link is not None and c.mode == "explode" and c.link == link)
        }
        if link is None:
            rows.append({**base, "columns": fixed})
            continue
        members: list[Optional[dict[str, Any]]] = _members(cache, anchor, exploded) or [None]
        for member in members:
            values = {}
            for c in projection.columns:
                if c.mode == "explode" and c.link == link:
                    values[c.key] = _leaf_from_member(cache, member, c)
                else:
                    values[c.key] = fixed[c.key]
            rows.append({**base, "columns": values})

    grain = None
    if exploded is not None:
        grain = {"anchor": len(items), "rows": len(rows), "edge": exploded.hops[exploded.many_at].slot}
    return rows, grain
