# Tasks — `aggregation-and-ordering`

Tracking issue: datahelix-platform/mosaic#156

> ADR-0007 ratified 2026-08-19 (gate decisions recorded in the ADR). Implemented
> 2026-08-19 (all increments).

## 0. Design gate

- [x] 0.1 ADR-0007 ratified (Status → Accepted; #154 closed with a link to the record).
- [x] 0.2 Temporal-ordering stance chosen (provenance-summary path with documented cost vs.
  omit from `order_by` in increment 1) and recorded in ADR-0007.
- [x] 0.3 As-of aggregation stance for increment 1 chosen (documented Python-path compute vs.
  coded error).

## 1. Pushdown + count mode (increment 1)

- [x] 1.1 `Query` carries `limit`/`offset`/`order_by`/`order_dir`; SQLite `_find_per_class`
  and Postgres `find` apply them in SQL. Shipped shape: the pushdown path engages when
  `order_by` is given (SQL order + LIMIT/OFFSET + per-page temporal derivation); without it
  the historical Python `created_at` path is byte-identical (no silent ordering change).
- [x] 1.2 `limit=0` → zero rows preserved on the pushdown path with correct `total` (#130);
  SDK test. (REST/GraphQL default paths unchanged — their existing #130 tests still cover
  them.)
- [x] 1.3 Filtered count on both adapters (`COUNT(*)` under the shared `_query_predicate`,
  `is_available` included); `MosaicClient.count`; ordered pages resolve `Page.total` from it
  (no match-set hydration — the count query selects no rows).
- [x] 1.4 Parity tests: identical totals/pages/orderings on SQLite
  (`tests/core/test_aggregation_and_ordering.py`) and Postgres
  (`TestPostgresAggregationAndOrdering`).

## 2. Facet counts + min/max (increment 2)

- [x] 2.1 `facet_counts` on both adapters (GROUP BY column / `data->field` jsonb),
  availability-consistent, NULLs never counted, count-desc-then-value order; SDK API +
  GraphQL `{plural}FacetCounts(field, filters, where, filterMode)`.
- [x] 2.2 `field_range` min/max under a filter (Postgres via the shared `_filter_cast`
  helper, driver types normalized to SQLite-identical Python values); SDK API + GraphQL
  `{plural}FieldRange(field, ...)` (numeric/temporal slots only at the transport).
- [x] 2.3 Availability-consistency tests on both adapters: seeded unavailable entities
  never appear in any aggregate; `count` equals the list surface's `total`.
- [x] 2.4 Coded errors extending #149: `UNKNOWN_AGGREGATION_FIELD` (unknown names),
  `UNAGGREGATABLE_FIELD` (computed temporal fields, multivalued/JSON slots, non-ordered
  ranges on `fieldRange`).

## 3. order_by (increment 3)

- [x] 3.1 Generated per-type `<Type>OrderField` enum + `orderDir` on list queries; SQL
  `ORDER BY` pushdown on both adapters, NULLs last both directions, stable `id` tiebreak.
  Shipped scope: single-valued scalar/enum stored columns incl. `id` (references and
  multivalued/JSON slots excluded — no meaningful scalar order).
- [x] 3.2 Computed temporal fields omitted from the enum per the 0.2 decision (option ii);
  SDK-level `order_by="created_at"` raises naming the constraint; `orderBy`+`asOf` is a
  coded `ASOF_ORDERING_UNSUPPORTED` error per the 0.3 decision.
- [x] 3.3 Parity tests for ordering, including numeric/date slots on Postgres JSONB (casts
  keep value order, not text order).

## 4. Docs

- [x] 4.1 sec4 §4.7 + `docs/graphql.md`: aggregation/ordering surface documented, including
  the availability-consistency guarantee and temporal-ordering semantics.
- [x] 4.2 `design/INDEX.md` Open Questions row updated on ship.
