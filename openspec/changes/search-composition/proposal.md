# Search Composition (page envelope + filters via id-set composition)

Tracking issue: datahelix-platform/mosaic#157

## Why

The per-class GraphQL search twins are second-class citizens of the query surface, and the
path has three shipped defects:

1. **Per-hit N+1:** `QueryService.search` calls `self.get(entity_type, entity_id)` once per
   FTS hit (`core/query_service.py`), so a 100-hit search issues 100 entity reads plus their
   temporal aggregations.
2. **No page envelope:** search resolvers return a bare list — no `total`, no honest
   pagination metadata — while every list query returns `Page`.
3. **`offset >= limit → []` slice bug:** the resolver fetches `limit` results and then slices
   `results[offset : offset + limit]` (`graphql/resolvers.py`), so any offset ≥ limit returns
   empty regardless of how many entities match. Deep paging through search results is silently
   broken today.

Search also composes with nothing: it cannot take `filters:`/`where:`, so "search *within* a
cohort" is inexpressible — search and facets are mutually exclusive on the consumer side
(Aperture must choose one or fake the other). Exploration: aperture
`design/cross-class-query.md` §7 (M3).

## What Changes

A small rewrite of the search path rather than a patch, on the **id-set composition** shape:

- **Ranked FTS ids feed `find()`:** the SQLite adapter's good bm25 path (`search` →
  `ScoredMatch`, already ranked and scored) produces an ordered id list; the search service
  feeds it as an `id IN (…)` filter into the normal `find()` path **alongside the caller's
  `filters:`/`where:`**. One batched read replaces N gets; availability filtering, filter
  semantics, and (once landed) typed operators apply identically to searched and listed
  results.
- **Page envelope:** search twins return the same `Page` shape as list queries (`items`,
  `total`, `limit`, `offset`), with `total` = matching-and-filtered count. The
  `offset >= limit` slice bug dies with the resolver-side slicing.
- **Rank precedence decision (to pin in this change):** when the caller passes no explicit
  ordering, results come back in bm25 rank order (the point of search); an explicit `order_by`
  (once `aggregation-and-ordering` lands) overrides rank. Recommended and to be confirmed at
  design review.
- **Postgres:** the Postgres adapter's search path composes the same way (its FTS mechanism
  differs; the id-set composition contract is adapter-independent). Parity discipline applies.

## Capabilities

### New Capabilities

- `search-composition` — search accepts the same filter arguments as list queries and returns
  the same page envelope; FTS becomes just another criterion.

### Modified Capabilities

- `graphql-search-twins` — **breaking shape change** (bare list → page envelope). Early
  software; sequenced with a version bump and consumer notice (Aperture is the known
  consumer; its exploration asks for exactly this envelope).
- `query-service-search` — N+1 replaced by one composed `find()`.

## Dependencies

- Benefits from `aggregation-and-ordering` (#156) for cheap `total` and the `order_by`
  precedence rule, and from `typed-filter-inputs` (#155) for `where:` on search — but the
  envelope fix, the N+1 fix, and the slice-bug fix do not wait on either: `filters:` + IN
  composition works against the shipped v0.12 surface.
- Related decisions: ADR-0006 (#153) for the filter argument it composes with.
- Driver / cross-reference: Aperture `design/cross-class-query.md` §7 M3 ("search that
  composes"); Aperture ADR-0035 (Proposed).

## Acceptance

- A search with `offset >= limit` returns the correct page (regression test for the slice
  bug), and paging through the full hit set visits every hit exactly once.
- Search issues one batched storage read for the page (no per-hit `get`; asserted via adapter
  instrumentation), including temporal-summary batching.
- Search twins return `Page` with a `total` that honors both the FTS match set and the
  composed filters; `total` excludes unavailable entities exactly as list queries do.
- Search + `filters:` returns the intersection (ranked); search with no ordering returns bm25
  rank order; explicit `order_by` (when available) overrides rank — identically on both
  adapters.
- Coded errors for unknown filter fields on the search path match the list-query discipline
  (#149).
- Full suite green; the breaking envelope change is called out in the changelog.
