## 9. LinkML-Centric Redesign

**Document status:** Draft v0.1
**Depends on:** sec2_architecture.md, sec3_data_model.md, sec3b_relational_storage.md, sec6_provenance.md
**Feeds into:** sec1_overview.md, sec2_architecture.md, sec3_data_model.md, sec3b_relational_storage.md, sec4_api_layer.md, sec5_ingestion.md, sec6_provenance.md, sec7_nfr.md, appendix_a_example_schema_omics.md, appendix_b_implementation_guide.md, reference_mosaic_yaml.md, reference_validators_yaml.md, reference_mosaic_core.md, reference_mosaic_ext.md

---

### 9.1 Motivation & Goals

Hippo's schema system predates its adoption of LinkML. The original design shipped a bespoke trio — `SchemaConfig`, `SchemaParser`, `FieldDefinition` — that duplicated concepts LinkML had already formalized: schema loading, slot definitions, type ranges, inheritance, constraints. Direct LinkML authoring was adopted mid-course (commits `49a11c9` "rewrite all schema examples to valid LinkML format" and `04f1448` "remove Hippo DSL — rename to EntityYAML, delete compiler") — a substantial correction, applied incrementally.

The trajectory that began with those commits has been extending ever since through a series of schema-adjacent refactors. This redesign is its natural endpoint: sec9 names the destination so we can reach it deliberately, not by gradual accretion, and so downstream decisions — provenance modeling, annotation vocabulary, typed client surface — can be made coherently rather than one refactor at a time.

**Current pain points.** The adoption so far has covered user schemas and the schema-loading pipeline. Hippo's own internal concepts — `Entity`, `ProvenanceRecord`, status enums, validator definitions — and several schema-adjacent behaviors still sit outside the LinkML model. Concretely:

- **Duplicated concepts.** `FieldDefinition` and its adjacent config types shadowed LinkML's `SlotDefinition`; two representations of the same idea had to be kept in sync by hand. Most have been deleted, but analogous duplication persists for `ProvenanceRecord` and the entity base concept.
- **Hand-rolled tooling.** DDL generation, schema diff, and schema-evolution plumbing are implemented from scratch where LinkML ships mature equivalents (`gen-sqlddl`, `linkml-diff`).
- **Stringly-typed annotations.** `mosaic_*` annotations are flat keys inside LinkML `annotations:` blocks — no formal vocabulary, no versioning, no spec. Adding a new annotation is a convention, not a contract.
- **Provenance outside the schema.** `ProvenanceRecord` is modeled entirely outside the schema system — its shape cannot be introspected, validated, evolved, or queried through the same tooling as entity data.
- **No typed client.** Callers interact with entities as generic dicts and kwargs, with no IDE, type-checker, or refactor support.
- **No formal schema validation.** Schema correctness is checked by ad-hoc Python, not by LinkML's own schema-level checkers.
- **No formal data validation.** Runtime instances are not validated against the schema through a single standard pipeline; type, pattern, enum, and range checks are partial and inconsistent.

**Goals.** The redesign is guided by principles, not a rewrite schedule. Every goal below is a principle that should survive the specific change sequence that implements it.

- **Single source of truth.** The LinkML schema describes data shape for everything Hippo models — user schemas *and* Hippo's own primitives (`Entity`, `ProvenanceRecord`, status/operation enums, validator definitions).
- **No shadow abstractions.** Pre-LinkML abstractions that duplicate or shadow LinkML equivalents are eliminated; the LinkML representation is preferred in every case.
- **Adopt, don't rebuild.** LinkML's generator and validator ecosystem is used wherever it meets Hippo's requirements; custom code is justified only by a genuine expressiveness gap.
- **Provenance in the schema system.** `ProvenanceRecord` is a first-class LinkML class and benefits from the same tooling — DDL generation, introspection, validation, diff — as user data.
- **Annotations as contracts.** The `mosaic_*` vocabulary is formalized as a versioned LinkML extension schema; annotations become contracts rather than conventions.
- **Typed client surface.** A typed client is generated from the merged schema, offered alongside (not in place of) the generic dynamic client.
- **Preserve what works.** The existing three-layer architecture, SDK-first layering, plugin storage adapters, CEL for dynamic and cross-entity rules, and provenance-log-only temporal data all carry forward.
- **Uniform introspection.** Anything describable in LinkML is discoverable through one pipeline — entity definitions, provenance structure, and validator metadata alike — including at the REST introspection surface.

---

### 9.2 Design Principles

These principles are the decision rules that apply when implementing and evolving sec9. They give a coding agent a consistent set of tie-breakers when a concrete choice arises. 9.1 states what the redesign pursues; 9.2 states how to pursue it.

**Schema as source of truth.**

- **Schema-first.** Any new data concept — entity class, slot, enum, annotation — is expressed in LinkML first. Implementation code follows the schema, never the reverse. A change that adds behavior without a corresponding schema change is a defect.
- **Prefer LinkML tooling over bespoke code.** If LinkML ships a generator or validator for the task (`gen-sqlddl`, `gen-pydantic`, `linkml-validate`, `linkml-diff`), use it. Custom Python is justified only by a documented expressiveness gap — see 9.10.
- **No shadow abstractions.** Do not introduce Python classes that mirror LinkML concepts (as `FieldDefinition` once did for `SlotDefinition`). If a typed Python view of schema data is needed, it is generated from the schema, not hand-authored.

**Layering.**

- **One merged schema at runtime.** Exactly one merged `SchemaView`, produced by the `SchemaRegistry` from the three layers. Code reads from it. Code never constructs parallel schema representations.
- **Layer independence.** `mosaic_ext` imports nothing. `mosaic_core` imports `mosaic_ext`. User schemas import `mosaic_core`. Imports flow downward only — no sideways, no upward.
- **Annotations are contracts.** Every `mosaic_*` annotation used in a user schema MUST be declared in `mosaic_ext`. Undocumented annotations are a defect, not a convention.

**Subsystem behavior.**

- **Validation is tiered.** Static shape (types, patterns, enums, ranges, required, multivalued) is validated by LinkML. Dynamic and cross-entity rules are validated by CEL. Python validators exist only for capabilities neither expresses — see 9.9.
- **Provenance is data.** `ProvenanceRecord` is processed by the same DDL generation, validation, introspection, and migration code paths as user entities. No provenance-shape-aware special cases in Python — see 9.6.
- **Provenance integrity is transactional and loud.** A mutation that cannot emit a valid `ProvenanceRecord` MUST fail atomically — the entity write does not complete. Missing, partial, or inconsistent provenance detected at read time raises an error rather than degrading silently. Incomplete provenance is a critical data-integrity defect, not a degraded state — see 9.6, 9.7.
- **Temporal fields are read-time.** `created_at`, `updated_at`, and `schema_version` are never stored as columns on entity tables; they are always computed from the provenance log — see 9.7.
- **Typed and dynamic clients are coequal.** Every SDK capability is reachable from both the Pydantic-generated typed client and the generic dynamic `MosaicClient`. Neither is the "real" one — see 9.8.
- **Identifiers are globally unique UUIDs.** Every entity `id` is a UUID, unique across the system by collision probability. Polymorphic references (`entity_id`, `process_id`, `actor_id`, `parent_process_id`, `derived_from_id`) carry a UUID alone — no tagged union, no prefix convention. Type resolution is adapter-specific: a lightweight registry table in relational adapters, native labels in Neo4j — see 9.5.

**Evolution discipline.**

- **Additive before breaking.** Schema changes are additive (new optional slot, new class, new annotation) unless a breaking change is explicitly scoped in an OpenSpec proposal. Breaking changes to `mosaic_core` or `mosaic_ext` are major events and carry version bumps.
- **SDK-first layering is preserved.** Transport layers (REST, future GraphQL) and storage adapters contain no schema logic; they call the Core SDK. This principle carries forward from sec2 and is not revisited by sec9.

---

### 9.3 Three-Layer Schema Stack

Hippo's schema data lives in three LinkML schemas layered by import. Each layer has a clear ownership and a single role. Together they form the one merged `SchemaView` consumed by every subsystem.

```
┌──────────────────────────────────────────────────────────────┐
│  user_schema.yaml          (per-deployment)                  │
│  · domain classes (Sample, Project, Aliquot, ...)            │
│  · is_a: Entity on every domain class                        │
│  · imports: mosaic_core                                       │
└──────────────────────────┬───────────────────────────────────┘
                           │  imports
┌──────────────────────────▼───────────────────────────────────┐
│  mosaic_core.yaml           (ships with Hippo)                │
│  · Entity, ProvenanceRecord, Process, Validator,             │
│    ReferenceLoader                                           │
│  · Status, Operation enums                                   │
│  · imports: mosaic_ext                                        │
└──────────────────────────┬───────────────────────────────────┘
                           │  imports
┌──────────────────────────▼───────────────────────────────────┐
│  mosaic_ext.yaml            (ships with Hippo)                │
│  · annotation vocabulary: mosaic_unique, mosaic_index,         │
│    mosaic_index_partial, mosaic_search, mosaic_append_only,     │
│    mosaic_accessor                                            │
│  · imports: nothing                                          │
└──────────────────────────────────────────────────────────────┘
```

**Layer contents.**

