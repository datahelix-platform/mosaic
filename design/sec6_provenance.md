## 6. Provenance & Audit

**Document status:** Draft v0.1 (light-touch revision post-provenance-migration, 2026-04-26)
**Depends on:** sec3_data_model.md
**Feeds into:** sec4_api_layer.md, sec5_ingestion.md

> **⚠ Authoritative reference:** sec9 §9.6 is authoritative for the post-migration provenance schema,
> `Operation` enum values, actor-resolution order, and enforcement mechanism. Where this section
> conflicts with sec9 §9.6 or the decisions in `sec9_decisions.md` (9.6.B–9.6.G), sec9 takes
> precedence. A broader revision of sec6 is deferred (task 7.3 of `provenance-migration`).

---

### 6.1 Design Philosophy

Provenance is a first-class feature of Hippo, not an afterthought. Every mutation to the
system — entity writes, availability changes, relationship operations, schema migrations,
reference data installs — produces a structured, immutable provenance event. The provenance
log is:

- **The authoritative source of temporal metadata** — `created_at`, `updated_at`, and
  `schema_version` are derived from it at read time; they are not stored on entity tables
- **The audit trail** — every change carries an actor, timestamp, and reason
- **The basis for history queries** — callers can retrieve the full change history of any entity
- **Permanent** — provenance records are never deleted or modified; there is no purge mechanism
  in v0.1

---

### 6.2 Provenance Event Model

Every provenance event shares a common structure:

