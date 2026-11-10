# ADR-0002: Multivalued reference slots persist as relationships

- **Status:** Accepted
- **Date:** 2026-11-10 (ratified as built; proposed 2026-06-26)
- **Deciders:** labadorf
- **Related:** sec3 (Data Model — config-driven relational storage with graph-shaped API), sec3b (storage), ADR-0001 (as-of reconstruction — relationship edges reconstructed from `relationship_add`/`relationship_remove` provenance), [issue #79](https://github.com/BU-Neuromics/hippo/issues/79), ADR-0011 (inverse slots — carve-out below)

## Context

`HippoClient.put` — and therefore `hippo ingest`, which calls `put` per instance —
**silently drops multivalued reference slots**. A slot like `Assay.inputs`
(`multivalued: true`, `range: Sample`) is persisted *neither* inline in the entity's
typed row *nor* as relationships, and no error or warning is raised. Single-valued
reference slots are unaffected. This makes any relationship-heavy schema's join edges
disappear: the data ingests "successfully," but cross-domain traversal returns empty
because the edges were never stored (issue #79, hit benchmarking the
brainbank-hippo-schema where `Assay.inputs` reifies the sample↔dataset many-to-many).

### Why it happens

Hippo stores each concrete class as one **per-class typed table**, one column per slot.
The storage representation of a slot is decided by `DDLGenerator.generate`
(`src/hippo/core/storage/ddl_generator.py`):

- **Single-valued reference** (`range` is a class) → LinkML's `SQLTableGenerator` emits a
  real column; it survives DDL post-processing (the FK *constraint* is stripped, the
  column kept). Stored inline as the target id string; queryable. ✅
- **Value-type slot** (`range: ExternalReference`, single- *or* multivalued) → rewritten
  by `_rewrite_value_type_slots` to a plain JSON TEXT column. ✅
- **Multivalued reference**, and in fact **any multivalued slot** → `SQLTableGenerator`
  emits a separate **linktable**, which `generate()` explicitly filters out
  (`ddl_generator.py:114-116`, "drop linktables"). The slot ends up with **no column and
  no junction table** — nowhere to live. ❌

With no column, two things follow mechanically:

1. **Write** — `_project_to_columns` (`sqlite_adapter.py:1310`) keeps only keys that map
   to a real column and drops the rest. The multivalued slot is dropped before the row is
   written. (`_insert_per_class` / `_update_per_class`.) The `ProvenanceRecord.patch`
   *does* retain the full submitted dict, but current-state reads never consult it.
2. **Read** — `_read_per_class` (`sqlite_adapter.py:2029`) and `_find_per_class`
   (`sqlite_adapter.py:2370`) rebuild `data` from the table's columns only, so the slot
   never reappears. (Note: the **as-of** path `_find_as_of` reconstructs from the
   provenance patch and therefore *does* surface the slot — a latent inconsistency
   between current-state and as-of reads that this ADR also resolves.)

### The question

How should Hippo persist a multivalued reference slot so that (a) its edges are
retrievable after `put`, (b) they participate in the existing relationship graph
(`find_relationships`, `traverse`, as-of edge reconstruction), and (c) the round-trip is
uniform across the SDK, the CLI, and both reconstruction paths? And what happens to
multivalued slots whose range is *not* a class, which today are dropped just as silently?

Constraints / invariants to honor:

- **No silent data loss** (issue #79 headline). If a value can't be stored, raise.
- **Graph-shaped API over relational storage** (sec3): edges belong in the relationships
  table, which already powers `find_relationships`/`traverse` and as-of edge replay.
- **Atomic with the entity write** (sec9 §9.2): provenance and stored state move together;
  edge materialization must share the entity write's SQL transaction.
- **Forward references must work.** During bulk ingest a referenced target may not exist
  yet. Single-valued refs already store an arbitrary id string with no existence check;
  multivalued refs must not be stricter.

## Decision

**Hippo will persist a populated multivalued slot whose range is an entity class as
relationships in the `relationships` table, keyed by the slot name as
`relationship_type`, materialized inside the entity-write transaction, and hydrate them
back into `entity["data"][slot]` on read.** Multivalued slots whose range is *not* an
entity class will be stored inline as a JSON TEXT column; if neither rule can apply, the
write raises rather than dropping the value.

Concretely:

### 1. Classify multivalued slots (schema introspection)

Add a registry helper (`SchemaRegistry.multivalued_reference_slots(class_name) ->
list[(slot_name, target_class)]`) returning induced slots where `multivalued` is true and
`range` is a domain class **excluding value types** (`ExternalReference` /
`VALUE_TYPE_CLASSES`). This is the existing `reference_slots` logic plus the `multivalued`
and non-value-type filters. Results are cached per class (like `_xref_slots_cache`).

### 2. Write — materialize edges in the adapter, in-transaction

In `SQLiteAdapter.create` and `update_data`, after the per-class row write and within the
same `_transaction()`, call a new `_materialize_multivalued_refs(conn, entity_type,
entity_id, data)`:

- For each multivalued reference slot present in `data`: normalize the value to a list
  (tolerate a bare scalar), and for each target id write a `relationships` row
  `(source_id=entity_id, target_id=tid, relationship_type=slot_name)` **via the
  relationship store directly** (`RelationshipStore.create`), which performs **no
  target-existence check** — matching single-valued-ref behavior and supporting forward
  references. Each add also records a `relationship_add` `ProvenanceRecord` (so as-of edge
  replay sees it), consistent with `RelationshipManager.relate`.
- **Update reconciliation:** on `update_data`, first soft-delete the existing live edges
  of those `relationship_type`s for this `source_id` (recording `relationship_remove`),
  then re-add from the new `data`. This mirrors `_update_per_class`'s
  replace-every-user-slot semantics and keeps edge history correct for as-of.
- The slot continues to be excluded from the typed row by `_project_to_columns` (no change
  there); the full submitted dict continues to be stored in the provenance `patch`.

Because this lives in the adapter, **every** write path — `put`, `replace`, `update`,
`create`, and `hippo ingest` — is covered with one implementation.

### 3. Read — hydrate edges back into `data`

Add `_hydrate_multivalued_refs(conn, entity_type, entity_id) -> dict[slot, list[id]]` and
call it in both `_read_per_class` and `_find_per_class` after the column-derived `data` is
built: for each multivalued reference slot, read live edges of `relationship_type ==
slot_name` for `source_id == entity_id`, ordered by insertion (rowid/created_at) for
determinism, and set `data[slot] = [target_ids]` when non-empty. For `_find_per_class`,
batch the lookup across the page's ids (single round-trip, no N+1), consistent with the
`get_temporal` batching already there. The as-of path needs no change — it already
reconstructs the slot from the patch; with edges now also replayed it stays consistent.

### 4. Non-reference multivalued slots — inline JSON, never drop

Extend the DDL rewrite so a multivalued slot whose range is **not** a class (string, enum,
int, …) emits a single JSON TEXT column (the same mechanism `_rewrite_value_type_slots`
already uses for value types — `_coerce_for_column`/`_decode_column_value` round-trip
lists through TEXT). This closes the broader silent-drop hole for the same root cause. Any
multivalued slot that, after these rules, still maps to neither a column nor a relationship
must cause DDL generation (adapter startup) or the write to **raise** — never drop.

## Consequences

- **Edges become first-class.** After `put`, `c.relationships.find_relationships()` and
  `traverse()` return the materialized edges; the donor→sample→assay→dataset chain is
  reconstructable. The issue's manual workaround (`relate(...)` after `put`) is no longer
  needed.
- **Round-trip restored, uniformly.** `entity["data"]["inputs"]` returns `["S1"]` from
  both `get`/`read` and `query`, and now agrees with the as-of path.
- **`relationship_type` namespace = slot name.** Edges are keyed by bare slot name, scoped
  by `source_id`, so two classes sharing a slot name don't collide. A user-created
  relationship of the same `(source, type)` and a slot-materialized one are
  indistinguishable in the table — acceptable (the slot *is* that relationship), but worth
  stating: re-`put`ting an entity reconciles (replaces) edges of those slot-named types
  from that source, which would clobber a hand-authored edge of the same name. Documented
  as a reserved-name consequence.
- **Forward references allowed.** Edges may point at not-yet-ingested targets, matching
  single-valued-ref semantics. Referential integrity for these edges remains advisory in
  v1 (consistent with how single-valued ref ids are unchecked today).
- **As-of consistency.** `relationship_add`/`relationship_remove` events on every
  materialize/reconcile make slot edges fully visible to ADR-0001 as-of traversal and
  resolve the current-vs-as-of read discrepancy.
- **Migration / backfill: not needed.** Hippo is dev-only with no production deployments
  (same directive that governs `_run_migrations` — drop-and-recreate rather than data
  migration), so stores written under the buggy behavior are simply re-ingested; new
  ingests are correct immediately. The values do survive in the provenance `patch`, so a
  patch-replay backfill is *possible* if a deployment ever needs it, but it is explicitly
  out of scope and unbuilt.
- **Cost.** One extra in-transaction write per edge on ingest and one batched edge query
  per page on read. Bounded by edge count; acceptable for the relationship-heavy schemas
  this targets.

## Alternatives considered

- **Store multivalued refs inline as a JSON list of ids (no relationships).** Simplest —
  one TEXT column like value types — and restores the round-trip. **Rejected** as the
  primary mechanism for *references*: the edges would be invisible to
  `find_relationships`/`traverse` and to as-of edge replay, so cross-domain joins (the
  actual use case in issue #79) still wouldn't work. This *is* the chosen mechanism for
  non-reference multivalued slots (§4), where there is no edge semantics to preserve.
- **Generate the LinkML linktables instead of filtering them out.** Most "native" to
  LinkML. **Rejected:** it forks the storage model (per-class tables *and* junction
  tables), duplicates the relationship substrate Hippo already has, bypasses the provenance/
  as-of machinery that the `relationships` table feeds, and is a far larger change than the
  defect warrants.
- **Validate-and-reject any populated multivalued reference slot (fail loud, store
  nothing).** Satisfies "don't drop silently" but not "should be retrievable" — it would
  break the brainbank schema and every relationship-heavy schema rather than support them.
  **Rejected** as a complete fix; retained only as the last-resort fallback in §4 for
  slots that genuinely map to no storage.
- **Materialize edges in the service layer (`IngestionService`) via
  `RelationshipManager.relate` after the entity write.** **Rejected:** a second
  transaction (non-atomic with the entity write), and `relate` enforces target existence,
  which breaks forward references during bulk ingest. Doing it in the adapter, in the same
  transaction, against the store directly, avoids both.
- **Namespace `relationship_type` as `<EntityType>.<slot>`.** Avoids the reserved-name
  clobber consequence. **Rejected** for v1: it diverges from the issue's documented
  workaround (`relate("A1","S1","inputs")`) and from how a user would naturally query the
  edge; the `source_id` scoping already prevents cross-class collisions. Revisit if
  hand-authored edges colliding with slot names proves to be a real problem.

## Implementation

Shipped for the SQLite backend:

- `SchemaRegistry.multivalued_reference_slots(class_name)` classifies the slots
  (`src/hippo/linkml_bridge.py`).
- `DDLGenerator._rewrite_multivalued_scalar_slots` collapses non-reference multivalued
  slots to a single JSON TEXT column (`src/hippo/core/storage/ddl_generator.py`); reference
  multivalued slots keep generating (and discarding) their linktable.
- `SQLiteAdapter._materialize_multivalued_refs` writes/reconciles edges inside the
  `create`/`update_data` transactions; `_hydrate_multivalued_refs_batch` restores them on
  `_read_per_class`/`_find_per_class` (`src/hippo/core/storage/adapters/sqlite_adapter.py`).
- Tests: `tests/core/test_multivalued_refs.py`.

**PostgreSQL parity is a follow-up** (the adapter in
`src/hippo/core/storage/adapters/postgres_adapter.py` needs the same materialize/hydrate
hooks), mirroring how ADR-0001 sequenced SQLite then PostgreSQL increments.

### Update (issue #81): PostgreSQL parity shipped

`PostgresAdapter` (now `src/mosaic/core/storage/adapters/postgres_adapter.py`) has no
per-class typed table — every entity is one row in a generic `entities` table with a JSONB
`data` document — so there was no column for a multivalued reference slot to be dropped
*from*; instead the whole submitted dict, edges included, was stored verbatim in `data`.
That is not silent data loss in the literal sense, but it is a representation gap from this
ADR's design intent: the slot's values lived only as a plain JSON array, invisible to
`find_relationships`/`traverse` and to `relationship_add`/`relationship_remove`-driven as-of
edge replay (ADR-0001) — exactly the gap §2/§3 close for SQLite.

Parity now ships: `PostgresAdapter._materialize_multivalued_refs` /
`_hydrate_multivalued_refs_batch` mirror the SQLite methods of the same name, called from
`create`/`update_data` and from `read`/`read_any`/`find`/`delete` respectively. Because
Postgres has one row per entity rather than per-class columns, the slot keys are stripped
from the stored JSONB document (the relationships table becomes the sole current-state
source, matching SQLite's column omission) while the unstripped dict is still recorded in
the `ProvenanceRecord.patch`, so `get_state_at`/as-of reconstruction is unaffected — as
already noted above, it never touches the relationships table. A `seq BIGSERIAL` column was
added to the `relationships` table to preserve edge insertion order on hydration (Postgres
has no SQLite-style implicit `rowid`), and `find()`'s batched hydration groups by entity
type since a single Postgres query can span multiple types, unlike SQLite's one-type-at-a-time
per-class table scan. `PostgresDDLGenerator` (`src/mosaic/core/storage/pg_ddl_generator.py`
— a separate per-class-table migration generator not currently wired into the CRUD path) was
also updated to exclude multivalued reference slots from generated tables and collapse
multivalued non-reference slots to a single TEXT column, for consistency with `DDLGenerator`.
Tests ported to `tests/integration/test_postgres_multivalued_refs.py`. (At the time, status
stayed Proposed — that update documented implementation, not ratification; see below.)

## Ratification (2026-11-10)

**Accepted as built** by labadorf in the 2026-11-10 decision pass over stale Proposed ADRs.
The decision above is binding as implemented on both backends; the open sub-questions below
are closed with the as-built answers, verified against `src/mosaic/core/storage/adapters/
sqlite_adapter.py` (and the mirrored `postgres_adapter.py`) at mosaic `v0.15.0`:

- **Reconciliation granularity → replace-all.** On `update_data`/`replace`,
  `_materialize_multivalued_refs(..., reconcile=True)` soft-deletes every live edge of each
  multivalued-reference slot for the source (recording one `relationship_remove` each), then
  re-adds from the submitted data (one `relationship_add` each); an omitted slot clears its
  edges, just as `_update_per_class` nulls an omitted column. A diff-based reconcile is not
  built; the coarser provenance trail is accepted — as-of replay is correct either way.
- **Edges to unavailable targets surface.** `_hydrate_multivalued_refs_batch` filters only
  the relationship row's own `is_available`; it does not join the target entity. Soft-deleting
  a target leaves the asserted edge live and its id still hydrates into `data[slot]`.
- **No id coercion needed on read.** Hydrated values are the `relationships.target_id` TEXT
  column, returned as-is; no `boolean_slot_names`-style decoding applies. (On *write*,
  `_normalize_ref_list` already tolerates a bare scalar, drops `None`, and `str()`s non-string
  ids so they match the TEXT column — that is the only normalization, and it is write-side.)

**Carve-out — ADR-0011.** An `inverse`-declared slot is *excluded* from this rule:
`SchemaRegistry.multivalued_reference_slots` skips any slot carrying `inverse`
(`src/mosaic/linkml_bridge.py`), so it is never materialized as relationship rows — it is a
virtual reverse edge resolved through the forward FK column. ADR-0002 governs *forward*
multivalued references only. (Slots marked `inlined`/`inlined_as_list` are likewise excluded
and round-trip as structured JSON — issue #121.)

**Decoupled from the 1.0 parity epic.** ADR-0002 and ADR-0003 were ratified separately from
[#99](https://github.com/datahelix-platform/mosaic/issues/99) (P2.5 Postgres feature
parity), which listed their ratification as a scope item; #99's remaining Postgres work
proceeds under ADR-0008.

## Notes / open sub-questions

*Closed at ratification (2026-11-10) — see **Ratification** above. Kept for the record.*

- **Reconciliation granularity:** replace-all (soft-delete every slot-named edge from the
  source, then re-add) is simplest and matches `_update_per_class`. A diff-based reconcile
  (only add/remove the delta) would produce a tighter provenance trail. Confirm
  replace-all is acceptable for the as-of history before ratifying.
- **`is_available` of edges to deleted targets:** when a target is soft-deleted, should its
  slot edges remain live? Current relationship semantics leave edges untouched on target
  deletion; keep that, but confirm hydration should still surface ids of unavailable
  targets (probably yes — the reference was asserted).
- Confirm `boolean_slot_names`-style decoding isn't needed for hydrated id lists (ids are
  strings; no coercion expected).
