## Appendix B: Implementation Guide for Coding Agents

**Document status:** Draft v0.1
**Depends on:** All sec1–sec7, reference_*.md documents

This document provides a structured implementation plan for a coding agent (or human
engineer) building Hippo from this specification. It covers build order, module
responsibilities, key invariants, and test strategy.

---

### B.1 Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.10+ | Required for `match` statements, `importlib.metadata` entry points |
| Package manager | `uv` | Preferred; `pip` acceptable |
| Config/schema validation | `pydantic>=2.0` | All config and schema types are Pydantic models |
| YAML parsing | `pyyaml` | Config and schema file parsing |
| CLI | `typer` | CLI framework |
| REST framework | `fastapi` | REST transport; auto-generates OpenAPI docs |
| ASGI server | `uvicorn` | ASGI server for `mosaic serve` |
| CEL evaluation | `cel-python` | Config-driven validator conditions |
| SQLite | stdlib `sqlite3` | Core storage; no ORM |
| Testing | `pytest` | Test framework; in-memory SQLite for integration tests |
| Packaging | `pyproject.toml` | Entry points declared here |

---

### B.2 Package Structure

```
hippo/
├── __init__.py                 # Public exports: MosaicClient, MosaicConfig
│
├── core/
│   ├── client.py               # MosaicClient — all business logic; no I/O except via adapters
│   ├── types.py                # Shared types: Filter, PaginatedResult, ScoredMatch,
│   │                           #   WriteOperation, ValidationResult, ProvenanceRecord, IngestResult
│   ├── errors.py               # Full error hierarchy (see §B.3)
│   └── ingestion.py            # IngestionPipeline, IngestOptions, IngestResult
│
├── schema/
│   ├── parser.py               # Parse LinkML schema.yaml → SchemaConfig (Pydantic model)
│   ├── validator.py            # Validate LinkML schema structure and references
│   ├── migrator.py             # Schema diff, migration plan generation, migration application
│   └── types.py                # SchemaConfig, EntityTypeConfig, FieldConfig, RelationshipConfig
│
├── config/
│   ├── loader.py               # Load mosaic.yaml → MosaicConfig (Pydantic model); env var substitution
│   └── types.py                # MosaicConfig, AdapterConfig, ServerConfig, LoggingConfig, etc.
│
├── adapters/
│   ├── base.py                 # EntityStore ABC, ExternalSourceAdapter ABC
│   ├── registry.py             # Adapter discovery via mosaic.storage_adapters entry points
│   └── sqlite/
│       ├── __init__.py
│       ├── store.py            # EntityStore implementation for SQLite
│       ├── schema_ddl.py       # DDL generation from SchemaConfig
│       ├── migrations.py       # SQLite-specific migration application
│       ├── provenance.py       # Provenance table + summary view management
│       └── search.py           # FTS5 search implementation
│
├── validators/
│   ├── base.py                 # WriteValidator ABC, WriteOperation, ValidationResult
│   ├── registry.py             # Validator discovery (schema + config + plugins), ordering
│   ├── schema_validator.py     # Tier 1: field-level schema validation (priority -1)
│   ├── cel_validator.py        # Tier 2: CEL condition evaluation, expand path engine
│   ├── expand.py               # Expand path parser, batch fetcher, cycle detector
│   ├── presets.py              # Built-in preset expansion (ref_check, count_constraint, etc.)
│   └── cel_context.py          # CEL context construction from entity + existing + expansions
│
├── reference/
│   ├── base.py                 # ReferenceLoader ABC, LoadResult
│   └── registry.py             # ReferenceLoader discovery via mosaic.reference_loaders entry points
│
├── rest/
│   ├── app.py                  # FastAPI app factory: create_app(client: MosaicClient)
│   ├── auth.py                 # AuthMiddleware ABC + pass-through stub
│   ├── dependencies.py         # FastAPI dependency injection (get_client, get_actor, etc.)
│   └── routers/
│       ├── entities.py         # GET/POST /entities/{type}, GET /entities/{type}/{id}
│       ├── history.py          # GET /entities/{type}/{id}/history
│       ├── availability.py     # POST /entities/{type}/{id}/availability
│       ├── supersede.py        # POST /entities/{type}/{id}/supersede
│       ├── relationships.py    # POST /relationships, DELETE /relationships/{id}
│       ├── external_ids.py     # GET/POST /external-ids/...
│       ├── search.py           # GET /search/{type}
│       ├── ingest.py           # POST /ingest/{type}
│       └── schema.py           # GET /schema/entity-types, etc.
│
├── cli/
│   └── main.py                 # Typer CLI: init, serve, migrate, ingest, validate,
│                               #   reference (install/update/list), validate-schema
│
└── graphql/
    └── __init__.py             # Reserved for future GraphQL transport; empty in v0.1
```

