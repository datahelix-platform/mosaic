# Tasks — `typed-filter-inputs`

Tracking issue: datahelix-platform/mosaic#155

> ADR-0006 ratified 2026-08-19; ALL four increments implemented (1–2 on 2026-08-19,
> 3–4 on 2026-08-19/20). The typed filter contract is complete.

## 0. Design gate

- [x] 0.1 ADR-0006 ratified (Status → Accepted; #153 closed with a link to the record).
- [x] 0.2 Deprecation stance for the flat `filters:` arg decided (keep + `@deprecated` vs.
  ADR-0005-style clean break) and recorded in ADR-0006.
- [x] 0.3 `contains` semantics pinned per range (string substring case rules; inline
  multivalued membership — SQLite JSON TEXT vs. Postgres JSONB pushdown differ).

## 1. Comparison operators on the flat path (increment 1)

- [x] 1.1 `VALID_FILTER_OPS` + `normalize_filter` extended (`neq/gt/gte/lt/lte/contains/
  is_null`); unknown ops still raise (#129 discipline preserved).
- [x] 1.2 SQLite `_find_per_class` column predicates for each operator.
- [x] 1.3 Postgres `find` predicates with per-range casts driven by `SlotModel.range`
  (`::numeric`, `::timestamptz`/`::date`, `::boolean`); cast helper written as common code
  shared with `aggregation-and-ordering`.
- [x] 1.4 All four `_matches_filters` as-of mirrors implement every operator — collapsed
  onto one shared evaluator (`mosaic.core.storage.matches_operator`) so semantics live
  once; current-state-vs-as-of agreement tests on both adapters.
- [x] 1.5 `isNull` semantics per ADR-0006 (`IS NULL` / absent-or-null JSONB key; `eq: null`
  raises coded error; not offered on relationship-backed multivalued refs).
- [x] 1.6 GraphQL `FilterOp` enum + `_to_sdk_filters` pass-through; coded errors for
  unsupported op-on-slot combinations.
- [x] 1.7 SQLite/Postgres parity suite for every operator; full suite green.

## 2. Generated `<Type>Filter` + `where:` (increment 2)

- [x] 2.1 Per-slot operator input objects (`StringFilterOps` etc.) selected by slot
  kind/range, with LinkML descriptions propagated.
- [x] 2.2 `_build_filter_input_types` pass generating `<Type>Filter` per class
  (slot-name keys + alias resolution as in `resolve_filter_field`); `and`/`or`/`not`.
- [x] 2.3 `where:` argument on list queries; translation walker to the normalized SDK filter
  representation; `where:` + `filters:` compose by AND.
- [x] 2.4 Introspection tests: operator sets per slot kind visible in `__schema`; docs
  (`docs/graphql.md`, sec4 §4.7) updated.

## 3. M5a — to-one relationship predicates (increment 3)

- [x] 3.1 Nested target-type filter under the to-one edge name in `<Type>Filter` (bare-class
  cross-reference; self-referential edges included; edge nesting counts toward the depth cap).
- [x] 3.2 Correlated `EXISTS` on the FK column, both adapters (SQLite per-class tables;
  Postgres entities-row with entity_type + availability guards and target-class casts);
  aliases threaded for nested/self-referential edges; parity tests mirrored
  (`tests/core/test_relationship_predicates.py` ↔ `TestPostgresRelationshipPredicates`).
- [x] 3.3 `asOf` + relationship predicate → coded `ASOF_RELATIONSHIP_FILTER_UNSUPPORTED` at
  the transport (list + count roots) and `ValidationError` in both adapters'
  `_find_as_of` (defense-in-depth: `matches_tree` also raises on edge nodes); tested.

## 4. M5b — to-many quantified predicates (increment 4)

- [x] 4.1 `some`/`none` quantifier inputs on relationship-backed multivalued reference slots
  (per-target `<Target>EdgeQuantifiers` input over the bare filter classes; both set AND
  together).
- [x] 4.2 `EXISTS`/`NOT EXISTS` against the `relationships` link table joined to the target
  table (availability guards on edge AND target; edgeless entities match `none`), both
  adapters; parity tests mirrored (`tests/core/test_quantified_predicates.py` ↔
  `TestPostgresQuantifiedPredicates`).
- [x] 4.3 Filter-nesting depth cap with coded error (`MAX_WHERE_INPUT_DEPTH`, landed inc. 2;
  quantified edges count toward it).
- [x] 4.4 The `where` path now quantifies over these slots; the flat `filters:` path keeps
  its `UNFILTERABLE_FIELD` error (no column exists to address) with the text updated to
  point at the typed `where` quantifiers.

## 5. Docs

- [x] 5.1 sec4 §4.7 filter documentation rewritten around the typed contract (limitation row
  retired at M5b); §4.3 CEL sketch annotated as superseded/narrowed per ADR-0006.
- [x] 5.2 `docs/graphql.md` filtering guide with per-operator, `where`-tree, and
  relationship-predicate examples (M5a + M5b).