| Field | Type | Description |
|---|---|---|
| `id` | UUID | Unique event identifier |
| `operation` | `Operation` enum | The operation that produced this record (see §6.3) |
| `entity_id` | UUID | The entity this event pertains to |
| `entity_type` | string | The entity type name |
| `actor_id` | string | Identity of the actor who triggered the change (see Actor below) |
| `timestamp` | datetime (UTC) | When the event was recorded |
| `schema_version` | string | The schema config version at the time of the event |
| `context` | JSON | Structured context from the caller (see §6.5) |
| `patch` | JSON | Operation-specific delta data (replaces pre-migration `payload`) |
| `derived_from_id` | UUID | Links to the source record (e.g. superseded entity's provenance) |
| `process_id` | UUID | FK to `Process` — associates the record with a workflow run |

**Immutability:** Provenance records are written once and never modified. The `ProvenanceRecord`
class carries the `mosaic_append_only` LinkML annotation; the storage adapter enforces this via
SQL triggers (see §6.6 and sec9 Decision 9.6.C).

**Actor:** `actor_id` is resolved in priority order: (1) explicit `actor_id` kwarg to
`ProvenanceStore.record()`; (2) legacy `user_context` shim (Decision 9.6.B); (3) the
`current_actor` ContextVar set by middleware or `with_actor()` (Decision 9.6.G); (4) the
sentinel string `"unknown"` when no actor context is available. The sentinel satisfies NOT NULL
and flags unmigrated call sites in the audit log. Future work will migrate those sites to
require a real actor identity (sec9 §9.5 identity model).

---

### 6.3 Event Types

> **Post-migration note:** The `event_type` string taxonomy below predates the
> `provenance-migration` change. The production `Operation` enum in `mosaic_core` uses
> lowercase values: `create`, `update`, `supersede`, `availability_change`,
> `relationship_add`, `relationship_remove`, `external_id_add`, `external_id_remove`.
> See sec9 §9.6 and Decision 9.6.B for the per-site mapping from legacy strings to
> `Operation` values, and for how `patch` replaces the `previous_state`/`new_state`
> payload structure. The subsections below are retained as design rationale only.

#### EntityCreated

Fired when a new entity is written for the first time.

```json
{
  "event_type": "EntityCreated",
  "payload": {
    "initial_state": { ...entity fields... }
  }
}
```

#### EntityUpdated

Fired when an existing entity's fields are changed.

```json
{
  "event_type": "EntityUpdated",
  "payload": {
    "previous_state": { ...fields before change... },
    "new_state":      { ...fields after change... },
    "changed_fields": ["tissue_type", "collection_date"]
  }
}
```

Only changed fields are listed in `changed_fields`. `previous_state` and `new_state` are
full entity snapshots (excluding system fields). Callers can reconstruct the state of an
entity at any point in time by replaying events in order.

#### AvailabilityChanged

Fired when an entity's `is_available` flag changes.

```json
{
  "event_type": "AvailabilityChanged",
  "payload": {
    "previous": true,
    "current": false,
    "reason": "Sample quality insufficient for sequencing"
  }
}
```

The `reason` field is the primary mechanism for recording *why* an entity became unavailable.
It is required when `current = false` and optional when `current = true` (re-activation).

#### EntitySuperseded

Fired on the *old* entity when it is superseded via `client.supersede_entity()`. The
`operation_type` value in the provenance record is `"EntitySuperseded"`.

A companion `EntityUpdated` event is fired on the *replacement* entity in the **same
transaction**, making the audit trail bidirectional: both the old and new entities carry
a provenance record documenting the supersession event. A `superseded_by` relationship edge
is also created from the old entity to the replacement entity in the same transaction.

All five writes (availability change, `superseded_by` column update, `EntitySuperseded` event,
relationship edge, `EntityUpdated` event on replacement) are atomic — they either all succeed
or all roll back.

```json
{
  "event_type": "EntitySuperseded",
  "payload": {
    "superseded_by_id": "uuid-of-new-entity",
    "reason": "Corrected tissue region annotation"
  }
}
```

The companion event on the replacement entity:

```json
{
  "event_type": "EntityUpdated",
  "payload": {
    "note": "Now the active replacement for superseded entity <old-entity-id>",
    "supersedes": "<old-entity-id>"
  }
}
```

#### RelationshipCreated

Fired when a relationship edge is created between two entities.

```json
{
  "event_type": "RelationshipCreated",
  "payload": {
    "relationship": "donated",
    "from_id": "subject-uuid",
    "from_type": "Subject",
    "to_id": "sample-uuid",
    "to_type": "Sample",
    "properties": {}
  }
}
```

#### RelationshipRemoved

Fired when a relationship edge is soft-deleted (status → removed).

```json
{
  "event_type": "RelationshipRemoved",
  "payload": {
    "relationship_id": "edge-uuid",
    "relationship": "donated",
    "reason": "Incorrectly linked"
  }
}
```

#### ExternalIdAdded

Fired when an external ID is registered for an entity.

```json
{
  "event_type": "ExternalIdAdded",
  "payload": {
    "system": "starlims",
    "external_id": "SL-12345"
  }
}
```

#### ExternalIdSuperseded

Fired when an existing external ID mapping is corrected (old mapping invalidated, new one
added).

```json
{
  "event_type": "ExternalIdSuperseded",
  "payload": {
    "old_external_id_record_id": "uuid",
    "new_external_id_record_id": "uuid",
    "system": "starlims",
    "old_value": "SL-12345",
    "new_value": "SL-12346",
    "reason": "Transcription error in source LIMS"
  }
}
```

#### MigrationApplied

Fired once per `mosaic migrate` run. Recorded at the instance level (not entity-specific).
`entity_id` and `entity_type` are `null` for this event type.

```json
{
  "event_type": "MigrationApplied",
  "payload": {
    "from_version": "1.0",
    "to_version": "1.1",
    "changes_applied": ["Added field Sample.passage", "New entity type CellLine"]
  }
}
```

#### ReferenceDataInstalled

Fired when a reference loader completes installation or update.
`entity_id` and `entity_type` are `null` for this event type.

```json
{
  "event_type": "ReferenceDataInstalled",
  "payload": {
    "loader": "mosaic-reference-fma",
    "version": "3.3",
    "entities_created": 15234,
    "entities_updated": 0
  }
}
```

---

### 6.4 Computed Temporal Fields

The system fields `created_at`, `updated_at`, and `schema_version` are derived from the
provenance log at read time. The derivation is:

| Field | Derivation |
|---|---|
| `created_at` | Timestamp of the first provenance record for the entity (`operation = 'create'`) |
| `updated_at` | Timestamp of the most recent provenance event for the entity (any operation) |
| `schema_version` | `schema_version` from the most recent provenance event for the entity |

**Performance:** Deriving these fields for individual entity reads (single `MAX(timestamp)`
query against the provenance table) is acceptable for low-volume use. For high-volume batch
reads (e.g. `client.query()` returning hundreds of entities), the adapter should use a
provenance summary view or denormalized cache. See §6.6 for the recommended SQLite
implementation.

---

### 6.5 Provenance Context

The `context` field carries structured caller-supplied metadata that enriches the audit trail
without requiring new event types. It is particularly important for Cappella, which needs to
associate write operations with specific workflow runs and sync jobs.

**Context schema (all fields optional):**

```json
{
  "workflow_run_id": "uuid-of-cappella-workflow-run",
  "workflow_name": "qc_pipeline",
  "sync_run_id": "uuid-of-cappella-sync-run",
  "adapter": "starlims",
  "trigger": "nightly_redcap_sync",
  "notes": "free-form annotation string"
}
```

Callers supply context via `provenance_context` parameter on write operations:

```python
client.put(
    "Sample", sample_data,
    actor="cappella",
    provenance_context={
        "sync_run_id": "abc-123",
        "adapter": "starlims",
        "trigger": "starlims_new_specimen"
    }
)
```

The context is stored verbatim in the `context` JSON column of the provenance record. Hippo
does not validate or interpret context keys — it is the caller's responsibility to use
consistent key names. The keys above are the **recommended conventions** for Cappella
integrations.

**Opinionated decision:** Context is unstructured JSON rather than a typed schema to avoid
tight coupling between Hippo and the systems that write to it. Structured context keys are
documented as conventions, not enforced constraints.

---

### 6.6 Relational Storage for Provenance

Provenance records are stored in the `ProvenanceRecord` table. The DDL is **LinkML-generated**
via the `mosaic_core` schema (Decision 9.6.D) — there is no hand-coded `CREATE TABLE` block
for this table in the adapter. The representative shape is:

```sql
-- Representative shape only; authoritative DDL is LinkML-generated from mosaic_core.ProvenanceRecord
CREATE TABLE "ProvenanceRecord" (
    id              TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,       -- Operation enum value (e.g. 'create', 'update')
    entity_id       TEXT,                -- null for instance-level events
    entity_type     TEXT,                -- null for instance-level events
    actor_id        TEXT NOT NULL,       -- resolved actor (UUID, sentinel, or legacy string)
    timestamp       TEXT NOT NULL,       -- ISO 8601 UTC
    schema_version  TEXT NOT NULL,
    context         TEXT,                -- JSON string, nullable
    patch           TEXT,                -- JSON delta (operation-specific)
    derived_from_id TEXT,                -- FK: source provenance record (e.g. for supersede)
    process_id      TEXT                 -- FK to Process (workflow association)
);

-- Core lookup index: all events for a given entity, chronological
CREATE INDEX idx_provenance_entity
ON "ProvenanceRecord" (entity_id, entity_type, timestamp);

-- Actor + time range queries (audit use case)
CREATE INDEX idx_provenance_actor_time
ON "ProvenanceRecord" (actor_id, timestamp);

-- Operation filter (e.g. "show all create events")
CREATE INDEX idx_provenance_operation
ON "ProvenanceRecord" (operation, timestamp);
```

**No `UPDATE` or `DELETE` permitted** on `ProvenanceRecord`. The `mosaic_append_only`
annotation drives SQL trigger generation (Decision 9.6.C). The triggers are generic —
any update to any column is rejected:

```sql
-- Block any UPDATE on any column
CREATE TRIGGER IF NOT EXISTS prevent_ProvenanceRecord_update
BEFORE UPDATE ON "ProvenanceRecord"
BEGIN
    SELECT RAISE(ABORT, 'Cannot update ProvenanceRecord: mosaic_append_only class');
END;

-- Block DELETE operations
CREATE TRIGGER IF NOT EXISTS prevent_ProvenanceRecord_delete
BEFORE DELETE ON "ProvenanceRecord"
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete ProvenanceRecord: mosaic_append_only class');
END;
```

Triggers fire at statement level (BEFORE), providing database-level enforcement that applies
even to direct SQL access outside the SDK.

**Provenance summary view** (REQUIRED — not optional):

The `entity_provenance_summary` view is **required** for correct operation of `client.query()`
with provenance-derived `created_at` and `updated_at` fields. `mosaic migrate` creates this
view before any entity table migrations so it is always available.

Expected columns and derivation logic:

| Column | Derivation |
|---|---|
| `entity_id` | The entity UUID |
| `entity_type` | The entity type name |
| `created_at` | `MIN(timestamp)` — timestamp of first provenance event (any `operation`) |
| `updated_at` | `MAX(timestamp)` for non-deletion events — timestamp of most recent write |
| `schema_version` | Derived from the most recent record's `schema_version` field |

```sql
CREATE VIEW IF NOT EXISTS entity_provenance_summary AS
SELECT
    entity_id,
    entity_type,
    MIN(timestamp) AS created_at,
    MAX(timestamp) AS updated_at,
    (SELECT schema_version FROM "ProvenanceRecord" p2
     WHERE p2.entity_id = p.entity_id
     ORDER BY p2.timestamp DESC LIMIT 1) AS schema_version
FROM "ProvenanceRecord" p
WHERE entity_id IS NOT NULL
GROUP BY entity_id, entity_type;
```

> **Note:** The pre-migration view excluded `SOFT_DELETE` operations from `updated_at`.
> After the migration, `SOFT_DELETE` maps to `operation = 'availability_change'` (Decision
> 9.6.B), but not all `availability_change` records represent hard deletions — the distinction
> lives in the `patch` column. The view above includes all operations in `updated_at`; refining
> the exclusion logic is deferred to a future sec6 revision.

The SDK's `query()` implementation JOINs against this view to resolve `created_at`,
`updated_at`, and `schema_version` in a single query rather than N+1 provenance lookups. The
`get()` implementation uses a direct provenance subquery for individual entity reads.

---

### 6.7 History API

The SDK exposes entity history as a first-class operation:

```python
# Full provenance history for an entity
events = client.history("Sample", "abc-123")
# Returns list[ProvenanceRecord] in chronological order

# Filtered history
events = client.history("Sample", "abc-123",
    event_types=["EntityUpdated", "AvailabilityChanged"])

# State reconstruction: what did this entity look like at a point in time?
state = client.state_at("Sample", "abc-123", timestamp="2024-06-01T00:00:00Z")
```

`client.state_at()` reconstructs entity state by replaying provenance events up to the given
timestamp. This is a read-only operation and does not require any additional storage.

The REST API exposes history at `GET /entities/{entity_type}/{entity_id}/history`.
See §6.8 for **graph-level** (query-spanning) as-of reconstruction, which generalizes
`state_at` from a single entity to a whole subgraph.

---

### 6.8 Graph-Level As-Of Reconstruction

**Status:** Design (2026-06-17). Resolves the open sub-questions of
[ADR-0001](./decisions/ADR-0001-graph-level-as-of-query.md) and records the design and
direction; the implementation is decomposed into increments (§6.8.6) and sequenced via OpenSpec
— it is not a v0.1 commitment.

§6.7's `state_at` reconstructs **one entity** at a point in time. A consumer that needs a
*reproducible view of the whole graph* — notably Aperture's data stories, which pin an "as-of
watermark" so a story reruns identically (Aperture ADR-0023) — needs to evaluate an *entire
query* (entity selection + relationship traversal + the schema in force) against the graph **as
it stood at a timestamp `T`**. This section specifies that capability: a read may carry `T`, and
when present the whole result is reconstructed to `T` from the append-only provenance log.

#### 6.8.1 Semantics

- **Transaction-time as-of.** `T` selects the graph **as Hippo had recorded it** at `T` — the
  provenance log's `timestamp` axis (§6.6). This is exactly what reproducibility needs: "what the
  system knew at `T`." Valid-time (when a fact was *true in the world*) is **out of scope** and
  would require a data-model change (per-fact `valid_from`/`valid_to`); see §6.8.7.
  *(Resolves ADR-0001 sub-question 1.)*
- **Opt-in and additive.** A read without `T` behaves exactly as today (current state). `T` is an
  additive parameter on reads; it never touches the write path.
- **Read-only.** Reconstruction is a *view* over data already retained in the provenance log
  (§6.6) and the immutable relationship history (§6.3) — no new state is written and no snapshots
  are materialized.
- **One snapshot per read scope.** All elements of a single as-of read — entity set, each
  entity's state, relationship liveness, schema version, temporal fields — are bound to the
  **same `T`**, yielding a coherent point-in-time subgraph. A consumer issuing several reads for
  one logical view (one data story) passes the same `T` to all of them (§6.8.5).

#### 6.8.2 Reconstruction model

For a timestamp `T`, the graph is reconstructed from the provenance log as follows.

**Entity state at `T`.** An entity's state at `T` is the post-image carried by its most recent
provenance record with `timestamp <= T` — i.e. `state_at` (§6.7) applied per entity.

> **Reconstruction contract (verified — increment 1, BU-Neuromics/hippo#71).** The `create` and
> `update` write paths both record the **full post-image** of the entity as the `patch`, so an
> entity's data state at `T` is the patch of its most recent **state-replacing** (`create` /
> `update`) record with `timestamp <= T` — no replay needed. Non-state-replacing records
> (`availability_change`, `external_id_add`, `supersede`) carry **deltas**, not entity state, and
> never define the reconstructed state; the most recent `availability_change` with
> `timestamp <= T` decides availability. `state_at` / `get_state_at` now implement exactly this
> (previously they returned the latest record's `patch` regardless of `operation` — incorrect for
> non-state ops). **Resolved (increment 2):** the supersede path now records the replacement's
> **full post-image** as the `patch` (the audit note moved to provenance `context`), so the
> full-post-image invariant holds for *every* `create`/`update` record and as-of reconstruction
> returns the replacement's real data — no annotation masquerading as state.

**Entity set at `T`.** A query for type `X` as-of `T` returns the entities that, at `T`, had been
created and were available: an entity is **present at `T`** iff its earliest `create` record has
`timestamp <= T` and its most recent availability-affecting record with `timestamp <= T` leaves
it available. Filters then apply to each entity's reconstructed state.

**Relationship liveness at `T`.** Edge liveness is reconstructed from the **provenance log**, not
from the `relationships.is_available` flag. The relationships table records only the *current*
flag with no change-timestamp, so it cannot answer "was this edge live at `T`?"; the provenance
log can, because edge mutations are recorded as `relationship_add` / `relationship_remove` events
(§6.3) with timestamps. An edge is **live at `T`** iff its most recent add/remove event with
`timestamp <= T` is an `add`. Traversal at `T` walks only edges live at `T`.

**Schema version at `T`.** The `schema_version` in force is that of the most recent provenance
record with `timestamp <= T` — the §6.4 derivation, bounded by `T`.

**Temporal fields at `T`.** `created_at` = the first `create` record (always `<= T` for present
entities); `updated_at` = the most recent record with `timestamp <= T`. The §6.4 derivation,
bounded by `T`.

#### 6.8.3 Schema-as-of and decoding

An entity reconstructed to `T` carries the `schema_version` in force at `T`. Because Hippo's
schema evolution is **additive-only** at the transport surface (§4.7), decoding an as-of-`T`
record against the *current* type model is tolerant: slots added after `T` are simply absent
(resolve to defaults/null). Non-additive schema change between `T` and now is **out of scope** —
a deployment that has made a breaking schema change cannot be guaranteed to decode pre-change
states under the new model; such cases are flagged rather than silently mis-decoded.
*(Resolves ADR-0001 sub-question 4.)*

#### 6.8.4 Storage & indexing

Reconstruction is provenance-driven and leans on the existing log and indexes (§6.6):

- **Per-entity state / temporal / schema at `T`** — `WHERE entity_id = ? AND timestamp <= ?
  ORDER BY timestamp DESC LIMIT 1` is already served by `idx_provenance_entity
  (entity_id, entity_type, timestamp)`. The batched `get_temporal` query (§6.4/§6.6) gains a
  single `AND timestamp <= :as_of` predicate.
- **Entity-set selection for a type at `T`** — "the entities of type `X` present at `T`" scans
  provenance by `entity_type` + `timestamp`. Add a covering index
  `idx_provenance_type_time (entity_type, timestamp, entity_id)`; the existing
  `idx_provenance_entity` already covers the per-entity reductions.
- **Relationship liveness at `T`** — resolved from `relationship_add` / `relationship_remove`
  records; benefits from a relationship-event lookup index (by edge source/target/type within the
  provenance log). Exact index is an increment detail.
- **No materialized snapshots.** Because `T` is a free parameter, the `entity_provenance_summary`
  view (§6.6) cannot be reused directly (it is unbounded in time); as-of reductions are computed
  at query time with the `timestamp <= :as_of` bound. A snapshot/caching tier is a *later*
  optimization, only if profiling demands it. *(Resolves ADR-0001 sub-question 3.)*

#### 6.8.5 SDK & transport surface

**SDK.** `as_of` is threaded through the read surface; the storage contract (`EntityStore` ABC)
formalizes methods the SQLite/Postgres adapters already implement de facto:

```python
# MosaicClient / QueryService
client.query("Sample", filters=[...], as_of="2026-06-01T00:00:00Z")          # set + state at T
client.relationships.traverse(source_id, ..., as_of="2026-06-01T00:00:00Z")  # edges live at T

# EntityStore ABC (formalize — currently de facto on the adapters)
def find(self, query: Query, *, as_of: str | None = None) -> Iterator[Entity]: ...
def get_temporal(self, entity_ids: list[str], *, as_of: str | None = None) -> dict[str, TemporalRecord]: ...
def state_at(self, entity_id: str, timestamp: str) -> dict | None: ...
def traverse(self, source_id, *, as_of: str | None = None, ...) -> list[dict]: ...
```

For consumers that need many reads under one coherent `T` (Aperture data stories), an ergonomic
**snapshot handle** scopes the watermark once:

```python
snap = client.snapshot(as_of="2026-06-01T00:00:00Z")   # thin read-only view; same T on every read
snap.query("Sample", filters=[...]); snap.relationships.traverse(...)
```

**Transports (additive — §4.3 / §4.7).**
- **REST:** an `?as_of=<ISO-8601>` query parameter on reads (e.g.
  `GET /entities/Sample?diagnosis=PTSD&as_of=2026-06-01T00:00:00Z`).
- **GraphQL:** an additive `asOf: DateTime` argument on the generated query and resolved-traversal
  fields. The chosen expression is a **per-field argument** (not a request header): explicit,
  composes with existing filters, and preserves the additive-only contract (§4.7). DataLoader
  batch/cache keys MUST include `as_of` so a request mixing timestamps stays correct (in practice
  one request uses one `T`). A request-level default may be layered later as pure ergonomics over
  the per-field argument. *(Resolves ADR-0001 sub-question 2.)*

#### 6.8.6 Implementation increments (sequencing)

Decomposed so each increment is independently shippable and testable *(resolves ADR-0001
sub-question 5 — sequence via OpenSpec, after the current surface; not a v0.1 commitment)*:

1. ✅ **(done — #73) Formalize the `EntityStore` as-of contract** — `state_at` / `get_temporal` /
   `find(..., as_of=)` on the ABC; verified the §6.8.2 reconstruction contract per `operation`.
2. ✅ **(done) Entity set + state + temporal + schema at `T`** — `client.query(..., as_of=)`
   end-to-end on the SQLite adapter (provenance-driven entity-set reconstruction); added
   `idx_provenance_type_time`; closed the §6.8.2 supersede-annotation gap.
3. ✅ **(done) Relationship liveness at `T`** — provenance-driven `traverse(..., as_of=)`: edge
   liveness reconstructed from `relationship_add`/`remove` events (SQLite). Postgres parity in (5).
4. ✅ **(done) Transports** — REST `?as_of=` (entity list) and GraphQL `asOf` (generated list
   queries) thread to `client.query(as_of=)`. As-of resolution of *nested* resolved-relationship
   fields (DataLoader keyed by `as_of`) and the optional `client.snapshot()` handle remain
   follow-ups.
5. ✅ **(done) Postgres adapter parity** — `find(as_of=)`, `entities_created_by` /
   `state_version_at`, provenance-driven relationship `traverse(as_of=)`, and the
   `idx_ProvenanceRecord_type_timestamp` index, all mirrored from the SQLite path. Verified by
   compile + the gated `tests/integration/test_postgres_asof.py` (runs only with a live
   PostgreSQL / `MOSAIC_DATABASE_URL`). The §6.8.4 snapshot/cache **performance tier** remains
   deliberately deferred ("only if profiling demands"); both adapters carry the type-scoped index.

#### 6.8.7 Deferred

- **Valid-time / full bitemporality** — would need per-fact `valid_from`/`valid_to`;
  transaction-time only for now (§6.8.1).
- **As-of across non-additive schema change** — flagged, not guaranteed (§6.8.3).
- **Snapshot/cache materialization** — query-time reconstruction first; optimize only if needed.

---

### 6.9 Retention Policy

**v0.1 position: no retention policy.** All provenance records are retained indefinitely.
There is no archive, purge, or truncation mechanism.

**Rationale:** For the expected v0.1 workload (small-to-medium research deployments),
provenance storage is not a significant concern. The provenance log grows at one row per
write operation; typical research deployments will accumulate millions, not billions, of rows.

**Future:** A configurable retention policy (e.g. compress or archive events older than N
years while retaining the most recent snapshot per entity) is a reasonable future addition.
This is flagged as an open question.

**Open question:** Should `MigrationApplied` and `ReferenceDataInstalled` events be stored
separately from entity-level events, given that they are instance-level rather than
entity-level events? The current design stores them in the same table with `entity_id = null`.
An alternative is a separate `system_events` table. Deferred to a future revision.

---
