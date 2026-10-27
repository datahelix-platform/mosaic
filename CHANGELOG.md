# Changelog

## [Unreleased]

### Fixed

- **Inverse slots over an abstract or subclassed range** (issue #224,
  ADR-0011 follow-up). A reverse edge such as `Donor.samples {range: Sample,
  inverse: donor}` with `Sample` abstract used to resolve against a
  `Sample` table that does not exist: hydration was empty,
  `count_relationship` returned 0 and `some`/`none` predicates raised. The
  edge now resolves across the range's concrete closure
  (`concrete_class_closure`: the range if concrete, plus every concrete
  descendant) in both adapters — SQLite hydrates with one `UNION ALL`,
  filters with an OR of per-table `EXISTS`, and sums per-table counts;
  Postgres matches `entity_type = ANY(...)`. This also fixes a *concrete*
  range with subclasses (`Donor.brains` → `Brain` now includes `Cerebrum`
  rows). Nested `where` fields must be owned by every table in the closure
  (a subclass-only field is rejected). An abstract range with no concrete
  descendant is now a load-time `SchemaError` instead of a silently empty
  edge. `InverseSlot` gains `target_classes` / `tables`.

## v0.14.0 — 2026-09-22 (the MCP boundary — QuerySpec tools over a capability manifest, conversational planning delegation, reverse edges via LinkML `inverse`)

### Added