---

### B.3 Error Hierarchy

All Hippo exceptions inherit from `MosaicError`. Callers should catch the most specific
subclass available; catch `MosaicError` only as a last resort.

```python
class MosaicError(Exception):
    """Base class for all Hippo errors."""

# ── Configuration errors (startup time) ──────────────────────────────────────

class ConfigError(MosaicError):
    """Invalid or missing configuration. Raised at startup."""

class SchemaError(MosaicError):
    """Schema file is invalid, references unknown types, or contains cycles."""

class MigrationError(MosaicError):
    """Migration cannot be applied (e.g. destructive change attempted)."""

class SearchCapabilityError(MosaicError):
    """Schema declares a search mode not supported by the active adapter."""

# ── Runtime errors (request time) ────────────────────────────────────────────

class ValidationError(MosaicError):
    """Write was rejected by a validator. HTTP 422."""
    validator: str          # name of the validator that rejected the write
    entity_type: str
    entity_id: str | None   # None for creates before ID assignment
    detail: dict            # validator-specific detail

class EntityNotFoundError(MosaicError):
    """Entity does not exist or is unavailable. HTTP 404."""
    entity_type: str
    entity_id: str

class RelationshipNotFoundError(MosaicError):
    """Relationship edge does not exist. HTTP 404."""
    relationship_id: str

class ExternalIdNotFoundError(MosaicError):
    """No entity found for the given (system, external_id) pair. HTTP 404."""
    system: str
    external_id: str

class ExternalIdConflictError(MosaicError):
    """ExternalID is already registered to a different entity. HTTP 409."""
    system: str
    external_id: str
    existing_entity_id: str

class SchemaVersionMismatchError(MosaicError):
    """Instance schema version differs from storage schema version (future v0.2). HTTP 503."""
    instance_version: str
    storage_version: str

# ── Adapter errors (infrastructure) ──────────────────────────────────────────

class AdapterError(MosaicError):
    """Storage backend failure. Always wraps the underlying exception. HTTP 500."""
    cause: Exception

class AdapterNotFoundError(ConfigError):
    """No adapter registered for the configured adapter type."""
    adapter_type: str
```

**Wrapping rule:** Adapters must catch all backend-specific exceptions
(e.g. `sqlite3.OperationalError`) and re-raise as `AdapterError(cause=original)`.
Internal implementation details must never leak through the SDK boundary.

---

### B.4 Build Order and Dependencies

Implement in this sequence. Each phase is independently testable before proceeding.

#### Phase 1 — Pure types and config (no I/O)

**Modules:** `core/types.py`, `core/errors.py`, `config/loader.py`, `config/types.py`,
`schema/parser.py`, `schema/types.py`

**Goal:** Parse `mosaic.yaml` and `schema.yaml` into validated Pydantic models. No database,
no network.

**Key tests:**
- `mosaic.yaml` with all valid fields parses correctly
- Missing required fields raise `ConfigError` with clear message
- Unknown keys raise `ConfigError`
- `schema.yaml` with entity types, fields, relationships, `base:`, `requires:`, `search:`
  parses correctly
- Cyclic `base:` declarations raise `SchemaError`
- Invalid field types raise `SchemaError`

#### Phase 2 — SQLite storage adapter (no business logic)

**Modules:** `adapters/base.py`, `adapters/sqlite/store.py`, `adapters/sqlite/schema_ddl.py`,
`adapters/sqlite/provenance.py`

**Goal:** Implement `EntityStore` ABC against SQLite. CRUD + provenance writing.
No validators, no relationships, no search yet.

**Key tests (all use `:memory:` SQLite):**
- `put()` creates a new entity and provenance `EntityCreated` event
- `put()` on existing entity updates fields and writes `EntityUpdated` event with `changed_fields`
- `get()` returns entity by UUID
- `get()` raises `EntityNotFoundError` for unknown UUID
- `get()` raises `EntityNotFoundError` for unavailable entity (unless `include_unavailable=True`)
- `query()` returns filtered results
- `history()` returns provenance events in chronological order
- Provenance immutability trigger prevents UPDATE/DELETE on `provenance_events`
- WAL mode is enabled on connection
- Entity tables use partial indexes on `is_available`

#### Phase 3 — Schema validation (Tier 1)

**Modules:** `validators/base.py`, `validators/schema_validator.py`, `validators/registry.py`

