# sec9 Autonomous Drafting — Decisions for Review

The user requested that sec9 (sections 9.7–9.13) be drafted without real-time feedback. This file captures every opinionated call made during that drafting pass. Each entry describes the decision point, the alternatives considered, the choice made, the reasoning, and how to revert if the call is wrong.

Review this file before sec9 is considered approved. If any decision is unwelcome, the rollback column indicates the blast radius.

---

## 9.7 Computed Temporal Fields

### Decision 9.7.A — Computation strategy: on-the-fly aggregation, no materialization by default [CONFIRMED 2026-04-18]

- **Alternatives:** (1) read-time aggregation only; (2) materialized view maintained by adapter; (3) hybrid (aggregation with opt-in materialization per-adapter).
- **Chosen:** (1) read-time aggregation; stateless. Optimization strategies are deferred to future work / add-ons.
- **Why:** Keeps SDK contract uniform across adapters; no adapter is forced to implement a materialization strategy; performance is acceptable at Hippo's scale given the required indexes.
- **Revert:** Can move to (2) or (3) adapter-by-adapter without changing SDK surface.

### Decision 9.7.B — Fields computed: `created_at`, `updated_at`, `schema_version`, plus `created_by` and `updated_by`

- **Alternatives:** Minimal set (timestamps + version only); full set including `_by` actor references.
- **Chosen:** Full set. `created_by` and `updated_by` pull from the corresponding provenance record's `actor_id`.
- **Why:** Actor attribution is a near-universal audit query; computing it from provenance is cheap in the same aggregation; adding the fields now avoids a future additive-but-visible change.
- **Revert:** Drop the two `_by` fields; their absence is additive-compatible (they'd be absent from reads).

### Decision 9.7.C — Always included on entity reads

- **Alternatives:** Opt-in via a `with_temporal=True` flag; always included.
- **Chosen:** Always included.
- **Why:** Absent temporal fields would surprise agent implementers; the aggregation is bounded (2 point-in-time lookups per entity with the declared index); conditional inclusion complicates the SDK contract with no compelling use case.
- **Revert:** Move to opt-in by adding a flag to the reader; low blast radius.

### Decision 9.7.D — Degenerate case (no provenance records): raise loudly, refuse to return the entity [REVISED 2026-04-18]

- **Alternatives:** Raise an integrity error; return null; materialize synthetic provenance.
- **Original choice:** Return null (soft degradation).
- **Revised choice:** Raise `ProvenanceIntegrityError`; refuse to return the entity. Writes that cannot emit a valid provenance record fail atomically — the entity change rolls back.
- **Why (revised):** Provenance integrity is the audit guarantee the system exists to provide. Silent degradation on a provenance inconsistency would compromise every audit, compliance, and debugging use case. Transactional write atomicity prevents the inconsistent state from arising under normal operation; loud read-time failure surfaces any that do arise (from bugs or corrupted adapters) immediately instead of masking them. Sec9 adds `Provenance integrity is transactional and loud` as a first-class principle in 9.2.
- **Revert:** Change to null-return in 9.7's "Degenerate cases" paragraph; drop the 9.2 principle; weaken 9.6's atomicity clause. High blast radius — this is a governing invariant.

### Decision 9.7.E — `schema_version` derivation from `SchemaView.schema.version`, `"unversioned"` fallback [NEW 2026-04-21]

- **Finding (computed-temporal-fields implementation):** Sec9 §9.6 says the SDK captures `schema_version` from the merged `SchemaView` at write time. In a three-layer stack (`mosaic_ext`, `mosaic_core`, user schema), "the merged schema's version" isn't a single unambiguous string — each YAML file has its own `version:` field.
- **Chosen:** Read `registry.schema_view.schema.version` (the user schema's top-level `version:` field, after LinkML's merge resolves it). Fall back to the string literal `"unversioned"` when the user schema doesn't set one.
- **Alternatives considered:** (A) Composite string like `mosaic_core-0.3.0+mosaic_ext-0.2.0+user-1.2.3` — human-unfriendly, changes when any layer bumps, hard to filter queries on. (B) Content hash of the merged schema YAML — precise but opaque; breaks after whitespace changes; not useful for "what version was this written under" questions. (C) Require callers to supply a version string at write time — violates sec9's "caller cannot supply it."
- **Why (A)/(B)/(C) rejected:** All three trade simplicity for marginal precision. The user-schema-version interpretation matches how deployments already talk about versions ("we're on user-schema 1.2.3"); Hippo-internal version bumps are a separate, less-important dimension.
- **Plumbing:** `MosaicClient.__init__` reads `registry.schema_view.schema.version or "unversioned"` when both a registry and storage are supplied; sets the adapter's `_schema_version`. Adapters constructed without a `MosaicClient` (direct `SQLiteAdapter(db_path)`) start with `""` — the legacy transition state from Decision 9.6.F.
- **Revert:** Change the derivation in `MosaicClient.__init__` (single line). No data migration needed — new rows would just carry a different string.

### Decision 9.7.F — Loud failure in `query()` mirrors `get()`: a single orphan poisons the whole page [NEW 2026-04-21]

- **Finding:** `MosaicClient.get(entity_id)` raises `ProvenanceIntegrityError` on missing provenance per Decision 9.7.D. `MosaicClient.query(entity_type)` returns a list; the natural implementation could either raise on the first inconsistency or skip the offending row and return the rest.
- **Alternatives considered:** (A) Raise — whole page fails if any entity is orphaned. (B) Skip — silently exclude orphaned entities from the result set, log a warning. (C) Include with null fields — the original pre-9.7.D soft-degradation behavior applied per-row.
- **Chosen:** (A). Mirrors `get()`. Per sec9 §9.2 *Provenance integrity is transactional and loud*, a corrupt audit log is a critical defect, not a row-level issue to absorb. A `query()` caller seeing 999 out of 1000 results silently is worse than seeing the exception and investigating.
- **Why (B) rejected:** "Silently return fewer rows than the user asked for" is indistinguishable from a legitimate empty result — caller can't tell why the row is missing. A tool that pages through results and skips corrupt rows would silently lose data.
- **Why (C) rejected:** Violates sec9 §9.2 explicitly. Already the pre-9.7.D behavior that was revised.
- **Operational note:** In a deployment that encounters integrity errors at read time, the fix is always *repair the adapter*, not *make the reader more permissive*. This decision makes that routing unambiguous.
- **Revert:** Switch to (B) by catching the exception inside the per-row loop. One-line change; backward-visible as a weakening of the invariant.

### Decision 9.7.G — `get_temporal` is a duck-typed adapter extension, not an abstract method [NEW 2026-04-21]

- **Finding:** `StorageAdapter.get_temporal(entity_ids)` is the batch primitive for sec9 §9.7 temporal aggregation. It's consumed by the SDK via `hasattr(self._storage, "get_temporal")` — SQLite and Postgres both implement it, but it's not declared on the `EntityStore` ABC.
- **Chosen:** Keep it duck-typed for now; do not add it to the abstract base class.
- **Why:** Matches existing precedent in the adapter layer (`get_fts_tables_for_entity_type`, `get_provenance_timestamps`, etc. are all duck-typed extensions). The `EntityStore` ABC already mandates the core read/write/find/history set; `get_temporal` sits alongside as a sec9-era extension. Non-relational adapters (Neo4j, when it lands) will need to decide whether they implement `get_temporal` natively or delegate — declaring it abstract up front would force a choice before the Neo4j constraints are understood.
- **Consequence:** SDK-side `hasattr` checks declare `get_temporal` optional; in practice, every concrete adapter implements it. If Neo4j (or a future adapter) doesn't, the SDK falls back to the entity's stored columns — same behavior as today for `query()` with a non-provenance-aware adapter.
- **Revert:** Promote to abstract on `EntityStore[T]` once all adapters are agreed. Low-cost later; additive-compatible for downstream subclassers since all existing concrete classes already implement it.

---

## 9.4 `mosaic_ext` Extension Vocabulary

### Decision 9.4.A — Scrub `hippo_summary_view` and auto-emitted summary views entirely [NEW 2026-04-19]

- **Finding (from the 2026-04-18 autonomous session):** sec9 §9.4 listed `hippo_summary_view` as a class-level annotation that opts in to summary-view emission, but reality disagreed. `src/mosaic/core/storage/view_generator.py` auto-emitted count + aggregate SQLite views for every non-abstract class during migration, with no annotation consumed. Nothing in the codebase consumed those views.
- **Alternatives considered:** (A) make the annotation a real opt-in by retiring auto-emission; (B) annotation-driven opt-out with auto-emission as default; (C) drop the annotation from sec9, keep auto-emission; (D) scrub summary views and the annotation entirely.
- **Chosen:** (D). No consumer existed; keeping dead DDL is not justified.
- **Actions landed:** Deleted `src/mosaic/core/storage/view_generator.py`; removed the `generate_summary_views` call and import from `src/mosaic/core/storage/migration.py`; removed `TestSummaryViews` from `tests/integration/test_partial_indexes_and_views.py` (partial-index tests retained, module docstring updated); removed the annotation from the sec9 §9.3 ASCII diagram, the sec9 §9.4 vocabulary table, and the sec9 §9.10 DDL-shim list; removed it from the `hippo-ext-vocabulary` proposal / tasks / draft `mosaic_ext.yaml`; removed it from the `reference_mosaic_ext.md` annotation list in `INDEX.md`.
- **Revert:** `view_generator.py` remains in git history (reachable from any commit before this one). To resurrect, restore the file, reinstate the call in `migration.py`, re-declare the annotation with a chosen option (A/B/C). The design-doc changes revert mechanically via `git revert`.

### Decision 9.4.B — Declare each annotation alongside its consumer; Wave 1 declares only annotations with live consumers [NEW 2026-04-19]

- **Finding (from the 2026-04-18 autonomous session):** `mosaic_append_only` and `mosaic_accessor` were drafted into `mosaic_ext.yaml` during the Wave 1 `hippo-ext-vocabulary` change even though their consumers don't land until Wave 2 (`provenance-as-linkml-class`) and Wave 3 (`typed-client`) respectively. This leaves two annotations documented-but-inert for the duration between their declaration and their consumer.
- **Alternatives considered:** (A) declare all annotations up front in `hippo-ext-vocabulary`, with consumer wiring following in later waves; (B) declare each annotation with its consumer, bumping `mosaic_ext`'s minor version in each change that adds one.
- **Chosen:** (B). Each OpenSpec change carrying both its annotation declaration and its consumer is more atomic. An agent reading `mosaic_ext.yaml` at any point in time sees only live annotations, matching 9.2's principle that schema reflects actual behavior.
- **Consequences:** Wave 1 `hippo-ext-vocabulary` declares four annotations (`mosaic_unique`, `mosaic_index`, `mosaic_index_partial`, `mosaic_search`). Wave 2 `provenance-as-linkml-class` extends `mosaic_ext` with `mosaic_append_only`. Wave 3 `typed-client` extends with `mosaic_accessor`. Each extension follows sec9 §9.4's four-step process (declare, implement, document, bump minor).
- **Actions landed:** Removed `mosaic_append_only` and `mosaic_accessor` from the Wave 1 draft `mosaic_ext.yaml` (with a comment pointer to their owning changes). Updated the `hippo-ext-vocabulary` proposal's "Why" line, initial-vocabulary table, and tasks.md §1.2 to reflect four initial annotations. Updated sec9 §9.12's scope/deliverables/acceptance columns for `provenance-as-linkml-class` and `typed-client` to include their respective `mosaic_ext` extension.
- **Revert:** Declare both annotations up front in Wave 1 (option A). Update the three Wave 1 files and the two §9.12 rows. Low blast radius.

### Decision 9.4.C — `mosaic_search` declared with `range: string`, not a closed enum [NEW 2026-04-21]

- **Finding (during hippo-ext-vocabulary implementation):** sec9 §9.4's draft table showed `mosaic_search` with value type `enum (fts5, …)`. The initial `mosaic_ext.yaml` declared a `mosaic_search_mode` enum with only `fts5` as a permissible value. Existing tests (`test_search_capability.py`, `test_fts_migration.py`) intentionally use non-`fts5` mode values (`embedding`, `fts`) to exercise adapter-layer validation — those tests failed at schema load with the new enum-backed validation.
- **Alternatives considered:** (A) keep the enum closed at `fts5`, rewrite the tests to assert on the new load-time failure path; (B) expand the enum to include every mode any adapter might support (impractical — the set is open across deployments); (C) loosen the range to `string`, let the adapter enforce which modes it supports.
- **Chosen:** (C). Matches sec9 §9.10's division of labor — schemas declare intent, adapters enforce capability. Different adapters (SQLite FTS5, PostgreSQL GIN, Neo4j full-text, future vector indexes) will genuinely support different modes; pinning a schema-level enum would force every deployment onto a single vocabulary.
- **Consequences:** `mosaic_search` accepts any string value at schema load. A typo like `fts6` passes validation and fails later at adapter startup with "unsupported mode" — the error is clear and points at the right layer. sec9 §9.4, `reference_mosaic_ext.md`, and the installed `mosaic_ext.yaml` all reflect the `string` range; the `mosaic_search_mode` enum has been removed entirely. Existing search-capability tests continue to pass unchanged.
- **Revert:** Reintroduce the `mosaic_search_mode` enum and change `mosaic_search.range` back to it. Rewrite `test_startup_raises_error_with_embedding_search_mode` and `test_fts_migration_planner::test_add_class_registers_fts_tables` to assert the schema-load-time failure instead of the adapter-startup failure. Moderate blast radius — any test fixture using an unsupported mode would need updating.

---

## 9.5 `mosaic_core` Schema

### Decision 9.5.A — Keep `is_available` hardcoded as a fallback when not induced [NEW 2026-04-21]

- **Finding (during hippo-core-schema implementation):** sec9 §9.5 declares `is_available` as a required slot on Entity, flowing into every domain class via `is_a: Entity`. DDLGenerator and PostgresDDLGenerator previously hardcoded the column unconditionally. With the schema-driven path, the column should come from induced_slots — but many test fixtures use `build_registry` to construct ad-hoc classes without `is_a: Entity`, and their tests rely on `is_available` being present in the generated DDL.
- **Alternatives considered:** (A) require all schemas (including ad-hoc test fixtures) to use `is_a: Entity`; (B) keep the hardcoded column as an unconditional fallback; (C) keep it as a fallback only when the class didn't already declare `is_available` via induction.
- **Chosen:** (C). Classes that inherit from Entity get `is_available` through the normal slot path; classes that don't fall through to a hardcoded append. Preserves the sec9 design goal (schema is source of truth) for proper schemas while keeping existing tests working without a sweeping migration.
- **Consequences:** `DDLGenerator._build_table` and `PostgresDDLGenerator._build_table` check `existing_column_names` before appending a hardcoded `is_available`. `superseded_by` remains unconditionally hardcoded; sec9 §9.6's provenance redesign will remove it in Wave 2.
- **Revert:** Remove the guard, restore unconditional hardcoding. Low blast radius. The principled alternative — Option A, requiring `is_a: Entity` everywhere — is deferred until Wave 2 consolidation of test fixtures.

### Decision 9.5.B — `slot_default()` helper coerces boolean `ifabsent` strings [NEW 2026-04-21]

- **Finding:** LinkML stores `ifabsent` as a string even for boolean slots (`ifabsent: "true"` / `"false"`). DDL generators format defaults via `_format_default(value)`; passing a raw string produces `DEFAULT 'true'` — a quoted string literal, not a native boolean — which is wrong for SQLite (`1`/`0`) and Postgres (`TRUE`/`FALSE`).
- **Alternatives considered:** (A) make `_format_default` LinkML-ifabsent-aware, parsing `"true"`/`"false"`/`"int(0)"`/`"uuid()"` inline; (B) a thin `slot_default(slot)` helper in `linkml_bridge` that coerces known LinkML ifabsent forms to Python values; (C) change `mosaic_core` to use `ifabsent: "1"` / `"0"` instead of `"true"` / `"false"`.
- **Chosen:** (B), minimal form. Current coverage: `range: boolean` + `ifabsent` in `{"true", "false"}` returns Python `True`/`False`; everything else passes through unchanged. Richer parsing (integer/datetime/uuid constructor forms) is deferred until a concrete slot needs it.
- **Consequences:** `slot_default()` lives in `mosaic.linkml_bridge` and is called by `DDLGenerator`, `PostgresDDLGenerator`, `migration.py`, and `pg_migration.py`. Schema authors write `ifabsent: true` in their YAML (standard LinkML idiom) and the DDL output is native SQL on both adapters.
- **Revert:** Inline the coercion at each call site or expand `_format_default` to handle ifabsent forms. The helper is a thin wrapper; removing it is mechanical.

### Decision 9.5.C — `_flatten_for_validator` materializes imports inline for `linkml.validator` [NEW 2026-04-21]

- **Finding:** LinkML's `Validator` (and `JsonschemaValidationPlugin`) resolve `imports:` independently of `SchemaView`'s `importmap`. When the user schema says `imports: [mosaic_core]` and mosaic_core lives outside the user-schema directory (inside `src/mosaic/schemas/` as a bundled resource), the validator tries to open `mosaic_core.yaml` relative to the user schema's directory and fails with `FileNotFoundError`. `SchemaView.materialize_derived_schema()` re-triggers the same import resolution.
- **Alternatives considered:** (A) pass the importmap to `Validator` (not supported in the current `linkml.validator` API); (B) pre-flatten the merged `SchemaView` into a self-contained dict with all classes/slots/enums/types inlined and `imports:` stripped, then hand that dict to `Validator`; (C) write the merged schema to a temp file with all imports resolved, then load from there.
- **Chosen:** (B). A module-level `_flatten_for_validator(sv)` helper reads `sv.all_classes(imports=True)`, `sv.all_slots(imports=True)`, `sv.all_enums(imports=True)`, `sv.all_types(imports=True)`, dumps each via `yaml_dumper`, and rebuilds a flat dict with only `linkml:types` as an import. The dict is passed to `Validator` which no longer needs to resolve imports from disk.
- **Consequences:** `Validator` works regardless of where `mosaic_core` lives on disk. Flattening runs once per `SchemaRegistry` construction (schema load is already a slow path; the extra round-trip is negligible). No behavior change visible to callers.
- **Revert:** Pass `sv.schema` directly to `Validator` again. Would break any setup where bundled schemas are imported, so not a practical revert.

### Decision 9.5.D — id-registry scope narrowed: existing `entities` table IS the registry [NEW 2026-04-21]

- **Finding (during id-registry-and-uuid-strategy implementation):** sec9 §9.5 and the `id-registry-and-uuid-strategy` proposal scope a new `_entity_registry` table with `(id, entity_type, created_at)`, populated transactionally on every entity create. Reality: the current SQLite and PostgreSQL adapters already store all entities in a single `entities` table with `id`, `entity_type`, `data` (JSON), and friends. The type discriminator `entity_type` is already maintained, already indexed (via primary key on id), and already populated transactionally with entity writes. A separate `_entity_registry` would be redundant against this design.
- **Alternatives considered:** (A) add `_entity_registry` anyway, keeping it in sync with the `entities` table (duplicate data + extra write); (B) use the `entities` table as the registry and expose SDK helpers (`resolve_type`, `resolve_types`) that wrap it; (C) defer the whole id-registry change until the storage model is consolidated (per-type-table vs. single-entities-table).
- **Chosen:** (B). The existing `entities` table serves the exact purpose a `_entity_registry` would — id → entity_type lookup — without duplicate data. SDK helpers wrap the existing query so callers use the sec9 API surface (`client.resolve_type(uuid)`); if the storage model later migrates to per-type tables, the helpers become the stable seam and the registry implementation switches underneath without callers noticing.
- **Consequences:** `SQLiteAdapter.resolve_type` / `resolve_types` and `PostgresAdapter.resolve_type` / `resolve_types` added. `MosaicClient.resolve_type(uuid)` and `MosaicClient.resolve_types(uuids)` added. No new table, no backfill migration, no write-path changes. Performance-benchmark task from the proposal is obsolete (the lookup path hasn't changed — it's the same SELECT the adapter already uses for read).
- **Deferred parts of the proposal:** UUID pattern on `Entity.id` (would break existing test fixtures using ids like "s1"; postpone until a test-fixture cleanup pass); `client.get(uuid)` overload without `entity_type` (requires deeper restructuring of `QueryService` which routes by entity_type today); one-time backfill migration (unnecessary — the `entities` table always had the type column).
- **Revert:** Remove the four methods (two adapters × {single, batch}) and two client methods. Low blast radius.

### Decision 9.5.E — `ReferenceLoader.schema_fragment` merges at plugin registration; instance validation runs after merge [NEW 2026-04-25]

- **Finding (PTS-67 design discussion):** A `ReferenceLoader` instance may reference classes declared in its own `schema_fragment` slot. Those classes don't exist in the merged `SchemaView` until the plugin's fragment is installed, so the lifecycle has to fix three things: (i) when the fragment is merged into the SchemaView, (ii) when the `ReferenceLoader` instance itself is validated against that view, and (iii) what the error surface looks like if either step fails.
- **Alternatives considered:** (A) merge fragment at plugin registration; validate the loader instance against the merged view immediately; failures abort registration. (B) lazy merge on first loader invocation; instance validation deferred until first use. (C) eager merge at registration but defer instance validation to first invocation.
- **Chosen:** (A). Per Director-of-Bioinformatics direction (PTS-67): the `schema_fragment` is loaded at plugin registration time, and end-user installation of a reference loader should not surface schema or data validation issues — the validation surface is plugin-developer-facing, not end-user-facing.
- **Consequences:** Plugin registration becomes a two-step lockstep: Hippo (1) merges `schema_fragment` into the live `SchemaView`, then (2) validates the `ReferenceLoader` instance against the merged view so its `entity_type` references resolve. Failure at either step aborts plugin registration with a single error path (`plugin X failed to register: <reason>`); partial state — fragment merged but instance rejected, or instance accepted before its fragment loads — is impossible. End users never see fragment-or-instance validation errors at runtime; those are caught by the plugin developer at registration. This locks the contract for sec9 §9.5 and for the `reference-loader-shape` OpenSpec change.
- **Revert:** Switch to (B) lazy-merge; would let invalid plugins register and fail mid-flight on first loader invocation. Higher operational cost (errors surface in user code paths instead of at registration); not recommended.

### Decision 9.5.F — `ReferenceLoader.entity_type` is declarative `multivalued: true` string; loader code owns runtime ingestion order [NEW 2026-04-25]

- **Finding (PTS-67 design discussion):** A multi-class `ReferenceLoader` (e.g. an ontology loader populating `Substance` and `Reaction`) needs some way to express the relationship between the classes it touches. Two open sub-questions: (i) does the order of `entity_type` entries carry runtime data-ingestion semantics — e.g., insert `Substance` rows before `Reaction` rows for FK satisfaction — or is it an unordered set? (ii) does per-class metadata (count estimates, intra-loader dependencies) live alongside each `entity_type` entry as a record, on separate `ReferenceLoader` instances per class, or somewhere else?
- **Alternatives considered:** (A) `entity_type` is an ordered list and Hippo enforces ingestion order from it; per-class metadata baked into a `LoaderTarget` record range. (B) `entity_type` is an unordered set; per-class metadata on separate `ReferenceLoader` instances. (C) `entity_type` is `multivalued: true` over `string`; treated as a declarative listing of which classes the loader populates; the loader's own code owns runtime data-ingestion order; per-class metadata is not modeled in the schema.
- **Chosen:** (C). Per Director-of-Bioinformatics direction (PTS-67): "Correct data-loading semantics is the loader developer's responsibility — we don't need any validation baked into the class. The `entity_type` slot is purely for provenance and discoverability." By the time end users install the plugin and trigger a load, all schema-resolution and ingestion-ordering concerns have already been resolved at the developer's layer.
- **Consequences:** `ReferenceLoader.entity_type` is declared as `multivalued: true` with `range: string`. Hippo records the list — for provenance (what classes did this loader populate?) and for discoverability via `SchemaRegistry.reference_loaders()` — but does **not** drive runtime data load order from it. Multi-class loaders ordering FK-dependent inserts (e.g., insert `Substance` rows before `Reaction` rows) handle that ordering inside their own loader code; the schema doesn't replicate that contract. Per-class metadata (count estimates, etc.) is not part of the `ReferenceLoader` shape today; if a future plugin needs Hippo to surface per-class metadata, promote `entity_type` to a `LoaderTarget` record range — mechanical migration, no semantics change for existing loaders. Wave-3 ReferenceLoader development documentation must explicitly call out the developer's responsibility for correct data-loading semantics so this contract isn't rediscovered later.
- **Revert:** Promote `entity_type` to `range: LoaderTarget` and treat order as load-order semantics. Mechanical migration but reopens the design question; only worth doing if Hippo gains a concrete need to drive ingestion from declared metadata.

---

## 9.6 Provenance as a LinkML Class

### Decision 9.6.A — Split `provenance-as-linkml-class` into declaration-only + dedicated `provenance-migration` change [NEW 2026-04-21]

- **Finding (Wave 2 kickoff):** The original `provenance-as-linkml-class` proposal bundled the LinkML declaration of `ProvenanceRecord` with the storage migration. The legacy `ProvenanceStore` + `provenance` table has ~40 deeply-coupled test references and concepts sec9 §9.6 doesn't model (`previous_state_hash`, `state_snapshot`, `operation_id` as a separate field, `source`). Bundling all of that into one OpenSpec change would make the opinionated calls about legacy-field retirement (drop vs. preserve) invisible — they'd be buried inside a sprawling change.
- **Alternatives considered:** (A) Full rewrite now — declare `ProvenanceRecord` in `mosaic_core`, rewrite `ProvenanceStore`, migrate ~40 tests in one change. (B) Narrow — declare only, keep `ProvenanceStore` on the legacy `provenance` table indefinitely. (C) Staged — declare only now as `provenance-as-linkml-class`; split the storage migration into a dedicated `provenance-migration` change where the legacy-field retirement decisions get explicit review.
- **Chosen:** (C). The LinkML declaration has independent value (introspection, typed-client support in Wave 3, `mosaic_append_only` annotation lands in the vocabulary, `Operation` enum becomes importable). Splitting the migration into its own change makes the opinionated calls (drop `previous_state_hash`, drop `state_snapshot`, rename `user_context` → `actor_id`, etc.) reviewable in isolation.
- **Consequences:** `provenance-as-linkml-class` is scoped to: declare `mosaic_append_only` in `mosaic_ext` (0.1.0 → 0.2.0); declare `ProvenanceRecord` in `mosaic_core` (0.2.0 → 0.3.0) with all sec9 §9.6 slots including `class_uri: prov:Activity` and `mosaic_append_only: true`; update reference docs; tests for the declaration. Adapter-side enforcement and `ProvenanceStore` rewrite move to the new `provenance-migration` change. sec9 §9.12 decomposition updated from 10 changes to 11; the Wave 2 dependency graph now reads `provenance-as-linkml-class → provenance-migration → computed-temporal-fields`.
- **Revert:** Merge `provenance-migration` back into `provenance-as-linkml-class`. Low-cost but doesn't address the scope concern that motivated the split.

### Decision 9.6.B — Legacy operation-string mapping to the `Operation` enum [NEW 2026-04-18]

- **Finding (provenance-migration commit 2 prep):** A grep across `src/mosaic/` surfaces eleven distinct operation strings passed to `ProvenanceStore.record()`, not the six the sec9 §9.6 `Operation` enum declares. The extras (`"EntitySuperseded"`, `"EntityUpdated"`, `"REPLACED"`, `"RELATE"`, `"UNRELATE"`, `"AvailabilityChanged"`) accreted as ad-hoc strings in different subsystems over time. A mechanical rename would lose semantic information; each call site needs per-site review.
- **Per-site mapping table** (verified by reading each call site):

  | Legacy string | Site | sec9 `Operation` | Notes |
  |---|---|---|---|
  | `"CREATE"` | sqlite_adapter.py:1190, postgres_adapter.py:1093 | `create` | direct map |
  | `"UPDATE"` | ingestion_service.py:174 | `update` | direct map |
  | `"EntityUpdated"` | provenance_service.py:190 | `update` | entity fields modified |
  | `"REPLACED"` | ingestion_service.py:299 | `update` | ingest-replace semantics: same entity_id, new data — not supersession (which requires distinct replacement_id) |
  | `"EntitySuperseded"` | provenance_service.py:173 | `supersede` | `derived_from_id` now carries the replacement; patch retains `{reason}` |
  | `"AvailabilityChanged"` | ingestion_service.py:420 | `availability_change` | patch carries `{is_available, reason}` |
  | `"SOFT_DELETE"` | sqlite_adapter.py:1305, postgres_adapter.py:1203 | `availability_change` | patch carries `{status: "deleted"}` per sec9 §9.6 |
  | `"RELATE"` | relationship.py:158 | `relationship_add` | patch carries `{slot, target_id}` |
  | `"UNRELATE"` | relationship.py:229 | `relationship_remove` | patch carries `{slot, target_id}` |
  | `"external_id_add"` | future | `external_id_add` | |
  | `"external_id_remove"` | future | `external_id_remove` | |

- **Chosen:** Apply the mapping table above at rewrite time. A small `_legacy_operation_string_map: dict[str, Operation]` helper lives in `ProvenanceStore` for the transition period; removed once all callers pass `Operation` enum values natively.
- **Why:** The per-site review surfaces the `"REPLACED"` nuance (mapped to `update`, not `supersede`) and the `"EntitySuperseded"` lineage (now carries `derived_from_id` instead of a patch field). Both are judgment calls that a mechanical rename would have missed.
- **Revert:** Per-row. If a caller wants different semantics, change the site and update the table.

### Decision 9.6.C — SQL triggers are the enforcement mechanism for `mosaic_append_only`, not adapter-level Python checks [NEW 2026-04-18]

- **Finding:** The provenance-migration proposal said "adapter write-guard" for `mosaic_append_only`. Reading the existing code, the legacy `provenance` table is already protected by five SQLite triggers (`sqlite_triggers.py`) that RAISE ABORT on UPDATE / DELETE. Adapter-level Python checks are strictly weaker — direct-SQL access (e.g., from `sqlite3` CLI or a raw-connection backdoor) bypasses them; DB triggers don't.
- **Alternatives considered:** (A) Keep adapter-level Python checks only (weaker than status quo, but simpler). (B) Keep triggers, rename to target the new `ProvenanceRecord` table with new column names. (C) Extend the DDL generator to emit triggers from `mosaic_append_only` — fully schema-driven. (D) Both — triggers for SQL-level enforcement plus adapter-level Python check for clearer error messages.
- **Chosen:** (B) for provenance-migration commit 2; (C) is the long-term target for a future change. (B) preserves the current security posture without extending the DDL generator scope mid-migration.
- **Why (B) over (A):** Not weakening existing protection is a strict correctness win. Rename is mechanical: change `provenance` → `ProvenanceRecord`, `entity_id` → `id`, drop `user_context` and `payload` triggers (absorbed by the generic "no UPDATE" behavior via BEFORE UPDATE without a target column).
- **Why (B) over (C):** DDL-generator extension is a separate capability with its own acceptance criteria (applies to other `mosaic_append_only` classes in user schemas, needs Postgres parity, etc.). Scope creep into commit 2 risks pulling the rewrite further behind.
- **Why (B) over (D):** Duplicate enforcement creates drift risk. If the triggers fire, the Python check never runs; if the Python check runs, the triggers haven't fired either. Single source of truth (SQL triggers) is cleaner.
- **Consequences for commit 2:**
  - `sqlite_triggers.py`: retarget from `provenance` to `ProvenanceRecord`; drop the column-specific UPDATE triggers in favor of a single BEFORE UPDATE trigger (any column, any row).
  - `SchemaRegistry.append_only_classes()` (landed in commit 1) is not consumed by adapters in commit 2 — it remains available for the future (C) work and for non-SQL adapters (Neo4j, etc.).
  - Postgres: equivalent `CREATE TRIGGER` replaces the "Postgres equivalent" adapter check mentioned in the proposal.
- **Revert:** Move to (A) by dropping the triggers module and adding Python-level checks. Backward-visible as a security downgrade.

### Decision 9.6.D — `ProvenanceRecord` table is DDL-generated via LinkML, not hand-coded [NEW 2026-04-18]

- **Finding (commit 1 verification):** `DDLGenerator().generate(registry)` against a registry importing `mosaic_core` already emits a correct `ProvenanceRecord` table — all sec9 §9.6 columns present, NOT NULL on required slots, indexes on `mosaic_index`-annotated slots, FK from `process_id` to `Process`. No generator changes required.
- **Chosen:** `SQLiteAdapter._init_schema` (and Postgres equivalent) drops the hand-coded `CREATE TABLE IF NOT EXISTS provenance (...)` block and the supporting `CREATE INDEX` statements; the `ProvenanceRecord` table comes in through the existing LinkML-DDL pipeline (same path used for domain classes). The adapter's startup code ensures that pipeline runs against a registry that includes `mosaic_core`.
- **Why:** Matches sec9 §9.2 principle (every class goes through the same DDL path). Hand-coded DDL diverging from the LinkML declaration is exactly the drift risk the sec9 redesign is meant to eliminate.
- **Consequences:** The `entities` / `relationships` / `entity_external_ids` / `schema_version` tables remain hand-coded for now (they're out of scope for this change; their LinkML-class migration is separate work in Wave 3). Only the `provenance` table and its indexes are removed from the hand-coded block.
- **Revert:** Restore the hand-coded DDL block; remove `ProvenanceRecord` from the merged registry that the adapter uses for LinkML DDL generation.

### Decision 9.6.F — Known transition-period fallbacks flagged explicitly [NEW 2026-04-18]

- **Finding:** Two semantic-contract compromises are active during the provenance-migration transition that sec9 §9.6 should eventually eliminate. Flagging them explicitly here so they don't ship as silent "done."
- **Fallback 1 — ``actor_id = "unknown"`` sentinel for new rows from unmigrated callers.** ``ProvenanceStore.record()`` defaults ``effective_actor`` to the literal string ``"unknown"`` when the caller passes ``None``. Several unmigrated src/ call sites (``ingestion_service.py``'s update paths, ``sqlite_adapter.create`` when ``user_context=None``) currently produce rows with ``actor_id = "unknown"``. Sec9 §9.5's identity model requires ``actor_id`` to be a UUID resolving to an agent entity. Follow-up: migrate those call sites to require a real ``actor_id`` (probably via the future service-context infrastructure); remove the fallback.
- **Fallback 2 — ``schema_version = ""`` on every new row.** Sec9 §9.6 specifies that the SDK captures ``schema_version`` from ``SchemaRegistry`` at write time. The adapter's ``ProvenanceStore.__init__`` accepts a ``schema_version`` parameter but neither ``SQLiteAdapter`` nor ``PostgresAdapter`` thread a registry through at construction, so every new row's ``schema_version`` column is an empty string. NOT NULL is satisfied syntactically; the semantic contract is not. Follow-up: plumb the registry (or a schema-version string derived from it) into both adapters, populated at adapter construction or on first write after schema load.
- **Why (tracking, not fixing now):** Both fallbacks are additive-compatible — removing them later won't require a data migration (new rows would just get richer values). Fixing them in this commit would expand the blast radius beyond "rewrite the provenance layer" into "propagate a schema registry through construction" and "plumb per-request actor context" — both multi-day undertakings on their own.
- **Revert / fix-forward:** Not applicable (these are known gaps to close, not decisions to revert).

### Decision 9.6.G — Actor context propagated via ContextVar, not method signatures [NEW 2026-04-26]

- **Finding (Decision 9.6.F follow-up):** Several `ProvenanceStore.record()` call sites in `ingestion_service.py` and the storage adapters pass `actor_id=None`, producing `actor_id = "unknown"` rows. Fixing these by threading an `actor_id` parameter through every write-method signature (`put`, `create`, `update`, `replace`, `delete`) would add 5+ parameter pairs and expand the blast radius significantly. A request-scoped ContextVar is the standard Python pattern for this problem.
- **Alternatives considered:**
  - (A) Explicit `actor_id` on every public SDK method — predictable but loud; adds 10+ signature changes across `MosaicClient`, `IngestionService`, and adapter `create`/`delete`.
  - (B) Client-level default actor set at `MosaicClient.__init__` — works for single-actor SDK use but fails for per-request multi-actor scenarios (FastAPI with multiple concurrent users).
  - (C) `ContextVar` in `mosaic.core.context` — per-request isolation is automatic; no signature pollution; plays well with async.
- **Chosen:** (C). `mosaic.core.context.current_actor` is a `ContextVar[Optional[str]]` with a `with_actor(actor_id)` context manager for direct SDK use.
- **Resolution order in `ProvenanceStore.record()`:**
  1. Explicit `actor_id=` kwarg (highest priority — existing call sites with real actors)
  2. Legacy `user_context=` shim (Decision 9.6.B)
  3. `current_actor.get()` from the ContextVar (set by middleware or `with_actor()`)
  4. `"unknown"` sentinel (last resort — satisfies NOT NULL; flags unmigrated paths)
- **FastAPI integration:** `PassThroughAuthMiddleware.__call__` calls `current_actor.set(actor_id)` at request entry and resets via token in a `try/finally` block, so each async request carries its own isolated actor value.
- **Scope note:** `track_creation`, `track_update`, `track_deletion` in `sqlite_adapter.py` produce in-memory records only (no DB writes) with `actor_id=""`. These are not part of the 9.6.F sentinel problem and are unchanged.
- **Why:** ContextVar is the idiomatic Python pattern for request-scoped state in both sync and async contexts. It avoids signature pollution, works naturally with FastAPI's async request lifecycle, and is compatible with threadpool (`concurrent.futures`) use via `copy_context()`. The sentinel remains as a safety net — its presence in the audit log signals that a call site still needs migration, without breaking the NOT NULL constraint.
- **Revert:** Remove `mosaic.core.context`; revert the three-line change in each adapter's `record()` and the middleware change. Low blast radius — all changes are additive.

### Decision 9.6.E — Legacy `user_context` strings are not back-populated to `actor_id` UUIDs [NEW 2026-04-18]

- **Finding (from the provenance-migration proposal's open question):** Legacy rows have `user_context` as a free-form string (often a username or `"sqlite_adapter"`). After rename to `actor_id`, these strings won't resolve through the UUID identity model (sec9 §9.5).
- **Alternatives considered:** (A) Leave as-is — historical audit records retain their historical actor strings; new records use UUIDs. (B) Synthesize a `LegacyActor` placeholder entity per unique legacy string and back-populate references.
- **Chosen:** (A). Simpler; matches append-only semantics (you don't rewrite history); legacy rows are a finite, read-only population that doesn't need forward compatibility with the identity model.
- **Why:** Back-population (B) conflicts with the append-only invariant — rewriting `actor_id` across a million legacy rows is a mass mutation of an audit log, which is exactly what the immutability triggers reject. Even if triggers were temporarily disabled for the migration, the resulting synthetic `LegacyActor` entities are clutter with no consumer. Since there are no production deployments (per earlier user directive), the population of legacy rows is near-empty or easily droppable.
- **Revert:** (B) remains available as a follow-up migration; low cost.

---

## 9.8 Typed Client

### Decision 9.8.A — Generation at `SchemaRegistry` load time, in-memory only

- **Alternatives:** (1) static file generation at build time; (2) runtime in-memory generation; (3) both.
- **Chosen:** (2) runtime in-memory. The generated Pydantic namespace is available under a stable SDK entry point after schema load.
- **Why:** Hippo's schema is deployment-specific; static generation would require a build step in user deployments. In-memory generation matches LinkML's own `PythonGenerator` patterns and avoids a second artifact to keep in sync.
- **Revert:** Add (1) as a supplementary path for IDE autocomplete if it becomes a felt need.

### Decision 9.8.B — Access pattern: dual-root + namespace-aware + nested-namespace support [FINALIZED 2026-04-18]

- **History:** Refined three times in sequence — (i) initial flat `client.samples.create(...)`; (ii) namespace-aware `client.<ns>.<accessor>.create(...)`; (iii) final form below. Left as a single entry with the history line so reviewers see the evolution without scanning for superseded versions.
- **Final form:**
  - Root-namespace classes accessible via both `client.<accessor>` (flat, default) and `client.root.<accessor>` (explicit). `client.root` is an alias; `root` is a reserved namespace name.
  - Non-root-namespace classes accessible via `client.<namespace>.<accessor>`.
  - Nested namespaces via dot notation: `namespace: assay.quant` is a literal string; the client splits on dots to produce `client.assay.quant`. No formal parent-child relationship between `assay` and `assay.quant` — the shared prefix is convention only, but the typed client presents them as a hierarchy.
  - Accessor default: `snake_case(ClassName) + "s"`. `mosaic_accessor` annotation overrides per class.
  - FQN rule: last dot-separated segment is the class name; everything before is the namespace string. Dots in class names are rejected at load.
- **Collision detection (four cases, all load-time, actionable error templates):** same-namespace duplicate accessor; class accessor vs. sub-namespace segment; namespace name vs. SDK-reserved attribute; accessor vs. SDK-reserved name.
- **Why:** Preserves the schema's namespace structure (multi-namespace is the norm, contrary to an earlier mistaken assumption). Keeps root-level access flat and convenient per user guidance. Supports organizational nesting without introducing a formal parent-child model in LinkML (which has none). Makes collisions genuinely rare — a typical deployment, even a multi-namespace one, adds zero `mosaic_accessor` annotations.
- **Supersedes sec3 decision "Root namespace canonicalization":** sec3 canonicalizes `root.X` to unqualified `X` at storage. sec9 keeps the flat-access convenience at the client level and adds the explicit `client.root.X` path. Storage-level canonicalization is untouched. The sec3 decision should be updated in INDEX.md to cite sec9 as the authoritative treatment.
- **Revert:** Revert to pure flat (no namespaces in the client) would break multi-namespace deployments; very high blast radius. Revert nested-namespace dot-splitting only is additive-compatible — collapses to flat strings on the client side with no schema changes needed.

### Decision 9.8.F — Nested namespaces via dot notation, no formal hierarchy [NEW 2026-04-18]

- **Alternatives:** (1) flat namespaces only (sec3 current state); (2) formal nested namespaces with declared parent-child relationships; (3) dot-notation-as-convention with client-side splitting.
- **Chosen:** (3). Namespace strings are literal (`"assay.quant"`); the typed client splits on dots for organizational convenience. `assay` and `assay.quant` are independent — the shared prefix is not a declared relationship.
- **Why:** LinkML has no native nested-namespace concept; any formal nesting would be a Hippo invention layered on top. The literal-string-with-client-side-splitting approach needs no new LinkML machinery, introduces no parent registration ceremony, and supports hierarchical organization where deployments want it. Risks (orphan parents, accessor/sub-namespace collisions) are manageable via load-time detection.
- **Revert:** Treat namespace strings as opaque in the client (no dot-splitting) — collapses nesting to flat attribute access. Additive-compatible; schemas don't need to change.

### Decision 9.8.G — FQN parsing rule: last dot-separated segment is the class name [NEW 2026-04-18]

- **Alternatives:** Explicit separator (e.g., `::`), last-dot rule, first-dot rule, require separate `namespace` and `class` fields in references.
- **Chosen:** Last-dot rule. `tissue.protocol.StepType` → namespace `tissue.protocol`, class `StepType`.
- **Why:** Unambiguous given the constraint that class names cannot contain dots (enforced at schema load). Human-readable. Consistent with how nested namespaces appear in the client.
- **Revert:** Change the separator rule and update schema validation; touches every FQN reference site. Moderate blast radius.

### Decision 9.8.E — Default accessor derivation rule: `snake_case(ClassName) + "s"` [NEW 2026-04-18]

- **Alternatives:** (1) inflect-library-backed plurals (handles irregulars but adds dependency and still misses some cases); (2) deterministic simple suffix rule; (3) no pluralization — use `snake_case(ClassName)` as-is.
- **Chosen:** (2). `snake_case` + `"s"`.
- **Why:** An LLM coding agent must be able to predict the accessor for any class without running code. The simple rule is perfectly predictable. Linguistic correctness is a non-goal; deployments that want `data` instead of `datums` use `mosaic_accessor`.
- **Revert:** Change the default in the typed-client generator; user-schema changes only required on classes whose default accessor changes.

### Decision 9.8.C — Pydantic v2 only

- **Alternatives:** Support v1+v2 via conditional imports; v2 only.
- **Chosen:** v2 only.
- **Why:** LinkML's current `gen-pydantic` targets v2; v1 is past its maintenance window; dual-targeting doubles maintenance cost.
- **Revert:** Low likelihood; would require a wrapper layer.

### Decision 9.8.D — Generic `MosaicClient` stays coequal (never "deprecated")

- **Alternatives:** Mark generic client as legacy once typed client lands; maintain both as first-class.
- **Chosen:** Both first-class (per 9.2 *Typed and dynamic coequal*).
- **Why:** Dynamic use cases (runtime schema discovery, tools, admin CLIs) need the generic client. No feature should land in one without the other.
- **Revert:** N/A — reversing this principle requires revisiting 9.2.

### Decision 9.8.H — Pydantic generation failures raise `TypedClientError`; schema is a compulsory contract [REVISED 2026-04-22]

- **History:** Original (initial 2026-04-22 autonomous landing): log WARNING and degrade to dict-only accessors. Revised same day per user feedback ("the schema is a compulsory contract"). The revised decision is recorded below; the original is preserved in git history at commit `9a7b5f4`.
- **Finding:** `PydanticGenerator` is a separate LinkML tool from `SchemaView` — it can fail for schemas that `SchemaView` accepts (unusual type references, generator quirks, LinkML version mismatches). The question is whether such a failure is a schema defect (hard error) or a tolerable transition state (warn-and-degrade).
- **Alternatives considered:** (A) Raise on generation failure — typed-client is all-or-nothing; a schema whose Pydantic generation fails cannot construct a `MosaicClient`. (B) Swallow silently — accessors still work against dicts but a failed generation is invisible. (C) Log a WARNING and degrade to dict-only — accessors still work, operators can diagnose, no exception surface.
- **Chosen (revised):** (A). `generate_pydantic_models()` raises `TypedClientError` at each of the four failure points (generator import, schema serialization, Pydantic import, generated-code execution), each with a distinct `.case` identifier (`pydantic_generator_unavailable`, `pydantic_generation_failed`, `pydantic_unavailable`, `generated_module_invalid`). The chain preserves the underlying exception via `raise … from exc`.
- **Why (revised):** The schema is the contract between callers and the system; degraded typed surfaces are a class of silent correctness violation equivalent to the provenance-integrity failures sec9 §9.2 rejects. A deployment whose schema can't produce Pydantic models has a defect that must be fixed in the schema, not papered over at runtime. User directive (2026-04-22): "switch to hard fail, the schema is a compulsory contract."
- **Consequence:** `EntityAccessor.model_class` is always non-`None` for every exposed domain class. `accessor.create(PydanticInstance)` is guaranteed to work; callers that want dict-only access can still pass a dict. A schema the generator can't handle fails at `MosaicClient.__init__` — the typed client does not silently fall back.
- **Revert:** Restore the WARNING-and-degrade behavior. Each failure point would return `{}` instead of raising. Backward-visible as a weakening of the contract — callers who relied on hard-fail semantics to catch schema defects would lose that guarantee.

---

## 9.9 Validation Division of Labor

### Decision 9.9.A — Three-tier validation model with strict boundaries [CONFIRMED 2026-04-18]

- **Alternatives:** Two-tier (LinkML + Python); three-tier (LinkML + CEL + Python); fully custom.
- **Chosen:** Three-tier. LinkML for static shape; CEL for dynamic / cross-entity; Python plugin for what neither can express.
- **Why:** LinkML + CEL covers 95%+ of cases; Python escape hatch avoids blocking unusual requirements without encouraging custom code as a first resort. User confirmed the flexibility is needed; coding agents are expected to reach for Python only when CEL genuinely cannot express the rule.
- **Revert:** Remove the Python tier by declaring it deprecated; callers forced to CEL (which is strictly more analyzable).

### Decision 9.9.B — Execution order: LinkML → CEL → Python plugin, fail-fast by default

- **Alternatives:** Fixed order fail-fast; fixed order collect-all; configurable.
- **Chosen:** Fixed order (cheapest first) with an opt-in collect-all mode for batch ingest.
- **Why:** Fail-fast is the cheaper default; LinkML-level failures are almost always the root cause and bubble up clearly. Collect-all is useful for ingest pipelines that want to report every error in a batch.
- **Revert:** Change default to collect-all; callers opt in to fail-fast.

### Decision 9.9.C — Unified `ValidationResult` envelope across tiers [CONFIRMED 2026-04-18]

- **Alternatives:** Tier-specific result types; common envelope with tier annotation.
- **Chosen:** Common envelope, each failure annotated with its origin tier.
- **Why:** Callers consume results uniformly; origin is visible for debugging; tier-specific types would fragment the error surface.
- **Revert:** Split into per-tier types; callers would need a union.

### Decision 9.9.D — Backward-compatible envelope extension; do NOT break existing `ValidationResult` callers [NEW 2026-04-22]

- **Finding (validation-tiering-clarification implementation):** The legacy `ValidationResult` in `mosaic.core.validation.validators` carried `is_valid: bool` and `errors: list[str]`. Dozens of callers (service-layer paths, REST handlers, TUI, plugin API) consume those two fields. Replacing the shape wholesale would fan out into a multi-day port; blocking the sec9 §9.9 envelope on that port would gate Wave 3.
- **Alternatives considered:** (A) Hard swap — rename legacy fields, retype to the new envelope, fix every caller. (B) Parallel class (`ValidationEnvelope`) alongside the legacy `ValidationResult` with conversion at boundaries. (C) Extend the existing dataclass — add `failures: list[ValidationFailure]`, keep `is_valid` / `errors` as the legacy view; reconcile both views in `__post_init__`.
- **Chosen:** (C). `ValidationResult` grows `failures: list[ValidationFailure]`; `__post_init__` synthesizes whichever view the caller didn't supply. `errors` carries raw messages (no tier prefix) so legacy substring-based test assertions continue to work; tier-aware callers read `failures`.
- **Why:** Keeps every existing caller working with zero changes while making the tier-annotated envelope available to sec9-era consumers (REST error surface, typed-client `ValidationFailed` exception, batch-ingest reporting). Callers can migrate to `failures` at their own pace.
- **Back-compat contract:** Legacy callers constructing `ValidationResult(is_valid=False, errors=["msg"])` get `failures=[ValidationFailure(tier="python", rule="legacy", message="msg")]` synthesized. Tier-aware callers constructing `ValidationResult(failures=[...])` get `errors=[f.message for f in failures]` synthesized. Both sides are always populated after construction.
- **Revert:** Promote to (A) or (B) once a dedicated port pass retires legacy `.errors` readers. Low-cost later; additive-compatible.

### Decision 9.9.E — `ValidationFailed` exception (new) vs. `ValidationFailure` exception (existing) [NEW 2026-04-22]

- **Finding:** The sec9 §9.9 proposal specs `ValidationFailed` as the exception that carries the envelope. `mosaic.core.exceptions` already had a `ValidationFailure` exception with a different shape (`rule_id`, `input_context`, `entity_type`, `entity_id`) used by existing callers.
- **Alternatives considered:** (A) Rename the existing `ValidationFailure` exception and break callers. (B) Add `ValidationFailed` as the new envelope-carrying exception; leave `ValidationFailure` (the exception) untouched. Callers transition by site.
- **Chosen:** (B). `ValidationFailed` is the sec9 envelope-aware exception; `ValidationFailure` (exception) remains as-is; `ValidationFailure` (dataclass, in `validation/validators.py`) is the envelope entry. Three names, three roles, each distinct.
- **Why:** Name collisions between the existing exception and the new dataclass (`ValidationFailure`) are tolerable because they live in different modules; the new exception (`ValidationFailed`) is a verb-past-tense distinguishable from both. Any alternative involved renaming something, and renaming the oldest identifier (`ValidationFailure` the exception) would cascade into every caller.
- **Revert:** Rename the existing exception to something like `ValidationRuleFailure` in a future cleanup; update all callers.

---

## 9.10 LinkML Ecosystem Integration

### Decision 9.10.A — Adopt LinkML tools directly; strictly bound shims to `mosaic_*` annotation effects [STRENGTHENED 2026-04-18]

- **Alternatives:** Adopt selectively and shim around weaknesses; adopt strictly and forbid shim drift.
- **Chosen (revised):** Adopt strictly. Shims exist for exactly one purpose — applying `mosaic_*` annotation effects that LinkML generators have no knowledge of. Shims are explicitly NOT permitted for workarounds, bug-hiding, or selective reimplementation of LinkML capabilities. Expanding the shim surface requires a dedicated OpenSpec proposal.
- **Why (revised):** Allowing shim drift would slowly turn Hippo into a parallel LinkML reimplementation. The user made this explicit: LinkML tool versions are hard requirements; if LinkML needs fixing, upgrade LinkML, do not shim around it.
- **Revert:** Loosen the shim boundaries; permit workaround shims case-by-case. Risk: gradual drift into reimplementation.

### Decision 9.10.B — Pin LinkML to exact versions; LinkML patch bump triggers a Hippo version bump [STRENGTHENED 2026-04-18]

- **Alternatives:** Unpinned; pin to major; pin to minor; pin to exact version with release discipline.
- **Chosen (revised):** Exact-version pinning in `pyproject.toml` for LinkML and every LinkML tool dependency. Any LinkML version bump (including patch-level) requires the full Hippo test suite to pass before the pin is updated. Every LinkML pin update triggers a Hippo version bump even when Hippo source is unchanged — the released artifact is the combined `(Hippo, LinkML)` pair, and different LinkML ⇒ different artifact. Breaking LinkML changes that require Hippo source changes are scoped in an OpenSpec proposal before the pin moves.
- **Why (revised):** Reproducibility and auditability. Exact pins guarantee the same code runs the same way in every environment. Tying Hippo version to LinkML version ensures consumers can answer "what LinkML is in this Hippo release?" from the version number alone.
- **Revert:** Loosen to major-only pinning. Low revert cost in `pyproject.toml`; higher cost if Hippo's release infrastructure gets built around the bump-on-LinkML-bump rule.

### Decision 9.10.C — Upstream contribution for annotation patterns that prove general

- **Alternatives:** Keep everything in Hippo forever; contribute nothing; contribute eagerly.
- **Chosen:** Contribute when an annotation in `mosaic_ext` demonstrates utility beyond Hippo and LinkML accepts such contributions.
- **Why:** Reduces long-term maintenance burden; aligns with "no shadow abstractions" principle at ecosystem scale.
- **Revert:** No revert needed — this is aspirational guidance.

---

## 9.11 Migration Narrative

### Decision 9.11.A — Narrative frame: three stages (pre-LinkML → SchemaRegistry seam → sec9 target)

- **Alternatives:** Simple "before/after"; detailed per-commit timeline; three-stage narrative.
- **Chosen:** Three-stage, matching 9.1's retrospective framing.
- **Why:** Most useful for an agent entering the codebase — names the stages, anchors recent refactors as "already done," points forward to sec9 target.
- **Revert:** Restructure as before/after; no cross-references depend on the staging.

### Decision 9.11.B — Cite specific commits by hash for already-done work

- **Alternatives:** Narrative only; cite commits.
- **Chosen:** Cite commits (consistent with 9.1).
- **Why:** Grounds the retrospective in observable history; agents reading git log can verify.
- **Revert:** Strip commit hashes if they become stale or misleading.

---

## 9.12 OpenSpec Decomposition

### Decision 9.12.A — 10 proposed OpenSpec changes, dependency-ordered

- **Alternatives:** Fewer, larger changes (3-4); more, smaller changes (15+); the chosen 10.
- **Chosen:** 10, grouped into three waves.
- **Why:** Each is independently reviewable and independently revertible. Fewer would mean each PR is too big; more would fragment related work.
- **Revert:** Merge or split any two adjacent changes without affecting others.

### Decision 9.12.B — Wave structure: foundation / data-model / consumer-facing

- **Alternatives:** Flat sequence; waves.
- **Chosen:** Waves. Wave 1 is foundation (vocabulary, core schema, IDs). Wave 2 is data model (Process, Provenance, temporal). Wave 3 is consumer-facing (validation clarification, typed client, REST).
- **Why:** Mirrors dependency structure naturally and lets the implementer pause between waves for validation.
- **Revert:** Drop the waves, keep the sequence — purely organizational.

### Decision 9.12.C — Per-change: scope, dependencies, deliverables, acceptance criteria

- **Alternatives:** Name and one-liner; full scoping per change.
- **Chosen:** Full scoping (consistent with openspec's expectations).
- **Why:** Enables an agent to pick up any change and execute without re-deriving scope.
- **Revert:** Strip to shorter form if overkill.

---

## 9.13 Non-Goals & Deferred Concerns

### Decision 9.13.A — Split into two groups: explicit non-goals, and deferred-for-later open questions [CONFIRMED 2026-04-18]

- **Alternatives:** One flat list; split.
- **Chosen:** Split. Non-goals are things sec9 explicitly excludes; deferred are things sec9 touches but doesn't finalize (ReferenceLoader shape, merge/fission primitives, etc.).
- **Why:** An agent can distinguish "don't do this" from "this is coming later."
- **Revert:** Merge into one list.

### Decision 9.13.B — Beyond the four user-confirmed non-goals, add three more discovered during drafting

- **Added non-goals:** no full PROV-O ontology import (only selective URIs); no human-readable IDs (UUIDs only); no first-class merge/fission primitives in this redesign.
- **Why:** These came up during the drafting pass and are worth making explicit so they don't silently get interpreted differently.
- **Revert:** Remove any individually.

---

## 2.14 Reference Loader Contract

Decisions D1–D9 were locked by the Director of Bioinformatics in [PTS-215](/PTS/issues/PTS-215) in response to [GitHub issue #6](https://github.com/VA-NCPTSDBB-Bioinformatics/DataHelix/issues/6), which identified nine unresolved contract gaps before the downstream `mosaic-reference-ensembl` package could ship. The full rationale is in the [PTS-215 plan document](/PTS/issues/PTS-215#document-plan).

### Decision 2.14.A — Loader-specific sub-commands: entry-point-registered Typer sub-app

- **Alternatives:** (a) Entry-point-registered `typer.Typer` sub-app; (b) ABC `subcommands()` returning a declarative spec that Hippo renders; (c) no special CLI integration.
- **Chosen:** (a). Loader declares a second entry point `mosaic.reference_loader_cli` pointing at a `typer.Typer` instance. Hippo mounts it under `mosaic reference <name> ...`.
- **Why:** Hippo standardizes on Typer throughout. Option (b) would require building a spec-to-Typer renderer (~hundreds of LOC, ongoing maintenance) for a portability dimension Hippo does not need. Option (a) is ~10 lines of glue and matches the entry-point pattern already used for loader discovery. The API-surface coupling to Typer is real but accepted: if the CLI framework ever changes, loaders re-export their subcommands.
- **Revert:** Remove the `mosaic.reference_loader_cli` entry point group and drop `subcommands_app` from the ABC. Existing sub-commands would need to be relocated or dropped. Moderate blast radius.

### Decision 2.14.B — `validate(user_artifact)`: recommended, not required

- **Alternatives:** (a) Required abstract method (all loaders must implement); (b) Recommended method with default `NotImplementedError`; (c) Not in the ABC at all.
- **Chosen:** (b). `validate()` is on the ABC with a default `NotImplementedError`. The CLI handles the error gracefully ("this loader doesn't expose a validator"). Loaders that have nothing user-side to validate omit the override.
- **Why:** An integration point users demonstrably need. Downstream packages benefit from a uniform contract. Forcing all loaders to implement one would over-fit to the FMA/GO case where user-side artifact validation exists; most ontology loaders have nothing to validate.
- **Revert:** Promote to `@abstractmethod`. Every existing loader must add a `validate()` stub. Low blast radius (mechanical).

### Decision 2.14.C — Version semantics: per-organism opaque slug; exact-match `requires:` for v1

- **Alternatives:** (a) Per-organism slug as the version string, opaque to Hippo; (b) Release-only version plus organism as a separate `load()` kwarg; (c) Extras-style `mosaic-reference-ensembl[mus_musculus]>=...` dependency syntax.
- **Chosen:** (a). `versions()` returns opaque slugs like `mus_musculus.GRCm39.115`. Format is loader-defined; Hippo performs only string equality when checking `requires:`. Range comparators (`>=`, `~=`) are deferred to v2. A future v2 may add an optional `parse_version(s) -> tuple` ABC method.
- **Why:** Option (b) conflates "release X exists for mouse" with "release X exists for human." Option (c) couples Hippo's `requires:` parser to a specific Python-packaging convention and leaks an implementation detail into the public contract — loaders may eventually be non-Python. Known v1 limitation: `>=` constraints are inexpressible; users pin exact versions.
- **Revert:** Adopt (b) or (c). Either requires changes to the `requires:` parser and `versions()` contract. Moderate blast radius.

### Decision 2.14.D — `load()` params: Pydantic v2 model, optional declaration

- **Alternatives:** (a) `**kwargs` with no schema; (b) Pydantic v2 model declared on the ABC as a class attribute; (c) LinkML fragment as params schema; (d) TypedDict.
- **Chosen:** (b). ABC adds `load_params_schema: type[BaseModel] | None = None`. When declared, the CLI auto-renders `--flag` args from the model and validates user input. `load()` signature changes to `load(client, version, params: BaseModel | None = None)`.
- **Why:** Hippo already uses Pydantic v2 throughout (CRUD models, MosaicClient). Pydantic gives free CLI rendering via tooling, runtime validation, and JSON schema export. LinkML is the right tool for data shape, not runtime call parameters. TypedDict provides no runtime validation. The shared model between `load()` and the CLI (D1↔D4 coupling) makes the choice load-bearing.
- **Revert:** Revert to `**kwargs`. Drop `load_params_schema` from the ABC. Loaders that declared schemas would need to internalize their own CLI rendering. Moderate blast radius.

### Decision 2.14.E — Caching contract: `client.cache_dir` + `client.cached_fetch` required for large downloads

- **Alternatives:** (a) `MosaicClient.cache_dir` and `client.cached_fetch(url)` — Hippo-managed; (b) Loader picks its own cache path; (c) User passes `--cache-dir` flag to each command.
- **Chosen:** (a). `MosaicClient.cache_dir_for(loader_name) -> Path` resolves to `$MOSAIC_CACHE_DIR/<loader_name>/` if set, else `~/.cache/mosaic/references/<loader_name>/`. `client.cached_fetch(url, *, expected_sha256=None, loader_name) -> Path` is a content-addressable cache (URL-keyed inside the per-loader directory) with optional sha256 verification on both download and cache hit; mismatches raise `CacheIntegrityError` and remove the offending file. Loaders MUST use it for any download >1 MB. (Surface note: the original draft showed `client.cache_dir: Path`; reshaped to a `cache_dir_for(loader_name)` accessor + explicit `loader_name` arg on `cached_fetch` during PTS-225 so a single client serves many loaders without stateful per-loader binding.)
- **Why:** A single managed location enables `mosaic reference clean-cache`, CI mount, and deterministic re-runs. Option (b) scatters cache directories unpredictably. Option (c) imposes per-command ceremony on users. The coupling to `MosaicClient` (D1↔D5) means subcommand apps already have access to the same cache path as `load()`.
- **Revert:** Remove `cached_fetch` and `cache_dir` from `MosaicClient`. Drop the MUST rule. Loaders revert to self-managed paths. High blast radius if any loader is already using `cached_fetch`.

### Decision 2.14.F — Upgrade semantics: additive default, `--prune-old` opt-in, separate `upgrade()` ABC method

- **Alternatives:** (a) Additive default with explicit `--prune-old` prune; (b) Replace-by-default (old version rows overwritten); (c) Migrate-with-deprecation (old rows marked superseded, not deleted).
- **Chosen:** (a). `upgrade()` is a separate ABC method (default: `load(to_version, params)`). Loaders override for efficient diffs. `--prune-old` removes prior version rows only after the new install succeeds. Option (c) is deferred to v2 pending entity write path support for row-superseded marking.
- **Why:** Additive aligns with how Hippo already keys versioned entities (e.g., `GeneVersion.gene_stable_id.version`). User FKs into old rows don't break on upgrade by default. Replace-by-default would silently invalidate user data. Migrate-with-deprecation requires infrastructure that doesn't exist in v1.
- **Revert:** Change the default to replace semantics. High blast radius — would invalidate existing user data on upgrade.

### Decision 2.14.G — Schema-fragment merge: mandatory per-loader prefix, `imports:` policy, `provided_by` injection

- **Alternatives:** Separate sub-decisions for prefix rules, `imports:` policy, and traceability annotation; unified schema-fragment merge rule.
- **Chosen:** Three sub-rules unified as one decision. (1) Every fragment MUST declare `default_prefix: <loader_name>:`. Two loaders declaring the same prefix → `ConfigError`. (2) Fragments MUST NOT redeclare `linkml:types` or prefixes already in the deployed schema; Hippo strips colliding top-level imports. (3) Hippo injects `annotations: { provided_by: { value: "<loader_name>@<pkg_version>" } }` on every class/slot the fragment introduces.
- **Why:** The mandatory unique prefix rule makes the existing class-name collision detection automatic (same-prefix check subsumes same-name check). The `imports:` policy prevents loaders from causing merge failures by re-importing types already present. The `provided_by` annotation enables runtime introspection of which loader introduced which classes.
- **Revert:** Remove any sub-rule individually. Dropping the prefix mandate would require restoring the old name-collision detection logic. Low-to-moderate blast radius per sub-rule.

### Decision 2.14.H — Cross-loader FKs: out of scope for v1, `loader_depends_on` soft annotation supported

- **Alternatives:** (a) Hard FK validation at install time; (b) Transitive `requires:` resolution; (c) Out-of-scope, no annotation; (d) Out-of-scope, `loader_depends_on` soft annotation with warning.
- **Chosen:** (d). v1 does not validate cross-loader FKs. Loaders MAY annotate their fragment with `loader_depends_on: [<other_loader_name>]`. Hippo emits a **warning** if a declared dependency is not installed; it does not block the install.
- **Why:** Cross-loader FK validation requires a transitive dependency resolution mechanism that adds significant complexity with unclear real-world benefit at v1 scale. Documenting the limitation explicitly (rather than silently allowing broken FKs) gives loader authors and users a clear signal. The soft annotation is a forward-compatibility hook for v2 hard FK support.
- **Revert:** (c) — remove the annotation support entirely. Very low blast radius. Or promote to (a/b) in v2.

### Decision 2.14.I — Test fixtures: `"test"` pseudo-version reserved convention

- **Alternatives:** (a) `"test"` as a reserved returned version with bundled fixture data; (b) `include_test=True` flag on `load()` / `versions()`; (c) No convention — leave testing to loader authors.
- **Chosen:** (a). Loaders SHOULD include `"test"` in `versions()`. The `"test"` version loads a deterministic, network-free subset bundled in the package. This is a convention, not enforced by the ABC. CI pipelines use `mosaic reference install <name> --version test`. `"test"` is a reserved slug — Hippo will never generate it as a real release version.
- **Why:** Per advisor feedback: a `--flag` approach (option b) adds complexity to the ABC surface with no benefit over a plain string version. `"test"` as a regular returned version is simpler; the loader code decides what `"test"` means. Loaders that omit it don't break Hippo, but their downstream consumers lose the hermetic test path.
- **Revert:** Remove the reserved-slug guarantee. Low blast radius.

---

## 2.14 Reference Loader v2 (PTS-243)

Decisions J–L lock the v2 ReferenceLoader contract in response to [GitHub #15](https://github.com/VA-NCPTSDBB-Bioinformatics/DataHelix/issues/15) (`--prune-old` opt-in undependable at production scale) and [GitHub #16](https://github.com/VA-NCPTSDBB-Bioinformatics/DataHelix/issues/16) (`EntityRef` shape). The two issues describe one design surface; the author explicitly asked for a coherent joint decision (#16 §"Relationship to DataHelix#15"). The full rationale is in this issue ([PTS-243](/PTS/issues/PTS-243)) and the v2 spec sits at [sec2 §2.14.8](sec2_architecture.md) and [§2.14.9](sec2_architecture.md).

### Decision 2.14.J — `--prune-old` substrate: Hippo-side write log with explicit `load_context` plumbing [NEW 2026-05-22]

- **Alternatives considered:**
  - **(a)** Hippo-side write log keyed by `(loader_name, version)`. Lifecycle wrapper intercepts every `client.put()` during a `load()` / `upgrade()` and records `(entity_id, entity_type, loader_name, version)` to an internal table. `--prune-old` queries the log. Loader authors do nothing.
  - **(b)** Context-scoped accumulator (`with client.track_entities() as batch:`). Auto-appends `put()` returns; loader retrieves at end. Still `list[str]` in memory.
  - **(c)** Required streaming `Iterable[str]` on `LoadResult`. Breaks v1 contract; every existing loader updates. Solves scale but keeps the opt-in footgun in a new form (loaders that yield zero IDs silently break prune).
  - **(d)** Hybrid: keep `list[str]` opt-in AND add `client.register_entities(iter)` streaming opt-in. Two paths, dual source of truth.
- **Chosen:** **(a)**, with explicit `with client.load_context(loader_name, version):` plumbing at the lifecycle boundary in `hippo/cli/commands/reference.py`. Loader code does not interact with the log; `client.put()` records a write-log row inside the `load_context` block and is a no-op outside it. Table schema and GC policy are specified in [sec2 §2.14.9](sec2_architecture.md).
- **Why:** `--prune-old` is the only mechanism that bounds disk growth across release upgrades. v1's opt-in `LoadResult.entity_ids` made it a property of how careful each loader author was, not a property of the platform — for a multi-organism Ensembl install (~234k entities single-organism, multi-seven-figure across the species set), that's the wrong abstraction layer. (a) puts the responsibility on Hippo, which is the only layer that has the loader context (`mosaic_meta.reference_versions` is written here) AND every write going through `MosaicClient.put()`. (c) and (d) push the problem partly back onto loader authors. (b) doesn't solve the scale problem from #15 failure mode 2. The author's weak preference matches (a); their compatible fallback (d) loses on the dual-source-of-truth concern.
- **Plumbing — ContextVar vs. explicit `with` vs. wrapper client.** Three substrate-equivalent options for binding `(loader_name, version)` to `client.put()`. Chose explicit `with client.load_context(...)` over ContextVar (obscures the boundary at the call site) and over a `_LoggingClient` wrapper (requires `MosaicClient` to be wrap-friendly for one caller). The lifecycle call is one stack frame deep in `install_reference()` / `upgrade_reference()`; the `with` is auditable in code review and reads cleanly at `hippo/cli/commands/reference.py:516`.
- **Failure recovery.** Write-log inserts share the entity write's SQL transaction. Mid-load exceptions roll back; orphan log rows never persist beyond committed entity writes. Loaders that batch with explicit commits own their retry semantics — Hippo does not retry on their behalf. Documented in [sec2 §2.14.9](sec2_architecture.md).
- **Consequences:** New table `reference_write_log` lands in `migration.py` for both SQLite and Postgres adapters. `MosaicClient` gains `load_context()` context manager (no API change to `put()`). `hippo/cli/commands/reference.py` swaps `_write_versions(..., load_result.entity_ids)` for the log-driven path. `mosaic_meta.reference_entity_ids` is deprecated and migrated on first v2 startup (read JSON, write rows, delete key — idempotent). Open question for v3: `keep_if_unchanged` policy for stable-id upgrade overlap (Ensembl `Gene.stable_id` pattern); v2 documents the constraint and accepts the constraint that `--prune-old` on overlap removes the entity, to be re-written by the new install.
- **Revert:** Drop the table and the `load_context` plumbing; restore `_write_versions(..., entity_ids)` and the `mosaic_meta.reference_entity_ids` JSON blob. Reintroduce the opt-in `entity_ids` field on `LoadResult`. High blast radius for downstream loaders that started relying on platform-managed prune; trivial blast radius today (zero downstream).

### Decision 2.14.K — `EntityRef` adopted on `LoadResult.entities` only; `MosaicClient.put()` return shape unchanged [NEW 2026-05-22]

- **Alternatives considered:**
  - **(1)** Adopt `EntityRef(id, type)` as a new public dataclass, bundle-change `MosaicClient.put() -> EntityRef`, replace `LoadResult.entity_type` + `entity_ids` with `LoadResult.entities: list[EntityRef]`. (Author's proposal.)
  - **(2)** Adopt `EntityRef` on `LoadResult.entities` only; keep `MosaicClient.put()` returning `dict[str, Any]`. Provide `EntityRef.from_put_result(rec)` classmethod to bridge.
  - **(3)** Widen `load()` return to `LoadResult | Iterable[LoadResult]`. Author rejected this; lifecycle layer would normalize a second return shape.
  - **(4)** Introduce a `LoadSummary` wrapper holding zero or more `LoadResult`s. Author rejected; flattening matches the data better than nesting.
- **Chosen:** **(2)**. Bundle is declined. Reasoning: a codebase audit at PTS-243 time found `MosaicClient.put()` callers index `result["data"]`, `result["version"]`, `result["entity_type"]`, `result["created_at"]`, and `result["updated_at"]` across ~30 test sites, the `TypedClient` public API (`hippo/src/mosaic/core/typed_client.py`), and the ingestion CLI pipeline (`hippo/src/mosaic/core/loaders/pipeline.py` chains into `register_external_id(created["id"], ...)`). The author's premise that the `dict[str, Any]` return was "a placeholder" is contradicted by the call-site evidence. Returning only `(id, type)` from `put()` would either silently break those consumers or require co-evolving them in this round — a much larger change than the GitHub #16 framing implied.
- **Why split the bundle.** GitHub #16 §"Asks" explicitly named the bundle (`EntityRef` type + `client.put()` return + `LoadResult.entities`) as a single accept-or-reject decision. We accept two of three. `EntityRef.from_put_result(rec)` makes the loader code one line longer than the bundled version (`entities.append(EntityRef.from_put_result(client.put(...)))` vs. `entities.append(client.put(...))`), which is acceptable in exchange for not co-evolving 30+ call sites in a follow-up of a follow-up. A future v3 decision may revisit `put()`'s return type with its own migration story.
- **Counter fields (Q2.3 in the issue body).** Counters stay scalar — `created`, `updated`, `unchanged`, `errors`, `error_messages`. They answer "what happened." `entities` answers "what was written (advisory)." (Option (a) in the GitHub #16 framing.) Rejected (b) outcome-enum-per-EntityRef: doubles per-item size; conflates substrate-for-prune with what-happened. Rejected (c) multiple lists: verbose; awkward when one is dominant.
- **`LoadResult.entities` is advisory.** Spec [sec2 §2.14.8](sec2_architecture.md) marks `entities` as advisory only. Large loaders are encouraged to leave it empty; the write log (D2.14.J) is the authoritative prune substrate. The CLI breakdown queries the log when `entities` is empty, so loader authors don't have to choose between ergonomic CLI and scale.
- **Consequences:** New public `EntityRef` dataclass in `hippo/core/loaders/reference.py` (sibling to `LoadResult` / `ReferenceLoader`). `LoadResult.entities: list[EntityRef]` replaces `entity_type` + `entity_ids` (see D2.14.L for the transition). `MosaicClient.put()` signature and return shape unchanged. Loader patterns documented in [sec2 §2.14.8](sec2_architecture.md).
- **Revert:** Adopt (1) the bundle in a later round. Would require co-evolving `TypedClient.create()`, `TypedClient.put()`, `IngestionService.upsert_by_external_id()`, and ~30 test assertions on the dict shape. Net new tasks; not free, but bounded.

### Decision 2.14.L — `LoadResult` v2: clean break from `entity_type` + `entity_ids` [NEW 2026-05-22]

- **Alternatives considered:**
  - **(i)** Replace `entity_type` + `entity_ids` outright with `entities: list[EntityRef]`.
  - **(ii)** Add `entities` alongside, deprecate `entity_type` and `entity_ids` with a release-version warning, derive old fields from new for back-compat.
  - **(iii)** New primary field + `@property` derived back-compat (only works when `entities` is single-typed; raises on heterogeneous list).
- **Chosen:** **(i)** clean break. Reasoning: downstream `mosaic-reference-*` package count at v2 cut-over is zero. The in-tree fixtures (`FakeReferenceLoader`, `RichParamsLoader`, `BareParamsLoader`) and the lifecycle code in `hippo/cli/commands/reference.py` are the entire migration surface. `hippo-takifugi` (last updated 2026-03-27) is a pre-v1 exploration repo, not a shipped loader. The Ensembl prototype team explicitly held off shipping pending the PTS-243 decision (per the PTS-215 thread). A deprecation cycle for a contract that nobody has built against burns reviewer attention and release-note budget for no real users.
- **Why:** Author's recommendation in GitHub #16 §"Breaking change story" was (ii) "unless the maintainers know the loader count is zero today; in that case (i) is fine." We verified loader count is zero and took the clean cut.
- **Consequences:** Mechanical migration of fixtures (drop `entity_type`, swap `entity_ids` → `entities`). `cli/commands/reference.py` install/upgrade paths drop `_write_versions(..., entity_ids)` in favor of the write-log path (D2.14.J). Tests in `tests/cli/test_reference_install_upgrade.py` and `tests/core/test_reference_loader.py` update to assert on `entities` and on write-log contents instead of `entity_ids`. The `--prune-old` no-IDs error message (`"loader did not populate LoadResult.entity_ids at install time"`) is removed because prune now works for every loader.
- **Revert:** Re-add `entity_type` and `entity_ids` to `LoadResult` and the lifecycle. Restore the v1 error path on `--prune-old` when `entity_ids` is empty. Mechanical; low blast radius today.

---

## 11. SchemaPackage Abstraction (sec11)

Decisions for the SchemaPackage genus / species split and associated migration chain. Sprint mapping: S0 = genus + back-compat; S2 = DomainModule + single-hop; S3 = chain resolver + deprovision; S4 = orchestrator + gate + bundle.

### Decision 11.A — SchemaPackage genus with ReferenceLoader + DomainModule as sibling species [COMMITTED]

- **Finding:** `ReferenceLoader` conflates two concerns: (1) versioned schema contribution (general — any reusable module needs it) and (2) external-data ingestion (specific — only loaders that fetch external datasets need it). Pure-schema modules that `required` a no-op `load()` surfaced the smell.
- **Alternatives considered:** (A) Keep a flat `ReferenceLoader` ABC, document that no-op `load()` is acceptable. (B) Add a separate `SchemaModule` ABC alongside `ReferenceLoader` with no data methods. (C) Abstract the common concern into a genus `SchemaPackage`; `ReferenceLoader` and a new `DomainModule` become sibling species.
- **Chosen:** (C). The genus carries everything a versioned, pinnable schema contributor needs (`name`, `versions()`, `schema_fragment()`, `depends_on()`, three no-op lifecycle hooks). Species add data behaviors via capability protocols (`ExternalData`, `MigratableData`).
- **Why (A) rejected:** Keeps the design smell permanent; no-op methods mislead future authors. **(B) rejected:** Separate ABC with no common lifecycle means the orchestrator needs two dispatch paths for what is logically one operation. (C) unifies dispatch while keeping the species independent.
- **Back-compat:** `ReferenceLoader` remains the public API for external-data loader authors; `mosaic.reference_loaders` entry point continues to work as a subset/alias of `mosaic.schema_packages`. No existing loader needs to change.
- **Revert:** Collapse back to a flat `ReferenceLoader` ABC with documented no-op `load()`; remove `DomainModule` (domain migration moves back to ad-hoc scripts). High blast radius on the S2–S5 sprint deliverables.

### Decision 11.B — Three lifecycle hooks default to no-op; populates_types() is a species concern [COMMITTED]

- **Finding:** Forcing pure-schema packages to implement `load()` was the original design smell. The genus needs a lifecycle surface that all species can share, but it must not require data-specific overrides.
- **Alternatives considered:** (A) Abstract methods on the genus (all species must override). (B) No hooks on the genus; species define their own independently. (C) Hooks on the genus defaulting to no-op; species override as needed.
- **Chosen:** (C). `provision`, `evolve`, `deprovision` are on `SchemaPackage` with default no-op implementations. `populates_types()` (formerly `entity_types()`) is removed from the genus and lives on species that carry data. Orchestrator can call `provision`/`evolve`/`deprovision` on any `SchemaPackage` without checking species type — the default no-op makes pure-schema packages safe to dispatch.
- **Why:** (A) would force every pure-schema package to write three stub overrides — the same smell as the current no-op `load()`. (B) breaks unified orchestrator dispatch.
- **Revert:** Promote all three hooks to abstract methods; add no-op stubs to every existing package. Low blast radius for existing packages; breaks the "pure-schema packages need no data methods" design principle.

### Decision 11.C — ExternalData and MigratableData as capability protocols, not a deep inheritance chain [COMMITTED]

- **Finding:** Placing data behaviors on `ReferenceLoader` and `DomainModule` via protocol mixin is the standard Python typing pattern and avoids forcing a class to inherit from multiple concrete base classes.
- **Alternatives considered:** (A) `SchemaPackage → DataPackage → ReferenceLoader` and `SchemaPackage → DataPackage → DomainModule` (three-level chain). (B) `ReferenceLoader(SchemaPackage, ExternalData)` and `DomainModule(SchemaPackage, MigratableData)` — protocols at the leaf level (sibling position).
- **Chosen:** (B). Protocols carry typing and orchestrator dispatch; the species hierarchy carries identity. `ExternalData` and `MigratableData` are siblings; neither implies the other.
- **Why (A) rejected:** A `DataPackage` intermediate class implies that all data packages share behavior beyond the three hooks — currently untrue. Adds complexity without a present use case.
- **Revert:** Introduce `DataPackage` as an intermediate or merge protocols into the genus. Low blast radius today; higher if the S2–S4 sprint builds against the protocol interface.

### Decision 11.D — Migration DAG with explicit edges, shortcut edges, and a floor; not sorted version slugs [COMMITTED]

- **Finding:** Version slugs are opaque strings per sec2 §2.14.C (Decision 2.14.C). Sorting them to derive a migration path is undefined behavior. Multi-hop upgrades (1.2.0 → 2.0.0 across intermediate versions) must be expressed as explicit edges in a DAG where the resolver finds the optimal path.
- **Alternatives considered:** (A) Sort version slugs and run each consecutive transition. (B) Require authors to ship a single `evolve(from, to)` that handles arbitrary version pairs (no DAG). (C) Alembic-style DAG of discrete `(from → to)` steps, all shipped with the current package version, with optional shortcut edges.
- **Chosen:** (C). All steps covering the package's supported range ship with the package at its current version. A **floor** marks the oldest supported migration source; below-floor fails loud with an actionable message. Shortcut edges let authors provide an efficient direct transform when one exists (e.g., a bulk SQL rewrite that is faster than running N intermediate steps).
- **Why (A) rejected:** Opaque-slug sorting is undefined. **(B) rejected:** An `evolve(1.2.0, 2.0.0)` that handles arbitrary ranges is impossible to test exhaustively; authors end up re-deriving the DAG implicitly.
- **Consequences:** S3 implements the path-resolver reading step metadata from the package; the resolver is not a separate service. Below-floor failure message must be actionable: name the floor version and the package.
- **Revert:** Revert to (B) with a single `evolve(from, to)` and document that authors must handle arbitrary ranges. High blast radius for S3.

### Decision 11.E — DomainModule.deprovision refuses by default; ReferenceLoader.deprovision prunes [COMMITTED]

- **Finding:** `ReferenceLoader` data is externally sourced and reconstructible; soft-deleting it on uninstall is safe. `DomainModule` data is the deployment's own operational records; silent soft-deletion on uninstall is a data loss event inconsistent with no-hard-deletes policy (sec3).
- **Alternatives considered:** (A) Both species prune on deprovision. (B) Both species refuse on deprovision. (C) Species-specific: `ReferenceLoader` prunes (safe, reconstructible); `DomainModule` refuses by default (live domain data must not be lost without explicit acknowledgement).
- **Chosen:** (C). `DomainModule.deprovision` raises a descriptive error unless `--force` is passed (and export is recommended). `ReferenceLoader.deprovision` soft-deletes rows by `provided_by` stamp.
- **Why (A) rejected:** Loses domain data without warning — violates the no-hard-deletes and no-silent-loss principles. **(B) rejected:** Prevents legitimate reference-data cleanup that `--prune-old` already supports.
- **Additional guard:** The orchestrator refuses to deprovision any package that other installed packages declare in their `depends_on()`, regardless of species. Avoids stranding dependents.
- **Revert:** Normalize both species to prune (A) or refuse (B). Either requires revisiting the design principle about data ownership and reconstructibility.

### Decision 11.F — mosaic.schema_packages entry-point group; mosaic.reference_loaders becomes a subset/alias [COMMITTED]

- **Finding:** The `mosaic.reference_loaders` entry-point group is the current discovery mechanism. Widening the abstraction to `SchemaPackage` requires a new group name, but renaming the old group would break every existing loader's `pyproject.toml`.
- **Alternatives considered:** (A) Rename `mosaic.reference_loaders` to `mosaic.schema_packages`. (B) Keep `mosaic.reference_loaders` only; pure-schema packages must also use it (confusing name). (C) Add `mosaic.schema_packages`; the discovery runtime queries both, deduplicates by name, and treats `mosaic.reference_loaders` entries as `ReferenceLoader` instances.
- **Chosen:** (C). Existing loaders need no entry-point change. New packages (pure-schema `SchemaPackage`, `DomainModule`) register under `mosaic.schema_packages`. The discovery runtime merges both groups.
- **Why:** Preserves zero-change back-compat for all existing loader authors.
- **Revert:** Remove `mosaic.schema_packages`; require all packages to register under `mosaic.reference_loaders` with a documented naming convention. Forces breaking change on future `DomainModule` authors.

### Decision 11.G — Dependency-ordered orchestrator: topological sort per hop, not global install-order [COMMITTED]

- **Finding:** `loader_depends_on` is documentation-only at runtime today (sec2 §2.14.H). The S4 orchestrator is the first runtime consumer. The dependency graph must be resolved once per operation (not per-package), and ordering must apply within each version hop, not just globally.
- **Alternatives considered:** (A) Global install order (topological sort once, run all packages). (B) Per-hop ordering: at each version hop, run affected packages in topological order. (C) Leave ordering to the author (document but don't enforce at runtime).
- **Chosen:** (B). Per-hop ordering ensures an extension's `evolve` always sees the base at that hop's shape, not at the final shape. A coordinated multi-package upgrade over N hops runs `N × (packages in topo order)` evolves.
- **Why (A) rejected:** A global sort runs extension packages against a base that may not yet be at the intermediate shape — extension migration steps would operate against wrong-version base data. **(C) rejected:** Contradicts the goal of making the orchestrator the safe, dependency-aware upgrade path.
- **Open (§8 / [VERIFY]):** Whether any existing runtime path already orders by `loader_depends_on`. If so, S4 can delegate to it; if not, S4 must implement topological sort. Resolve before S4.
- **Revert:** Remove per-hop ordering; run packages in registration order. High blast radius on deployments where extension steps depend on base shape at intermediate versions.
