## Reference: `mosaic_core` — Hippo's Built-In Classes and Enums

**Document status:** Draft v0.1
**Depends on:** sec9_linkml_redesign.md §9.5, `src/mosaic/schemas/mosaic_core.yaml`

This document is the authoritative reference for `mosaic_core.yaml` — Hippo's
built-in primitives shipped as a LinkML schema. Every user schema imports
`mosaic_core` and every domain class uses `is_a: Entity`. See sec9 §9.5 for
the design rationale.

The live schema ships at `src/mosaic/schemas/mosaic_core.yaml` and is loaded
by `SchemaRegistry` via a bundled importmap: callers declare
`imports: [mosaic_core]` in their own schemas and LinkML resolves the
reference to the shipped file.

---

### Class and enum inventory

| Element | Kind | Purpose |
|---|---|---|
| `Entity` | abstract class | Base class for every domain entity. Provides `id` and `is_available`. |
| `ProvenanceRecord` | class | Atomic audit record of an operation on an entity. Annotated `mosaic_append_only: true`; PROV-O-aligned. `is_a: Entity`. |
| `Process` | class | Composite activity grouping atomic operations under one logical execution (reference loads, migrations, pipeline runs). `is_a: Entity`. |
| `Validator` | class (placeholder) | Declarative validator definition; slot inventory finalized later. |
| `ReferenceLoader` | class | Metadata for reference-data loader plugins. Full slot inventory: `name`, `entity_type` (multivalued), `source`, `schema_fragment`. |
| `ExternalReference` | value type (class) | Inline structured cross-system identifier (`system`, `value`, optional `retrieved_at`/`version`). NOT `is_a: Entity` — no id/lifecycle; stored inline on the slot that ranges it (issue #48). |
| `ExternalID` | class — **DEPRECATED** | Legacy entity-shaped cross-system identifier. Superseded by `ExternalReference` + `mosaic_external_xref`; removal scoped to a future major. |
| `Status` | enum | Lifecycle state of an entity. |
| `Operation` | enum | Operation kinds recorded on a `ProvenanceRecord`. |

---

### `Entity`

Abstract base class. `class_uri: prov:Entity` for PROV-O alignment. Domain
classes specialize via `is_a: Entity`. Two slots:

| Slot | Type | Required | Default | Semantics |
|---|---|---|---|---|
| `id` | string (identifier) | yes | — | Stable primary key. UUID-typed per sec9 §9.5. SDK-assigned on create; opaque to callers. |
| `is_available` | boolean | yes | `true` | Lifecycle flag. `true` = entity participates in normal reads; `false` = superseded/archived/deleted/distributed/removed. The reason lives in provenance, not on the entity. |

Temporal state (`created_at`, `updated_at`, `schema_version`) is
deliberately NOT declared here — computed at read time from
`ProvenanceRecord` per sec9 §9.7. Status transitions are recorded as
`ProvenanceRecord` entries with an `availability_change` operation.

**Example.**
```yaml
# user_schema.yaml
imports:
  - mosaic_core
classes:
  Sample:
    is_a: Entity
    attributes:
      barcode:
        range: string
        annotations:
          mosaic_unique: true
```

---

### `ProvenanceRecord`

Atomic audit record of an operation on an entity. `is_a: Entity`
(participates in the normal identity model — UUID id, lifecycle) but
annotated `mosaic_append_only: true` — adapters MUST reject `UPDATE` and
`DELETE` against the backing table. Only `INSERT` is permitted.

`class_uri: prov:Activity` at the atomic scale. `Process` shares the
same class URI at the composite scale; PROV-O treats activities at
multiple granularities uniformly.

| Slot | Type | Required | PROV-O | Semantics |
|---|---|---|---|---|
| `id` | string (UUID) | yes | — | Inherited from `Entity`. |
| `is_available` | boolean | yes | — | Inherited from `Entity`. |
| `entity_id` | string (UUID) | no | `prov:wasGeneratedBy` / `prov:wasInvalidatedBy` (operation-dependent) | UUID of the target entity. Null ONLY for system operations (`migration_applied`, `reference_data_installed`). |
| `entity_type` | string | no | — | FQN of the target entity at write time. Denormalized onto the record so audit queries can avoid a registry lookup. Null when `entity_id` is null. |
| `operation` | `Operation` enum | yes | — | Kind of operation — see the `Operation` enum below. |
| `actor_id` | string (UUID) | yes | `prov:wasAssociatedWith` | UUID of the responsible entity (user-schema agent, Process, or system entity). |
| `timestamp` | datetime | yes | `prov:endedAtTime` | UTC wall-clock time at which the operation completed. |
| `schema_version` | string | yes | — | Version of the merged schema at the moment of write. Captured by the SDK; never caller-supplied. |
| `derived_from_id` | string (UUID) | no | `prov:wasDerivedFrom` | For `supersede` operations, the previous entity version. |
| `process_id` | Process | no | `prov:wasInformedBy` (convention) | Enclosing `Process`. Used to reconstruct multi-entity activities from their atomic members. |
| `patch` | string (JSON) | no | — | Operation-specific change payload. Shape varies by operation. Unstructured by design. |
| `context` | string (JSON) | no | — | Caller-supplied contextual metadata. Structure at the caller's discretion. |

**Indexes** (via `mosaic_index: true` annotations): `entity_id`,
`operation`, `timestamp`, `process_id`. Wave 2 `computed-temporal-fields`
adds composite indexes as needed for the read-time aggregation paths.

**Status.** Active. Wave 2's `provenance-migration` landed the storage
migration: the `ProvenanceRecord` table replaces the legacy `provenance`
table, `ProvenanceStore` writes the sec9 §9.6 shape, and the SQLite /
Postgres adapters enforce `mosaic_append_only` via BEFORE UPDATE /
BEFORE DELETE triggers (Decision 9.6.C). Two known transition gaps
remain — the `actor_id = "unknown"` fallback for unmigrated callers,
and `schema_version = ""` until `SchemaRegistry` is plumbed through
adapter construction; both tracked under Decision 9.6.F.

---

### `Process`

Composite activity — a grouping of atomic operations under one logical
execution. `is_a: Entity`; `class_uri: prov:Activity` for PROV-O alignment.
Shares the class URI with `ProvenanceRecord` at the atomic scale;
PROV-O treats activities at multiple granularities uniformly.

Use cases: reference-data loads, schema migrations, Cappella pipeline runs,
batch upserts. Callers specialize via `is_a: Process` for domain-specific
attributes (pipeline parameters, input/output manifests, runtime metadata).

| Slot | Type | Required | Semantics |
|---|---|---|---|
| `id` | string (UUID) | yes | Inherited from `Entity`. |
| `is_available` | boolean | yes | Inherited from `Entity`. Processes are archived, not hard-deleted. |
| `parent_process_id` | Process (self-ref) | no | Process composition. Processes form a tree; caller can model a DAG by adding further references on subclasses. Null for root processes. |
| `operation_kind` | string | yes | Caller-defined category (`reference_data_installed`, `pipeline_run`, `schema_migration`, `manual_edit`, …). Not a closed enum. Annotated `mosaic_index: true`. |
| `started_at` | datetime | yes | UTC wall-clock time at process start. Annotated `mosaic_index: true`. |
| `ended_at` | datetime | no | UTC wall-clock time at process completion. Null while running or after failure without completion. |
| `actor_id` | string (UUID) | yes | UUID of the entity responsible for initiating this process. Resolvable via `MosaicClient.resolve_type(uuid)` per sec9 §9.5. |

**Lifecycle via provenance (Wave 2).** Process creation emits a `create`
ProvenanceRecord (like any other entity). State transitions (setting
`ended_at`, archival via `availability_change`) emit further records.
Recursion bottoms out at root processes whose creation record has
`process_id: null`. See sec9 §9.6 for the full semantics.

**Example.**
```yaml
# Cappella schema fragment
imports:
  - mosaic_core
classes:
  PipelineRun:
    is_a: Process
    attributes:
      pipeline_name:
        range: string
        required: true
      parameters:
        range: string   # JSON blob
      run_host:
        range: string
```

---

### `Status` enum

Lifecycle state of an entity. Recorded in provenance via
`availability_change` operations; not stored on the entity itself.

| Value | Description |
|---|---|
| `active` | Entity is live and participates in normal reads. |
| `archived` | Retained but not available for active use. |
| `superseded` | Replaced by another (pointer in provenance via `derived_from_id`). |
| `deleted` | Soft-deleted. Data retained; `is_available` is false. |
| `distributed` | Sent elsewhere (e.g., external distribution). |
| `removed` | Physically removed (rare; normally `superseded` / `deleted`). |

Adding a value is a minor bump on `mosaic_core`. Removing a value is breaking.

---

### `Operation` enum

Kind of operation recorded on a `ProvenanceRecord`. See sec9 §9.6 for the
per-value PROV-O mapping and payload semantics.

| Value | Semantics |
|---|---|
| `create` | Entity created. |
| `update` | Entity slot values modified in place. |
| `availability_change` | `is_available` flag transitioned; new value and `Status` driver carried in the record's `patch`. |
| `supersede` | Entity superseded by another; `derived_from_id` carries the predecessor. |
| `relationship_add` | A relationship slot gained a value; `patch` carries `{slot, target_id}`. |
| `relationship_remove` | A relationship slot lost a value; `patch` carries `{slot, target_id}`. |
| `external_id_add` | **DEPRECATED** with the ExternalID entity (issue #48) — `ExternalReference` slot changes are ordinary `update` operations. An ExternalID was associated with the entity. |
| `external_id_remove` | **DEPRECATED** with the ExternalID entity (issue #48). An ExternalID was disassociated. |
| `migration_applied` | A schema migration was applied (system event; `entity_id` is null). |
| `reference_data_installed` | A reference loader installed data (system event; `entity_id` is null). |

---

### `Validator` (placeholder)

Declarative validator definition — full slot inventory is finalized in a
later OpenSpec change that reconciles this class with the existing
`validators.yaml` format. Current declared slots:

| Slot | Type | Required |
|---|---|---|
| `name` | string (identifier) | yes |

Implementation-side `validators.yaml` continues to work unchanged until
that change lands; this placeholder exists so `is_a: Validator`
user-schema extensions become possible as soon as the shape firms up.

---

### `ReferenceLoader`

Metadata for a reference-data loader plugin. Instances are contributed by
plugins registered under the `mosaic.reference_loaders` entry point. The full
slot inventory was finalized by the `reference-loader-shape` OpenSpec change
(Wave 3 per sec9 §9.12), resolving Decisions 9.5.E and 9.5.F.

| Slot | Type | Multivalued | Required | Semantics |
|---|---|---|---|---|
| `name` | string (identifier) | no | yes | Stable plugin identifier. Used in registration error messages (`plugin <name> failed to register: <reason>`). |
| `entity_type` | string | yes | no | Declarative list of entity class names this loader populates. Used for provenance and discoverability via `SchemaRegistry.reference_loaders()`. Does NOT drive runtime ingestion order. |
| `source` | string | no | no | URL, file path, or other locator identifying the origin of the reference data. Recorded in provenance; not interpreted by Hippo. |
| `schema_fragment` | string | no | no | Inline LinkML schema YAML fragment contributed by this plugin. Merged into the live `SchemaView` at plugin registration. Classes declared here may be referenced in `entity_type`. |

#### Plugin registration contract (Decision 9.5.E)

Plugin registration is a two-step lockstep:

1. Hippo merges the loader's `schema_fragment` into the live `SchemaView`.
2. Hippo validates the `ReferenceLoader` instance against the merged view,
   so `entity_type` references can resolve.

Either step failing aborts plugin registration with a single error:
`plugin <name> failed to register: <reason>`. Partial state — fragment merged
but instance rejected, or instance accepted before its fragment loads — is
impossible. End users never see schema or validation errors at runtime; those
are developer-facing, caught during plugin registration.

#### Developer responsibility — data-loading semantics (Decision 9.5.F)

`entity_type` is purely declarative. **Hippo does not enforce ingestion order
from it.** A multi-class loader (e.g., one that populates both `Substance` and
`Reaction`) must insert rows in the correct FK-satisfying order inside its own
code — Hippo schema validation provides no such guarantee. If `Substance` must
exist before `Reaction`, the loader inserts `Substance` rows first. This is an
explicit contract: schema-side validation cannot substitute for correct
data-loading logic.

If a future plugin needs Hippo to surface per-class metadata, promote
`entity_type` to a `LoaderTarget` record range — a mechanical migration with
no semantics change for existing loaders.

#### Introspection

`SchemaRegistry.reference_loaders()` returns the names of all concrete classes
in the merged schema that subclass `ReferenceLoader`. After a plugin's
`schema_fragment` is merged, any loader-specific subclass it declares (via
`is_a: ReferenceLoader`) appears in this list. The optional REST endpoint
(`GET /schemas/reference_loaders`) is deferred until the
`generated-rest-surface` change lands.

---

### `ExternalReference` (value type, issue #48)

Structured **value type** describing a cross-system identifier. Deliberately
NOT `is_a: Entity`: an `ExternalReference` has no `id`, no lifecycle, and no
provenance of its own — it is stored **inline** on the entity slot that
ranges it (relational adapters persist it as a JSON `TEXT` column, single-
and multivalued alike), so changing an entity's external references is an
ordinary entity update captured by normal provenance.

| Slot | Type | Required | Semantics |
|---|---|---|---|
| `system` | string | yes | External system that owns the identifier (e.g., `STARLIMS`). Deployer-controlled vocabulary. |
| `value` | string | yes | Identifier as it appears in `system`. |
| `retrieved_at` | datetime | no | When the reference was captured. |
| `version` | string | no | External record version the reference was captured against. |

Slots ranged against `ExternalReference` may carry the
`mosaic_external_xref` annotation to become reverse-lookup keys — global
`(system, value)` uniqueness among available entities plus
`MosaicClient.find_by_xref` / `GET /xref/{system}/{value}` / GraphQL
`findByXref`, served by the `mosaic_xref_index` side table. See
`reference_mosaic_ext.md` for the annotation contract.

`ExternalReference` is excluded from every entity surface: no table, no
typed-client accessor, no tree-root bundle slot, no GraphQL object type,
no TUI listing (`VALUE_TYPE_CLASSES` in `mosaic.linkml_bridge`).

#### `ExternalID` deprecation

`ExternalID` (entity-shaped cross-system identifier with its own
`is_active` mapping lifecycle, dedicated client methods, REST router, and
`external_id_add`/`external_id_remove` provenance ops) is **deprecated** in
favor of the value-type + annotation pattern above:

- The class stays in `mosaic_core` and remains fully functional.
- `MosaicClient.register_external_id` / `get_by_external_id` /
  `list_external_ids` / mapping-level `supersede` emit
  `DeprecationWarning` (entity-level `supersede_entity` is unaffected).
- The `/external-ids` REST endpoints are marked `deprecated` in OpenAPI.
- Data migration tooling and removal of the class are scoped to a future
  major release (issue #48 follow-ups).

---

### Version and compatibility

`mosaic_core.version` is `0.6.0` (0.1.0 initial → 0.2.0 added `Process` → 0.3.0 added `ProvenanceRecord` → 0.4.0 finalized `ReferenceLoader` slot inventory → 0.5.0 added `ExternalID` → 0.6.0 added `ExternalReference` and deprecated `ExternalID`, issue #48). Bump rules (per sec9 §9.3):

| Change | Bump |
|---|---|
| Add an optional slot on `Entity`, new enum value, new class | Minor |
| Change an existing class's slot shape, newly required slot, enum removal | Major |

Deployments pin a specific `(mosaic_ext, mosaic_core)` pair. User schemas
declare which `mosaic_core` major they target; `SchemaRegistry` refuses to
merge against an incompatible major.

---

### Design invariants

Three invariants a coding agent must preserve when touching `mosaic_core`:

1. **`Entity` stays sparse.** Only `id` and `is_available`. Resist adding
   slots; everything else belongs either in a user-schema subclass or
   derives from provenance.
2. **No cross-layer references.** `mosaic_core` MUST NOT reference any
   domain class. Any relationship from a domain entity to its provenance
   is expressed via `entity_id` on `ProvenanceRecord` (Wave 2), never by
   a slot on `Entity` pointing into system-level data.
3. **`mosaic_core` validates against itself.** Its own shape satisfies the
   LinkML metamodel. A malformed `mosaic_core` is a startup failure, not a
   silent degradation.

---

### What `mosaic_core` deliberately excludes

Some concepts sec9 specifies but does not land in this change. An agent
encountering a gap should consult the owning change below rather than
adding the concept to `mosaic_core` ad-hoc.

| Concept | Owning change |
|---|---|
| `superseded_by` as a proper provenance-derived relationship | Future follow-up (derive from `ProvenanceRecord.derived_from_id` instead of the hand-coded column) |
| Computed temporal fields on entity reads | `computed-temporal-fields` (Wave 2) |
| `actor_id` UUID resolution through an identity-model service layer | Follow-up to `provenance-migration` (see Decision 9.6.F) |
| `schema_version` populated from `SchemaRegistry` at write time | Follow-up to `provenance-migration` (see Decision 9.6.F) |
| Final `Validator` slot inventory | Later OpenSpec change; deferred pending `validators.yaml` reconciliation |
| REST introspection endpoint for reference loaders | `generated-rest-surface` (deferred) |
| Typed client Pydantic generation | `typed-client` (Wave 3) |