**Goal:** Integrate field-level schema validation into the write path. Every `put()` now
validates against schema before writing.

**Key tests:**
- Required field missing → `ValidationError`
- Field type mismatch → `ValidationError`
- Enum value not in allowed set → `ValidationError`
- Unknown field in input → warning (not error) + field ignored
- Valid write succeeds

#### Phase 4 — Full MosaicClient (no CEL validators yet)

**Modules:** `core/client.py`, `core/ingestion.py`

**Goal:** Implement the full public `MosaicClient` interface — availability, supersession,
relationships, ExternalID operations, history, state_at, ingestion pipeline.

**Key tests:**
- `set_availability(available=False)` writes `AvailabilityChanged` provenance event
- `set_availability(available=False)` without `reason` raises `ValidationError`
- `supersede(old_id, new_id)` writes `EntitySuperseded` event on old, creates `superseded_by` edge
- `relate()` creates edge and `RelationshipCreated` event
- `unrelate()` sets edge status to removed and writes `RelationshipRemoved` event
- `register_external_id()` writes `ExternalIdAdded` event
- `register_external_id()` with existing (system, external_id) on different entity raises `ExternalIdConflictError`
- `correct_external_id()` invalidates old record and writes `ExternalIdSuperseded` event
- `get_by_external_id()` returns entity
- `state_at()` reconstructs entity state at a given timestamp
- `IngestionPipeline` creates/updates/skips correctly; `IngestResult` counts are accurate
- `IngestionPipeline` with `--fail-fast` stops on first error

#### Phase 5 — CEL validators (Tier 2) and expand path engine

**Modules:** `validators/cel_validator.py`, `validators/expand.py`, `validators/presets.py`,
`validators/cel_context.py`

**Goal:** Load and execute `validators.yaml` rules. Implement expand path engine with batch
fetch and cycle detection.

**Key tests:**
- `validators.yaml` with unknown keys raises `ConfigError`
- Validator `when` pre-condition = false → validator skipped (write succeeds even if condition would fail)
- Validator `condition` = false → `ValidationError` with `error` template rendered
- `expand: subject` — single ref expanded to full entity map in CEL context
- `expand: samples[]` — list ref batch-fetched and expanded to list of maps
- `expand: subject.diagnosis_group` — nested ref expansion
- Cycle in expand graph → expansion stops; no infinite loop
- `max_expand_list_size` cap — list truncated; warning logged
- Built-in preset `ref_check` — rejects write when ref points to unavailable entity
- Built-in preset `immutable_field` — rejects update that changes field value
- `existing` is null for creates; present for updates
- CEL sandbox has no I/O functions (test that `http` or `open` are not available)
- Plugin validators (`mosaic.write_validators` entry points) are discovered and ordered

#### Phase 6 — REST transport

**Modules:** `rest/app.py`, `rest/routers/*`, `rest/auth.py`, `rest/dependencies.py`

**Goal:** Thin FastAPI transport wrapping `MosaicClient`. All routing, serialisation,
request/response envelope, error mapping.

**Key tests:**
- All entity CRUD endpoints return correct HTTP status codes
- `ValidationError` maps to HTTP 422 with structured error body
- `EntityNotFoundError` maps to HTTP 404
- `AdapterError` maps to HTTP 500
- Response envelope always present: `{data, error, meta}`
- Pagination: `?limit=10&offset=20` returns correct slice + meta pagination fields
- `X-DataHelix-Actor` header sets actor on writes; defaults to `"anonymous"` if absent
- `X-Hippo-Context` header parsed as JSON and passed as provenance context
- OpenAPI docs generated at `/docs`

#### Phase 7 — CLI

**Module:** `cli/main.py`

**Goal:** Implement all CLI commands using `MosaicClient` and supporting modules.

**Commands to implement:**
- `mosaic init` — scaffold `mosaic.yaml` and blank `schema.yaml` in current directory
- `mosaic serve` — start REST server (loads config, validates, starts Uvicorn)
- `mosaic migrate [--yes]` — diff schema, print plan, apply on confirmation
- `mosaic validate` — run validators.yaml against current data (read-only dry run)
- `mosaic ingest <entity_type> <file> [--dry-run] [--fail-fast] [--actor] [--context]`
- `mosaic reference install <name> [--version]`
- `mosaic reference update <name>`
- `mosaic reference list`
- `mosaic validate-schema <schema_file>`

#### Phase 8 — Search

**Modules:** `adapters/sqlite/search.py`, `core/client.py` (search method), REST search router

**Goal:** FTS5 full-text search on fields declared `search: fts`. Embedding and synonym
modes are deferred; stubs only.

