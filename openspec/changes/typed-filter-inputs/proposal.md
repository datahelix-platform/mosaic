# Typed Filter Inputs (generated `<Type>Filter` + `where:` with phased relationship predicates)

Tracking issue: datahelix-platform/mosaic#155

## Why

The GraphQL filter surface is the flat `filters: [FilterInput]` argument with `op: EQ | IN`
only (`graphql/resolvers.py`, #102); every other operator raises by design at the storage
chokepoint (`VALID_FILTER_OPS`/`normalize_filter`, `core/storage/__init__.py`, #129), and
relationship predicates do not exist — multivalued references are walled off from filtering
with a coded `UNFILTERABLE_FIELD` error, and the SQL builders have no join/subquery machinery.

Aperture's cross-class query builder derives everything it offers from GraphQL introspection
(its ADR-0002/0004/0029), so the filter contract must be **typed input objects the schema
itself advertises** — which predicates each slot supports, per slot, discoverable by
`__schema` alone. **ADR-0006** records that decision (and the decision *against* CEL on the
GraphQL wire); this change implements it. Design authority: ADR-0006; exploration:
aperture `design/cross-class-query.md` §7 (M1/M5a/M5b).

## What Changes

Delivered as four increments under one capability, each independently valuable, each priced
for **both** adapters (SQLite typed per-class columns; Postgres JSONB with per-range casts
driven by `SlotModel.range`).

### Increment 1 — Comparison operators on the flat path

- Extend `VALID_FILTER_OPS` with `neq`, `gt`, `gte`, `lt`, `lte`, `contains`, `is_null`;
  `normalize_filter` normalizes them (single chokepoint preserved — an unknown op still
  raises, never degrades, per #129).
- SQLite `_find_per_class`: column-level predicates for each new operator.
- Postgres `find`: `data->>'slot'` predicates **with per-range casts** (`::numeric` for
  integer/float ranges, `::timestamptz`/`::date` for temporal ranges, `::boolean`) — the main
  correctness risk; wrong-cast behavior must be a test, not a surprise.
- **Every operator lands in all four as-of mirrors** (`_matches_filters` on both adapters and
  their call sites) with shared tests asserting current-state/as-of agreement — an operator
  present in SQL but absent from a mirror silently forks as-of results.
- GraphQL `FilterOp` enum grows the same members; `_to_sdk_filters` passes them through.
- `isNull` semantics per ADR-0006: matches "no stored value" (column `IS NULL` / JSONB key
  absent-or-null); `eq: null` rejected with a coded error; not offered on relationship-backed
  multivalued references.

### Increment 2 — Generated `<Type>Filter` inputs + `where:` argument

- New schema-builder pass generating per-class filter input objects: per-slot operator
  objects (`StringFilterOps`, `NumberFilterOps`, `DateFilterOps`, `BooleanFilterOps`,
  `EnumFilterOps`) selected by slot kind/range, plus `and`/`or`/`not` combinators. Copies the
  `_build_one_input` pattern; inputs key on LinkML slot names with the same alias resolution
  as `resolve_filter_field`.
- `where: <Type>Filter` argument added beside `filters:` on list queries; both translate to
  the same SDK filter representation. The flat arg survives (deprecation path is ADR-0006's
  open sub-question; ADR-0005's clean-break precedent applies when decided).
- LinkML `description`s propagate onto the new inputs and operator objects (the literate-
  schema ask; descriptions already propagate for object types/fields/enums).

### Increment 3 — M5a: to-one relationship predicates

- A single-valued reference slot exposes the target type's filter nested under the edge name:
  `where: { donor: { age: { gt: 60 } } }` → one correlated `EXISTS` subquery against the
  target's table keyed on the FK column (no link table). Both adapters.
- `asOf` + any relationship predicate rejected with coded
  `ASOF_RELATIONSHIP_FILTER_UNSUPPORTED` (ADR-0006; the temporal path declares
  relationship-existence out of scope — hippo#71).

### Increment 4 — M5b: to-many quantified predicates

- Multivalued (relationship-backed) reference slots expose `some`/`none` quantifiers taking
  the target type's filter: `EXISTS`/`NOT EXISTS` against the `relationships` link table
  joined to the target table. Both adapters.
- Recursive input types via the two-pass bare-class trick (the cyclic object-type precedent
  in the schema builder); a **filter-nesting depth cap** with a coded error (the existing
  `QueryDepthLimiter` guards output nesting only).
- The `UNFILTERABLE_FIELD` wall comes down only here; until then the coded error stands.

## Capabilities

### New Capabilities

- `typed-filter-operators` — comparison operators on list-query filters, pushed down on both
  adapters and mirrored in the as-of path. *(Increment 1.)*
- `generated-filter-inputs` — introspectable per-class `<Type>Filter` input objects exposed
  via `where:`; the schema is the capability contract. *(Increment 2.)*
- `relationship-predicates` — nested to-one predicates, then to-many `some`/`none`
  quantifiers. *(Increments 3–4.)*

### Modified Capabilities

- `graphql-list-queries` — gain the `where:` argument; flat `filters:` unchanged.
- `storage-adapter-contract` — `VALID_FILTER_OPS` widens; both adapters must implement every
  advertised operator (parity discipline).

## Dependencies

- **ADR-0006** (design authority; Proposed — ratification tracked in #153).
- Shares the Postgres per-range cast helper with `aggregation-and-ordering` (#156) — build it
  as common code in whichever lands first.
- `search-composition` (#157) composes its FTS id-sets with this `where:` argument.
- Driver / cross-reference: Aperture `design/cross-class-query.md` §7; Aperture ADR-0035
  (Proposed); Aperture `portal-requirements.md` X-tracker entry for the typed filter contract.

## Acceptance

- Introspection alone reveals, per slot, exactly the operators the storage layer will push
  down; no advertised operator ever falls back to in-memory evaluation or silent equality.
- Every operator produces identical results on SQLite and Postgres (parity suite), including
  type-sensitive comparisons on numeric/date slots stored as JSONB text on Postgres.
- Every operator produces identical results with and without `asOf` on unchanged data
  (as-of mirror suite).
- `asOf` + relationship predicate returns the coded error on both adapters; no path returns
  silently-wrong temporal answers.
- Unknown fields, unsupported operators on a slot, `eq: null`, and over-deep nesting all fail
  with coded GraphQL errors listing the valid alternatives (extends #149's discipline).
- Existing consumers of the flat `filters:` argument are unaffected; full suite green.
