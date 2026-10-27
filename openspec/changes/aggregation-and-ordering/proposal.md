# Aggregation and Ordering (count mode, facet counts, min/max, order_by + pushdown)

Tracking issue: datahelix-platform/mosaic#156

## Why

The list surface cannot aggregate or order. `Page.total` exists but costs full
materialization: `QueryService` builds the storage `Query` without limit/offset
(`core/query_service.py`), loads every matching row, and pages in Python — so "how many
match?" is as expensive as "give me everything." There are no facet value counts, no min/max,
and the only ordering anywhere is a hardcoded Python-side `created_at` sort. The one
aggregation precedent, `entity_counts()`, counts unavailable entities while every list query
filters `is_available = 1` — a trap waiting to be copied.

Aperture's portal requirement **X1** (aggregation primitive: facet/group-by counts,
`totalCount`, range filters, `order_by` — its top cross-component ask, previously tracked as
hippo#96, which has zero footprint in this working tree) is blocked on exactly this. **ADR-0007**
records the decision and its two pinned rules; this change implements it. Exploration:
aperture `design/cross-class-query.md` §7 (M2).

## What Changes

One change, because every piece shares the same enabling work: **limit/offset/order pushdown
into the storage `Query` and both adapters**.

### Increment 1 — Pushdown + count mode

- `Query` grows `limit`/`offset`/`order_by`; both adapters apply them in SQL. The Python-side
  paging in `QueryService` becomes the fallback for adapters that don't advertise pushdown.
- Preserve the `limit=0` → zero rows discipline (#130) end to end.
- Cheap filtered count (`SELECT COUNT(*)` under the identical predicate + `is_available = 1`);
  `Page.total` resolved from it. SDK-first: `MosaicClient` count API, GraphQL resolves through
  it.

### Increment 2 — Facet counts + min/max

- `facetCounts(field, where)` — per-value counts for one filterable slot under a filter
  (GROUP BY on the slot column / JSONB key). The `entity_counts()` GROUP BY shape is the
  mechanical precedent only — **the availability-consistency rule applies** (below).
- Min/max per numeric/date slot under the same filter, for range facets.

### Increment 3 — order_by

- Generated per-type `order_by` enum from `filterable_slot_names()` + direction, pushed down
  as SQL `ORDER BY` (Postgres with the shared per-range cast helper).
- Computed temporal fields per ADR-0007: `created_at`/`updated_at` are provenance-derived,
  never column sorts — either via the provenance-summary path with documented cost, or
  omitted from the enum in this increment (decide at implementation; ADR-0007 forbids only
  pretending they are columns).

### Pinned rules (ADR-0007)

- **Availability consistency:** every aggregate sees exactly what list queries see
  (`is_available = 1` + caller filters). `entity_counts()` keeps its registry semantics for
  existing callers but is not the model.
- **As-of honesty:** aggregations under `asOf` either compute over the Python-reconstructed
  set (documented as such) or return a coded error in the first increment; never silently
  ignore `asOf`.

## Capabilities

### New Capabilities

- `filtered-count` — cheap filtered totals without materialization. *(Increment 1.)*
- `facet-aggregation` — per-value counts and min/max per slot under a filter.
  *(Increment 2.)*
- `order-by` — generated per-type ordering pushed down to SQL. *(Increment 3.)*

### Modified Capabilities

- `storage-adapter-contract` — `Query` carries limit/offset/order; adapters push down
  (parity discipline: identical counts and orderings on SQLite and Postgres).
- `graphql-list-queries` — `Page.total` becomes cheap; new `facetCounts`/min-max fields;
  `orderBy` argument.

## Dependencies

- **ADR-0007** (design authority; Proposed — ratification tracked in #154).
- Shares the Postgres per-range JSONB cast helper with `typed-filter-inputs` (#155) — common
  code, whichever lands first.
- `search-composition` (#157) reuses the page envelope + cheap `total`, and defines bm25-rank
  vs. `order_by` precedence.
- Driver / cross-reference: Aperture `design/portal-requirements.md` X1; Aperture
  `design/cross-class-query.md` §7 M2; Aperture ADR-0035 (Proposed).

## Acceptance

- A filtered count query performs no row hydration (asserted via adapter instrumentation) and
  equals `len(items)` of the equivalent unpaged list query — on both adapters.
- `facetCounts`, min/max, and counts **exclude unavailable entities** exactly as list queries
  do; a test seeds unavailable entities and asserts aggregates match visible lists (the
  `entity_counts()` divergence is documented, not inherited).
- `orderBy` produces identical orderings on SQLite and Postgres, including numeric/date slots
  (cast correctness), with stable tie-breaking.
- `limit=0` returns zero rows with a correct `total` everywhere (#130 preserved).
- `asOf` + aggregation behaves per the pinned rule (documented Python path or coded error) —
  never a silently-wrong number.
- Existing list-query behavior unchanged for callers that pass no new arguments; full suite
  green.