**Key tests:**
- `EntityStore.search_capabilities()` returns `{"fts"}` for SQLite adapter
- Fields with `search: fts` have FTS5 virtual table populated on write
- `client.search(entity_type, field, query)` returns `list[ScoredMatch]` ordered by score desc
- Startup raises `SearchCapabilityError` if schema declares `search: embedding` and adapter
  doesn't support it

---

### B.5 Key Invariants

These invariants must hold at all times. Tests should verify them directly.

1. **Atomic write + provenance:** Every successful `put()` produces exactly one provenance
   event in the same transaction. If provenance write fails, entity write rolls back. If
   entity write fails, no provenance event is written.

2. **No data loss:** `is_available = false` is the only way to "remove" entities. There
   is no DELETE path in the SDK or REST API for entity data.

3. **Schema validation always runs:** There is no `skip_validation` parameter on any write
   method. Schema validators (Tier 1) cannot be disabled via config.

4. **Provenance immutability:** Once written, provenance records cannot be modified or
   deleted. Verified at the database level by triggers (SQLite) or equivalent constraints.

5. **ExternalID uniqueness:** `(system, external_id)` is globally unique. Two entities
   cannot share the same ExternalID in the same system. Enforced by a unique constraint on
   the `external_ids` table.

6. **Temporal fields derived, never stored:** `created_at`, `updated_at`, `schema_version`
   are computed from the provenance log. They must never appear as columns on entity tables.

7. **Adapter wraps all exceptions:** No raw `sqlite3` or `psycopg2` exceptions reach the
   caller. All backend errors are wrapped as `AdapterError`.

8. **`__type__` is immutable:** An entity's concrete type cannot be changed after creation.
   Attempts to update `__type__` raise `ValidationError` with `immutable_field` reason.

---

### B.6 Testing Conventions

- All integration tests use `adapter.type: sqlite` with `path: ":memory:"`
- A `@pytest.fixture` named `client` provides a fresh `MosaicClient` with an in-memory
  SQLite adapter for each test. No test isolation issues — each test gets a clean DB.
- A `@pytest.fixture` named `client_with_schema(schema_yaml)` loads a schema from a
  YAML string and returns a configured client.
- Test schemas should be minimal — declare only the entity types and fields needed for
  the test. Avoid re-using the Appendix A omics schema across unrelated tests.
- Provenance events should be asserted explicitly in write operation tests — not just
  that the write succeeded, but that the correct event type was recorded with the
  correct fields.

**Standard test schema (minimal, used in unit tests):**

```yaml
version: "1.0"
entities:
  Thing:
    fields:
      name: {type: string, required: true, indexed: true}
      category: {type: enum, values: [A, B, C]}
      count: {type: int}
      parent: {type: ref}
relationships:
  - name: related_to
    from: Thing
    to: Thing
    cardinality: many-to-many
```

---

### B.7 OpenSpec Integration

This spec feeds directly into the OpenSpec workflow. Each phase in §B.4 maps to one or
more OpenSpec features. Recommended mapping:

| OpenSpec Epic | Phases | Key spec sections |
|---|---|---|
| `hippo-foundation` | Phase 1 | sec2 §2.4, reference_mosaic_yaml.md, sec3 §3.6 |
| `hippo-storage` | Phase 2 | sec3b, sec6 §6.6 |
| `hippo-validation-tier1` | Phase 3 | sec2 §2.13 (Tier 1 only) |
| `hippo-sdk-core` | Phase 4 | sec4 §4.2, sec5 §5.2–5.6, sec6 §6.3 |
| `hippo-validation-tier2` | Phase 5 | sec2 §2.13 (Tier 2+3), reference_validators_yaml.md, reference_cel_context.md |
| `hippo-rest` | Phase 6 | sec4 §4.3–4.5 |
| `hippo-cli` | Phase 7 | sec2 §2.5 |
| `hippo-search` | Phase 8 | sec2 §2.3 (search), sec3 §3.5 (search field type) |

Each OpenSpec feature should reference the specific section(s) of this design spec that
define its behaviour. OpenSpec acceptance criteria should map directly to the test cases
listed in §B.4.

---

### B.8 ReferenceLoader v2 Authoring Patterns

Authoritative spec for the v2 contract lives in `sec2_architecture.md` §2.14.8
(`LoadResult` / `EntityRef` shape) and §2.14.9 (reference write log). The rationale
is captured in `sec9_decisions.md` D2.14.J / D2.14.K / D2.14.L. This section is
authoring guidance for agents writing or porting a `ReferenceLoader`; consult the
spec sections above for the contract itself.