| Layer | Ships with | Purpose | Imports |
|---|---|---|---|
| `mosaic_ext` | Hippo | Formal vocabulary for `mosaic_*` annotations used in user schemas | none |
| `mosaic_core` | Hippo | `Entity` base class, `ProvenanceRecord`, `Process`, `Validator` and `ReferenceLoader` classes, `Status` / `Operation` enums | `mosaic_ext` |
| user schema | deployment | Domain classes expressing what a deployment tracks | `mosaic_core` |

Deployments author only the user schema. `mosaic_core` and `mosaic_ext` are shipped artifacts; they change only through Hippo releases and carry their own versions (see below, 9.4, 9.5).

**Layering rules.**

- Imports are unidirectional (user → core → ext) and transitive. No schema may import a higher layer; no schema may import sideways within a layer. (See 9.2 *Layer independence*.)
- `mosaic_core` MUST NOT reference any domain class. A domain concept appearing in `mosaic_core` is a layering violation.
- User schemas MUST NOT redefine classes, slots, enums, or annotations owned by `mosaic_core` or `mosaic_ext`. Extending via `is_a` or `slot_usage` is allowed; replacing is not.
- Every `mosaic_*` annotation used in any schema MUST be declared in `mosaic_ext`. An undeclared annotation is a defect. (See 9.2 *Annotations are contracts*.)

**Runtime merging.**

At startup, `SchemaRegistry` loads the three layers through LinkML's `SchemaView` and produces one merged view. Every downstream subsystem — DDL generation, validation pipeline, typed-client generation, REST introspection — reads from this merged view. No subsystem loads LinkML schemas directly; no subsystem caches parallel derived state.

The merged view is the only schema representation held in memory. Requests for per-layer slices (e.g., "only the user classes") are served by filtering the merged view, not by re-parsing.

**Versioning semantics.**

Each layer declares a LinkML `version:` attribute. Bump discipline:

| Layer | Additive (minor bump) | Breaking (major bump) |
|---|---|---|
| `mosaic_ext` | New annotation; documentation refinement | Renamed or removed annotation; semantic change to an existing annotation |
| `mosaic_core` | New optional slot on `Entity`, new enum value, new class | Change to existing class shape, newly required slot, enum value removal, class removal |
| user schema | New class, new optional slot, new enum value | Removed or renamed class/slot, newly required slot with existing rows, enum value removal |

`mosaic_core` declares which `mosaic_ext` major version it targets; a user schema declares which `mosaic_core` major version it targets. `SchemaRegistry` refuses to merge against an incompatible major version and reports the mismatch at load time — a running Hippo instance therefore pins a specific `(mosaic_ext, mosaic_core)` pair, and user schemas are validated against that pair before any data operation.

Any breaking change to `mosaic_core` or `mosaic_ext` is a major event and requires an OpenSpec proposal scoping the migration path (see 9.12).

---

### 9.4 `mosaic_ext` Extension Vocabulary

`mosaic_ext` is a LinkML schema whose sole role is to declare the `mosaic_*` annotations consumed elsewhere in the stack. It owns no classes, no slots, and no enums — it is a vocabulary schema.

Before this redesign, `mosaic_*` keys appeared directly inside LinkML `annotations:` blocks on user schemas with no formal declaration. `mosaic_ext` replaces that convention with a contract: every annotation name, its value type, which elements it may attach to, and its semantics are declared in one place and version-controlled.

**Declaration pattern.**

Each annotation in `mosaic_ext` is declared with, at minimum:

| Attribute | Purpose |
|---|---|
| `name` | The bare annotation key (e.g., `mosaic_index`). The `mosaic_` prefix is literal; no implicit prefixing. |
| `value_type` | LinkML type of the annotation value (boolean, string, integer, enum). |
| `applies_to` | Which LinkML element types the annotation is valid on: class, slot, enum, permissible_value, or a combination. |
| `cardinality` | Whether the annotation is a singleton or multivalued on a given element. |
| `default` | Value assumed when the annotation is omitted, if any. |
| `description` | Human-readable semantics. |

**Usage pattern.**

A user schema applies an annotation from `mosaic_ext` via an `annotations:` block on the target element. At schema-load time, `SchemaRegistry` validates that every applied annotation is declared in `mosaic_ext`, that its value type matches the declaration, and that its `applies_to` permits the target element. Undeclared annotations, mistyped values, and wrong-target attachments are reported as load-time errors. No annotation is silently ignored.

**Current vocabulary.**

| Annotation | Applies to | Value type | Semantics |
|---|---|---|---|
| `mosaic_unique` | slot | boolean | Emit a single-column `UNIQUE` constraint for this slot. Composite uniqueness uses LinkML-native `unique_keys`. |
| `mosaic_index` | slot | boolean | Emit a single-column index for this slot. |
| `mosaic_index_partial` | slot | boolean | When `mosaic_index: true`, emit the index as partial with `WHERE is_available = 1`. Ignored if `mosaic_index` is false. |
| `mosaic_search` | slot | string | Include this slot in a full-text index of the declared mode. Adapter enforces which modes it supports (current canonical mode: `fts5`). |
| `mosaic_append_only` | class | boolean | Storage adapter MUST reject updates and deletes on rows of this class. Used by `ProvenanceRecord` (see 9.6). |
| `mosaic_accessor` | class | string | Override the derived typed-client accessor name. Optional escape hatch used only when the default derivation produces a collision or when a deployment prefers a non-default name. See 9.8. |

The authoritative reference — default values, exact enum ranges, interactions between annotations — lives in `reference_mosaic_ext.md`.

Some capabilities intentionally have no `mosaic_*` annotation because LinkML covers them natively. Default values use LinkML's `ifabsent` (literal values or expressions like `uuid()`, `datetime(now)`, `int(0)`). Composite uniqueness uses LinkML's `unique_keys`. Required slots use LinkML's `required: true`. An agent implementing a user schema should reach for the LinkML-native attribute first and only use a `mosaic_*` annotation when no LinkML equivalent exists.

**Extensibility.**

Adding a new annotation is a four-step change, scoped in an OpenSpec proposal:

1. Declare the annotation in `mosaic_ext` (name, value type, `applies_to`, cardinality, default, description).
2. Implement its effect in the subsystem that consumes it (DDL generator, validator, typed-client generator, etc.).
3. Document it in `reference_mosaic_ext.md`.
4. Bump `mosaic_ext` minor version.

Removing or renaming an annotation bumps `mosaic_ext` major version and requires an OpenSpec proposal that scopes the migration path for user schemas using the affected annotation.

The vocabulary above is the starting set. `mosaic_ext` is expected to grow as new cross-cutting concerns surface; it is not a closed list.

---

### 9.5 `mosaic_core` Schema

`mosaic_core` is the LinkML schema that declares Hippo's built-in classes and enums. Every user schema imports `mosaic_core`; every domain class uses `is_a: Entity`. `mosaic_core` carries no domain knowledge — it is the minimal set of primitives the rest of the system needs to operate.

**Class and enum inventory.**

| Element | Kind | Purpose |
|---|---|---|
| `Entity` | abstract class | Base class for all domain entities. Provides `id` and `is_available`. |
| `ProvenanceRecord` | class | Immutable record of an operation on an entity. Drives the audit log, computed temporal fields, and schema-version tracking. Detailed in 9.6. |
| `Process` | class | Composite activity — a grouping of atomic operations under one logical execution (reference loads, pipeline runs, migrations). Detailed below. |
| `Validator` | class | Declarative validator definition. Instances are loaded from `validators.yaml` and validated against this class at load time. |
| `ReferenceLoader` | class | Metadata for a reference-data loader plugin. Instances are contributed by plugins registered under the `mosaic.reference_loaders` entry point. |
| `Status` | enum | Lifecycle state of an entity: `active`, `archived`, `superseded`, `deleted`, `distributed`, `removed`. |
| `Operation` | enum | Operation kinds recorded in `ProvenanceRecord`. Full set enumerated in 9.6. |

The authoritative slot-level reference lives in `reference_mosaic_core.md`. The summaries below cover design-relevant slots; field-level details (types, cardinality, examples) are deferred.

**Identity model.**

Every `id` slot across the system carries a UUID (v4 random or v7 time-ordered; SDK-assigned on create, opaque to callers). UUIDs are globally unique by collision probability; no Hippo-specific uniqueness enforcement is required beyond the per-class primary-key constraint each adapter emits.

Polymorphic references — slots whose target class is not fixed at schema-definition time — are uniformly UUID-typed. `ProvenanceRecord.entity_id`, `ProvenanceRecord.actor_id`, `ProvenanceRecord.process_id`, `ProvenanceRecord.derived_from_id`, and `Process.parent_process_id` are all of this form. A caller holding a UUID can resolve it to a specific entity class without any information beyond the UUID itself.

Type resolution is adapter-specific:

| Adapter | Mechanism | Cost |
|---|---|---|
| SQLite / PostgreSQL (relational) | A lightweight `_entity_registry` table keyed on `id`, populated in the same transaction as every entity create. Maps `id → entity_type`. Indexed on `id` (primary key) and optionally on `entity_type`. | ~O(log n) indexed lookup; well-cacheable; ~10% write overhead on create. Negligible at Hippo's target scale. |
| Neo4j (graph) | Native node labels. Every node carries its concrete class label plus a shared `:Entity` label. `MATCH (n:Entity {id: $uuid}) RETURN labels(n)` resolves both the node and its type in one operation. | Native index lookup; no separate registry maintained by the adapter. |
| Future adapters | Free to implement type resolution optimally for the backend; SDK contract is uniform. | — |

