# Heterogeneous Roots (`searchAll` + `neighbors` JSON-envelope queries)

Tracking issue: datahelix-platform/mosaic#158

## Why

Every GraphQL query root is per-class; the only cross-class roots are `findByXref` and
`relatedTo`. A consumer cannot ask "what matches *q* anywhere?" (global search requires a
client-side fan-out over every per-class search twin) or "what surrounds this entity?" (a
graph neighborhood requires per-entity detail queries, one hop at a time). Aperture's
global-search landing surface and its graph exploration view need both as single requests
(aperture `design/cross-class-query.md` §7 M4, §8).

The house already has the pattern: `XrefMatch` and `RelatedEntity` (`graphql/resolvers.py`)
are heterogeneous **JSON envelopes** — `{entityId, entityType, data: JSON, …}` with typed
follow-up via the per-type queries — deliberately avoiding a GraphQL union/interface root
(the `Entity` class is excluded from the schema by design; ADR-0005 flags union/interface
polymorphism as future work).

## What Changes

Two new root queries following the envelope pattern:

- **`searchAll(q, limit)`** — fans across every FTS-indexed class server-side, merges by
  rank, returns `[SearchHit]` envelopes (`entityId`, `entityType`, `score`, `data: JSON`,
  temporal fields). Degrades nothing: per-class search twins remain.
- **`neighbors(id, depth, asOf)`** — returns `{nodes: [{entityId, entityType, data}], edges:
  [{source, target, type}]}` by wrapping the existing depth-bounded BFS
  `RelationshipManager.traverse` — which already has a full as-of variant replaying edge
  liveness from provenance events, so the graph view gets time travel for free.

### The single-valued-edge gap (must address)

`traverse` walks the `relationships` link table only. Since ADR-0002, that table holds
relationship-backed **multivalued** reference edges — but **single-valued references are
stored as columns** and are invisible to it. A naive `neighbors` therefore returns an
incomplete graph (a Sample→Donor edge stored as `donor_id` would not appear). This change
must either:

1. **Union column edges into the traversal** (preferred): at each BFS frontier, read the
   frontier entities' single-valued reference slots (forward edges) and, for reverse
   direction, query referencing classes by FK value — schema-driven via `SlotModel`, no
   domain knowledge; or
2. **Document the hole loudly** as an explicit, introspectable limitation (envelope carries
   `edgeSources: [LINK_TABLE]`) — acceptable only as a stopgap increment, with the union as
   the accepted end state.

The as-of variant of column-edge traversal must reconstruct the column value at `T` (per-slot
provenance state), or `asOf` neighbors declares column edges out of scope with a coded
notice — never a silently partial temporal graph.

### Materialization discipline

Node payloads are materialized **batched by type** (group frontier ids by `entityType`, one
`find()`/DataLoader-style batched read per type — the `get_entity_loader` precedent), never
get-per-node.

### Out of scope

A true GraphQL union/interface root (`search: [Entity!]`). It inverts the deliberate `Entity`
exclusion from the schema and is flagged by ADR-0005 as future work; if pursued later, it is
its own ADR.

## Capabilities

### New Capabilities

- `global-search` — `searchAll(q, limit)` heterogeneous ranked search in one request.
- `graph-neighborhood` — `neighbors(id, depth, asOf)` subgraph envelope with complete edge
  coverage (link-table + column edges) and provenance-replayed time travel.

### Modified Capabilities

- none (purely additive roots; per-class queries and `findByXref`/`relatedTo` unchanged).

## Dependencies

- `search-composition` (#157): `searchAll` should reuse the composed, non-N+1 search path
  and its rank-merge semantics rather than duplicating the twin path's defects.
- Related decisions: ADR-0005 (envelope-over-union posture), ADR-0002 (why edges live in two
  places — the gap this change must close), ADR-0001 (as-of semantics `neighbors` inherits).
- Driver / cross-reference: Aperture `design/cross-class-query.md` §7 M4 and §8 (graph view
  primitive); Aperture ADR-0035/0036/0037 (Proposed slate).

## Acceptance

- `searchAll` returns rank-merged hits across all FTS-indexed classes in one request, with
  per-page batched materialization (no per-hit reads), availability-filtered exactly as list
  queries are.
- `neighbors` on a fixture where an entity has both a link-table edge and a column-stored
  single-valued reference returns **both** edges (the gap test — this is the headline
  assertion), on both adapters.
- `neighbors(depth=N)` respects the depth bound; edges carry source/target/type sufficient to
  render without further queries; nodes materialize batched by type (instrumented).
- `neighbors(asOf=T)` replays link-table edge liveness from provenance (existing behavior)
  and handles column edges per the pinned decision — with a coded notice if scoped out, never
  a silently partial graph.
- Both roots are visible to introspection with full SDL descriptions (capability gating reads
  presence off `__schema`).
- Full suite green; purely additive — no existing query shape changes.