#### B.8.1 Two patterns by scale

Loader authors pick the pattern that matches their dataset size. Both produce the
same CLI breakdown — the lifecycle layer falls back to the write log when
`LoadResult.entities` is empty (§2.14.8), so the choice is purely about whether
the loader process accumulates `EntityRef`s in memory.

**Pattern A — small loader (≤ ~10⁴ entities, single-class or few-class).**
Populate `entities` via `EntityRef.from_put_result(client.put(...))`. The list is
cheap at this scale and gives the CLI a precise breakdown without a write-log
query.

```python
from hippo.core.loaders.reference import EntityRef, LoadResult, ReferenceLoader

class FakeTermLoader(ReferenceLoader):
    name = "fake_terms"
    ...

    def load(self, client, version, params=None):
        entities: list[EntityRef] = []
        for row in self._rows_for(version):
            entities.append(
                EntityRef.from_put_result(client.put("FakeTerm", row))
            )
        return LoadResult(created=len(entities), entities=entities)
```

See `hippo/src/mosaic/testing/fake_reference_loader.py` for the in-tree example.

**Pattern B — large loader (~10⁵+ entities).** Skip the `entities` accumulator.
Return scalar counters only; the reference write log (§2.14.9) already records
every `client.put()` made inside the lifecycle `load_context` block, so
`--prune-old` works with no loader-side cooperation.

```python
from collections import Counter
from hippo.core.loaders.reference import LoadResult, ReferenceLoader

class EnsemblGeneLoader(ReferenceLoader):
    name = "ensembl_genes"
    ...

    def load(self, client, version, params=None):
        counts: Counter[str] = Counter()
        for row in parse_gtf(self._fetch(version)):
            client.put("Gene", row)
            counts["Gene"] += 1
        # No EntityRef accumulation; the write log is authoritative for prune.
        return LoadResult(created=sum(counts.values()))
```

#### B.8.2 `LoadResult.entities` is advisory only

The write log (§2.14.9) is the **authoritative substrate** for `--prune-old`.
`LoadResult.entities` is **advisory only**: consumers MUST NOT treat it as
authoritative for any operation. In particular:

- `entities` MAY be empty (Pattern B) or partial (a loader may sample one
  `EntityRef` per entity type for inspection).
- The lifecycle layer never derives prune sets from `entities`; it always reads
  from `reference_write_log`.
- The `mosaic reference install` CLI breakdown queries the write log when
  `entities` is empty, so the loader author does not have to choose between an
  ergonomic CLI and operating at scale.

If a loader author finds themselves wanting to reach back into `entities` from
outside the loader, that is a signal they should be querying the write log
instead.

#### B.8.3 v1 → v2 migration (clean break)

v2 is a clean break per D2.14.L — there is no deprecation window because the
downstream loader count at the cut-over was zero. Any loader code that was
written against v1 (the sibling `entity_type: str | None` + `entity_ids:
list[str]` shape) needs a mechanical rewrite, not a careful port:

1. Drop the `entity_type=...` and `entity_ids=[...]` kwargs from every
   `LoadResult(...)` construction.
2. Add `entities=[...]` populated via `EntityRef.from_put_result(client.put(...))`
   for Pattern A loaders, or omit the kwarg entirely for Pattern B loaders.
3. Replace any `--prune-old` reasoning that depended on `LoadResult.entity_ids`
   with the assumption that prune now works for every loader — the platform
   owns the substrate via the write log.

The migration is bounded; v1 `LoadResult.entity_type` and `LoadResult.entity_ids`
attributes no longer exist on the dataclass, so a port that misses one of them
fails fast at import or call time rather than silently passing the wrong data.

#### B.8.4 Lifecycle and write-log diagnostics

Loader code never opens or queries `reference_write_log` directly — the
lifecycle in `hippo/cli/commands/reference.py` wraps every `loader.load()` /
`loader.upgrade()` call in `client.load_context(loader_name, version)` (§2.14.9).
Inside the block, `client.put()` records a log row; outside the block it is a
no-op for the log. User-data writes, ingestion CLI writes, and REST writes
therefore never hit the log.

`mosaic reference doctor` (planned diagnostic command) reports orphan write-log
rows — log rows whose `loader_name` is not present in
`mosaic_meta.reference_versions`. Orphans typically arise when a loader was
force-removed without going through `mosaic reference uninstall`; cleanup is
manual until v3. The doctor command does not exist in source yet; the child
issue that lands `--prune-old` and the lifecycle wiring owns the matching
command surface.

---