The SDK contract `client.get(uuid)` is uniform across adapters; only the internal resolution strategy differs. Foreign-key integrity for polymorphic slots is validated at the SDK layer on write, not at the adapter DDL level — adapters that can express conditional FKs (e.g., Neo4j via labels) MAY strengthen this, but the cross-adapter contract assumes SDK-level validation.

**Fully qualified class names.**

A class is referred to by its fully qualified name (FQN) — the namespace string joined to the class name with a dot. The final segment of an FQN is always the class name; everything before the last dot is the namespace string. This rule makes FQN parsing unambiguous: a parser always splits on the last dot.

- `Sample` — root-namespace class `Sample`. Equivalent to `root.Sample`.
- `tissue.Sample` — class `Sample` in namespace `tissue`.
- `assay.quant.Measurement` — class `Measurement` in namespace `assay.quant`.

Hippo namespace strings MAY contain dots for organizational convenience (see 9.8 for the typed-client consequences); dots in *class names* are forbidden and rejected at schema load. `root` is a reserved namespace name — a user schema MUST NOT declare `namespace: root`; root-namespace classes are declared by omitting the `namespace:` key or by a deployment convention that Hippo treats as equivalent.

FQNs appear on `ProvenanceRecord.entity_type` (9.6), in cross-class references, and in introspection APIs. The FQN rule above is the single source of truth for parsing them.

**`Entity`.**

`Entity` is abstract. It MUST NOT be instantiated directly; it exists to be specialized by user-schema classes via `is_a: Entity`. It carries exactly two slots:

| Slot | Type | Required | Semantics |
|---|---|---|---|
| `id` | string (identifier) | yes | Stable primary key. Assigned by the SDK on create; treated as opaque by callers. |
| `is_available` | boolean | yes | Lifecycle flag. `true` means the entity participates in normal reads; `false` means it has been superseded, archived, deleted, distributed, or removed. The reason lives in provenance, not on the entity. |

No temporal fields, no status enum, no foreign-key columns. Temporal state (`created_at`, `updated_at`, `schema_version`) is computed from `ProvenanceRecord` at read time (see 9.7); status transitions are recorded as `ProvenanceRecord` entries with an `availability_change` operation (see 9.6). Domain classes add their own slots and MAY annotate them with `mosaic_*` vocabulary from `mosaic_ext`.

**`ProvenanceRecord`.**

Detailed in 9.6. Summary: an append-only class annotated `mosaic_append_only: true`, with slots carrying the operation, the target entity, the actor, the timestamp, the schema version, and the change payload. It is DDL-generated by the same pipeline as user entities and uses formal PROV-O alignment.

**`Process`.**

`Process` represents a composite activity — a grouping of atomic operations that together constitute one logical execution. Reference-data loads, schema migrations, and caller-side activities like Cappella pipelines each emit one `Process` and associate their atomic `ProvenanceRecord` entries with it via the records' `process_id` slot (see 9.6).

`Process is_a Entity` and carries `class_uri: prov:Activity`. The shared PROV-O class URI across `Process` and `ProvenanceRecord` reflects PROV-O's treatment of activities at both composite and atomic scales — both are legitimately `prov:Activity`, differing only in granularity.

Minimum slot set for sec9 (callers specialize via `is_a: Process` to add domain-specific attributes such as pipeline parameters, input/output manifests, runtime metadata):

| Slot | Type | Required | Semantics |
|---|---|---|---|
| `id` | string (UUID) | yes | Inherited from `Entity`. |
| `is_available` | boolean | yes | Inherited from `Entity`. Processes are archived, not hard-deleted. |
| `parent_process_id` | string (UUID) | no | Self-reference for process composition. Processes form a tree; if a caller needs shared dependencies, the same mechanism supports a DAG. |
| `operation_kind` | string | yes | Caller-defined categorization (e.g., `reference_data_installed`, `pipeline_run`, `schema_migration`, `manual_edit`). Not a closed enum — deployments categorize freely. |
| `started_at` | datetime | yes | UTC wall-clock time at which the process began. |
| `ended_at` | datetime | no | UTC wall-clock time at which the process completed. Null while running or after failure without completion. |
| `actor_id` | string (UUID) | yes | Identifier of the entity responsible for initiating this process. Resolved via the identity model above. |

