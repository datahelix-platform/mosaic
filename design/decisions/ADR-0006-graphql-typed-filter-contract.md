# ADR-0006: The GraphQL filter contract is typed per-class input objects

- **Status:** Accepted
- **Date:** 2026-08-19 (ratified same day; see #153)
- **Deciders:** labadorf
- **Related:** sec4 §4.3 (REST filtering — the CEL sketch this ADR supersedes/narrows for the
  GraphQL transport), sec4 §4.7 (GraphQL transport), ADR-0001 (graph-level as-of — every filter
  feature must mirror into the as-of path), ADR-0002 (multivalued reference slots persist as
  relationship edges — why they are unfilterable today and why M5b needs the link table),
  ADR-0005 (edge-only reference emission; its "clean break, early software" precedent is the
  prior art for eventually retiring the flat `filters:` arg); **Aperture
  `design/cross-class-query.md` §7** (the co-design exploration this ADR lands, M1/M5 and the
  wire-contract decision), **Aperture ADR-0035** (Accepted 2026-08-19 — cross-class queries
  are a typed QuerySpec artifact; carries the reciprocal cross-reference), **Aperture
  `design/portal-requirements.md` X5** (the "typed operator + relationship-predicate filter
  contract" requirement pointing here; filed as X5 because the exploration's "X3" name was
  already taken by X3a/X3b — the X4/L9/L10 citation form modeled by
  `design/sec5_ingestion.md` §5.4 and `openspec/changes/batch-unit-of-work/proposal.md`).
  Code landing sites cited inline (mosaic @ 502991c, v0.12.1).
- **Tracking issue:** [#153](https://github.com/datahelix-platform/mosaic/issues/153)
  (implementation: OpenSpec `typed-filter-inputs`,
  [#155](https://github.com/datahelix-platform/mosaic/issues/155))

## Context

The GraphQL list surface filters with a flat `filters: [FilterInput]` argument — `{field,
value, op}` where `op` is `EQ | IN` (`graphql/resolvers.py:53-72`, #102). Everything else
raises by design: #129 made unsupported operators (`gt`, `lt`, `ne`, `contains`, …) fail loud
at the single chokepoint (`VALID_FILTER_OPS` + `normalize_filter`,
`core/storage/__init__.py:50-106`) rather than silently degrade to equality. Relationship
predicates do not exist: multivalued references are walled off from filtering entirely
(`resolvers.py:435-442`, coded `UNFILTERABLE_FIELD`), and the SQL builders
(SQLite `_find_per_class`, `sqlite_adapter.py:2741-2794`; Postgres `find`,
`postgres_adapter.py:2039-2092`) have no join/subquery machinery at all.

The documented future for richer predicates is **CEL**: sec4 §4.3 sketches a REST `filter`
parameter carrying a CEL expression, evaluated in the storage adapter by "translating common
patterns to SQL predicates; others fall back to in-memory evaluation."

Meanwhile Aperture is building cross-class criteria queries ("donors over 60 having ≥1 sample
of type X"), derived **entirely from GraphQL introspection** per its constitution: every class,
edge, operator, and traversal offered by its query builder must come from the introspected
schema (Aperture ADR-0002 derive-everything, ADR-0004 no scripting layer, ADR-0029
capability-honest gating). The co-design exploration (Aperture `design/cross-class-query.md`,
Rev 2, priced against this repo @ 502991c) concluded the governing principle: **the
introspected GraphQL schema itself becomes the query-capability contract.** What Mosaic
advertises in `__schema` is exactly what consumers may offer; nothing more is promised,
nothing less needs a side-channel.

The question: when the GraphQL filter surface grows comparison operators and relationship
predicates, is the wire contract **generated typed input objects** or a **CEL expression
argument**?

## Decision

**Mosaic will generate typed per-class filter input objects as the GraphQL filter contract.**

- For each entity class, a `<Type>Filter` input object is generated (in
  `graphql/schema_builder.py`, alongside the existing create/update input pass) with:
  - one field per filterable slot, typed as a **per-slot operator object** — e.g.
    `StringFilterOps {eq, neq, in, contains, isNull}`, `NumberFilterOps {eq, neq, in, gt, gte,
    lt, lte, isNull}`, `DateFilterOps` (comparison set over ISO dates), `BooleanFilterOps {eq,
    isNull}`, `EnumFilterOps {eq, neq, in, isNull}` — the operator set **selected by slot
    kind/range**, so introspection tells a consumer exactly which predicates each slot
    supports;
  - `and: [<Type>Filter!]`, `or: [<Type>Filter!]`, `not: <Type>Filter` combinators;
  - (phased, below) **relationship predicates**: a to-one reference slot exposes the target
    type's filter nested under the edge name (`where: {donor: {age: {gt: 60}}}`); a
    multivalued (relationship-backed) reference exposes quantifiers
    (`where: {samples: {some: <SampleFilter>, none: <SampleFilter>}}`).
- The new contract is exposed as a **`where:` argument beside the existing flat `filters:`
  argument** on list queries (and, once search composes — OpenSpec `search-composition` — on
  the search twins). Both arguments translate to the same SDK filter representation; `where:`
  is strictly more expressive.
- Operator semantics land once at the storage chokepoint (`VALID_FILTER_OPS` /
  `normalize_filter`) and in **both** adapters' clause builders, preserving #129's discipline:
  an operator is either advertised and pushed down, or absent from the schema — never accepted
  and approximated.

**Explicitly decided against: CEL on the GraphQL wire.** CEL keeps its current Mosaic role
(validator conditions) and may serve internally as an adapter IR if useful, but the
consumer-facing GraphQL contract is typed inputs. This **supersedes/narrows the sec4 §4.3 CEL
filtering sketch for the GraphQL transport**: the "CEL filters follow the REST roadmap" line
formerly in §4.7's limitations table does not happen; whether the REST-side CEL `filter`
parameter itself ever ships is left to sec4 §4.3's own roadmap, but it is no longer the plan
of record for rich GraphQL predicates. Reasons:

1. **Introspectability.** A `filter: String` CEL argument is opaque to introspection — a
   consumer cannot learn which predicates the server supports, so Aperture's derive-everything
   rule and capability gating would need a side-channel capability contract anyway. Typed
   inputs *are* the contract.
2. **Validation.** Malformed typed input fails GraphQL validation with a precise error before
   execution — the dry-run property Aperture's builder and its NL→query loop depend on. Bad
   CEL fails at runtime, in a second grammar, with a second error vocabulary.
3. **Pushdown honesty.** The documented CEL strategy — "translate common patterns to SQL, fall
   back to in-memory" (sec4 §4.3) — is an invisible performance cliff: two queries that look
   the same on the wire execute in different complexity classes. Typed inputs make everything
   advertised pushdown-able by construction.
4. **Scripting-layer pressure.** An expression string on the wire is one temptation away from
   a user-facing scripting layer, which Aperture's ADR-0004 forbids; a closed noun vocabulary
   is not.

**Relationship predicates phase in two increments (to-one before to-many):**

- **M5a — to-one predicates** (moderate): a single-valued reference is a column (the target
  id), so `{donor: {age: {gt: 60}}}` compiles to one correlated `EXISTS` subquery against the
  target's table keyed on the FK column. No link table involved.
- **M5b — to-many quantified predicates** (expensive; nothing else waits on it): `some`/`none`
  compile to `EXISTS` / `NOT EXISTS` against the `relationships` link table joined to the
  target table. Requires recursive input types — the two-pass bare-class trick used for cyclic
  object types (`schema_builder.py:507-513`) is the precedent — and a **filter-nesting depth
  cap**, because the existing `QueryDepthLimiter` guards output selection nesting only, not
  input nesting. Multivalued references remain unfilterable (today's coded
  `UNFILTERABLE_FIELD`) until M5b lands; the error message already points at `relatedTo`.

**`isNull` semantics (decided):** `isNull: true` matches entities with **no stored value** for
the slot — SQLite: column `IS NULL`; Postgres: JSONB key absent or JSON `null` — and
`isNull: false` the complement. This matches read semantics, where unset columns are simply
omitted from the reconstructed `data` dict (`sqlite_adapter.py:2830-2839`). Two consequences
pinned now: (a) `eq: null` is rejected with a coded error — GraphQL cannot distinguish
"explicit null" from "absent" reliably enough for a filter contract; the only way to ask about
absence is `isNull`; (b) `isNull` is **not offered** on relationship-backed multivalued
references (they have no column; a required multivalued slot already reads back as `[]`, not
null, `schema_builder.py:729-730`) — under M5b, emptiness is expressed as `none: {}`.

**asOf × relationship predicates (decided):** the temporal read path filters in Python over
provenance-reconstructed state and declares relationship-existence filtering out of scope
(`sqlite_adapter.py:2674-2675`, hippo#71). Until a temporal join is designed and funded,
a query combining `asOf` with any relationship predicate (M5a or M5b) is **rejected with a
coded GraphQLError** (e.g. `ASOF_RELATIONSHIP_FILTER_UNSUPPORTED`), in both adapters. Scalar
operator filters (`gt`, `contains`, …) **do** work under `asOf` — but only by landing every
operator in the as-of mirrors: `_matches_filters` exists in four places (SQLite
`sqlite_adapter.py:2709-2739`, Postgres `postgres_adapter.py:2132+`, and their call sites);
an operator implemented in the SQL builders but not the mirrors makes as-of queries silently
diverge from current-state queries, which is the forbidden outcome (an undocumented wrong
answer).

## Consequences

- **The introspected schema becomes the capability contract.** Aperture (and any consumer,
  including an LLM agent) derives every operator it offers, per slot, from `__schema` — no
  capability side-channel, no hardcoded operator tables. Capability gating reduces to "offer
  exactly what the schema advertises"; dry-run validation reduces to GraphQL validation.
- **Both adapters, every increment.** SQLite's typed per-class columns make comparisons
  trivial; Postgres stores one JSONB document, so `data->>'slot'` yields text and **every
  typed comparison needs a per-range cast** (`::numeric` for LinkML integer/float ranges,
  `::timestamptz`/`::date` for temporal ranges, `::boolean`) driven by `SlotModel.range`
  (`core/schema_typing.py:93`). This is the main cost and the main correctness risk of the
  whole feature; the SQLite/Postgres parity discipline double-prices every estimate.
- **Four as-of mirrors are now load-bearing.** Every operator lands in the SQL builders *and*
  in each `_matches_filters` mirror, with shared tests asserting current-state/as-of agreement.
- **Input-type generation extends the existing pass.** `_build_one_input`
  (`schema_builder.py:673-724`) is the pattern to copy; filter inputs key on LinkML slot names
  exactly as create/update inputs do (with the same alias resolution as
  `resolve_filter_field`, `schema_builder.py:214-235`).
- **The flat `filters:` arg survives initially** (additive change, no consumer breaks).
  Retiring it is an open sub-question below; ADR-0005's clean-break precedent
  (ADR-0005, Consequences: "early software; a clean break is acceptable") is the prior art
  for removing it in a breaking release once Aperture is off it.
- **GraphQL translation** (`_to_sdk_filters` + `FilterInput`, `graphql/resolvers.py:59-72,
  405-444`) grows a `where:`-tree walker emitting the normalized SDK filter representation;
  the coded-error discipline (`UNKNOWN_FILTER_FIELD`, `UNFILTERABLE_FIELD`) extends to the
  nested contract.
- **Unblocks Aperture:** the QuerySpec builder's per-slot operator menus, its Metabase-style
  implicit to-one traversal (M5a), and its Atlas-style "having ≥1 / exactly 0" relationship
  criteria (M5b) — until M5 lands, Aperture compensates with a server-assisted semijoin over
  the native `in` operator, visibly gated.

## Alternatives considered

- **CEL on the GraphQL wire** (`filter: String`). Rejected for the four reasons in the
  Decision: opaque to introspection (breaks derive-everything and capability gating), runtime
  errors in a second grammar (loses the dry-run property), the documented translate-or-
  fall-back strategy is an invisible performance cliff (a capability lie in Aperture ADR-0029
  terms), and expression-on-the-wire invites a scripting layer. CEL remains for validator
  conditions and as a possible internal IR.
- **Extend the flat `filters:` arg with more `FilterOp` values only** (no `where:`). Cheapest
  increment and it ships first (OpenSpec `typed-filter-inputs` increment 1), but as the final
  state it cannot express nesting (`and`/`or` beyond one mode, `not`, relationship
  predicates), and a single generic `value: JSON` field advertises nothing per-slot — the
  introspection-as-contract principle fails.
- **One generic `EntityFilter` input shared by all classes.** Loses per-slot typing — every
  field name is a string, every value JSON — so introspection again says nothing; also
  reintroduces the unknown-field-at-runtime class of errors #149 eliminated.
- **A true GraphQL union/interface root with polymorphic filtering.** Inverts the deliberate
  `Entity` exclusion from the schema (`core/schema_typing.py:37-45`); ADR-0005 flags
  union/interface polymorphism as future work. Out of scope here; if pursued, it is its own
  ADR (see OpenSpec `heterogeneous-roots` for the envelope-shaped alternative).
- **Reject `asOf` × relationship predicates silently / return best-effort results.** Forbidden
  outcome — an undocumented wrong answer. The coded error keeps the surface honest until the
  temporal join is funded.

## Notes / open sub-questions

- **Deprecation path for the flat `filters:` arg (decided at ratification):** the flat arg
  stays supported beside `where:`; once increment 2 ships, its SDL description names it the
  legacy form. Removal, if ever, is a later breaking release per the ADR-0005 clean-break
  precedent, and not before Aperture's planner emits `where:` everywhere.
- **`contains` semantics (decided at ratification):** case-insensitive substring on
  string-ranged slots — SQLite `LIKE` (ASCII case folding) / Postgres `ILIKE` (locale case
  folding; the non-ASCII divergence is a documented backend difference), with `%`/`_`
  treated as literals via an `ESCAPE` clause and the same semantics in the as-of mirror
  (`matches_operator`). Membership testing on inline (non-reference) multivalued slots is
  **deferred** — those slots offer `eq`/`isNull` only until a membership design lands
  (`LIKE`/`json_each` vs. `@>` pushdowns differ).
- **Literate schema (M0 ask):** LinkML `description`s already propagate into SDL for object
  types, fields, and enums (`schema_builder.py:404,463,477,599`; enum descriptions at
  `:361-381`); LinkML `comments` do **not** propagate anywhere today. The new filter inputs
  and operator objects must carry descriptions from day one; whether `comments` should append
  to SDL descriptions is a small open question to settle in the same increment.
- **Filter-nesting depth cap default** (M5b): pick a value (Airtable/Notion cap criteria
  nesting at 3; Aperture's QuerySpec caps at 3) and a coded error for exceeding it.
- Implementation increments, tasks, and acceptance live in the OpenSpec change
  `typed-filter-inputs` ([#155](https://github.com/datahelix-platform/mosaic/issues/155));
  do not implement ahead of it.