- **The MCP boundary — `mosaic serve --mcp`, a fourth transport**
  (issue #182, ADR-0009 decision 1). `mosaic/mcp/` mounts alongside
  REST/GraphQL/CLI, gated by a new `--mcp` flag exactly the way
  `--graphql` already gates its own surface, and sharing the same
  `MosaicClient`/`SchemaRegistry` — no new data-access path. Two
  resources: `mosaic://schema` (the type model) and
  `mosaic://capabilities` (the manifest below). **Streamable HTTP, not
  stdio** — ADR-0009's Notes hedged stdio as "the likely default", but
  stdio runs as a separate client-invoked process with no access to a
  running FastAPI app, so it cannot satisfy the Decision text's actual
  requirement of shared app state; see the `mosaic.mcp` package
  docstring. Because Starlette never runs a mounted sub-app's lifespan,
  `create_default_app` reassigns the host app's lifespan to enter the
  MCP session manager's task group when `mcp=True`; `MOSAIC_SERVE_MCP`
  is threaded through `create_app_from_env()` so `--mcp` does not
  silently drop under `--reload`/`--workers`. **Read-only: no write or
  mutation tool exists on this surface.** New optional extra `mcp`
  (`mcp>=2.1.1`, also in the `all` aggregate).

- **Capability manifest — per-field filter ops, predicates, ordering,
  aggregation and search** (issue #181). `build_capability_manifest()`
  in `mosaic.core.schema_typing` lifts three computations that
  previously existed only inside the GraphQL layer (`schema_builder`'s
  `<Type>Filter` generation, the resolvers' aggregate-field gating, and
  the FTS availability check) into the core, on top of the existing
  `build_type_model()`. This resolved a real divergence between the
  `where:` contract and the older flat `filters:` list on single-valued
  references: the manifest reports `where:` semantics — a reference gets
  no direct `FilterOp`, only relationship-predicate filtering through
  the nested edge — since QuerySpec only ever compiles to `where:`. A
  parity test builds the real `GraphQLTypeBuilder` alongside the
  manifest and asserts they agree on every field, so the two
  representations cannot drift apart silently again.

- **`validate_query_spec` / `execute_query_spec` MCP tools, and the
  QuerySpec parser, validator and compiler** (issue #183, ADR-0009
  decisions 2/3/5). Aperture ADR-0035's `QuerySpec` artifact and its
  `validateQuerySpec` discipline, ported server-side. Validation is
  total and manifest-driven: every anchor, slot, op, edge, enum value
  and sort field is checked against what *this deployment's* schema
  actually advertises, with structured, coded errors naming the
  offending item and the valid set — the validate-fix-retry loop
  ADR-0009 item 3 calls load-bearing. Nested `CriteriaGroup`s are
  accepted (the `and`/`or`/`not` tree already supports arbitrary depth);
  `asOf` is accepted under the same relationship-predicate mutual
  exclusion the storage layer already enforces; `columns` is rejected
  with a coded error, since its aggregate-vs-explode choice has no
  Mosaic-side compiler to validate against. The compiler
  (`mosaic/core/query_spec_compiler.py`) resolves two mappings that have
  no direct `where:` shape, both verified against the real
  `normalize_where` rather than assumed: a to-one `RelatedCondition`
  with quantifier `none` compiles to `{"not": {edge, where}}`, and an
  empty `criteria` list ("has / has no related record") fills with a
  trivially-true predicate on the target's own identifier slot, looked
  up from the manifest rather than hardcoded to `id`. Both tools return
  a symmetric envelope — `{valid, errors}`, plus `items`/`total` on
  execute (null when invalid) — so a client can validate-fix-retry with
  either tool alone. `execute_query_spec` validates unconditionally
  before compiling and bounds `limit`/`offset` to 1–1000, mirroring
  REST's `list_entities`.

- **`construct-query-spec` prompt** (issue #184, ADR-0009 decision 4): a
  static MCP prompt carrying the procedural guidance the two resources
  do not convey on their own — the field-name convention, how to express
  relationship predicates, the `asOf`+`RelatedCondition` exclusion, the
  single-sort-column cap, and (guidance item 11) `inverse_of` edges.
  Takes an optional `goal` argument echoed into the returned message.

- **Aggregation tools — `count_query_spec`, `facet_query_spec`,
  `field_range_query_spec`** (issue #195). QuerySpec has no
  representation for aggregation at all, which left the MCP boundary
  strictly less capable than Mosaic's own GraphQL surface: a question
  like "how many donors per cohort", answerable today via
  `facetCounts`, became inexpressible after adopting the boundary as it
  stood — and a QuerySpec-emitting client with no such tool returned a
  *valid* spec listing every donor sorted by cohort, since validation
  checks shape and legality, never faithfulness to the instruction. All
  three are thin wrappers over the same `compile_query_spec` output and
  the same `MosaicClient.count()`/`facet_counts()`/`field_range()`
  methods the GraphQL resolvers call, with the error codes
  (`UNKNOWN_AGGREGATION_FIELD`, `UNAGGREGATABLE_FIELD`) copied verbatim
  so a client sees the same failure for the same field on either
  transport. `sort` is rejected on all three; `asOf` is honored on
  `count_query_spec` and rejected on the other two, matching each
  underlying method's own signature. The server `instructions` and the
  `construct-query-spec` prompt now route counting-style questions here.

- **`search_query_spec`** (issue #196): the other half of the same gap —
  the manifest advertises `search_available`/`searchable` flags no tool
  could act on. A thin wrapper over `MosaicClient.search()`. No `field`
  argument (FTS runs across every `hippo_search`-annotated slot at once,
  so there is nothing to check legality against) and `sort` is *honored*
  rather than rejected, since `search()` takes `order_by`/`order_dir`
  and an explicit sort overrides FTS rank. `asOf` is rejected. It
  deliberately does not add a stricter "reject a non-searchable entity"
  check that the GraphQL `search{Plural}` resolvers lack — matching the
  documented silent-empty behavior on both transports rather than
  improving on it unilaterally on one.

- **`converse_query_spec` — outbound planning delegation** (issue #186,
  ADR-0010): the one tool on this surface that is not a wrapper over a
  `MosaicClient` method. It delegates server-to-server over HTTP to a
  stateless turn-taking planning core, because Aperture's browser has no
  backend of its own and must not hold LLM credentials. Registered
  **only** when `MOSAIC_EXON_URL` is set — an unconfigured deployment
  must not advertise a delegate it cannot reach — with
  `MOSAIC_EXON_TIMEOUT` read at the same mount time so the delegate
  configuration has one lifecycle. Every returned `QuerySpec` is
  **re-validated in-process** before it can come back as a `proposal`:
  the planner is untrusted as far as this boundary is concerned, so
  "Mosaic validates before anything executes" holds even if the
  planner's own check is stale or bypassed. The response carries
  `turns`, the authoritative conversation after the call, on every call
  and not just edits — a recompute after an edit produces turns a caller
  cannot derive from its own copy, and one that tried read a *pre-edit*
  spec and executed the wrong query. Every failure becomes an `error`
  turn inside the same discriminated envelope (unreachable, timed out,
  non-JSON body, the planner's own 4xx, still-invalid-after-retry)
  rather than a bare tool exception, and an out-of-contract response —
  unrecognized status, missing message, a `proposal` without a spec, a
  `clarification`/`suspended` turn smuggling one — fails here with the
  reason named. Per ADR-0010 decision 5, client-visible failures name
  the delegate's **role**, not its address: the URL, the exception cause
  and the planner's response body go to `logger.warning` for the
  operator, since this surface carries no authn and an error turn is
  effectively public. Error paths **omit** `turns` rather than sending
  `[]` — every error means nothing was applied, so the conversation is
  unchanged, and a caller told to adopt the returned state would
  otherwise wipe the chat.

- **Opt-in CORS** (issues #207, #209): `CORSMiddleware` on the shared
  FastAPI app — covering REST, GraphQL and MCP alike — but only when an
  operator supplies an explicit origin list via `--cors-origin`
  (repeatable) or `mosaic.yaml`'s `cors_allow_origins`. No wildcard
  default, since write mutations live on the same surface; without it,
  behavior is unchanged and no CORS headers are emitted at all. This
  unblocks a browser-hosted Aperture build calling Mosaic across origins
  (previously only a same-origin dev proxy worked). Also adds a
  field-level GraphQL description on `ConversationTurn.id` (issue #208)
  explaining why it is null on `error` turns and to key off `status`
  instead.


- **`is_external_xref` on the MCP `mosaic://schema` resource; `has_default` and
  `is_external_xref` on the GraphQL `MosaicSlotInfo` type** (issue #211):
  `SlotModel` models these two attributes but neither transport surfaced
  both of them, so a consumer building a data dictionary from either one
  got a description that was incomplete with no way to tell. Both
  additions are additive — no existing consumer breaks.

- **`converseQuerySpec` GraphQL mutation** (issue #205): the MCP transport's
  `converse_query_spec` tool (issue #186, ADR-0010) now has a GraphQL
  surface too, so a browser-based client (Aperture's conversational query
  panel) that only speaks GraphQL can reach it. Hand-authored on the
  generated `Mutation` root alongside the codegen'd mutations — the same
  pattern `hippoSchema` already uses on `Query` — and registered ONLY when
  `MOSAIC_EXON_URL` is set, the identical gate the MCP tool uses. Pure
  transport plumbing: the planning delegation, the ADR-0010 relay terms,
  the in-process re-validation of every candidate `QuerySpec`, and the
  suspend/recompute semantics are unchanged and now live in
  `mosaic.core.converse_query_spec.run_converse_turn`, called directly by
  both the MCP tool and this mutation — neither reimplements any of it.
  One deliberate GraphQL-side difference: the underlying handler omits its
  `turns` field on every failure (and on a legacy-planner success), since
  GraphQL's `ConverseResult.turns` is non-null, `converseQuerySpec` fills
  it in with the caller's own input `turns` unchanged whenever the handler
  omits it — the full conversation so far, never `[]` and never absent,
  even on an `error` turn.
- **Reverse edges via LinkML `inverse` — virtual reverse references**
  (issue #204, ADR-0011). A multivalued reference slot that names its
  `inverse` (`Donor.samples: {range: Sample, multivalued: true, inverse:
  donor}`) is now a *computed* reverse edge over the forward FK column:
  no column, no link table, no relationship rows of its own. Reads
  hydrate `data["samples"]` from the target table's `donor` column
  (available targets only); the SDK/GraphQL `where:` tree and a
  `QuerySpec` `RelatedCondition` accept it as a `some`/`none` to-many
  edge compiled to a correlated `EXISTS` on the target table (SQLite and
  Postgres); `count_relationship`/GraphQL `samplesCount` count the same
  rows. Writes carrying the slot are accepted and ignored (it never
  reaches storage or the provenance patch); GraphQL Create/Update inputs
  omit it and OpenAPI marks it `readOnly`. The type model, MCP
  `mosaic://schema`/`mosaic://capabilities` resources and GraphQL
  `hippoSchema` introspection carry a new `inverse_of` field naming the
  forward slot. Schema load validates the declaration (forward slot
  exists, is single-valued, points back at the declaring class; derived
  slot multivalued and not required) — the reverse of a *multivalued*
  forward slot is rejected. This unblocks `mosaic-demo-small`'s chat
  grounding ("show me the donors of those samples"): the QuerySpec
  validator and compiler needed no change, only the storage adapters.
  `construct-query-spec` gains guidance item 11 on `inverse_of` edges.

### Fixed

- **The MCP transport's Host allow-list is configurable —
  `MOSAIC_MCP_ALLOWED_HOSTS`** (issue #214). The boundary answered only
  loopback callers; anything else got `421 Invalid Host header`, a
  response whose status and body say nothing about `Host` unless you
  read the raw bytes, and which the Python client surfaces as a generic
  "Server returned an error response". `streamable_http_app()` was
  called with neither `transport_security` nor `host`, so the SDK's
  DNS-rebinding protection derived its allow-list from the default
  `127.0.0.1`. Invisible while every caller is a host process on the
  same machine, and a hard wall the moment the boundary is
  containerized: a sibling container sends `Host: mosaic:8001`, a client
  on the Docker host sends `Host: host.docker.internal:8099`, both
  rejected. The protection is kept — it is what stops a malicious page
  driving a localhost MCP server via DNS rebinding — and made
  configurable, with loopback always permitted so the single-machine
  case is unchanged. `MOSAIC_MCP_ALLOWED_HOSTS=*` turns the check off
  for a deployment that has decided its own network boundary is the
  control, the same judgement `--cors-origin '*'` already offers. An
  empty value is **not** read as `*`: failing open on a typo would
  disable a security control with nobody noticing.

- **The published image installs the `mcp` extra, so `converseQuerySpec`
  actually works.** The image installed `.[graphql,postgres]`, but
  `httpx2` ships only with the `mcp` extra and
  `core/converse_query_spec.py` imports it at call time — so in a
  published image the mutation registered, appeared in introspection,
  and then raised an unhandled `ModuleNotFoundError` on the first real
  call: a top-level GraphQL error with `data: null`, not the structured
  `error` turn every client error path expects. ADR-0010 term 1 requires
  an optional integration be *absent*, never present-and-broken. CI did
  not catch this because `tests.yml` installs `[dev,graphql,mcp]` and no
  job built graphql-without-mcp.

- **Multi-column sort in a QuerySpec is rejected rather than silently
  truncated** (issue #183). Mosaic's query surface takes a single
  `order_by`/`order_dir` pair (mirrored by GraphQL's single-valued
  `orderBy`), so there is no second sort column to compile against. A
  two-field `sort` used to validate clean and would have dropped the
  second column at execute time — the class of bug issue #129's "loud
  over wrong" rule exists to prevent. Caught during design of the
  compiler, before any executor code existed to hide it.

- **`mosaic serve --reload-dir` — reload stops watching the CWD**
  (issue #174). `serve --reload` never passed `reload_dirs` to uvicorn,
  so it defaulted to `Path.cwd()` — the project directory, not
  necessarily where Mosaic's own source lives (e.g. a PYTHONPATH-mounted
  checkout shadowing the installed package in the DataHelix `ide`
  recipe), and edits to the mounted source silently triggered no reload.
  Adds a repeatable `--reload-dir`; when absent it defaults to the
  resolved `mosaic` package directory rather than the CWD, which must
  stay the project directory for `schema_path`/`database_url`
  resolution.

- **Provenance tracking records the adapter's real `schema_version`**
  (issue #176, sec9 decisions Fallback 2). The `schema_version` column
  on `ProvenanceRecord` rows built by `track_creation`/`track_update`/
  `track_deletion` on both the SQLite and Postgres adapters hardcoded
  `""`, even though both adapters already accept a real
  `schema_version` at construction (threaded by `MosaicClient` from
  `SchemaRegistry` per Decision 9.7.E) and use it correctly on the
  actual write path. These in-memory tracking methods just never picked
  it up.

- **Entity classes must key their identifier `id`; the schema now says so
  up front** (issue #172). LinkML's `SQLTableGenerator` names a class's SQL
  primary key after whatever slot is marked `identifier: true`, but
  Mosaic's storage layer hardcodes a literal `id` column for every
  per-class entity table (provenance, cross-class UUID resolution,
  generic insert/update/select all key off `"id"`). A schema class
  carrying the `is_available` system slot (i.e. specializing `Entity`)
  whose identifier is named something else used to fail deep inside
  ingestion with a raw "table X has no column named id" SQLite error.
  `build_schema_registry()` now rejects such a schema immediately with an
  actionable message naming the offending class/slot. Declarative,
  non-persisted config classes (the bundled `Validator`/`ReferenceLoader`,
  which have no `is_available`) are unaffected.

- **`mosaic serve --reload`/`--workers` no longer exits 1** (issue #171).
  Uvicorn requires an import-string app target — not a live app object —
  for `--reload`/`--workers`, since each reload/worker re-imports the app
  in its own subprocess; `serve()` was handing it the in-process object
  directly. Added `mosaic.serve.create_app_from_env()`, a zero-argument
  factory uvicorn can import, and threaded the resolved `--config`/
  `--graphql` options through the `MOSAIC_*` environment convention so the
  subprocess rebuilds the identical deployment. The default (no
  `--reload`/`--workers`) path is unchanged.

- **Relative schema `imports:` resolve against the schema, not the process
  CWD** (datahelix #66). LinkML anchors a schema's relative imports to that
  schema's `source_file`, but `linkml_runtime` only records `source_file`
  when a schema is loaded from a *path* — one built from an in-memory
  *string* has `source_file = None` and falls back to the current working
  directory. Mosaic builds a `SchemaView` from text whenever it must
  pre-process a schema first (stripping the Mosaic-only `requires:`
  directive, or merging every file in a schema directory), so
  `mosaic migrate --schema-dir schemas` run from `/project` looked for
  `/project/pathology.yaml` instead of `/project/schemas/pathology.yaml`.
  `SchemaRegistry` now re-anchors `source_file` to the originating path in
  both paths, restoring the LinkML contract: a relative import resolves
  relative to the referencing schema. Out-of-directory relative imports
  (e.g. `../common/base`) resolve correctly too.

### Upgrading

- **`inverse:` is a schema-authoring change, not a code upgrade.** Nothing
  in a deployment gains a reverse edge by moving to this version. The
  capability is reachable only once a deployment's LinkML declares one —
  a multivalued reference slot naming its `inverse` (`Donor.samples:
  {range: Sample, multivalued: true, inverse: donor}`) — after which the
  edge is computed over the forward FK column, with no migration and no
  rows of its own. Existing schemas are unaffected.

- **`--mcp` and every QuerySpec tool are opt-in.** The MCP surface mounts
  only under `mosaic serve --mcp`, needs the new `mcp` extra installed,
  and is read-only. `converse_query_spec` additionally registers only
  when `MOSAIC_EXON_URL` is set. Containerized MCP callers must set
  `MOSAIC_MCP_ALLOWED_HOSTS` — the default allow-list is loopback only.

- **CORS stays off** unless an operator supplies an explicit origin list
  (`--cors-origin` / `cors_allow_origins`). There is no wildcard default.

## v0.13.0 — 2026-08-20 (BREAKING: search returns a page envelope; the typed filter contract completes — relationship predicates, aggregation & ordering, cross-class roots)

### Added

- **Relationship cardinality count** (issue #132, deferred from
  ADR-0005): `MosaicClient.count_relationship(entity_type, entity_id,
  edge)` answers the cardinality of a multivalued reference edge with a
  single indexed `COUNT(*)` over the ADR-0002 `relationships` table
  joined to the target's table, without resolving any member object —
  the interim resolve-to-count optimization ADR-0005 called for. Reuses
  M5b's `_reference_edge` resolution, so an unknown edge or a to-one
  edge raises the same errors that path already does, and the same
  availability-consistency rule applies (an edge whose target is
  unavailable is not counted). GraphQL: every resolvable multivalued
  reference field gets a `<field>Count: Int!` sibling (e.g.
  `samplesCount` beside `samples`) — the relationship-count-badge
  consumer named in the issue no longer has to resolve the whole list
  just to show its length.

- **To-many quantified relationship predicates** (ADR-0006 M5b /
  increment 4, issue #155 — the FINAL typed-filter increment): the
  `where` tree's edge node gains a quantifier —
  `{"edge": <multivalued reference slot>, "quantifier": "some"|"none",
  "where": subtree}` — compiled by both adapters to `EXISTS`/`NOT
  EXISTS` against the ADR-0002 `relationships` link table joined to the
  target's table, with availability guards on BOTH the edge and the
  target (an entity with no edges matches `none`; an unavailable target
  never satisfies `some`). GraphQL: multivalued reference edges on
  `<Type>Filter` nest a per-target `{some, none}` quantifier object
  (`<Target>EdgeQuantifiers`) taking the target's filter — recursive via
  the same bare-class pass as M5a, counted against the `where` depth
  cap, `asOf` rejected with the coded
  `ASOF_RELATIONSHIP_FILTER_UNSUPPORTED` error. Loud
  quantifier/cardinality validation: a multivalued edge without a
  quantifier, a quantifier on a to-one edge, and unknown quantifiers all
  raise; the flat `filters:` path's `UNFILTERABLE_FIELD` message for
  multivalued references now points at the typed `where` quantifiers.
  With this, the ADR-0006 typed filter contract is fully implemented
  (increments 1–4).

- **Heterogeneous roots** (issue #158): two additive cross-class query
  roots following the house JSON-envelope pattern (ADR-0005 —
  deliberately not a GraphQL union/interface root; that remains future
  work with its own ADR).
  `searchAll(q, limit): [SearchHit]` fans over every FTS-indexed class
  server-side (SDK: `client.search_all`), merges by the per-index
  normalized rank with a deterministic tiebreak, and materializes the
  page batched by type — one composed read per class present, never
  per-hit; availability parity with list queries.
  `neighbors(id, depth, asOf): NeighborhoodGraph` (SDK:
  `client.neighbors`) returns a renderable `{nodes, edges, edgeSources,
  notices}` subgraph covering **both edge stores** — the ADR-0002
  `relationships` link table (both directions) AND column-stored
  single-valued references (forward off the frontier's data; reverse
  via schema-driven FK queries) — closing the visibility gap a
  link-table-only traversal has. Depth cap 5, 1000-node budget with a
  disclosed truncation notice, batched-by-type node materialization,
  and edges kept only when both endpoints materialize (availability
  parity). Under `asOf`, link-table edges replay from provenance (sec6
  §6.8.2) and node states reconstruct at that time; column edges are
  out of scope under as-of (hippo#71) — disclosed via
  `edgeSources`/`notices`, never a silently partial temporal graph.

- **To-one relationship predicates** (ADR-0006 M5a / increment 3, issue
  #155): the `where` filter tree gains the node
  `{"edge": <to-one reference slot>, "where": subtree}` — matching
  entities whose referenced target exists, is available, and satisfies
  the nested tree — compiled by both adapters to ONE correlated
  `EXISTS` keyed on the FK value (SQLite: against the target's
  per-class table; Postgres: against the target's `entities` row with
  `entity_type` + availability guards, nested trees cast against the
  TARGET class's ranges). Edges nest arbitrarily (an edge inside an
  edge) and self-referential edges work (aliases are threaded so inner
  scopes never collide); `not` over an edge is two-valued — an entity
  with no referenced target satisfies the negation. GraphQL:
  `<Type>Filter` nests the target type's filter under the resolved edge
  name (`where: {donor: {age: {gt: 60}}}`), generated via the two-pass
  bare-class cross-reference; edge nesting counts toward the
  `where`-input depth cap. Because the predicate compiles into the
  shared query WHERE, it composes with everything that rides it: flat
  filters, search composition, `count`/`facetCounts`/`fieldRange`, and
  `orderBy`. Gate decisions: `asOf` + any relationship predicate is a
  coded `ASOF_RELATIONSHIP_FILTER_UNSUPPORTED` error (cross-class
  temporal joins are out of scope — hippo#71), enforced at the
  transport and again in both adapters; an unknown edge or a
  multivalued reference edge raises loudly (the latter naming the M5b
  `some`/`none` quantifiers that remain the last increment).

### Changed

- **BREAKING — search composed with the list surface** (issue #157):
  `MosaicClient.search` / `QueryService.search` return the same
  `PaginatedResult` envelope as `query()` instead of a bare entity list;
  the GraphQL search twins return the `Page` type
  (`items`/`total`/`limit`/`offset`) instead of `[Entity]`, and REST
  `GET /search` returns the same paginated envelope as `GET /entities`
  (Aperture — the known consumer — asked for exactly this envelope).
  Search now runs on **id-set composition**: the adapters' ranked FTS
  path (bm25 / ts_rank, availability-filtered) produces an ordered id
  list that feeds ONE composed `find()` — eliminating the per-hit N+1
  (a 100-hit search used to issue 100 entity reads plus temporal
  aggregations; now one batched read + one batched temporal aggregation
  per page) and fixing the `offset >= limit → []` slice bug that
  silently broke deep paging on GraphQL and REST. Search accepts the
  list surface's `filters`/`where`/`filter_mode` (REST: the same
  arbitrary field-filter query params as `GET /entities`), with the
  identical coded-error discipline; the ranked id set always bounds the
  result whatever `filter_mode` says. Rank-precedence rule (pinned):
  results come back in FTS rank order by default; an explicit
  `order_by` overrides rank via the ADR-0007 pushdown. `total` is the
  matching-and-filtered count, bounded by the 1000-hit FTS budget, and
  excludes unavailable entities exactly as list queries do.

### Added

- **Aggregation & ordering surface** (ADR-0007, issue #156): the storage
  `Query` carries `order_by`/`order_dir`, pushed down by both adapters as
  SQL `ORDER BY` (typed casts on Postgres JSONB so numeric/temporal slots
  order by value; NULLs last in either direction on both backends; stable
  `id` tiebreak). `MosaicClient.query(order_by=..., order_dir=...)` runs a
  pushdown path — SQL ordering + `LIMIT`/`OFFSET` paging, `total` from a
  `COUNT(*)` under the identical predicate, temporal fields derived for
  the page only — so ordered paging no longer materializes the match set;
  without `order_by` the historical `created_at`-ascending path is
  byte-identical. New SDK aggregation methods `count`, `facet_counts`
  (per-value buckets, count desc then value, NULLs never counted), and
  `field_range` (min/max for range facets), all **availability-consistent**:
  every aggregate sees exactly the entities list queries see
  (`is_available` + the caller's `filters`/`where`; the pre-existing
  `entity_counts()` registry semantics are documented as NOT the model).
  GraphQL: generated per-class `<Type>OrderField` enums (stored columns
  only — computed temporal fields, references, and multivalued slots are
  not orderable) with `orderBy`/`orderDir` on every list query, and new
  roots `{plural}Count`, `{plural}FacetCounts(field)`,
  `{plural}FieldRange(field)` sharing the list surface's `filters`/`where`
  arguments. Per the ADR-0007 gate decisions: `orderBy` + `asOf` is a
  coded error (`ASOF_ORDERING_UNSUPPORTED`), `count` under `asOf` keeps
  the documented Python-path semantics, the facet/range roots are
  current-state only, and unknown/unaggregatable fields are coded errors
  (`UNKNOWN_AGGREGATION_FIELD`/`UNAGGREGATABLE_FIELD`) extending the #149
  discipline. The `limit=0` → zero rows rule (#130) holds on the pushdown
  path, with `total` still reported.

- **Generated typed `where:` filter argument** (ADR-0006 increment 2,
  issue #155): every list query gains `where: <Type>Filter` — a generated
  per-class input whose slot fields are per-kind operator objects
  (`StringFilterOps`, `IntFilterOps`, `FloatFilterOps`, `DateTimeFilterOps`,
  `BooleanFilterOps`, per-enum `<Enum>FilterOps`, …) carrying exactly the
  operators each slot's LinkML range supports, plus `and`/`or`/`not`
  combinators (input depth cap 10, coded `FILTER_TOO_DEEP`). The
  introspected schema is the capability contract: wrong operators or
  mistyped values fail GraphQL validation before execution. The SDK gains
  the matching `client.query(where=...)` boolean filter tree
  (`{"field","op","value"}` leaves under `and`/`or`/`not`), validated by
  `normalize_where` and compiled to SQL on both adapters — with `NOT`
  forced to two-valued logic (`COALESCE(..., FALSE)`) so an entity missing
  a field satisfies a negation identically in SQL and in the shared as-of
  mirror (`matches_tree`). `where` composes with the flat `filters:` by
  AND; reference slots are deliberately absent from `<Type>Filter` until
  the relationship-predicate increments (M5a/M5b) land. LinkML slot
  descriptions propagate onto the generated filter inputs.

- **Comparison and null-test filter operators** (ADR-0006 increment 1,
  issue #155): list-query filters now support `neq`, `gt`/`gte`/`lt`/`lte`
  (numeric and temporal slots), `contains` (case-insensitive substring,
  `%`/`_` literal), and `is_null` (boolean value; `true` matches entities
  with no stored value) alongside `eq`/`in` — on the SDK
  (`client.query(filters=[{"field": ..., "op": ..., "value": ...}])`) and
  the GraphQL `FilterOp` enum. Pushed down on both adapters: SQLite typed
  column predicates; Postgres JSONB predicates with per-range casts
  (`::numeric`, `::date`/`::timestamptz`) resolved from the slot's LinkML
  range (typeof chains included) so numeric/temporal comparisons are never
  lexicographic. The as-of path evaluates every operator through one shared
  evaluator (`mosaic.core.storage.matches_operator`), so as-of results
  cannot diverge from live SQL. GraphQL enforces the per-slot operator
  vocabulary (comparisons on ordered ranges, `CONTAINS` on strings,
  `IS_NULL` everywhere) with coded `UNSUPPORTED_FILTER_OP` /
  `INVALID_FILTER_VALUE` errors; `value: null` is rejected — absence is
  asked with `IS_NULL`. SQL NULL semantics apply throughout: comparison
  operators (including `neq` and the previously-divergent as-of `eq`)
  never match entities missing the field.

### Fixed

- **GraphQL list filters no longer answer an unmatched `field` with an empty
  page (#149).** `filters.field` reaches storage, which is keyed by LinkML slot
  name (`sample_type`), while the generated type exposes the camelCase spelling
  of the same slot (`sampleType`). Nothing in the schema signalled that two
  vocabularies were in play, and the adapter's response to an unknown column is
  to match zero rows — so a filter built from ordinary GraphQL introspection
  returned a valid-shaped, empty result indistinguishable from "nothing
  matched". Both spellings now resolve to the same column, as does a
  reference's resolved edge name (`donor` → the `donor_id` column), whose raw
  carrier is hidden under edge-only emission (ADR-0005). A `field` that
  addresses no column is now a GraphQL error: `UNKNOWN_FILTER_FIELD` for an
  unrecognized name, and `UNFILTERABLE_FIELD` for the two kinds that look
  filterable but cannot be — read-time computed fields (`createdAt`,
  `updatedAt`, `supersededBy`; sec9 §9.7, recognized under either spelling
  since introspection only shows the camelCase one) and multivalued references
  (relationship edges rather than columns per ADR-0002; use `relatedTo`).
  REST is unaffected — its filters are query params spelled as slot names —
  but the SDK/storage layer still answers an unknown filter field with zero
  rows for non-GraphQL callers.

## v0.12.1 — 2026-07-21 (multi-arch images; publish as `mosaic`)

### Changed

- **Release pipeline now publishes multi-arch images** (`linux/amd64` +
  `linux/arm64`) so the image runs natively on arm64 (Graviton) hosts without
  emulation.
- **Published image renamed `ghcr.io/bu-neuromics/hippo` →
  `ghcr.io/bu-neuromics/mosaic`** (ADR-0004 "Phase R"). No code changes; the
  repo redirect was already in place. The `hippo.*` import/entry-point
  namespace migration remains separate and unchanged.

## v0.12.0 — 2026-07-18 (BREAKING: edge-only GraphQL references; REST field filtering; query-correctness & referential-integrity fixes)

### Added

- **REST entity-field filtering — eq + IN via multi-value query params (#102).**
  `GET /entities` previously accepted `filter_mode` but never actually
  filtered on entity data fields (`filters=[]` was hardcoded). Per sec4 §4.3's
  "multi-value parameters" convention, any query param other than the
  endpoint's own reserved set (`entity_type`, `limit`, `offset`,
  `filter_mode`, `updated_since`, `as_of`) is now treated as an entity-field
  filter: given once it's an `eq` filter, repeated it's an `in` (set-membership)
  filter — e.g. `?tissue_type=brain&tissue_type=liver` — composed across
  fields per `filter_mode`. This closes the REST leg of #102; the SDK
  (`QueryEngine`), both storage adapters, and GraphQL already supported `in`
  (#125).

### Fixed

- **`client.get(expand=…)` now resolves real references (#128).** Reference
  slots are stored as bare ids — a single-valued reference is a plain string
  id, a multivalued reference a hydrated `list[str]` (#79 / ADR-0002) — but
  `BatchFetcher._extract_entity_ids` only recognized nested `list[dict]` /
  `dict` shapes, so `_expanded` came back `{}` for every real reference, with
  no error. Resolution is rewritten to handle the actual id shapes: a
  single-valued reference resolves to one entity dict, a multivalued reference
  to a list, keyed by the expanded slot name, with nested paths (`a.b`) hung
  off each level's own `_expanded` map. The embedded `{"id": …}` shape still
  works. Adds end-to-end coverage against a real SQLite adapter (every prior
  expand test used a `Mock()` with hand-shaped dicts and never exercised real
  reference data).
- **Polymorphic-base references are now referentially enforced (#127).** A
  single-valued reference whose range is a polymorphic base (abstract, or
  concrete with concrete subclasses) has no SQL foreign key — subtype
  instances live in their own per-class tables, so no single table can be the
  FK target (#93) — and a dangling id on such a slot was silently accepted on
  both `put` and `ingest` (`errors=0`), while non-polymorphic references (real
  FKs) still caught it. Referential integrity for these slots is now checked
  in the application layer against the `_entity_registry` cross-class index,
  **deferred to commit** so forward references within a staged bundle still
  resolve, mirroring the deferred-FK behavior for non-polymorphic references
  (#95). A dangling or out-of-range id raises the new `DanglingReferenceError`
  (a `ValidationError` subclass → REST 422 / GraphQL `VALIDATION_FAILED`) and
  rolls the write back. In the Brain Bank schema this closes the gap on
  `Dataset.produced_by → Activity`, `Diagnosis.sample → Sample`, and
  `Measurement.sample → Sample`.
- **Unsupported filter operators now raise instead of silently degrading to
  equality (#129).** Only `eq` and `in` are implemented; any other `op`
  (`gt`/`lt`/`ne`/`contains`/…) previously fell through to exact equality,
  returning a **wrong** result set rather than an error — `ne` returned the
  logical *inverse* of what was asked. `mosaic.core.storage.normalize_filter`
  now validates `op` against `VALID_FILTER_OPS` and raises `ValidationError`
  for anything else, covering every backend and both the live and as-of read
  paths. A canonical filter dict carrying the key `operator` (the
  `FilterCondition` model field name) instead of `op`, previously ignored and
  defaulted to `eq`, now raises the same clear error. (The `mosaic entity`
  CLI's own filter parser, which used the `operator` key, now emits `op`.)
- **`query(limit=0)` now returns zero rows instead of all rows (#130).** The
  limiting code was gated on `if limit:`, so Python's falsy-`0` skipped the
  slice and returned the full result set — identical to `limit=None`. Fixed to
  `if limit is not None:` on every paginating path: `QueryService.query`,
  `QueryService.query_updated_since`, and the SQLite and PostgreSQL adapters'
  live and as-of `find` slices.

### Changed

- **BREAKING — GraphQL reference emission is edge-only ([ADR-0005](design/decisions/ADR-0005-graphql-reference-emission-edge-only.md)).**
  A reference slot now renders as a single resolved relationship field
  (`donor: Donor`, `samples: [Sample!]!`); the raw `*_id`/`*_ids` output scalar
  is **removed**. The stored foreign-key id becomes a hidden
  `strawberry.Private` carrier the resolver reads — physical identifiers no
  longer cross the API boundary (the *logical-identity boundary* invariant).
  Read a reference's id through the edge (`donor { id }`); filter/create/update
  inputs are unchanged (they still take the target id under the slot name). A
  reference to an abstract/polymorphic base (no generated type) retains a raw
  `*_id` field as an interim (build-time warning); a resolved-name collision is
  now a build-time error rather than a silent raw-id fallback. Consumers
  reading the raw output field must migrate to the edge. (#133)
- **Removed the bearer-token gate on REST/GraphQL operations (#54 Part A).**
  Mosaic holds **zero authn/authz** — authentication and authorization are
  entirely the deployment's responsibility (the platform's Bridge PEP/PDP). The
  previously-required bearer token on operations is gone; deployments that
  fronted Mosaic with their own gateway are unaffected.

## v0.11.0 — 2026-07-17 (Hippo is now Mosaic; status introspection, incremental polling, cyclic-reference ingest)

### Added

- **IN / set-membership filter — SDK + GraphQL (partial issue #102).**
  `Query.filters` entries gain an optional `op` key alongside `field`/
  `value` (`{"field": ..., "op": "in", "value": [...]}`); `op` defaults to
  `"eq"` so every pre-existing filter dict — canonical or the bare
  `{field: value}` shorthand — keeps behaving exactly as before. `"in"`
  matches when the field's value is a member of the given list; an empty
  list short-circuits to "no rows match" rather than emitting invalid
  `IN ()` SQL. Implemented across all four filter-evaluation paths: the
  SQLite typed-column path (`"field" IN (?,...)`), the SQLite as-of
  Python matcher, the PostgreSQL JSONB path (`data->>%s = ANY(%s)`), and
  the PostgreSQL as-of Python matcher — sharing a new
  `mosaic.core.storage.normalize_filter()` helper. GraphQL's
  `FilterInput` gains an `op: FilterOp` field (`EQ`/`IN`, default `EQ`)
  threaded through to the SDK. REST field filtering is out of scope for
  this change — `mosaic/serve/routers/entity.py` does not yet wire
  arbitrary field filters at all (a separate prerequisite gap).
- **Status introspection across SDK / REST / CLI (sec4 system
  endpoints).** `MosaicClient.status()` owns a single status summary —
  Mosaic version, active storage adapter, schema version, declared entity
  types, per-type entity counts, and adapter capability declarations
  (`search` modes, `staged_transaction`). Both storage adapters gain
  `entity_counts()` (SQLite via the `_entity_registry` shadow table,
  PostgreSQL via the `entities` table); counts include unavailable
  (soft-deleted / superseded) entities since Mosaic never hard-deletes.
  The REST `GET /status` route is a thin wrapper over the SDK and is
  authenticated (it describes the deployment's data); `/health` stays an
  open liveness probe. A new read-only CLI surface mirrors the SDK query
  layer: `mosaic entity get/query/search/history` and a top-level
  `mosaic status` verb, both built through the shared core factory and
  defaulting to YAML (`--json` for machine output). The API root and
  OpenAPI metadata now report the installed package version instead of a
  hardcoded `0.1.0`. Also fixes `docker-compose.test.yml`, which mounted
  both a named volume and a tmpfs at the PostgreSQL data directory and
  refused to start.
- **Ship `py.typed` (PEP 561) + package metadata (issue #56).** Mosaic now
  advertises inline type information to downstream type-checkers via a
  `py.typed` marker (`src/mosaic/py.typed`), and `pyproject.toml` gains
  `keywords` and trove `classifiers` (including `Typing :: Typed`).
- **`query_updated_since` incremental polling (sec4 §4.5).**
  `MosaicClient.query_updated_since(entity_type=None, since, ...)` returns
  entities whose provenance-derived `updated_at` is strictly greater than the
  `since` watermark, ordered by `updated_at` ascending so polling callers (e.g.
  Cappella's `hippo_poll` trigger) can advance their watermark incrementally.
  `entity_type` is optional and composes with the issue #44/#49 cross-class
  scan (`None` polls across all types). Exposed over REST as
  `GET /entities?updated_since=<ISO8601>`; an unparseable watermark raises
  `TemporalQueryError`, mapped to HTTP 400 "Temporal Query Error". Comparison
  uses Mosaic's server-side provenance timestamps (UTC) to avoid clock skew.
- **Full SDK exception → HTTP status mapping in the REST layer (sec4 §4.3).**
  Previously only `EntityNotFoundError` (404) and the validation errors (422)
  had dedicated REST handlers; every other `MosaicError` (supersession
  conflicts, config/adapter failures, provenance-integrity violations,
  ingestion / search-capability / temporal-query / schema errors, recipe and
  migration errors, ...) collapsed to an anonymous `500 Internal Server
  Error`, leaving clients unable to distinguish causes. The API factory now
  maps the hierarchy via a `_make_mosaic_handler(status, title)` factory: 409
  for `EntityAlreadySupersededError` and `ConfigError` (adapter conflict),
  422 for `ValidationFailure`, 400 for `IngestionError` (and its subclass
  `IngestionValidationError`) / `SearchCapabilityError` / `TemporalQueryError`
  / `SchemaError`, named-and-logged 500s for `AdapterError` and
  `ProvenanceIntegrityError`, and a named-500 `MosaicError` fallback (using the
  concrete class name) for any other SDK error. Non-Mosaic exceptions still
  return an opaque 500 with the detail withheld. All responses reuse the
  existing `ErrorResponse` body, and the tier-tagged `ValidationFailed`
  envelope handler (sec9 §9.9) is unchanged. Documented in
  `docs/api-reference.md`.

### Fixed

- **A multivalued `inlined_as_list` slot ranged on a class with its own
  identifier no longer stringifies its inline objects (issue #121).**
  `SchemaRegistry.multivalued_reference_slots` classified any multivalued
  slot ranged on a known, non-value-type class as a reference — it never
  consulted `slot.inlined`/`slot.inlined_as_list`. An explicitly inlined
  list of objects whose range class declared an identifier (e.g.
  `Analysis.parameters: Parameter`, `Parameter` having an `id`) was
  therefore routed through the id-normalization relationship path, which
  `str()`-coerced each element — the values round-tripped as a list of
  stringified Python dicts (`"{'pid': 'P1', ...}"`) instead of structured
  objects, silently (`errors=0`). Identifier-less inline value objects
  (`Mass`/`Volume`/`Concentration`) were unaffected. `inlined`/
  `inlined_as_list` slots are now excluded from
  `multivalued_reference_slots` regardless of whether the range declares an
  identifier, and both DDL generators (`DDLGenerator` for SQLite,
  `PostgresDDLGenerator` for the not-yet-wired-into-CRUD per-class-table
  migration path) collapse them to a single JSON TEXT column instead of a
  linktable/FK, mirroring the existing scalar-multivalued and
  identifier-less-value-type treatment.
- **PostgreSQL now has parity with SQLite for multivalued reference/scalar
  slots (issue #81, follow-up to #79 / ADR-0002).** `PostgresAdapter`
  previously stored every submitted field verbatim in the `entities.data`
  JSONB document, so multivalued reference slots round-tripped as plain
  JSON arrays instead of relationships — no edges, no `relationship_add`/
  `relationship_remove` provenance, no as-of edge replay (ADR-0001), and no
  traversal support, unlike the SQLite backend fixed under #79. Multivalued
  reference slot values are now materialized as relationships keyed by the
  slot name inside the same transaction as `create`/`update_data` (target
  existence is not checked, preserving forward references; update/replace
  reconciles edges, so an omitted slot clears them) and stripped from the
  stored JSONB document — the relationships table is the sole current-state
  source, matching SQLite's per-class-table omission. `read`, `read_any`,
  `find` (query), and `delete`'s snapshot now hydrate those edges back into
  `entity["data"][slot]` in one batched round-trip (`find` batches per
  distinct entity type present in the result set, since a single Postgres
  query can span multiple types). The `relationships` table gains a `seq`
  `BIGSERIAL` column so edge order is preserved on hydration (Postgres has
  no SQLite-style implicit `rowid`). `PostgresDDLGenerator` (the separate,
  not-yet-wired-into-CRUD per-class-table migration generator) also now
  excludes multivalued reference slots from generated tables (no column, no
  FK) and collapses multivalued non-reference slots to a single inline TEXT
  column, mirroring `DDLGenerator._rewrite_multivalued_scalar_slots`. Ported
  `tests/core/test_multivalued_refs.py`'s scenarios onto the adapter
  directly as `tests/integration/test_postgres_multivalued_refs.py`. No data
  migration/backfill — reconciliation is idempotent runtime upsert
  behavior, and the project's dev-only stance covers any prior rows.
- **Ingesting an id that already exists under a different class no longer
  silently drops the row (issue #116).** `id` values are meant to be globally
  unique (`_entity_registry` resolves an id to exactly one class), but
  `MosaicClient.put()`/`replace()` resolved an id polymorphically without
  checking it against the *requested* class — a second write to the same id
  under a different class was silently treated as an update against the
  wrong class's table, updated zero rows, and reported success (`created`/
  `errors=0`) while persisting nothing. `put`, `replace`, and the SQLite
  adapter's `create()` now raise a new `EntityTypeConflictError` (REST 409
  "Entity Type Conflict", GraphQL `ENTITY_TYPE_CONFLICT`) when an id is
  already registered under a different class, instead of silently dropping
  the write. `mosaic ingest` surfaces this per-row as an `errors`-counted
  failure with the conflicting id and both class names.
- **`mosaic ingest` now accepts `--db-path` (issue #89).** It previously had
  no `--db-path` option and always wrote to the factory-default
  `data/mosaic.db` relative to the CWD, regardless of the store a prior
  `mosaic migrate --db-path` had targeted — `migrate` and `ingest` silently
  diverged to different databases. `--db-path` now mirrors `migrate`/
  `query`/`get` for both the LinkML-native path and the generic csv/json/sql
  loader path.
- **`test_cli_deprovision_not_installed_exits_nonzero` no longer fails under
  the project's locked click/typer versions (issue #70).** The test asserted
  on `result.stderr`, but with click 8.1.8 (pinned in `uv.lock`) typer's
  `CliRunner` merges stderr into stdout by default and only separates it
  when constructed with the click-8.2-removed `mix_stderr=False` flag, so
  `.stderr` raised `ValueError: stderr not separately captured`. Now asserts
  on `result.output`, matching every other CLI test in the suite and stable
  across click 8.1/8.2.
- **`docs/reference-loaders.md` `requires:` pin examples no longer use the
  unsupported data-version-slug form (issue #78).** The examples showed
  `mosaic-reference-ensembl==mus_musculus.GRCm39.115`, but v1's `requires:`
  check matches the pin against the installed **pip package version**
  (`importlib.metadata.version`), not the data-version slug passed to
  `mosaic reference install --version` — so copying the documented form
  failed with `HIPPO_REQUIRES_UNSATISFIED`. Examples now use the pip-version
  form (`==0.1.0`) with a note on the distinction and the planned v2
  slug-aware pin surface.
- **Mount the GA4GH DRS router in the default serve app (issue #55).** The DRS
  read-only router (`GET /ga4gh/drs/v1/objects/{object_id}`) shipped with tests
  but was never added to `create_default_app()`, so `mosaic serve` silently
  omitted it. It is now mounted in the default router set.
- **Self-referential / cyclic reference slots can now be ingested (issue #95).**
  `mosaic ingest` inserted entities one row at a time with foreign keys enforced
  immediately, so instances forming a reference cycle (`A → B → A`, or a
  self-loop `A → A`) failed with `FOREIGN KEY constraint failed` under every
  insertion order — no ordering or number of passes could satisfy a cycle. The
  whole bundle now writes inside a single staged transaction with foreign-key
  checks **deferred to commit** (SQLite `PRAGMA defer_foreign_keys`), so rows
  may reference each other in any order — cycles, self-loops, and plain forward
  references all resolve and are validated together at commit. Ingest is now
  atomic: a genuinely dangling reference fails the commit and rolls the whole
  bundle back rather than leaving a partial write. Deferral is enabled for the
  `staged_transaction` scope generally, so `batch_put` gains the same cyclic
  support.

### Changed

- **Reference-loader install/upgrade/deprovision lifecycle relocated into
  the core SDK (issue #69).** Follow-up to #67 / PR #68, which moved
  schema-package *discovery* and `requires:` resolution out of the CLI
  layer into `mosaic/core/loaders/discovery.py`. The remaining lifecycle
  orchestration (`install_reference`, `upgrade_reference`,
  `deprovision_reference`, `migrate_bundle`, `compute_exposure`,
  `list_reference_loaders`, and their private helpers) is pure business
  logic with no `typer`/`argparse` dependency, so per CLAUDE.md's
  "business logic in SDK, transport layers are thin wrappers" principle
  it now lives in `mosaic/core/loaders/lifecycle.py`. `mosaic/cli/commands/
  reference.py` re-exports every moved name so existing imports and test
  monkeypatch targets keep resolving, and is now purely presentational
  (argparse `--flag` rendering, typer sub-app mounting, breakdown table
  formatting, and the cache verbs). `cli/main.py`'s command bodies import
  the lifecycle entry points directly from core. Behavior-preserving; no
  functional changes.
- **The component is renamed Hippo → Mosaic** (ADR-0004). The distribution
  is now `datahelix-mosaic` (platform ADR-0002 prefixed-dist convention),
  the import package is `mosaic`, the CLI is `mosaic`, and the public
  client class is `MosaicClient`. This is a naming change only — **no
  data-model impact**: LinkML schema-layer names (`hippo_core`,
  `hippo_ext`, `hippo_*` annotation keys), SQL identifiers (`hippo_meta`),
  and on-disk provenance are untouched.
- Canonical entry-point groups are now `mosaic.storage_adapters`,
  `mosaic.write_validators`, `mosaic.schema_packages`,
  `mosaic.reference_loaders`, and `mosaic.reference_loader_cli`. The
  legacy `hippo.*` groups are still resolved (mosaic canonical, dedup by
  entry-point name), and built-ins are registered under both spellings.
  New third-party plugins should be named `mosaic-reference-<name>` /
  `mosaic-adapter-<name>`; `hippo-reference-*` packages remain
  discoverable for the whole deprecation window.
- Runtime config is now `mosaic.yaml` (written by `mosaic init`);
  `hippo.yaml` / `hippo.yml` are still auto-detected as a fallback with a
  `DeprecationWarning`. The default SQLite path for new deployments is
  `data/mosaic.db`; an existing `data/hippo.db` is still picked up.
- Environment variables are now `MOSAIC_*` (`MOSAIC_CACHE_DIR`,
  `MOSAIC_RECIPE_CACHE`, `MOSAIC_TUI_TOKEN`, `MOSAIC_DATABASE_URL`); the
  `HIPPO_*` spellings are honored as a fallback with a one-time
  `DeprecationWarning` per variable.

### Deprecated

- `import hippo` — a shim package re-exports mosaic's surface and aliases
  every `hippo.*` submodule to the same module object as its `mosaic.*`
  counterpart (module identity holds; `isinstance` checks are safe).
  Emits one `DeprecationWarning` on import.
- The `hippo` console script — delegates to `mosaic` after printing a
  deprecation notice to stderr.
- `HippoClient` — assignment alias of `MosaicClient` (also `HippoError`,
  `HippoConfig`, `load_hippo_config`).
- Deprecation window: the `hippo` aliases (import shim, CLI alias, legacy
  entry-point groups, config/env fallbacks) will be kept for **at least
  two minor releases**; removal will be decided by a future ADR.

## v0.10.6 — 2026-07-08 (Postgres write parity: updates, availability, boolean filters)

### Fixed

- **Updates and availability changes work on postgres.** The ingestion
  service calls `storage.update_data` / `storage.set_availability` /
  `storage.mark_superseded` on any adapter; the postgres adapter had none
  of them, so every SDK/transport update, `set<T>Availability`, and
  supersede crashed with AttributeError. All three now mirror the SQLite
  semantics over the generic `entities` table (document update + version
  bump + provenance; availability flip + `availability_change` record).
- **Boolean equality filters match on postgres.** `data->>field` yields
  JSON literals (`true`/`false`) but the filter compared against
  `str(False) == "False"`, silently matching nothing — boolean facets
  returned empty sets. Filter values are now rendered as JSONB text.
  Both found by the DataHelix certification golden path (datahelix#45).

## v0.10.5 — 2026-07-08 (Postgres FTS parity at init)

### Fixed

- **Search works on a fresh postgres deployment.** SQLite creates FTS
  shadow tables alongside its typed tables, but the postgres adapter never
  created them, so `search()` (and the GraphQL `search<T>s` fields) crashed
  with `relation "fts_<type>_<slot>" does not exist` on any deployment
  that had not created them explicitly. `_init_database` now creates the
  shadow tables for every `hippo_search` slot from the schema registry
  (idempotent), so ingestion syncs content from the first write and search
  works out of the box. Found by the DataHelix certification golden path
  (datahelix#45).

## v0.10.4 — 2026-07-08 (String slots round-trip verbatim)

### Fixed

- **Scalar `range: string` slots no longer JSON-decode on read (SQLite
  adapter).** `_decode_column_value` decoded any stored text that parsed
  as a JSON container, so a string slot carrying serialized JSON (e.g.
  Aperture's control-plane `payload` envelopes) came back as a dict and
  the GraphQL `String` type refused to serialize it, nulling the whole
  page. The decode is now schema-driven via the new
  `SchemaRegistry.string_slot_names()` (mirroring the boolean reversal):
  scalar string slots pass through verbatim; multivalued string slots
  still decode their JSON arrays. Found by the aperture#15 live-seam
  reconciliation (datahelix#45).

## v0.10.3 — 2026-07-07 (Postgres FTS write fix)

### Fixed

- **Writes on a postgres deployment no longer crash when the schema declares
  `hippo_search` slots (#108).** `IngestionService._sync_entity_to_fts`
  checked FTS-table existence with the SQLite helper (`sqlite_master` + `?`
  placeholder), which psycopg rejects as "the query has 0 placeholders but 1
  parameters were passed" — every `put()` failed. The check now goes through
  the storage adapter's own FTS store. Found by the first real DataHelix
  certification boot (datahelix#45).

## v0.10.2 — 2026-07-07 (Release pipeline + polymorphic-base reference fix)

### Added

- **Release pipeline (issue #97, DataHelix 1.0 epic P1.1).** Pushing a
  `vX.Y.Z` tag now builds and publishes a wheel/sdist and a digest-addressed
  image at `ghcr.io/bu-neuromics/hippo`, attaching the digest as an
  `image-digest.json` release asset for the DataHelix certification ledger
  (`composition.lock.json`). PyPI publishing is gated behind the
  `PYPI_PUBLISH` repository variable pending the distribution-name decision
  (`hippo` is taken on PyPI). The Dockerfile now actually builds (the old
  builder stage never copied `src/`) and carries the `graphql` and `postgres`
  extras the certification compose requires. Process documented in
  `RELEASING.md`.

### Fixed

- **References ranged on a polymorphic base class no longer fail their foreign
  key on ingest (issue #93).** `hippo migrate` emitted a foreign key against
  the base-class table for any single-valued reference whose declared range is
  a polymorphic base (a class with `designates_type` and/or concrete
  subclasses), but `hippo ingest` dispatches a subtype instance into its own
  per-subclass table (issue #80) and never populates the base table — so every
  such reference failed with `FOREIGN KEY constraint failed` and no amount of
  re-ingest could resolve it. The DDL generators now recognize a **polymorphic
  base** (abstract, or concrete-with-concrete-subclasses) and store references
  to it as a plain TEXT id column instead of a foreign key, matching how
  references to an abstract base already behaved. References to a concrete leaf
  class (no subclasses) keep their foreign key unchanged. New
  `SchemaRegistry.is_polymorphic_base()` exposes the classification.

## v0.10.1 — 2026-07-01 (Schema-driven inline value types)

Patch release fixing an ingest data-loss bug: inline value-type objects
(identifier-less LinkML classes used as a slot range) were reified into their
own table with a synthetic FK that ingest never populated, so the value was
dropped silently. Value-type detection is now schema-driven — any
identifier-less, non-tree-root class is stored inline as JSON TEXT, exactly as
the built-in `ExternalReference` already was (issue #90).

### Fixed

- **Inlined value-type objects (identifier-less LinkML classes) no longer
  silently dropped on ingest (issue #90).** Value-type detection is now
  **schema-driven** rather than a hardcoded allowlist of one class
  (`ExternalReference`): any non-tree-root class with no identifier slot is
  treated as an inline value type — stored as one JSON TEXT column on the
  owning entity, never reified into its own table with a synthetic FK. Domain
  value objects such as `Mass`/`Volume`/`Concentration` (identifier-less
  subclasses of an abstract `Quantity`) now round-trip exactly as
  `ExternalReference` does; previously `hippo migrate` gave them their own
  table, `hippo ingest` could not populate the resulting `_id` FK from the
  inline dict, and the value vanished while ingest reported `errors=0`. New
  `SchemaRegistry.value_type_classes()` / `is_value_type()` expose the
  schema-driven set.

## v0.10.0 — 2026-06-30 (Graph-level as-of reconstruction + batch unit-of-work + polymorphic ingest)

This release adds **graph-level / query-spanning as-of reconstruction** (time-travel
queries over the whole subgraph, ADR-0001) and a **batch unit-of-work** (whole-set
validation + atomic multi-entity writes, issue #84), and fixes two ingest data-loss bugs
(multivalued slots #79, polymorphic tree-root collections #80).

> **Behavior change (issue #80).** `hippo ingest` no longer silently downcasts a subtype
> instance to a polymorphic base that declares no `designates_type` discriminator — it now
> raises an actionable error instead of dropping the subtype's fields. Bundles that
> previously "succeeded" by discarding those fields will now error; add a `designates_type`
> slot or ingest under the concrete-subclass accessor (see `docs/polymorphic-ingest.md`).

### Added

- **Graph-level / query-spanning as-of reconstruction (ADR-0001, sec6 §6.8;
  issue #71, increments #73–#77).** Queries can now be evaluated as the graph
  stood at a transaction-time `T`, reconstructed from the append-only
  provenance log (no materialized snapshots; transaction-time only, valid-time
  deferred). `HippoClient.query(as_of=...)` reconstructs the matching entity
  set as of `T` (candidates created `<= T`, each rebuilt via `get_state_at`,
  filters/pagination applied to the reconstructed data) and binds the computed
  temporal fields to `T`; `client.relationships.traverse(..., as_of=T)` walks
  only edges live at `T`, replayed from `relationship_add`/`remove` events
  rather than the relationships table's current flag. Exposed additively on the
  read transports — REST `GET /entities?as_of=<ISO-8601>` and GraphQL
  `asOf: String` on generated list queries (omitted = current state). Backed by
  a new `idx_ProvenanceRecord_type_timestamp` index, and brought to parity on
  both the SQLite and PostgreSQL adapters. As-of resolution of nested
  resolved-relationship fields and a `client.snapshot()` handle remain
  follow-ups (sec6 §6.8.6).

- **Batch unit-of-work: whole-set validation + atomic multi-entity write
  (issue #84, increments #85–#87).** Commit a set of related entities
  all-or-nothing, or dry-run validate the whole set first.
  `HippoClient.validate_batch(operations, *, assign_ids=True)` runs the standard
  per-entity pipeline (LinkML → CEL → Python) over every operation and
  **aggregates** per-entity outcomes (not fail-fast), assigning provisional ids
  on copies and writing nothing (`BatchValidationResult`).
  `HippoClient.batch_put(operations, *, relationships=None, dry_run=False)`
  assigns real ids up front (caller data untouched), validates the whole set,
  and wraps all entity writes — then relationships — in one
  `staged_transaction()` so the group commits or rolls back together
  (`BatchWriteResult`); intra-batch relationship forward references resolve
  naturally because staged reads observe staged writes. Exposed over the
  transports for non-SDK clients: REST `POST /ingest/validate` (always 200 with
  the batch envelope, never writes) and `POST /ingest/batch` (422 on validation
  failure, 200 on dry-run plan or commit); GraphQL `validateBatch` /
  `ingestBatch` root mutations with tier-annotated failure types.

### Fixed

- **Multivalued slots no longer silently dropped on ingest (issue #79 /
  [ADR-0002](design/decisions/ADR-0002-multivalued-reference-slots-as-relationships.md)).**
  `HippoClient.put` — and therefore `hippo ingest` — previously discarded
  any multivalued slot with no error: it was persisted neither inline nor
  as relationships, because multivalued slots get no per-class column and
  the DDL generator filters out LinkML's linktables. Two storage rules now
  close the gap, both materialized inside the entity-write transaction so a
  failure rolls the whole write back: (1) a multivalued slot whose range is
  an **entity class** persists as relationships keyed by the slot name —
  visible to `find_relationships`/`traverse` and as-of edge replay — and is
  hydrated back into `entity["data"][slot]` on `get`/`query`; reference
  targets are not existence-checked, so forward references during bulk
  ingest are preserved, and `put`/`replace`/`update` reconcile edges
  (an omitted slot clears them, mirroring the typed-column NULL semantics);
  (2) a multivalued slot whose range is a **scalar/enum** stores inline as a
  single JSON TEXT column. SQLite backend; PostgreSQL parity is a follow-up.

- **Polymorphic tree-root collections now ingest with subtype dispatch (issue #80 /
  [ADR-0003](design/decisions/ADR-0003-polymorphic-tree-root-ingest.md)).**
  `hippo ingest` previously skipped abstract bases when building the bundle, so a
  collection ranged on an abstract base (e.g. `samples:` → abstract `Sample`) had no
  accessor and the bundle hard-failed validation; and it ignored the `designates_type`
  discriminator, so an instance under a base-class accessor (e.g. `assays:` → `Assay`) was
  silently stored as the base — subclass-specific fields dropped, not queryable as the
  subclass. Now an abstract class that declares a `designates_type` slot gets a base-ranged
  tree-root accessor (plain abstract roots like `Entity` are still excluded), and ingest
  dispatches each instance under a base accessor to the concrete subclass its discriminator
  names, storing it as that subclass so its fields persist and it is queryable as its real
  type. Concrete-subclass accessors keep working alongside base accessors. The synthesized
  `_HippoInstanceBundle` remains the (hidden, table-less) wire contract. To close the
  related silent-loss hole, a polymorphic base that declares *no* `designates_type` slot no
  longer downcasts a subtype instance to the base (dropping its fields) — ingest now raises
  an actionable error naming the dropped fields, the valid subclasses, and the fix
  (add a `designates_type` discriminator, or use the concrete accessor), with a new guide at
  `docs/polymorphic-ingest.md`. This is a behavior change: bundles that previously
  "succeeded" by discarding subtype fields now error.

## v0.9.0 — 2026-06-16 (Autogenerated REST/GraphQL transports + config-driven serving + consumer spanning client)

This release builds the **shared LinkML schema-typing core** (issue #47) and
projects it into two autogenerated transports — **LinkML-derived OpenAPI
components** (issue #46) and a full **GraphQL transport** (issue #45) — makes
`hippo serve` persist through a **config-driven storage/client factory**
(issue #42, with REST get/delete-by-id type resolution, issue #44), introduces
the **`ExternalReference` value type + `hippo_external_xref`** cross-system
identifier model (issue #48, deprecating the `ExternalID` entity), and adds a
**public spanning client** so a consumer package can link its own entities to an
installed reference loader through public APIs alone (issue #67).

### Added

- **`ExternalReference` value type + `hippo_external_xref` annotation
  (issue #48, non-breaking phase).** Cross-system identifiers are now a
  framework value type plus a declarative behavior annotation instead of a
  bespoke entity. `hippo_core` 0.6.0 ships `ExternalReference` (`system`,
  `value`, optional `retrieved_at`/`version`; NOT `is_a: Entity` — no
  id/lifecycle, stored inline as a JSON TEXT column on the slot that
  ranges it, single- and multivalued). `hippo_ext` 0.4.0 declares
  `hippo_external_xref`: annotating an `ExternalReference`-ranged slot
  opts it into reverse lookup via the new `hippo_xref_index` side table
  (`entity_id`, `entity_type`, `slot`, `system`, `value`;
  `UNIQUE(system, value)`), maintained by the SQLite adapter's per-class
  write hooks in the same transaction as the entity write across
  create/update/replace/availability/supersede. Index rows exist only for
  available entities (the PTS-348 "unique among live records" semantics),
  so `(system, value)` is globally unique among live entities; violations
  raise the new `XrefUniquenessError` (a `ValidationError` — REST 422,
  GraphQL `VALIDATION_FAILED`) naming the system, value, and conflicting
  entity, rolling back the write. Reverse-lookup surface:
  `HippoClient.find_by_xref(system, value)` (entity envelope or `None`)
  and `HippoClient.list_xrefs(entity_id)`; REST
  `GET /xref/{system}/{value}`; GraphQL `findByXref(system, value)`
  returning a generic `XrefMatch` envelope. Typing core (additive):
  `SlotKind.STRUCTURED` for value-type-ranged slots,
  `SlotModel.is_external_xref`, and `VALUE_TYPE_CLASSES` —
  `ExternalReference` is excluded from every entity surface (typed
  client, GraphQL, OpenAPI components, TUI, tree-root bundle). The
  PostgreSQL adapter raises `NotImplementedError` on the lookup surface
  pending its per-class-table migration. Docs:
  `docs/data-model.md` (External References),
  `design/reference_hippo_ext.md`, `design/reference_hippo_core.md`.

- **LinkML-derived OpenAPI components (issue #46, approach A).** When the
  REST app is built around a schema-bearing client (as `hippo serve` does),
  `/openapi.json` now carries one JSON Schema component per exposed entity
  type — typed user fields with required/enum markers, reference slots as
  uuid strings naming the target type, multivalued slots as arrays, and
  read-only system (`id`, `is_available`) plus computed temporal fields
  (`created_at`, `updated_at`, `schema_version`, `created_by`,
  `updated_by`) — together with an umbrella `Entity` (`oneOf`) component
  and an `EntityEnvelope` for read responses. The generic entity endpoints
  (PUT replace, GET by id, GET list) reference these components.
  Exposure/classification derives from `hippo.core.schema_typing` (the
  issue #47 contract); rendering lives in the new `hippo.api.openapi`
  module via an `app.openapi` override cached on `app.openapi_schema`.
  Routes and runtime behavior are unchanged; apps without a
  client/registry serve the default document. See
  `docs/typed-openapi.md` for client-codegen usage.
- **Autogenerated GraphQL transport (issue #45).** New optional `graphql` extra
  (strawberry-graphql; folded into `all`) and `hippo/graphql/` package. At
  startup the transport renders the shared LinkML type model
  (`hippo.core.schema_typing`, issue #47) into a full GraphQL schema: per-entity
  object/page/input types, enums, and relationship fields exposing both the raw
  stored UUID (`donorId: ID`) and a resolved field (`donor: Donor`) traversing
  the graph through a per-request DataLoader (one batched query per target type,
  no N+1). Per entity type: by-id and filtered/paginated list queries, per-type
  full-text search (`searchSamples`, mirrors `GET /search`), create / partial
  update / availability / bulk availability (mirrors
  `POST /entities/{type}/bulk-availability`, per-record error isolation) /
  supersede mutations — no delete mutations, matching the no-hard-delete
  lifecycle. Cross-cutting queries: `entityHistory`, `supersededBy(id)`
  (supersession chain, mirrors `GET /entities/{id}/superseded`), and
  `hippoSchema` / `hippoEntityType` (LinkML type-model introspection, mirrors
  `GET /schemas[/{type}/references]`). Resolvers are thin `HippoClient`
  delegations reading `app.state.hippo_client` — the same config-built client
  the REST routers use. SDK errors map to structured GraphQL errors
  (`extensions.code`: `VALIDATION_FAILED` with the tier-tagged envelope,
  `NOT_FOUND`, `ALREADY_SUPERSEDED`, `AVAILABILITY_CHANGE_FAILED`,
  `INTERNAL_ERROR`). Bearer auth on operation execution mirrors REST; GET stays
  open for GraphiQL. Queries are depth-limited (default 10, configurable).
  `hippo serve --graphql [--graphql-max-depth N]` mounts the transport at
  `/graphql`. Known limitations (tracked in issue #45): offset pagination only;
  no schema-versioning of the generated surface. User docs: `docs/graphql.md`;
  design: sec4 §4.7.
- `hippo.core.schema_typing.SlotModel` gained `has_default` (LinkML `ifabsent`
  present), so transports can derive nullability without re-walking the schema.
- **Public spanning client for consumer schema + installed reference loaders
  (issue #67).** A consumer package can now obtain a client/registry that spans
  **its own schema + the reference loaders it declares in `requires:`** using
  only public APIs — no hand-assembled registry code. New top-level
  `hippo.client_for_schema(schema_path, database_url=...)` and
  `hippo.registry_for_schema(schema_path)` resolve the schema's `requires:`
  block, merge the installed loaders' fragments, and return a ready
  client/registry; the same merge is now wired into `create_client` (default
  on), so the CLI, `hippo serve`, and the TUI span loaders automatically. A
  declared-but-uninstalled loader fails fast at construction, mirroring
  `hippo validate`. `hippo migrate` / `hippo schema migrate` now apply the
  `requires:` gate before any DDL (the documented behavior), and a consumer
  slot ranged on a loader class — either the bare class name or the documented
  loader-prefixed CURIE form `range: ensembl:Gene` — is now a recognized,
  validated cross-loader reference rather than an opaque value (loader-to-loader
  references stay advisory per D2.14.H). Schema-package discovery and `requires:`
  resolution were relocated from the CLI layer into the new core module
  `hippo.core.loaders.discovery` (re-exported from `hippo.cli.commands.reference`
  for back-compat). Docs: `docs/reference-loaders.md`.

### Deprecated

- **The `ExternalID` entity pattern (issue #48).** The `ExternalID` class
  stays in `hippo_core` and remains fully functional, but is deprecated in
  favor of `ExternalReference` + `hippo_external_xref`.
  `HippoClient.register_external_id`, `get_by_external_id`,
  `list_external_ids`, and the mapping-level `supersede` now emit
  `DeprecationWarning` (entity-level `supersede_entity` is unaffected);
  the `/external-ids` REST endpoints are marked `deprecated` in OpenAPI;
  the `external_id_add`/`external_id_remove` provenance operations are
  documented as deprecated (`ExternalReference` changes are ordinary
  `update` operations). Data migration tooling and removal of the entity
  are scoped to a future major release.

### Fixed

- **REST get/delete-by-id resolve the entity's real type (issue #44).**
  `GET /entities/{id}` and `DELETE /entities/{id}` passed a hardcoded
  placeholder `entity_type="entity"` to the SDK; against relational storage
  that type does not exist, so get-by-id 404'd for entities that exist and
  delete skipped its existence check. Both routes now resolve the type from
  the id via `HippoClient.resolve_type` (storage-adapter lookup) and 404
  cleanly on unknown ids. The list endpoint's no-type path
  (`GET /entities` without `entity_type`) now queries across all types:
  `HippoClient.query` accepts `entity_type=None` (cross-class scan in the
  SQLite adapter; type predicate dropped in Postgres) instead of the same
  broken placeholder.
- **`hippo serve` now persists (issue #42).** The serve command built the
  FastAPI app with no `HippoClient`, so every request fell back to a no-arg
  client with `storage=None` — a non-persistent echo stub that ignored config
  and the schema. `serve` now builds a configured client from the resolved
  config (new `--config` flag; auto-detects `config.json` / `hippo.yaml` in the
  cwd, else a default SQLite database) and injects it, so REST serves the same
  deployment the SDK and TUI do.

### Changed

- **Config-driven storage/client construction (issue #42).** New
  `hippo.core.factory` is the single construction path shared by the CLI, the
  TUI SDK backend, and `hippo serve`. It resolves the storage backend through
  the `hippo.storage_adapters` entry-point group (`storage_backend` in config,
  default `sqlite`) and assembles a `HippoClient` (registry + adapter +
  validation pipeline). The TUI now honours a deployment's configured
  `storage_backend` rather than assuming SQLite.
- `HippoClient.storage` is now typed against the `EntityStore` ABC instead of
  the concrete `SQLiteAdapter`, so the SDK depends on the storage interface.
- Corrected the stale `hippo.storage_adapters` `sqlite` entry point, which
  pointed at a non-existent module (`hippo.adapters.sqlite.store`); it now
  resolves to `hippo.core.storage.adapters.sqlite_adapter:SQLiteAdapter`.

## v0.8.0 — 2026-06-03 (SchemaPackage extension model + Recipe system v1)

This release introduces the **SchemaPackage** genus/species extension model and
its full migration + bundle-orchestration machinery (Doc 2 §2A / `sec11`), the
**Recipe v1** subsystem for sharing schema fragments between deployments
(`sec10`), and a documentation pass repositioning Hippo as a LinkML *runtime
engine*. No hard breaking changes: the `ReferenceLoader` ABC is re-parented
under `SchemaPackage` with method names intact and a deprecated alias for the
one rename.

### New Features — SchemaPackage (Doc 2 §2A / sec11)

- **`SchemaPackage` genus ABC + back-compat re-parenting (PTS-335).** Abstracts
  "contribute a versioned, pinnable schema fragment" into a base genus with
  abstract `versions()`/`schema_fragment()`, concrete `depends_on()`/`validate()`,
  and three no-op-default lifecycle hooks (`provision`/`evolve`/`deprovision`)
  so a pure-schema package needs no hand-written code. Capability protocols
  `ExternalData` and `MigratableData` keep external-data and migratable
  packages distinct. `ReferenceLoader(SchemaPackage)` is re-parented with method
  names intact (`provision → load()`, `evolve → upgrade()`); `entity_types()` is
  renamed to `populates_types()` with `entity_types()` kept as a **deprecated
  alias**. New `hippo.schema_packages` entry-point group (`hippo.reference_loaders`
  is a subset/alias); `discover_schema_packages()` resolves both and dedups by
  name. Existing loaders install unchanged.
- **`DomainModule` species + single-hop `evolve()` with staged gate (PTS-338).**
  First-party mutable-data species satisfying `MigratableData` via
  `migration_steps()` + `evolve()`. A `MigrationStep` is a `(from→to)` DAG edge
  whose transform stages new-shape records onto a `MigrationPlan`; `evolve()`
  runs a staged dry-run validation gate before any committed write (commit only
  on green, else `MigrationGateError` with the DB untouched) and emits supersede
  provenance + `superseded_by` lineage. Adds the read-only `HippoClient.registry`
  property and `MigrationStepNotFoundError`.
- **Real reference-data species + diff-based evolve (PTS-337).** `OboDemoLoader`
  exercises the full external-data path end to end: content-addressed,
  sha256-pinned `cached_fetch` (hermetic via a bundled `file://` manifest), a
  diff-based `upgrade()` that re-reads the cached prior release plus a JSON
  delta, and a `test` pseudo-version reading a bundled `.obo` fixture for
  network-free CI.
- **Multi-hop migration-chain resolver + `deprovision` (PTS-339).** `evolve()`
  BFS-resolves the fewest-hop path through the declared migration DAG (a
  shortcut edge wins automatically); below-floor raises `MigrationFloorError`,
  unreachable target raises `MigrationStepNotFoundError`. `deprovision` refuses
  by default when it owns live domain data (`DeprovisionRefusedError`), with
  `--force` soft-deleting via the availability transition; new
  `hippo reference deprovision` CLI with a dependents guard.
- **S4 dependency-ordered bundle orchestrator + commit-or-rollback gate
  (PTS-340).** `SQLiteAdapter.staged_transaction()` / `HippoClient.staged_transaction()`
  give a re-entrant scope so a whole multi-package / multi-hop migration commits
  or rolls back as one unit. The orchestrator adds `topological_sort()` (Kahn,
  cycle → `OrchestrationError`), `migrate_to_bundle()` driving a deployment
  through a target `Bundle`'s coordinate sequence in dependency order, and
  `run_end_to_end_gate()` validating the full post-migration state (staged base
  writes + existing extension rows) so a stranded extension field forces a
  rollback. Bundle meta-coordinate manifest per sec11 §11.6.2.
- **`hippo reference bundle-requires <manifest>` (PTS-346).** Surfaces the
  previously-unwired `Bundle.to_requires()` to a user-facing command:
  generates a deployment's `requires:` block from a bundle manifest's pinned
  packages (each an exact `==` pin) as paste-ready YAML, with a leading
  comment naming the bundle and its version. The block is *generated, not
  hand-maintained* (sec11 §11.6.2), so pins always match a known-coherent
  combination.
- **End-to-end proof — brain bank extension integration test (PTS-341).** A
  19-test suite proves a brain bank `DomainModule` extension upgrades across a
  base major through the full S2–S4 machinery: two-hop BFS chain
  (`v1.0→v1.1→v2.0`), dependency-ordered orchestration with bundle intermediate
  coordinates, the end-to-end gate blocking a stranded field, and per-class
  table type-exactness (`query("Subject")` never sweeps `BrainDonor` rows).

### New Features — Recipe system v1 (sec10)

- **Declarative recipe bundles + `hippo recipe` CLI (PTS-289, PTS-293).** A
  *recipe* packages a LinkML schema fragment for sharing between Hippo
  deployments: a reverse-DNS stable `id`, sha256 content digest, atomic install,
  lockfile reproducibility, and a PEP 440 `hippo_version` compatibility gate
  (`RecipeVersionIncompatibleError`). New `hippo recipe`
  `inspect`/`import`/`export`/`extend`/`diff`/`export-lockfile`/`install-from-lockfile`
  commands. Spec in `design/sec10_recipes.md`; author/install/reference guides in
  `docs/`.

### Documentation & Positioning

- **Repositioned as a LinkML runtime engine (PTS-268, PTS-269, PTS-270).**
  README/`pyproject`, `introduction.md`, CLI help, and REST OpenAPI descriptions
  reframed runtime-first.
- **New onboarding docs (PTS-271–PTS-274).** Comparison guide,
  `design-principles.md` (six architectural commitments), `why-hippo.md` (three
  moats), and a bibliography citation-graph worked example.
- **Bibliography is now the default `hippo init` template (PTS-275, PTS-276).**
  Plus a bibliography quickstart.
- **Omics preface + `examples/omics/` reference app (PTS-277).** Shipped fixtures
  also neutralized of biomedical-specific defaults (PTS-278).
- Removed a stale "metadata tracking service" paragraph from `introduction.md`
  (PTS-261).

### Bug Fixes / Hardening

- **`hippo migrate` repaired against the LinkML-backed DDLGenerator (PTS-281).**
- **`recipe_*` method names added to `SDK_RESERVED_NAMES` (PTS-307).** User
  schemas can no longer define `recipe_*` members that would collide with the
  generated recipe SDK surface.
- **`hippo_unique` index made partial (`WHERE is_available=1`); migration
  predecessors retired before writing replacements (PTS-348).**
- **SQLite `0`/`1` booleans reversed to `bool` on read (PTS-349).**
- **S4 pre-merge review follow-ups (PTS-346).** `Bundle.from_manifest()` now
  validates that optional `version`/`ontology_snapshot` fields are strings when
  present (a YAML `version: 1.0` previously parsed to a float silently);
  `extension_referenced_elements()` documents its `isupper()` CamelCase
  assumption and collects a non-standard fragment-level `slots` list; and
  `SQLiteAdapter._transaction`/`staged_transaction` route rollback through
  `_safe_rollback()` so a failing rollback can't mask the in-flight exception.

### CI / Internal

- Quickstart end-to-end smoke workflow (PTS-279); restored GitHub Pages docs
  site (PTS-294); explicit read-only permissions on `tests.yml` (PTS-299);
  merged-closure integrity check job (PTS-345); repaired CI test regressions
  after the repo split (PTS-282); Dependabot bumps — idna, urllib3, pytest,
  pygments (PTS-284); added a top-level `.gitignore`.

Closes PTS-351.

---

## v0.7.0 — 2026-05-25 (ReferenceLoader v2 — write-log substrate + EntityRef)

### Breaking Changes

- **`LoadResult` v2 shape (PTS-255).** Dropped `entity_type` and `entity_ids` fields. New: `entities: list[EntityRef]`. Loader authors populate via `EntityRef.from_put_result(client.put(...))` for small loaders (Pattern A), or leave empty for large loaders (Pattern B) — the write log is the authoritative `--prune-old` substrate. Clean break per D2.14.L; no deprecation window.

- **`hippo_meta.reference_entity_ids` JSON blob retired (PTS-256).** Replaced by `reference_write_log` table (PTS-253). One-time backfill migrates existing rows on adapter init.

### New Features — ReferenceLoader v2 (§2.14.8/.9)

- **`EntityRef(id, type)` frozen dataclass (PTS-255).** Public, in `hippo.core.loaders.reference`. `EntityRef.from_put_result(rec: dict) -> EntityRef` classmethod bridges the v1 `client.put()` dict return.

- **`reference_write_log` table on SQLite + Postgres (PTS-253).** Composite PK `(loader_name, version, entity_id)`, lookup index on `(loader_name, version)`. Records every entity write that occurred inside a `load_context` block. Authoritative substrate for `--prune-old` per D2.14.J.

- **`HippoClient.load_context(loader_name, version)` context manager (PTS-254).** Used by the lifecycle layer to scope reference-loader puts; write-log INSERT shares the entity write's SQL transaction so orphan log rows never persist beyond committed entity writes. State on the `HippoClient` instance per D2.14.J (no ContextVar); nested entries raise `RuntimeError`.

- **Lifecycle wiring + write-log prune (PTS-256).** `hippo reference install`/`upgrade` wrap `loader.load()`/`loader.upgrade()` in `client.load_context(...)`. `--prune-old` queries the write log scoped to `(loader_name, version)`, deletes per-entity-type rows in a single transaction, then clears the log rows. Loader authors do nothing for prune.

- **Per-`entity_type` breakdown in CLI (PTS-257).** `hippo reference install`/`upgrade` print a per-class breakdown table (rows sorted by `count desc, type asc`, scalar `total` line preserved for v1 parity). Counts derive from `LoadResult.entities` when populated, else from `GROUP BY entity_type` over `reference_write_log`.

### Documentation

- **§B.8 Appendix B — ReferenceLoader v2 Authoring Patterns (PTS-258).** Pattern A (small) + Pattern B (large) with code samples; `LoadResult.entities` advisory-only doctrine; v1 → v2 migration paragraph; lifecycle + write-log diagnostics.
- **§2.14.8/.9 sec2_architecture (PTS-243).** Authoritative v2 contract spec.
- **D2.14.J/K/L sec9_decisions (PTS-243).** Decision log for write-log substrate, EntityRef-on-LoadResult-only (defer `put()` bundle), clean-break transition.

Closes PTS-252.

---

## v0.6.0 — 2026-05-21 (LinkML-native Phase 2/3 + §2.14 Reference Loaders)

### Breaking Changes

- **`EntityYAMLLoader` retired (PTS-178).** The legacy `entities: [{type, data, external_id}]` wrapper format is no longer accepted by `hippo ingest`. Instance files must now use the **tree-root bundle** format: a YAML mapping whose top-level keys are pluralized class names (`samples:`, `projects:`, …) and whose values are lists of instance dicts. The `ingest_entity_file` function is replaced by `ingest_linkml_yaml`.

- **`hippo validate --schema` now performs real LinkML validation (PTS-177).** The previous implementation only checked that the file was a YAML dict. Schemas that pass structural checks but are not valid LinkML will now exit non-zero. Closes GitHub Issue #1.

- **Legacy `entities` blob table and `entity_external_ids` side-table dropped (PTS-174).** Every entity now lives in its own per-class typed table. Any code reading these tables via raw SQL must switch to the per-class tables (or use the SDK). `_run_migrations` idempotently drops the legacy tables on next adapter init for dev databases still carrying them.

- **`EntityStore` protocol tightened (PTS-170).** `EntityStore(ABC, Generic[T])` is now `EntityStore(ABC)` with `dict[str, Any]` entity records and a required `schema_registry: SchemaRegistry` constructor parameter. External adapter implementations must adopt the new signature.

### New Features — Reference Loaders (§2.14)

- **`ReferenceLoader` ABC + `LoadResult` (PTS-224).** Reference loaders are discovered via the `hippo.reference_loaders` entry-point group and implement a uniform contract: `versions()`, `entity_types()`, `schema_fragment()`, `load()`, `upgrade()`, `validate()`. Non-subclass entries fail loud at discovery time. See `docs/writing-a-reference-loader.md` for the full author guide.

- **`requires:` schema directive (PTS-227).** User schemas may pin a reference loader at an exact version (`requires: { loader_name: "==1.2.3" }`). `SchemaRegistry` enforces the pin against installed `hippo_meta.reference_versions` at load time and refuses to merge on mismatch. `_HIPPO_TOP_LEVEL_KEYS` is extended to strip this directive before constructing `SchemaView`.

- **Schema fragment merge engine (PTS-226).** Loaders contribute additive LinkML fragments (`schema_fragment()`) that are merged into the working schema with explicit precedence: user schema > reference loaders > `hippo_core`/`hippo_ext`. `loader_depends_on` declarations surface as warnings when the dependency graph is inconsistent.

- **`hippo reference install / upgrade / list / clean-cache` CLI + `hippo_meta.reference_versions` tracking (PTS-229).** Per-loader install/upgrade lifecycle persists the active version under `hippo_meta` so user-schema `requires:` pins can be evaluated. The `test` slug is reserved for fixture loaders shipped with the `hippo.testing` module.

- **`HippoClient.cache_dir_for(loader_name)` + `cached_fetch(url, *, expected_sha256=None, loader_name)` (PTS-225).** Loaders converge on a single Hippo-managed, content-addressable HTTP cache rooted at `$HIPPO_CACHE_DIR` (default `~/.cache/hippo/references/`). Downloads land in a sibling `.part` and are atomically renamed; sha256 mismatch raises `CacheIntegrityError` and removes the corrupt file. `hippo reference clean-cache [<name>]` scopes deletion to a single loader or wipes the whole root.

- **Pydantic `load_params_schema` rendered as CLI flags (PTS-230).** Loaders that declare a `load_params_schema` (Pydantic v2 `BaseModel`) get their fields auto-rendered as `--<field-name>` flags on `hippo reference install` and `upgrade`. Supported types: `str`, `int`, `bool`, `list[str]`, and `Optional[...]` of any of these. Field defaults are preserved; `list[str]` defaults are replaced (not extended) the first time the user passes a value. Exotic types fail loud at registration time with `ReferenceLoaderRegistrationError`.

- **Loader-provided Typer sub-apps mounted under `hippo reference <name> ...` (PTS-228).** Loaders may register a `typer.Typer` instance via the `hippo.reference_loader_cli` entry-point group; Hippo enumerates that group at CLI startup and mounts each sub-app, sharing the same `cache_dir_for(<loader>)` resolution as `load()`. Sub-app registration is optional and orthogonal to the parent `install/upgrade/list` verbs.

- **`CacheIntegrityError`** — new exception class in `core/exceptions.py` (PTS-225).

### New Features — LinkML-native Storage (Phase 2/3)

- **Per-class typed tables (PTS-168/172/173/174).** Domain entities are stored in dedicated `<ClassName>` tables generated by `LinkML SQLTableGenerator`. `ExternalID` is now a first-class LinkML class (declared in `hippo_core.yaml`, PTS-168) written through the normal entity machinery (PTS-173). The legacy generic `entities` blob table and hand-coded `external_ids` table have been dropped (PTS-174).

- **`_entity_registry` shadow table (PTS-175).** `SQLiteAdapter` now maintains a `_entity_registry(uuid PRIMARY KEY, class_name TEXT NOT NULL)` table updated on every write. This enables `client.read(uuid)` without the caller knowing the target class. Pre-PR databases are idempotently backfilled from `ProvenanceRecord` create events on next adapter init.

- **`_HippoInstanceBundle` tree-root synthesis (PTS-176).** `SchemaRegistry` synthesizes a tree-root class at construction time — one multivalued slot per concrete class in the schema, named by `snake_case(ClassName) + "s"` (overridable via `hippo_accessor` annotation). This class is held off the `SchemaView` so DDL/diff/typed-client machinery is unaware of it. `Process` declares `hippo_accessor: processes` to override the naive `+ "s"` pluralizer.

- **DDL generation delegated to LinkML `SQLTableGenerator` (PTS-169).** `DDLGenerator.generate()` is a thin wrapper around the upstream generator with Hippo-specific post-processing: `superseded_by TEXT`, `ifabsent` defaults, SQLite-compatible `BOOLEAN→INTEGER`, partial indexes for `hippo_index_partial` slots, unique indexes for `hippo_unique`, and `BEFORE UPDATE/DELETE` triggers for `hippo_append_only` classes.

### New Features — Ingest & Validate

- **`hippo validate --data PATH` (PTS-177).** Validates a tree-root instance YAML bundle against a LinkML schema. Requires `--schema`. Field-level errors include path information (e.g. `/samples/0/name`). Closes GitHub Issue #1.

- **`hippo ingest --validate-schema PATH` (PTS-178).** Pre-validates the instance bundle against the given schema before any writes. Identical validation logic to `hippo validate --schema --data`, making dry-run and live-ingest consistent.

### Documentation

- **§2.14 ReferenceLoader contract landed in `sec2_architecture.md` (PTS-215).** Full ABC signature, two entry-point groups, and seven sub-sections covering version semantics, load params, caching contract, upgrade semantics, schema-fragment merge rules, cross-loader FKs, and test fixtures. Decisions D2.14.A–I appended to `sec9_decisions.md`.
- **`docs/reference-loaders.md` user guide and `docs/writing-a-reference-loader.md` author guide (PTS-231).** Covers `list/install/upgrade/clean-cache`, `$HIPPO_CACHE_DIR`, `requires: "=="` pinning, the reserved `test` slug, `hippo_meta.reference_versions`, the ABC walkthrough, `load_params_schema → CLI flags`, optional Typer sub-app, the "test" fixture pattern, the `cached_fetch` contract, `loader_depends_on`, and a ship checklist.
- **Soft-delete language corrected in `reference-loaders.md` and `data-model.md` (PTS-238).** `--prune-old` deactivates rows (`is_available = false` + provenance record) rather than hard-deleting; the prior "destructive / remove rows" copy was misleading. Soft-delete invariant confirmed in both the SQLite and Postgres adapters.
- **`README.md` Validation Pipeline section rewritten** with a real LinkML schema example, tree-root instance YAML example, and CLI examples for `hippo validate` and `hippo ingest` (PTS-179).
- **`docs/cli-reference.md` updated**: `validate` adds `--data` option; `ingest` adds `--validate-schema` option (PTS-179).

---

## v0.5.0 — 2026-04-27

### Breaking Changes

- **`created_at` and `updated_at` dropped from entity tables (PTS-69).**
  These columns no longer exist in the storage layer. Both fields are now computed
  exclusively from the provenance log at read time and returned by `client.get()` and
  `client.query()`. Run `hippo migrate` to drop the legacy columns from an existing
  database; no data loss occurs because the provenance log is the authoritative source.

- **`ValidationFailed` raised on write (PTS-70).**
  Entities that fail schema validation now raise `ValidationFailed` at write time
  (previously, failures were silently logged). Callers that write entities without catching
  this exception will see it surface. This is intentional: silent validation bypass was a
  data-integrity hazard.

### New Features

- **`hippo.models.<namespace>` direct-import surface (PTS-71).**
  Pydantic model classes generated from a namespace's LinkML schema are now importable as
  `from hippo.models.<namespace> import <ClassName>`. The namespace module is generated
  lazily on first access and cached.

- **Pydantic typed-client surface.**
  `HippoClient` now exposes namespace-aware typed accessors backed by generated Pydantic
  models. `client.<namespace>.get(id)` returns a typed instance; `client.<namespace>.query()`
  returns `PaginatedResult[<Model>]`. The untyped `client.get()` / `client.query()` paths
  remain unchanged.

- **Computed temporal fields.**
  `created_at` and `updated_at` are derived from the provenance log at read time via
  `entity_provenance_summary` (a view created by `hippo migrate`). The values are consistent
  with the provenance record rather than the entity table, which was the authoritative source
  all along.

- **Reference-loader shape (PTS-74).**
  Reference loaders now produce well-typed fragments with explicit merge semantics: each
  fragment declares its `entity_type`, and the loader merges overlapping fields according to
  the schema rather than by positional order. The `ReferenceLoader` ABC and
  `ConfigurableReferenceLoader` base class are the new extension points.

- **actor_id ContextVar sentinel (PTS-68).**
  Actor identity is now threaded through nested SDK calls automatically via a `ContextVar`.
  The `UUID4_SENTINEL` value is resolved to the caller's actual UUID at write time;
  callers no longer need to pass `actor=...` explicitly on every inner call.

- **LinkML-native SchemaRegistry.**
  `SchemaRegistry` loads and merges `hippo_core.yaml` (system-level schema: `id`,
  `is_available`, `superseded_by`, `actor_id`) and `hippo_ext.yaml` (annotation
  vocabulary: `hippo_unique`, `hippo_immutable`, `hippo_computed`) into a three-layer
  merge with user schemas. `SchemaRegistry.validate_hippo_annotations()` hard-fails on
  unknown `hippo_*` annotations.

- **Provenance migration: ProvenanceRecord model.**
  `ProvenanceStore` has been replaced by a `ProvenanceRecord` dataclass. A DDL
  verification helper ensures the provenance table schema is current before writes;
  a write-guard helper prevents in-flight provenance events from racing with schema
  migration.

- **Unified validation envelope + REST surface.**
  The validation result envelope (`ValidationEnvelope`) now unifies SDK-level and
  REST-level validation feedback. The FastAPI transport layer uses the same envelope
  for 422 responses.

### Bug Fixes

- **actor_id UUID sentinel drift (PTS-68).** Under nested calls, the sentinel UUID was
  occasionally propagated to storage instead of being resolved to the caller's identity.
  The ContextVar implementation fixes this deterministically.

- **Hard-fail on Pydantic generation failure.** A prior revision allowed silent pass-through
  when model generation failed (e.g. due to a malformed schema). This is now a hard error
  surfaced immediately at schema-load time.

### Documentation

- **`docs/reference_typed_client.md`** — user guide for the Pydantic typed-client surface (PTS-72).
- **Sec6 provenance spec revision (PTS-73)** — light-touch pass to align the provenance spec
  with the finalized `ProvenanceRecord` model and `entity_provenance_summary` view.

## v0.4.0 — 2026-04-14

### Breaking Changes

- **`client.query()` return type changed from `list[dict]` to `PaginatedResult`.**
  Callers that iterate the return value directly (`for e in client.query(...)`) must update
  to iterate via `.items` (`for e in client.query(...).items`). The `PaginatedResult` object
  exposes `items: list[dict]`, `total: int`, `limit: int`, and `offset: int`. `total`
  reflects the count of all matching entities before `limit`/`offset` are applied — callers
  can use this to implement pagination UI without a separate count query.

- **Hippo DSL removed.** All schema authoring is now exclusively LinkML. The `compile-schema`
  CLI command, `HippoDSLLoader`, `schema_compiler.py`, and all DSL-related APIs have been
  deleted. The entity YAML wrapper format (`entities: [{type, data, external_id}]`) was
  preserved at this point under `EntityYAMLLoader`. **Note:** `EntityYAMLLoader` and the
  wrapper format were subsequently retired in v0.6.0 (PTS-178); use the tree-root bundle
  format instead.

### New Features

- **PostgreSQL storage adapter.** Hippo now supports PostgreSQL as a storage backend in
  addition to SQLite. Configure via `hippo.yaml` adapter settings.

- **v0.5 enhancements:** PUT endpoint for entity upsert, bulk operations API, OR filters
  in query expressions, and schema migration tooling.

- **`schema_references()` API + GA4GH DRS server.** Query schema-level relationship
  metadata and serve data objects via the GA4GH Data Repository Service standard.

- **Unified ingestion framework.** `EntityLoader` ABC with `ConfigurableLoader` base class
  and built-in loaders: `CSVLoader`, `JSONLoader`, `SQLLoader`. `IngestPipeline` orchestrates
  fetch → transform → validate → write for any loader. (`EntityYAMLLoader` was also introduced
  here but retired in v0.6.0 — see PTS-178.)

- **`client.supersede_entity(entity_id, replacement_id, reason=None, actor=None)`**
  Atomically marks an entity as superseded by a replacement:
  - Sets `is_available = false` on the source entity.
  - Sets `superseded_by = replacement_id` on the source entity (new system column).
  - Writes an `EntitySuperseded` provenance event on the source entity.
  - Writes a `superseded_by` relationship edge from source to replacement.
  - Writes an `EntityUpdated` provenance event on the replacement entity.
  All five writes are transactional — partial failure rolls back entirely.
  Raises `EntityAlreadySupersededError` if the source entity is already superseded.
  Raises `EntityNotFoundError` if either entity does not exist.

- **`EntityAlreadySupersededError`** — new exception class in `core/exceptions.py`.

- **`superseded_by` system field on all entities.**
  `client.get()` now returns `superseded_by` in the entity dict. `hippo migrate` adds this
  column to all entity tables via `ALTER TABLE ... ADD COLUMN superseded_by TEXT`.

- **`entity_provenance_summary` view created by `hippo migrate`.**
  Provides efficient batch derivation of `created_at` and `updated_at` for `client.query()`.

### Refactor

- **Decomposed `HippoClient` into domain facades** for better separation of concerns.
- **Removed Hippo DSL compiler and compile-schema CLI command.** Schemas are authored
  directly in LinkML — no intermediate compilation step.

### Behaviour Changes

- **`client.get()` and `client.query()` now return provenance-derived `created_at` and
  `updated_at`** (authoritative source per spec). Entity table columns are treated as a
  write-through cache; reads derive values from the provenance log.

- **`client.get()` now returns superseded entities** with `superseded_by` populated.

- **`client.history()` now works on superseded entities.**

### Bug Fixes

- Fix ingest idempotency — catch `EntityNotFoundError` explicitly in `_upsert_records`.
- Resolve CI test failures across Canon, Hippo, and Cappella test suites.

### Documentation

- Rewrite all schema examples to valid LinkML format.
- Remove legacy Hippo DSL references from all specs and docs.
- Consolidate `user-docs/` into `docs/` across all components.
- Add Schema Writer's Guide and auth integration spec (sec8).
- Spec Phase 1 REST API gaps in sec4 (v0.5 targets).

## v0.3.1 — 2026-03-25

### Bug Fixes

- **`client.get()` now raises `EntityNotFoundError` for deleted and superseded entities by default.**
  Previously, `get()` silently returned unavailable entities via a `read_any()` fallback, inconsistent
  with `query()` which already excludes them. Added `include_unavailable=False` parameter — set to `True`
  for audit/provenance queries that need to read deleted or superseded entities.

- **`client.update()` now raises `EntityNotFoundError` for nonexistent, deleted, or superseded entities.**
  Previously, `update()` on a nonexistent entity ID silently upserted (created a new entity), which
  was a data integrity bug. Now verifies the entity exists and is available before proceeding.

### Tests

- Added `tests/core/test_client_availability.py` — 16 unit tests covering availability filtering
  and update existence checks, developed via TDD (red → green → refactor).
- Platform-level integration test suite added at monorepo root (`tests/platform/`, `tests/contracts/`):
  937 total tests passing across all three tiers (unit, contract, platform).