Processes themselves emit `ProvenanceRecord` entries for their own lifecycle (creation, state updates, archival). This is recursive but terminates: a root process has `parent_process_id: null`, and its own creation/update `ProvenanceRecord` entries have `process_id: null` (or are associated with an enclosing system-level process, caller's choice).

Caller specializations are encouraged to add domain-specific slots. Hippo_core intentionally does not enumerate inputs, outputs, or execution state machines — those are caller concerns.

**`Validator`.**

`Validator` is the LinkML-declared shape of an entry in `validators.yaml`. The config file declares *instances* of `Validator`; the runtime loads each instance and validates it against this class before registering it in the validation pipeline. Design-relevant slots:

- `name` — stable identifier for the validator.
- `entity_types` — domain class(es) the validator applies to; subtype-aware.
- `condition` — CEL expression returning boolean.
- `error_message` — message surfaced on failure.
- `when`, `expand`, `requires` — pre-condition, path pre-fetching, and ergonomic shorthand carried forward from the existing design (see `reference_validators_yaml.md`).
- `priority` — ordering hint when multiple validators apply.

LinkML-native type, pattern, enum, range, and required-slot checks run upstream of any `Validator`. A `Validator` exists only for logic that is dynamic or cross-entity — see 9.9.

**`ReferenceLoader`.**

`ReferenceLoader` exists in `mosaic_core` so that a deployment can answer the question "what reference-data loaders are installed, and what do they populate?" through the same introspection pipeline used for every other concept — `SchemaRegistry`, REST introspection, the typed client. Plugin code continues to live in Python and to register under the `mosaic.reference_loaders` entry point; `ReferenceLoader` is the introspectable metadata shape for each registered plugin.

The exact slot inventory is deferred to a subsequent OpenSpec change (see 9.12). Two design questions must be resolved before the shape is finalized:

- **Multi-class loaders.** A single reference-data source frequently populates several entity classes simultaneously (e.g., an ontology loader contributing `Organism`, `Tissue`, and `Assay`). The `entity_type` slot must be multivalued; the exact cardinality semantics (ordered? set?) and whether per-class metadata is carried alongside each entry remain open.
- **Referential boundary of `schema_fragment`.** A `ReferenceLoader` instance may reference classes declared in its own `schema_fragment`. Those classes do not exist in the merged `SchemaView` until the plugin's fragment is installed. The load-order contract — when the fragment is merged, when the `ReferenceLoader` instance is validated, what the error surface is if validation fails — requires a deliberate design, not an implicit ordering.

Until these are resolved, `ReferenceLoader` is treated as a placeholder entry in `mosaic_core` with no committed slot inventory. User-facing plugin registration continues unchanged from sec5; it is not blocked on this design.

**Enums.**

`Status` and `Operation` are closed enums in `mosaic_core`. Adding a value is additive (minor bump); removing a value is breaking (major bump) per 9.3.

**Design notes.**

- `Entity` is intentionally sparse. The SDK needs exactly `id` and `is_available` on every entity; everything else is either domain-specific (user schema) or derived from provenance (9.7). Resist adding slots to `Entity`.
- `mosaic_core` contains no foreign keys into user-schema classes. A relationship from a domain entity to its provenance is expressed via the `entity_id` slot on `ProvenanceRecord`, not by a reference in the opposite direction.
- `mosaic_core` MUST validate against itself. Its own shape satisfies the LinkML meta-model, and `SchemaRegistry` verifies this at load time. A malformed `mosaic_core` is a startup failure, not a silent degradation.

---

### 9.6 Provenance as a LinkML Class

`ProvenanceRecord` is declared in `mosaic_core` as a first-class LinkML class. It is DDL-generated by the same pipeline as user entities, validated against the same schema machinery, and introspectable through the same `SchemaRegistry` view. There is no provenance-specific code path in the core SDK.

**PROV-O alignment.**

`ProvenanceRecord` maps to the W3C PROV-O ontology via LinkML URI alignment. Each instance is a `prov:Activity`: an operation that occurred over a conceptually instantaneous period, associated with an agent, that generated, invalidated, or derived one or more `prov:Entity` instances.

| LinkML element | PROV-O URI | Role |
|---|---|---|
| `ProvenanceRecord` (class) | `prov:Activity` (atomic) | The operation itself — an atomic PROV-O activity. Composite activities are modeled by `Process` (see 9.5), which shares the same class URI at the composite scale. |
| `entity_id` (slot) | `prov:wasGeneratedBy` / `prov:wasInvalidatedBy` (operation-dependent) | Target entity. Direction of the relationship is implicit in the operation kind. |
| `actor_id` (slot) | `prov:wasAssociatedWith` | Identifier of the `prov:Agent` responsible for the operation. Resolves via the identity model in 9.5. |
| `timestamp` (slot) | `prov:endedAtTime` | Wall-clock time at which the operation completed. |
| `derived_from_id` (slot, optional) | `prov:wasDerivedFrom` | For supersession: identifier of the previous entity version. |
| `process_id` (slot, optional) | `prov:wasInformedBy` (conventionally) | Enclosing `Process` that contained this operation. Gives atomic activities their composite-activity context. |

Slots without a direct PROV-O equivalent (`operation`, `schema_version`, `patch`, `context`, `entity_type`) are Hippo-specific extensions; they sit alongside the PROV-O-aligned slots without interfering with PROV-O interpretation.

**Slot inventory.**

| Slot | Type | Required | Semantics |
|---|---|---|---|
| `id` | string (UUID) | yes | Stable identifier for this provenance record. |
| `entity_id` | string (UUID) | no | Identifier of the entity the operation targets. Null only for system operations (see `Operation` enum below). |
| `entity_type` | string | no | Fully qualified class name of the target entity at write time. Denormalized onto the record so audit queries don't need to resolve type through the identity registry. Null when `entity_id` is null. |
| `operation` | `Operation` enum | yes | Kind of operation. |
| `actor_id` | string (UUID) | yes | Identifier of the entity responsible for this operation. Resolves through the identity model (9.5) to an agent entity — a user-schema `User`/`Service` class, a `Process` (for autonomous execution), or a system entity. Deployments that need rich actor modeling define agent classes in their user schema; `mosaic_core` prescribes only the reference. |
| `timestamp` | datetime | yes | When the operation completed, in UTC. |
| `schema_version` | string | yes | Version of the merged schema at the moment of write. Captured by the SDK; not caller-supplied. |
| `derived_from_id` | string (UUID) | no | For supersession operations, the previous entity version. |
| `process_id` | string (UUID) | no | Identifier of the enclosing `Process` (9.5). Provides schema-level correlation for operations that belong to the same logical execution. Null for operations outside any `Process` (e.g., direct human edits). |
| `patch` | JSON blob | no | Operation-specific change payload (diff, new values, relationship target). Unstructured; schema intentionally open. |
| `context` | JSON blob | no | Caller-supplied contextual metadata. Structured at the caller's discretion; conventions documented in sec6. |

**`Operation` enum.**

| Value | Semantics | Associated PROV-O pattern |
|---|---|---|
| `create` | Entity was created. | `prov:wasGeneratedBy` (entity → this activity) |
| `update` | Entity's slot values were modified in place. | (Hippo extension; no direct PROV-O predicate for mutation) |
| `availability_change` | `is_available` flag transitioned. `patch` carries the new value and the driving `Status` enum value. | `prov:wasInvalidatedBy` when transitioning to unavailable |
| `supersede` | Entity superseded by another. `derived_from_id` carries the predecessor. | `prov:wasDerivedFrom` (new entity → predecessor) |
| `relationship_add` | A relationship slot gained a value. `patch` carries `{slot, target_id}`. | (Hippo-specific) |
| `relationship_remove` | A relationship slot lost a value. `patch` carries `{slot, target_id}`. | (Hippo-specific) |
| `external_id_add` | An ExternalID was associated with the entity. | (Hippo-specific) |
| `external_id_remove` | An ExternalID was disassociated. | (Hippo-specific) |
| `migration_applied` | A schema migration was applied. `entity_id` is null; system operation. | (Hippo-specific) |
| `reference_data_installed` | A reference loader installed data. `entity_id` is null; system operation. | (Hippo-specific) |

This section takes the position, on the long-open question of whether system events should live in a separate table, that all events — entity events and system events alike — live in one `ProvenanceRecord` table. System operations (`migration_applied`, `reference_data_installed`) carry `entity_id = null` and a non-null `patch` with operation-specific context. One table, one code path; consistent with 9.2 *Provenance is data*.

**Multi-entity activities.**

A pure PROV-O encoding lets a single `prov:Activity` `prov:used` or generate several `prov:Entity` instances — one activity, many entity relationships. `ProvenanceRecord` is deliberately 1:1 with entity: `entity_id` is single-valued and each row records one operation on one entity. Atomic and composite activities live in different classes — `ProvenanceRecord` (atomic) and `Process` (composite, 9.5). Both map to `prov:Activity`; they differ only in granularity.

Activities that touch multiple entities — reference-data loads, schema migrations, Cappella pipelines, batch upserts — are represented as a *set* of `ProvenanceRecord` rows sharing a `process_id` that points to one `Process` instance. The `Process` itself (with `class_uri: prov:Activity`) carries start/end times, the responsible actor, operation kind, and optionally a parent process; domain-specific attributes (pipeline parameters, input/output manifests, runtime metadata) are added by caller specializations via `is_a: Process`.

| Scenario | Hippo encoding |
|---|---|
| 1 predecessor → 1 successor (supersession) | One `ProvenanceRecord`, `operation: supersede`, `derived_from_id` set. `process_id` optional. |
| N predecessors → 1 successor (merge) | Multiple records sharing a `process_id`. Per-record `operation` is caller's choice (`supersede` or domain-specific); `derived_from_id` is single-valued, so merging history is reconstructed by joining on `process_id`. First-class merge (multivalued `derived_from_ids`) is additive-compatible and deferred until a concrete use case arises. |
| 1 predecessor → N successors (fission) | Multiple `supersede` records sharing `process_id`, each with `derived_from_id` pointing to the common predecessor. |
| Pipeline activity producing multiple outputs (Cappella) | A `PipelineRun is_a Process` instance; multiple `ProvenanceRecord` rows (one per entity touched) each with `process_id = PipelineRun.id`. `PipelineRun` carries pipeline-specific attributes and has its own independent provenance history. |
| Reference-data load (Hippo itself) | A `Process` instance (e.g., `operation_kind: reference_data_installed`); one `ProvenanceRecord` per entity loaded, all with `process_id` set to the `Process`. System-level provenance records for the `Process` itself have `entity_id` pointing at the `Process.id`. |

Reconstructing a logical activity's full entity trail is `SELECT * FROM ProvenanceRecord WHERE process_id = <id>`. Traversal in the other direction — entity → processes it participated in — is `SELECT DISTINCT process_id FROM ProvenanceRecord WHERE entity_id = <id>`. Process composition (a reference load that internally spawns sub-processes, a pipeline with sub-stages) is captured by `Process.parent_process_id`; the call-graph DAG lives on `Process`, not on `ProvenanceRecord`, keeping atomic records flat and cheap to query.

Activity chains across Processes (`prov:wasInformedBy` between distinct Processes) are caller-modeled relationships on user-schema subclasses of `Process`; `mosaic_core` does not prescribe the predicate.

The current shape is additive-compatible with future extensions — `derived_from_ids` as multivalued, a dedicated generation/use join class, richer Process lifecycle state slots. None are needed as long as the 1:1 atomic record model holds.

**Storage semantics.**

`ProvenanceRecord` is a normal entity table from the adapter's perspective, generated by the same DDL pipeline used for user entities. It is distinguished by the class-level annotation `mosaic_append_only: true` (declared in `mosaic_ext`; see 9.4), which imposes two rules the storage adapter MUST enforce:

1. `INSERT` is permitted. `UPDATE` and `DELETE` are rejected at the adapter layer.
2. Partial-index and summary-view annotations do not apply to `ProvenanceRecord`; rows are read linearly by entity, by time window, or by operation kind.

Append-only enforcement is a runtime check in the adapter, not a DDL constraint — LinkML annotations declare intent; the adapter honors them. A violation is a defect in the adapter, not in `mosaic_core`.

**Write atomicity.**

Every entity mutation and its accompanying `ProvenanceRecord` write are one transactional unit. Either both land or neither does. If the adapter cannot append the provenance record, the entity write MUST roll back and the caller MUST receive a failure — the caller's operation does not complete. This applies to every operation that emits provenance: `create`, `update`, `supersede`, `availability_change`, `relationship_add`, `relationship_remove`, `external_id_add`, `external_id_remove`.

Storage adapters that cannot provide transactional semantics spanning the entity table and the `ProvenanceRecord` table are unacceptable. For relational adapters this is a standard transaction. For Neo4j it is a single Cypher transaction spanning the entity node and the provenance node. For any future adapter, this is a conformance requirement.

Provenance integrity is not a best-effort property. A deployment that encounters missing or inconsistent provenance has a critical defect in its adapter or in the SDK, not an acceptable degraded state.

**Schema-version tracking.**

Every `ProvenanceRecord` carries the `schema_version` in effect at write time. This enables:

- Read-time determination of which schema an entity was written under (relevant during and after migrations).
- Audit queries of the form "which records were written against schema version X?"
- Migration verification: a schema migration emits a `migration_applied` record whose `patch` identifies the before/after versions.

The SDK captures `schema_version` from the merged `SchemaView` at write time. Callers cannot supply it.

**Relationship to `Entity`.**

`ProvenanceRecord.entity_id` points from provenance to entity, not the other way. An `Entity` has no slot referencing its provenance records. This is deliberate:

- It preserves the "`Entity` is sparse" invariant from 9.5.
- It keeps `mosaic_core` free of cross-layer references (no slot on `Entity` points into system-level data).
- It matches the PROV-O pattern: entities are entities; activities point at the entities they affect.

Provenance for a given entity is retrieved by querying `ProvenanceRecord` filtered by `entity_id` — a cheap, indexed read pattern. See 9.7 for how this supports computed temporal fields.

**Query patterns.**

Every downstream use of provenance reduces to one of:

- Latest N events by `timestamp` for a given `entity_id` (history viewer, audit trail).
- Earliest `create` event for a given `entity_id` (computed `created_at`).
- Latest event for a given `entity_id` (computed `updated_at`).
- Events by `operation` kind within a time window (audit/reporting).
- System events (`entity_id = null`) filtered by `operation` (migration/reference-data history).

Indexes supporting these queries are declared on `ProvenanceRecord` slots via `mosaic_index` from `mosaic_ext`. The specific index set is implementation detail, deferred to the OpenSpec change that introduces the class.

---

### 9.7 Computed Temporal Fields

Temporal state — `created_at`, `updated_at`, `schema_version`, `created_by`, `updated_by` — is never stored as columns on entity tables. It is computed at read time by aggregating over `ProvenanceRecord` entries for the target entity. This is a direct consequence of 9.2 *Temporal fields are read-time* and 9.6's append-only provenance log.

**Computed fields.**

| Field | Source aggregation | Null when |
|---|---|---|
| `created_at` | `timestamp` of the earliest `ProvenanceRecord` with `operation = create` for this entity | No `create` record exists (indicates a data integrity defect). |
| `updated_at` | `timestamp` of the latest `ProvenanceRecord` for this entity (any operation) | No provenance record exists. |
| `schema_version` | `schema_version` from the latest `ProvenanceRecord` for this entity | No provenance record exists. |
| `created_by` | `actor_id` from the `create` record | No `create` record exists. |
| `updated_by` | `actor_id` from the latest record | No provenance record exists. |

Temporal fields are always included on entity reads. The SDK computes them regardless of adapter — callers see the same entity shape whether the adapter is SQLite, PostgreSQL, or Neo4j.

**Computation strategy.**

Aggregation happens at read time, from two point-in-time queries against `ProvenanceRecord`:

1. Earliest `create` record for the entity: `MIN(timestamp) WHERE entity_id = ? AND operation = 'create'` → provides `created_at` and `created_by`.
2. Latest record for the entity: `MAX(timestamp) WHERE entity_id = ?` → provides `updated_at`, `schema_version`, and `updated_by`.

Both are indexed lookups. The required indexes on `ProvenanceRecord` are:

- `(entity_id, timestamp)` — supports both queries with range scans.
- `(entity_id, operation, timestamp)` — supports the `create`-filtered query without a secondary scan.

Adapters MAY materialize these aggregates as views or cached columns for hot read paths; the SDK does not prescribe materialization. The Neo4j adapter, for example, can expose `created_at` / `updated_at` as read-time traversals across provenance relationships without any special caching — graph traversal is native.

**Batch reads.**

For entity-list queries (returning N entities with temporal fields), the SDK MUST batch the aggregation — one query per entity is unacceptable at scale. The adapter contract exposes a `get_temporal(entity_ids)` primitive that returns the aggregated fields for a set of entity IDs in one round-trip. Adapter-specific implementation:

- Relational: `GROUP BY entity_id` with window functions or a correlated subquery; one SQL statement per batch.
- Neo4j: single Cypher query with `MATCH (e:Entity) WHERE e.id IN $ids` followed by aggregation across provenance relationships.

**Degenerate cases.**

An entity with no `ProvenanceRecord` entries is a critical data-integrity error. Every mutation emits a record (9.6), and mutations are transactional with their provenance writes — this state cannot be produced by normal operation. When the SDK encounters it at read time, it raises `ProvenanceIntegrityError` and refuses to return the entity. The same applies to other inconsistency shapes: a non-`create` record as the earliest entry, missing `actor_id` on a record, or a `schema_version` unrecognized by the merged view.

Rationale: provenance is the audit guarantee the system exists to provide. Silent degradation would compromise every downstream audit, compliance, and debugging use case. Loud failure on any inconsistency — even if it surfaces in a read path rather than a write path — is the only acceptable behavior. See 9.2 *Provenance integrity is transactional and loud*.

**Interaction with supersession and availability changes.**

- `supersede` records advance `updated_at` and `updated_by`, as expected. The superseded-by chain is followed via `derived_from_id`, not through computed fields.
- `availability_change` records advance `updated_at` and `updated_by` on the target entity. `is_available` itself is a stored column (per 9.5); temporal fields describe when the flag last changed, not its current value.

**Schema-version tracking across evolution.**

Because `schema_version` is recorded on every `ProvenanceRecord`, a single entity can carry provenance from multiple schema versions. The computed `schema_version` always reflects the latest write. Migration queries that need to partition records by schema version filter `ProvenanceRecord.schema_version` directly — no traversal needed.

---

### 9.8 Typed Client

The typed client is a Pydantic-generated surface that gives callers class-specific accessors over the SDK — `client.samples.create(Sample(name="S001"))` — alongside the generic `MosaicClient.create("sample", {...})` that already exists. Per 9.2 *Typed and dynamic clients are coequal*, both surfaces have identical capabilities; neither is deprecated in favor of the other.

**Generation trigger.**

Pydantic classes are generated at `SchemaRegistry` load time via LinkML's `PythonGenerator` (the same tool that produces `gen-pydantic` artifacts). Generation is in-memory — no file artifacts are written for user code. The generated namespace is exposed through a stable SDK entry point (e.g., `mosaic.models`) and regenerates when the schema reloads. Static file generation for IDE autocomplete is a possible future supplement; it is not part of the base contract.

**Accessor shape.**

The typed client mirrors the schema's namespace structure. Root-namespace classes are reachable both directly on `client` (flat) and via an explicit `client.root.*` path (dual access). Non-root-namespace classes are reached through a namespace path that matches their declared `namespace:` string.

| Declared namespace | Access form | Example |
|---|---|---|
| root (no `namespace:`) | Flat (default) | `client.samples.create(Sample(...))` |
| root | Explicit | `client.root.samples.create(Sample(...))` |
| `tissue` | — | `client.tissue.samples.create(Sample(...))` |
| `assay.quant` | — | `client.assay.quant.measurements.create(Measurement(...))` |
| `sequencing` | — | `client.sequencing.runs.query(...)` |

`client.samples` and `client.root.samples` resolve to the same underlying method — `client.root` is an alias for the flat root attributes. `root` is a reserved namespace name; a user schema MUST NOT declare `namespace: root`.

The generated Pydantic classes mirror the same layout — each namespace becomes a submodule under `mosaic.models`:

```python
from hippo.models import Sample                   # root.Sample
from hippo.models.root import Sample              # equivalent
from hippo.models.tissue import Sample as TissueSample
from hippo.models.assay.quant import Measurement
```

**Nested namespaces via dot notation.**

Hippo namespace strings MAY contain dots for organizational convenience. A namespace like `assay.quant` is literally the string `"assay.quant"`; the typed-client generator splits on dots when building attribute structures, producing `client.assay.quant`. Two namespaces that share a dot-prefix (e.g., `assay` and `assay.quant`) are formally independent — the shared prefix is convention, not a declared parent-child relationship — but the client presents them as a nested hierarchy.

Consequence: a namespace `assay.quant` causes `client.assay` to materialize as a container attribute even if no classes are declared in the `assay` namespace. The container hosts only the `.quant` sub-attribute. This is legal; empty parent containers of this kind are not an error.

FQN parsing follows the rule in 9.5: the final dot-separated segment is the class name; everything before is the namespace string.

**Accessor-name derivation.**

Within a namespace, accessor names are derived from class names via a deterministic default rule, with an optional override annotation for cases where the default is unsuitable.

Default rule: accessor = `snake_case(ClassName) + "s"`.

| Class | Default accessor |
|---|---|
| `Sample` | `samples` |
| `Project` | `projects` |
| `ProvenanceRecord` | `provenance_records` |
| `DNASample` | `dna_samples` |
| `Datum` | `datums` |

The rule is intentionally simple (no dependency on an inflection library) and predictable (an agent can compute the accessor for any class). It does not attempt to produce linguistically correct plurals for irregular cases — deployments that want `data` instead of `datums`, or need to resolve a genuine collision, use the override annotation.

Override: `mosaic_accessor` (declared in `mosaic_ext`; see 9.4). Class-level string annotation. Optional and rare — a typical deployment, including one with many namespaces, adds zero `mosaic_accessor` annotations.

**Collision detection at schema load.**

`SchemaRegistry` validates the combined attribute space on `client` and on every namespace level at schema load and fails loudly on any collision. A collision is a load-time error; there is no first-wins resolution and no silent override. Four cases, each with a distinct error template:

1. **Same-namespace accessor duplication.** Two classes in the same namespace whose names (or `mosaic_accessor` overrides) resolve to the same accessor.
2. **Class accessor vs. sub-namespace segment.** At some attribute level, a class accessor conflicts with a sub-namespace name at the same level.
3. **Namespace name vs. SDK-reserved attribute.** A namespace (or sub-namespace segment) uses a reserved name: `root`, `query`, `create`, `get`, `update`, `supersede`, `schemas`, `metadata`, or any other existing attribute on `MosaicClient`.
4. **Accessor vs. SDK-reserved name.** A class's derived or override accessor conflicts with an existing client attribute.

Error templates:

Case 1:

```
SchemaRegistry load error: typed-client accessor collision.
  Classes `tissue.DNASample` and `tissue.DnaSample` both resolve to
  accessor `dna_samples` in namespace `tissue`.
  Add `mosaic_accessor` to at least one class to disambiguate, e.g.:

    classes:
      DnaSample:
        annotations:
          mosaic_accessor: dna_samples_legacy
```

Case 2:

```
SchemaRegistry load error: accessor/sub-namespace collision.
  Class `tissue.Protocol` (accessor `protocol`) conflicts with
  sub-namespace `tissue.protocol`.
  `client.tissue.protocol` would be ambiguous.
  Resolve by renaming the class, renaming the sub-namespace, or
  adding `mosaic_accessor`:

    classes:
      Protocol:   # in namespace: tissue
        annotations:
          mosaic_accessor: protocol_templates
```

Case 3:

```
SchemaRegistry load error: reserved namespace name.
  Namespace `root` is reserved for root-namespace dual access.
  Rename the namespace to a non-reserved value.
```

Case 4:

```
SchemaRegistry load error: accessor conflicts with SDK-reserved name.
  Class `root.Query` (accessor `query`) conflicts with the SDK-reserved
  attribute `query`.
  Add `mosaic_accessor` to the class with a non-reserved value:

    classes:
      Query:
        annotations:
          mosaic_accessor: search_queries
```

Collision detection is part of schema validation — it runs before any subsystem sees the merged view.

**Type checking and IDE support.**

The generated classes are Pydantic v2 models (LinkML's current target). Static type checkers (mypy, pyright) see the full class surface via the generated module. IDE autocomplete works against the in-memory module once `SchemaRegistry` has loaded. Pre-load autocomplete (before a running process) requires the optional static generation supplement mentioned above.

**Validation during write.**

- Pydantic v2 validates LinkML-native shape (types, patterns, enums, required slots) at construction time — before the call reaches the SDK.
- The SDK runs the full validation pipeline (LinkML re-check for safety, CEL validators, Python plugins) as described in 9.9.
- Failures from Pydantic surface as `ValidationError`; failures from the pipeline surface as the SDK's unified `ValidationResult` envelope. Callers see both paths with clear origin annotations.

**Coexistence contract.**

- The generic `MosaicClient` surface is unchanged by sec9. Every dynamic call continues to work.
- New features land in both surfaces at the same time. A capability that exists only in the typed accessor is a defect, and vice versa.
- The typed accessors delegate to the same SDK internals; there is no duplicate code path.

**What the typed client does NOT provide.**

- Query-builder syntax beyond what the generic client supports. `client.samples.query(...)` takes the same filter dict as the generic `query`; a typed query-builder is a possible future refinement, not a sec9 deliverable.
- Relationship traversal helpers. Those live on the query engine, which is adapter-agnostic.
- Compile-time schema checks. The Pydantic classes are only as static as the schema loaded at runtime; if the schema changes, the accessors change.

---

### 9.9 Validation Division of Labor

Hippo runs three tiers of validation in a fixed order. Each tier has a bounded responsibility; the boundaries are design rules an agent can apply mechanically when deciding where to add a new validation concern.

**The three tiers.**

| Tier | Validator type | Responsibility | Examples |
|---|---|---|---|
| 1 | LinkML-native | Static shape of the data | Types, patterns (regex), enum membership, `minimum_value`/`maximum_value`, `required: true`, `multivalued` cardinality, `unique_keys` |
| 2 | CEL validator | Dynamic or cross-entity rules expressible as a boolean expression | "Aliquot volume must not exceed parent sample volume"; "project must be active when sample created under it"; "ExternalID must match source-specific pattern" |
| 3 | Python plugin | Rules that require capabilities neither LinkML nor CEL can express | External I/O (reachability checks against a remote service); complex computation not expressible as a single boolean expression; multi-step workflows |

**Boundary rules.**

These rules tell an agent where a new validation concern belongs:

- If LinkML can express it, it MUST be in LinkML. Moving a pattern or enum check out of LinkML into CEL is a defect.
- If CEL can express it (pure function over entity data, possibly with `expand` pre-fetching), it MUST be in CEL. Writing a Python plugin for a CEL-expressible rule is a defect.
- Python plugins are the escape hatch. Their existence is justified by specific use cases (e.g., an external-service reachability check that CEL cannot model), not by developer preference.

**Execution order.**

Tier 1 runs first (cheapest, purely schema-derived), tier 2 second, tier 3 last. The pipeline fails fast on the first tier that reports a failure by default — later tiers only run when earlier tiers passed. An opt-in collect-all mode (for batch ingest that wants to report every error in a batch) runs all tiers and aggregates results.

**Unified result envelope.**

Every tier reports through a common `ValidationResult` envelope:

```
ValidationResult:
  passed: bool
  failures: [ValidationFailure]

ValidationFailure:
  tier: enum (linkml | cel | python)
  rule: str         # rule name (validator name, slot name, or plugin identifier)
  field: str|null   # offending slot, if applicable
  message: str      # human-readable
  details: dict     # tier-specific structured detail
```

Callers consume results uniformly; the `tier` annotation preserves debugging context. REST responses map `ValidationResult` to a structured HTTP 400/422 body; the typed client raises a typed `ValidationFailed` exception carrying the envelope.

**Interactions.**

- The validation pipeline runs on every write: `create`, `update`, `supersede`, `relationship_add`, `relationship_remove`, `external_id_add`, `external_id_remove`. Read operations do not invoke validators.
- Temporal fields (9.7) are not re-validated — they are computed, not authored.
- `ProvenanceRecord` and `Process` writes skip CEL and Python tiers (they have no caller-authored CEL validators); LinkML tier still applies.

**Reference to existing `validators.yaml`.**

The existing `validators.yaml` config format continues to describe CEL validators as instances of the `Validator` class (see 9.5). `expand`, `when`, `requires`, `priority`, and `entity_types` semantics carry forward unchanged; `reference_validators_yaml.md` remains the authoritative reference for the file format.

---

### 9.10 LinkML Ecosystem Integration

This section names the specific LinkML tools Hippo adopts, where Hippo retains custom code, and how the seams between them work. The operating principle (from 9.2 *Adopt, don't rebuild*) is: use LinkML's ecosystem wherever it fits; justify custom code by a specific gap.

**Adopted tools.**

| LinkML component | Hippo's use | Replaces |
|---|---|---|
| `SchemaView` (linkml-runtime) | Loading, merging, and introspecting the three-layer schema stack at runtime | The previous `SchemaConfig` / `SchemaParser` / `FieldDefinition` trio |
| `gen-sqlddl` | Base DDL generation for entity and `ProvenanceRecord` / `Process` tables | Bespoke DDL generator (legacy path) |
| `gen-pydantic` (PythonGenerator) | Generating the typed client's Pydantic classes at `SchemaRegistry` load time | No prior equivalent — this is new capability |
| `linkml-validate` | Validating user-schema syntax/structure at load time; validating instance data against the schema for tier-1 checks (9.9) | Bespoke type/pattern/enum validation logic |
| `linkml-diff` | Computing the diff between two schema versions for migration planning | Bespoke schema-diff code |
| LinkML's `Annotation` metamodel | Declaring `mosaic_ext` vocabulary with typed values and `applies_to` constraints | The previous convention of flat keys inside `annotations:` blocks |

**Operating rule: adopt, do not reimplement.**

Hippo uses LinkML's ecosystem as-is. Bugs, missing features, and quality gaps in LinkML are addressed by upgrading LinkML or contributing upstream — never by reimplementing the affected capability in Hippo. The alternative would be a slow drift into a parallel LinkML maintained by this project, which contradicts 9.1's motivation and negates the point of adopting LinkML in the first place.

**Hippo-specific shims — strictly bounded.**

Shims exist for exactly one reason: to apply the effects of `mosaic_*` annotations that LinkML generators have no knowledge of. The shim layer is bounded by this scope; it is not a general-purpose place to absorb LinkML bugs or limitations.

Shims permitted:

- **DDL shim.** Post-pass over `gen-sqlddl` output that applies `mosaic_*` annotation effects: `mosaic_unique` → `UNIQUE` column constraints, `mosaic_index` → `CREATE INDEX`, `mosaic_index_partial` → partial indexes, `mosaic_search` → FTS indexes, `mosaic_append_only` → adapter-side write-guard registration. The base DDL is unchanged from LinkML's output.
- **Validation result mapping.** `linkml-validate` output is mapped into Hippo's `ValidationResult` envelope (9.9). No semantic translation; LinkML error messages are preserved verbatim.
- **Diff consumption.** `linkml-diff` structured output is consumed by Hippo's migration planner, which decides which adapter operations (DDL alter, data migration, version bump) the diff implies.

Shims explicitly NOT permitted:

- Workarounds for LinkML generator bugs. The fix path is upstream or version-upgrade, not a local patch.
- Replacements for LinkML tools. If a tool is inadequate, the remedy is contribution or version change.
- Selective reimplementation of LinkML's schema loading, type system, enum handling, or inheritance resolution.

Expanding the shim surface beyond these bounds is a major architectural change and requires its own OpenSpec proposal that scopes the new shim and justifies why upstream change or version upgrade is insufficient.

**Retained custom code.**

Where LinkML genuinely has no equivalent, Hippo's custom code stays:

- **CEL evaluation** (`cel-python`-backed validators). Outside LinkML's scope by design; carries forward unchanged.
- **Storage adapters** (`SQLiteAdapter`, `PostgresAdapter`, future `Neo4jAdapter`). LinkML does not ship adapters.
- **`mosaic_ext` vocabulary itself**, as a schema artifact. LinkML provides the mechanism (annotation metamodel); Hippo provides the vocabulary.
- **Append-only enforcement, partial-index predicates, FTS adapter bindings.** Adapter-layer behaviors that LinkML annotations declare but adapters enforce.

**Version pinning — exact versions, deliberate upgrades.**

`pyproject.toml` pins LinkML and every LinkML tool dependency to exact versions (e.g., `linkml == 1.8.2`, `linkml-runtime == 1.8.2`). Exact pinning guarantees reproducibility across environments and avoids surprise behavior change from upstream releases. The `SchemaRegistry` reports the LinkML version it loaded against at startup; a mismatch between the declared pin and the installed LinkML is a startup failure.

Upgrade discipline:

- Any LinkML version bump — including patch-level — is treated as a potential behavior change. The full Hippo test suite must pass against the new version before the pin is updated.
- Every LinkML pin update triggers a Hippo version bump, even when Hippo's own code is unchanged. Rationale: the released artifact is the combined `(Hippo, LinkML)` pair; a different LinkML is a different artifact and the version must reflect that.
- Security-relevant LinkML releases are prioritized. The version-bump workflow is fast-tracked but not skipped — tests must still pass before release.
- If LinkML introduces a breaking change that requires Hippo source changes, the upgrade is scoped in an OpenSpec proposal before the pin moves.

**Upstream contribution.**

`mosaic_ext` annotations that prove general (not Hippo-specific) are candidate contributions to LinkML upstream. When a pattern is adopted upstream, `mosaic_ext` phases out its local version in the next major bump with a migration note. Contributions happen opportunistically; they are not a prerequisite for any sec9 work.

**What the integration is NOT.**

- Hippo does not become a LinkML tool. It uses LinkML as a library; LinkML does not dictate Hippo's architecture.
- Hippo does not vendor LinkML. The dependency is declared in `pyproject.toml` like any other.
- Hippo does not re-export LinkML's public API. Callers that need LinkML directly import `linkml_runtime`; Hippo's SDK exposes Hippo-level concepts only.

---

### 9.11 Migration Narrative

This section tells the story of how Hippo's schema system evolved, what is already in place, and what sec9 asks to change. The narrative is in three stages: pre-LinkML, the current SchemaRegistry-seam transitional state, and the sec9 target state.

**Stage 1 — Pre-LinkML (initial design).**

Hippo's first schema system was bespoke. `SchemaConfig` loaded YAML. `SchemaParser` parsed it. `FieldDefinition` represented a slot. Classes were hand-rolled Python types; validation was a mix of type checks and CEL-backed rules; DDL was a custom generator; schema diff was a custom module. Entity types were registered through a plugin system. Provenance was handled entirely outside the schema system by a `ProvenanceManager` that knew its own data shape.

This design worked but carried significant duplication of concepts LinkML already formalized: schema loading, slot shape, inheritance, enum modeling, constraint semantics. The duplication was maintenance cost.

**Stage 2 — The SchemaRegistry seam (where we are now).**

Direct LinkML authoring was adopted mid-course (commits `49a11c9` "rewrite all schema examples to valid LinkML format" and `04f1448` "remove Hippo DSL — rename to EntityYAML, delete compiler"). User schemas became LinkML YAML files; the compiler step disappeared.

A subsequent series of refactors introduced `SchemaRegistry` as a LinkML-backed seam and migrated the user-facing schema plumbing onto it:

- `67f798e` — `feat(hippo): add LinkML-backed SchemaRegistry seam`
- `3b1722d` — `refactor(hippo): swap DDL/migration/diff path to SchemaRegistry`
- `3726938` — `refactor(hippo): swap MosaicClient/validator/manager to SchemaRegistry`
- `f65fef4` — `refactor(hippo): delete SchemaParser, SchemaConfig, FieldDefinition`

After these, the user-schema loading pipeline runs on LinkML. User classes are authored directly in LinkML; SchemaRegistry wraps `SchemaView`; DDL generation and schema diff consume the merged view. The bespoke abstractions that shadowed LinkML are deleted.

What remains outside the LinkML model:

- Hippo's own primitives (`Entity`, `ProvenanceRecord`, status/operation enums, `Validator`, `ReferenceLoader`) are not yet declared in a LinkML schema shipped with Hippo. They exist as Python constructs that a hand-written DDL path produces tables for.
- The `mosaic_*` annotation vocabulary is used but not formally declared anywhere — it is a stringly-typed convention.
- No typed client surface exists; callers use the generic `MosaicClient` exclusively.
- Temporal fields are partially computed from provenance but not uniformly formalized as read-time aggregations.
- Validation is tiered (LinkML → CEL → Python) by behavior but not formalized by contract.

**Stage 3 — sec9 target state.**

sec9 completes the direction. Hippo's primitives move into a shipped-with-Hippo LinkML schema (`mosaic_core`). The annotation vocabulary moves into a separate shipped-with-Hippo LinkML schema (`mosaic_ext`). `Process` joins `Entity` and `ProvenanceRecord` as a first-class class in `mosaic_core`, formalizing the multi-entity activity grouping that reference loads, pipelines, and migrations need. Temporal fields become a uniform read-time computation. The validation tiering becomes an explicit contract. A typed Pydantic client surface becomes available alongside the dynamic `MosaicClient`.

After sec9:

- Three LinkML schemas form the spine: `mosaic_ext` (vocabulary), `mosaic_core` (primitives), user schema (domain).
- The merged `SchemaView` is the only schema representation in memory; every subsystem reads from it.
- `ProvenanceRecord` and `Process` are DDL-generated by the same pipeline as user entities.
- Identifiers are UUIDs everywhere; type resolution is adapter-specific; the SDK contract is uniform across adapters including the future Neo4j adapter.
- The typed client and the generic client are coequal surfaces over the same SDK.
- LinkML's ecosystem tools (`gen-sqlddl`, `gen-pydantic`, `linkml-validate`, `linkml-diff`) handle the bulk of the work; Hippo-specific code is limited to adapter logic, CEL evaluation, and the shims that apply `mosaic_*` annotation effects.

**What sec9 does NOT require.**

- No data migration. Existing provenance records and entities remain readable. `mosaic_core` is introduced additively; its tables are created at first migration; existing tables keep their shape.
- No API break. `MosaicClient`'s existing surface is preserved; the typed accessors are additive.
- No storage-adapter rewrite. Adapters gain a registry table (relational) or labeling convention (Neo4j) but the core interface is unchanged.
- No cross-component changes. Cappella, Bridge, and Aperture are untouched; their integration with Hippo continues through the existing REST and SDK surfaces.

The work is scoped into OpenSpec changes in 9.12. Each change is independently reviewable; the sequence produces the target state incrementally without intermediate states that are broken for callers.

---

### 9.12 OpenSpec Decomposition

sec9 is decomposed into eleven OpenSpec changes, grouped into three waves by dependency. Each change is independently reviewable; later changes assume the earlier ones have landed. (The eleventh change — `provenance-migration` — was scoped during Wave 2 implementation per Decision 9.6.A to split the `ProvenanceRecord` declaration from its storage migration.)

**Wave 1 — Foundation.** Introduce the schemas and the identity model. No observable behavior change for callers, but the groundwork for everything in Waves 2 and 3.

**Wave 2 — Data model.** Move Hippo's internal concepts (Process, ProvenanceRecord, temporal computation) onto the LinkML spine.

**Wave 3 — Consumer-facing.** Improvements visible to SDK and REST callers.

**Dependency graph.**

```
Wave 1: hippo-ext-vocabulary  ─┐
                               ├─► hippo-core-schema ──► id-registry-and-uuid-strategy ──► process-class
                                                                                              │
                                                                                              ▼
Wave 2:                     provenance-as-linkml-class ──► provenance-migration ──► computed-temporal-fields
                            (declaration-only; Decision 9.6.A)
                                                                                                      │
                                                                                                      ▼
Wave 3:                                                         validation-tiering-clarification ─► typed-client
                                                                                                      │
                                                                reference-loader-shape                │
                                                                (parallel, independent)                │
                                                                                                      ▼
                                                                                            generated-rest-surface (optional, later)
```

**Wave 1 — Foundation**

| Change | Scope | Dependencies | Deliverables | Acceptance |
|---|---|---|---|---|
| `hippo-ext-vocabulary` | Create `mosaic_ext.yaml` declaring the current `mosaic_*` annotations as typed, `applies_to`-constrained LinkML annotations. Add `reference_mosaic_ext.md`. Update `SchemaRegistry` to validate every `mosaic_*` use against the declaration. | none | `mosaic_ext.yaml`; reference doc; SchemaRegistry validation hook; tests covering declared/undeclared/mistyped annotations. | Every existing `mosaic_*` usage in the codebase validates against `mosaic_ext`. Undeclared annotations surface as load-time errors. |
| `hippo-core-schema` | Create `mosaic_core.yaml` with `Entity`, `Status`, `Operation` enums. Deployments' user schemas declare `imports: mosaic_core` and use `is_a: Entity` on domain classes. `SchemaRegistry` merges the three layers. `Validator` and `ReferenceLoader` added as placeholder classes (slot-finalization deferred). | `hippo-ext-vocabulary` | `mosaic_core.yaml`; updated user-schema import pattern; reference doc; migration of existing user schemas to `is_a: Entity`. | All existing user schemas load against the merged view. No observable SDK behavior change. |
| `id-registry-and-uuid-strategy` | Formalize UUID-only identifiers. Relational adapters add an `_entity_registry` table populated on every create. SDK exposes type-resolution helpers. Neo4j adapter (when implemented) uses labels. | `hippo-core-schema` | Registry table migration; SDK helpers; updated entity-creation code paths; tests covering cold UUID resolution. | `client.get(uuid)` works without `entity_type` hint. Polymorphic references resolve correctly. |
| `process-class` | Add `Process` to `mosaic_core` (minimum slot set: `parent_process_id`, `operation_kind`, `started_at`, `ended_at`, `actor_id`). DDL generates the table. Callers can declare `is_a: Process` subclasses. No consumers yet — foundation for Wave 2. | `id-registry-and-uuid-strategy` | `Process` class in `mosaic_core`; DDL generation coverage; tests. | `Process` table exists; instances can be created; `is_a: Process` user-schema subclasses work. |

**Wave 2 — Data model**

| Change | Scope | Dependencies | Deliverables | Acceptance |
|---|---|---|---|---|
| `provenance-as-linkml-class` | Declares `mosaic_append_only` in `mosaic_ext` and `ProvenanceRecord` in `mosaic_core` (PROV-O alignment, all sec9 §9.6 slots, class-level `mosaic_append_only: true`). **Declaration-only per Decision 9.6.A** — introspection, typed-client support, and `mosaic_append_only` vocabulary availability land here; the actual storage migration is a dedicated follow-up. | `process-class` | `ProvenanceRecord` class in `mosaic_core`; `mosaic_append_only` in `mosaic_ext`; reference docs; tests for declaration + applies_to enforcement. | LinkML declaration complete; `ProvenanceRecord` present via `imports: [mosaic_core]`; `mosaic_append_only` validates via the existing `SchemaRegistry` annotation-validation hook. |
| `provenance-migration` | Migrates the legacy `provenance` table + `ProvenanceStore` onto the LinkML-declared `ProvenanceRecord` shape. Drops `previous_state_hash` / `state_snapshot` / `operation_id`; maps `operation_type` strings to the `Operation` enum; captures `schema_version`; adds `derived_from_id` / `process_id` / `context`. Adapter runtime write-guard for `mosaic_append_only`. | `provenance-as-linkml-class` | `provenance_record` table DDL-generated; `ProvenanceStore` API rewrite; adapter write-guard; ~40 test updates; one-time data migration. | Legacy `provenance` table gone; append-only enforced; no legacy operation strings remain; full suite green. |
| `computed-temporal-fields` | Formalize read-time aggregation for `created_at`, `updated_at`, `schema_version`, `created_by`, `updated_by`. Add `(entity_id, timestamp)` and `(entity_id, operation, timestamp)` indexes. Batch aggregation primitives. Remove any stored temporal columns on entity tables if any exist. | `provenance-as-linkml-class` | SDK aggregation code; batch primitive; index migration; tests covering batch and degenerate cases. | Entity reads include temporal fields. Batch reads use one aggregation query per request. |

**Wave 3 — Consumer-facing**

| Change | Scope | Dependencies | Deliverables | Acceptance |
|---|---|---|---|---|
| `validation-tiering-clarification` | Formalize the three-tier validation pipeline (LinkML → CEL → Python) with unified `ValidationResult` envelope and tier annotation on failures. REST surface maps failures to structured HTTP 400/422. | `computed-temporal-fields` (soft) | Pipeline code with tier annotation; REST error mapping; typed exception in SDK; documentation in `reference_validators_yaml.md`. | Every validation failure reports its tier. REST clients get structured error bodies. Existing validators run unchanged. |
| `typed-client` | Declares `mosaic_accessor` in `mosaic_ext` (minor bump) alongside its consumer. Generates Pydantic classes from merged schema at `SchemaRegistry` load time. Exposes class-specific accessors on `MosaicClient` (`client.samples.create(...)`). Generic client unchanged. | `validation-tiering-clarification` | Generation code; typed accessors; `mosaic_accessor` in `mosaic_ext`; documentation; tests covering typed + generic parity and `mosaic_accessor` override behavior. | Every generic call has a typed equivalent; every typed call works. `mosaic_accessor` is validated against `mosaic_ext` and overrides the default accessor derivation. |
| `reference-loader-shape` | Finalize `ReferenceLoader` slot inventory (multivalued `entity_type`, load-order contract for `schema_fragment`). Independent of Wave 2 ordering; can land in parallel once Wave 1 is complete. | `hippo-core-schema` | Finalized `ReferenceLoader` slots; tests; migration of existing loaders. | Existing loaders continue to work under the new shape. |
| `generated-rest-surface` *(optional, deferred)* | Generate REST endpoints and OpenAPI from the merged schema instead of hand-wrapping. | `typed-client` | Generated router; OpenAPI; migration of hand-written endpoints. | REST callers see the same surface; maintenance cost of REST layer drops. |

**Per-change artifacts.**

Each change carries its standard OpenSpec artifacts: a proposal (motivation, scope, non-goals), a design sketch (architecture diagrams if relevant), a task list, an acceptance-criteria checklist, and a migration plan if data or schema shape changes. See `openspec/changes/` for the template.

**Sequencing constraint.**

Waves 1 and 2 MUST land in order — later changes have hard dependencies on earlier ones. Wave 3 changes CAN be parallelized once their Wave 2 dependencies are complete, but `validation-tiering-clarification` is recommended before `typed-client` so the typed client's error surface matches the pipeline's.

**Rollback discipline.**

Every change MUST be revertible in isolation. Wave 1 changes are additive-only (new schemas, new tables) and trivially revertible. Wave 2 changes include DDL changes that require down-migrations; the migration plan in each proposal documents the revert path. Wave 3 changes touch consumer-facing surfaces but preserve the generic client as a fallback.

---

### 9.13 Non-Goals & Deferred Concerns

This section distinguishes what sec9 explicitly excludes (non-goals) from what it touches but does not finalize (deferred). An agent reading sec9 should treat non-goals as out-of-bounds and deferred items as "coming later, not in this change."

**Explicit non-goals.**

sec9 does NOT do any of the following. If the work is tempting during implementation, it belongs in a separate proposal, not inside a sec9-scoped OpenSpec change.

| Non-goal | Reason |
|---|---|
| Replace CEL validation with a LinkML-native rule language | LinkML does not offer an equivalent; CEL serves its tier well. Removing it would force complex business rules into Python plugins, an anti-pattern per 9.9. |
| Change GraphQL semantics or introduce new GraphQL features | GraphQL remains reserved in `hippo/graphql/` (sec2) and is outside sec9's scope. |
| Reopen multi-tenancy | Explicitly out of scope per sec3's Key Decisions; nothing in sec9 requires or enables multi-tenant semantics. |
| Coordinate with Cappella, Bridge, or Aperture | Cross-component coordination is handled separately; sec9 is Hippo-internal. Bridge's actor-propagation contract is assumed unchanged. |
| Import the full PROV-O ontology into `mosaic_core` | Only selective URIs (`prov:Activity`, `prov:wasGeneratedBy`, etc.) are used for alignment. A full PROV-O import would bloat the schema and conflate Hippo's primitives with PROV-O's broader ontological commitments. |
| Adopt human-readable or prefixed IDs (Stripe-style ULIDs) | UUIDs are the committed ID strategy (9.5). Migrating to prefixed IDs would be a distinct proposal. |
| Add first-class merge or fission primitives to `ProvenanceRecord` | 9.6 shows these scenarios are representable with `process_id` correlation. First-class primitives are deferred until a use case requires them. |
| Replace the storage-adapter architecture | Adapters carry forward from sec2; sec9 adds a registry obligation for relational adapters but does not reshape the adapter interface. |

**Deferred concerns.**

sec9 touches these but does not finalize them. Each has its own OpenSpec change (or open question) to resolve later.

| Deferred concern | Where addressed |
|---|---|
| Final slot inventory for `ReferenceLoader` (multivalued `entity_type`, `schema_fragment` load-order contract) | OpenSpec change `reference-loader-shape` (9.12), Wave 3 |
| First-class multi-entity activity primitives (multivalued `derived_from_ids`, join class for `wasGeneratedBy`/`used`) | Not scheduled; triggered by a concrete use case |
| Materialized temporal-field views for hot read paths | Adapter-internal optimization; not scheduled |
| REST-surface generation from schema | OpenSpec change `generated-rest-surface` (9.12), optional and deferred |
| Upstream contribution of `mosaic_ext` annotations to LinkML | Aspirational (9.10); not scheduled |
| Cross-deployment ID federation / DRS URI resolution across deployments | Out of sec9 scope; would build on the UUID foundation |
| Typed query-builder syntax on the typed client | Possible future refinement (9.8); not in the first typed-client change |
| Extended lifecycle state machine on `Process` (inputs/outputs slots, state transitions) | Caller-extension territory (9.5); user schemas add these via `is_a: Process` subclasses |
| Per-validator timeout tuning | Existing open question from sec2, unchanged by sec9 |

**Cross-reference to existing open questions.**

The INDEX.md Open Questions table includes items that sec9 relates to but does not resolve:

- *Entity type remapping / namespace migration path (OQ1)* — sec9's namespace semantics reconcile with LinkML but do not provide a migration path. Remains open.
- *`mosaic_poll` efficiency at scale* — unchanged by sec9.
- *GraphQL transport* — unchanged.
- *Schema version check on writes (503 on mismatch)* — benefits from 9.6's `schema_version` tracking but is scheduled separately.
- *Dynamic schema reload via polling* — benefits from the `SchemaRegistry` seam but is scheduled separately.

Items resolved or reaffirmed by sec9 are tagged `Reaffirmed by sec9` or `Resolved by sec9` in the INDEX decision log after approval.
