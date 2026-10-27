# ADR-0007: Aggregation & ordering surface for list queries

- **Status:** Accepted
- **Date:** 2026-08-19 (ratified same day; see #154)
- **Deciders:** labadorf
- **Related:** sec4 §4.7 (GraphQL transport; `Page` envelope), sec6 (temporal fields are
  provenance-computed, never stored — the ordering constraint below), ADR-0006 (typed filter
  contract — aggregations take the same `where:` argument), ADR-0001 (as-of reads);
  **Aperture `design/portal-requirements.md` X1** (aggregation primitive — facet/group-by
  counts, `totalCount`, range filters, `order_by`; the top cross-component ask, previously
  tracked as hippo#96, which has **zero footprint** in this working tree — this ADR fills a
  void rather than reconciling prior design), **Aperture `design/cross-class-query.md` §7 M2**
  (the co-design exploration), **Aperture ADR-0035** (Accepted 2026-08-19 — the QuerySpec
  artifact whose counts/sort compile to this surface). Code landing sites cited inline
  (mosaic @ 502991c, v0.12.1).
- **Tracking issue:** [#154](https://github.com/datahelix-platform/mosaic/issues/154)
  (implementation: OpenSpec `aggregation-and-ordering`,
  [#156](https://github.com/datahelix-platform/mosaic/issues/156))

## Context

The list surface can page but cannot aggregate or order:

- `Page.total` exists (`graphql/schema_builder.py:641`, `core/query_service.py:270`) but is
  computed by **materializing every matching row**: the storage `Query` is built without
  limit/offset (`query_service.py:184-188`), all rows are loaded, and paging happens in Python.
  A "how many match?" question costs a full scan and full hydration.
- There are no facet value counts, no group-by, no min/max — Aperture's faceted browse cannot
  show counts, ranges, or predicted result sizes without lying (its ADR-0029 forbids a count
  computed over a partial page).
- The only ordering anywhere is a hardcoded Python-side `created_at` sort
  (`query_service.py:268`). There is no `order_by` on the SDK, REST, or GraphQL surface.

Aperture's portal requirement **X1** names exactly this bundle — facet/group-by counts,
`totalCount`, range filters, `order_by` on the GraphQL list surface — as its top
cross-component ask (unblocks live per-criterion attrition counts, the cohort-builder
trust-builder its exploration identifies).

Two existing facts shape the design:

- **Availability inconsistency trap:** the one aggregation precedent, `entity_counts()`,
  counts **all** entities including unavailable ones (SQLite
  `sqlite_adapter.py:3034-3053`, Postgres `postgres_adapter.py:2370-2385` — both GROUP BY over
  the full registry/table), while every list query filters `is_available = 1`
  (`sqlite_adapter.py:2760`). An aggregation surface built on that precedent would report
  counts that disagree with the lists next to them.
- **Temporal fields are computed, not stored:** `created_at`/`updated_at` live exclusively in
  the provenance log and are derived at read time (sec6; the platform invariant). `orderBy:
  createdAt` therefore cannot be a column sort on either adapter.

## Decision

**Mosaic will add an aggregation and ordering surface to list queries, shipped as one change**
because every piece shares the same enabling work — pushing limit/offset/order down into the
storage `Query` and both adapters:

- **(a) Count mode:** a cheap filtered count — `SELECT COUNT(*)` under the same predicate the
  list query would use — so `Page.total` (and a bare "count only" ask) stops costing full
  materialization. Exposed on the SDK first (SDK-first principle), with the GraphQL `Page.total`
  resolved from it.
- **(b) Facet value counts:** `facetCounts(field, where)` — per-value counts for one slot under
  a filter (GROUP BY on the slot's column / JSONB key). The `entity_counts()` GROUP BY shape is
  the mechanical precedent on both adapters, but **not** the semantic one (see the availability
  rule).
- **(c) Min/max per slot** for range facets (numeric/date slots), under the same filter.
- **(d) `order_by`:** a generated per-type enum over `filterable_slot_names()`
  (`graphql/schema_builder.py:191-202`) plus a direction, pushed down as SQL `ORDER BY`
  (Postgres: with the same per-range JSONB casts ADR-0006 requires for comparisons).

**Availability-consistency rule (pinned):** every aggregate in this surface sees **exactly what
list queries see** — `is_available = 1` plus the caller's filters. `entity_counts()` keeps its
current registry semantics for its existing callers, but it is not the model; the new surface
must not inherit its include-unavailable behavior, or Aperture's attrition counts will disagree
with the result lists they annotate.

**Computed-temporal-field ordering constraint (pinned):** `created_at`/`updated_at` are
provenance-derived, so they cannot appear in the generated column `order_by` enum as plain
column sorts. Temporal ordering either (i) goes through the provenance-summary path (the
batched temporal aggregation that already annotates reads), applied after the pushed-down
ordering/paging — in which case it is documented as such and its cost stated — or (ii) is
omitted from `order_by` in the first increment (today's Python-side `created_at` sort remains
the default order). Which of (i)/(ii) ships first is decided in the OpenSpec change; what this
ADR forbids is pretending a computed field is a column.

**Pagination discipline preserved:** the pushdown must keep `limit=0` meaning "zero rows",
never "unlimited" (#130; `query_service.py:276-279` and the adapters' guards), and
`Page.total` remains the **filtered** total independent of the page window.

## Consequences

- **Both adapters, one estimate ×2:** SQLite pushdown is straightforward on per-class typed
  columns; Postgres needs `ORDER BY`/`GROUP BY`/`MIN`/`MAX` over `data->>'slot'` with
  per-range casts (shared machinery with ADR-0006 — a reason to sequence M1 first or land the
  cast helper as common code). The parity discipline double-prices every piece; parity tests
  must assert identical counts/orderings on both backends.
- **The as-of path is explicitly out of scope for pushdown:** aggregations under `asOf` either
  compute over the Python-reconstructed set (correct, slow, documented) or are rejected with a
  coded error in the first increment — the same honesty rule as ADR-0006's as-of stance; an
  aggregate that silently ignores `asOf` is forbidden.
- **`Page.total` gets cheap**, which also benefits the search twins once search returns a page
  envelope (OpenSpec `search-composition`) and cursor pagination later (the pushdown is the
  shared prerequisite).
- **Unblocks Aperture X1:** live per-criterion counts, facet counts on the existing panel,
  range facets, predicted explode sizes for its export grain choices, and honest sort.
  Aperture's capability gating reads the presence of `facetCounts`/`orderBy` off introspection
  — no side-channel.
- **New GraphQL surface is additive** (new root/page fields and arguments); no existing
  consumer breaks.

## Alternatives considered

- **Ship `order_by` alone first (or counts alone):** every sub-feature needs the same
  limit/offset/order pushdown and (on Postgres) the same cast helper; splitting them re-prices
  the shared work twice and leaves `Page.total` expensive in the meantime. Rejected — one
  change, several increments if needed.
- **Compute facet counts client-side over fetched pages:** forbidden by the consumer's own
  constitution (Aperture ADR-0029 — never fake a count over a partial page) and wasteful; the
  server owns the data and the GROUP BY.
- **Model the new aggregates on `entity_counts()`:** rejected; it counts unavailable entities
  (correct for its registry/inventory purpose, wrong for a query surface). The
  availability-consistency rule exists precisely because this trap is one copy-paste away.
- **A generic `groupBy(fields, aggregates)` analytic surface:** overshoots the requirement
  (X1 needs single-slot value counts and min/max), explodes the generated schema, and starts
  competing with real analytic stores. Rejected; revisit only with a concrete consumer need.
- **Treat `created_at` as a stored column for sorting** (denormalize it onto entity tables):
  contradicts the sec6 invariant that temporal fields live exclusively in the provenance log
  (system fields on entity tables are `id`/`is_available` only) and would desynchronize under
  migration/replay. Rejected.

## Notes / open sub-questions

- **Temporal ordering (decided at ratification): option (ii)** — `created_at`/`updated_at`
  are omitted from the generated `order_by` enum in the first increment (today's default
  Python-side `created_at` ordering remains the no-`order_by` behavior); the
  provenance-summary path is the accepted design if/when temporal ordering is funded.
- **As-of aggregation (decided at ratification):** in the first increment, `facetCounts`,
  min/max, and `orderBy` combined with `asOf` return a coded error; `Page.total` under
  `asOf` keeps its current documented Python-path semantics.
- `facetCounts` on relationship-backed multivalued reference slots (a GROUP BY over the link
  table) is deliberately **not** in scope; it belongs with ADR-0006's M5b machinery if ever.
- Interaction with bm25-ranked search ordering is settled in OpenSpec `search-composition`
  (rank vs. `order_by` precedence). **Settled at implementation (issue #157):** search
  results come back in FTS rank order when no explicit ordering is given; an explicit
  `order_by` overrides rank and reuses this ADR's pushdown.
- Implementation increments, tasks, and acceptance live in the OpenSpec change
  `aggregation-and-ordering` ([#156](https://github.com/datahelix-platform/mosaic/issues/156));
  do not implement ahead of it.
