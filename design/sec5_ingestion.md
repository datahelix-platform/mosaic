## 5. Ingestion & Integration

**Document status:** Draft v0.1
**Depends on:** sec3_data_model.md, sec6_provenance.md
**Feeds into:** sec4_api_layer.md

---

### 5.1 Design Philosophy

Hippo provides three ingestion mechanisms, each serving a distinct use case:

| Tier | Mechanism | Use case |
|---|---|---|
| **Flat-file** | `mosaic ingest` CLI + `IngestionPipeline` SDK | One-off or batch loads from CSV/JSON/JSONL; config data; re-ingestion |
| **Reference data** | `mosaic reference install` CLI + `ReferenceLoader` plugins | Community-standard ontologies (FMA, Ensembl, GO, etc.) |
| **External systems** | `ExternalSourceAdapter` implementations (lives in Cappella) | STARLIMS, HALO, REDCap, partner systems |

All three tiers write through the same SDK write path — schema validation, business rule
validation, and provenance recording apply uniformly regardless of ingestion source.

---

### 5.2 Flat-File Ingestion

#### Supported formats

| Format | Description |
|---|---|
| CSV | One entity per row; header row declares field names |
| JSON | Single JSON object (one entity) or JSON array (multiple entities) |
| JSONL | One JSON object per line (newline-delimited); preferred for large datasets |

#### CLI

```bash
# Ingest a CSV of Sample entities
mosaic ingest Sample samples.csv

# Ingest from JSONL
mosaic ingest Subject subjects.jsonl

# Dry run — validate without writing
mosaic ingest Sample samples.csv --dry-run

# Supply provenance context
mosaic ingest Sample samples.csv --actor "data-team" --context '{"notes": "batch load Q4 2024"}'

# Fail on first error (default: continue and report all errors at end)
mosaic ingest Sample samples.csv --fail-fast
```

#### `IngestionPipeline` SDK

```python
from hippo.core.ingestion import IngestionPipeline, IngestOptions

pipeline = IngestionPipeline(client)
result = pipeline.ingest_file(
    entity_type="Sample",
    path="samples.csv",
    options=IngestOptions(
        actor="data-team",
        provenance_context={"notes": "batch load Q4 2024"},
        dry_run=False,
        fail_fast=False,
        upsert=True,          # default True — see §5.4
    )
)

print(f"Created: {result.created}, Updated: {result.updated}, "
      f"Unchanged: {result.unchanged}, Errors: {len(result.errors)}")
```

#### Field mapping

Fields in the input file are matched to schema-declared fields by name. Unrecognised field
names emit a warning and are ignored (they do not cause failure). Missing required fields
cause a `ValidationError` for that row.

The `id` field is optional in input files. If absent, Hippo generates a UUID. If present,
the value is treated as the entity's Hippo UUID — callers may supply pre-determined IDs for
idempotent re-ingestion.

#### `IngestResult`

```python
@dataclass
class IngestResult:
    created: int
    updated: int
    unchanged: int
    errors: list[IngestError]   # carries row number and error detail

@dataclass
class IngestError:
    row: int
    entity_id: str | None
    error: str
    validator: str | None       # set if a write validator rejected the row
```

---

### 5.3 Reference Data Ingestion

Reference data loaders are distributed as `mosaic-reference-<name>` pip packages. See
sec2 §2.14 for the `ReferenceLoader` ABC and plugin system.

#### Install lifecycle

```bash
# Install at latest available version
mosaic reference install fma

# Install at specific version
mosaic reference install ensembl --version GRCh38.112

# Update to latest
mosaic reference update go

# List all installed loaders and their versions
mosaic reference list
```

**Install steps (automated):**
1. Resolve loader from `mosaic.reference_loaders` entry points
2. Call `loader.schema_fragment()` — merge entity type definitions into deployed schema
3. Run `mosaic migrate` automatically (additive changes only; any structural conflict is an
   error that requires manual resolution before proceeding)
4. Call `loader.load(client, version)` — ingest the reference data using the standard
   `IngestionPipeline` with `upsert=True`
5. Record `{loader_name: version}` in `mosaic_meta` under key `reference_versions`
6. Write a `ReferenceDataInstalled` provenance event (see sec6 §6.3)

**Idempotency:** Re-running `mosaic reference install` on an already-installed loader performs
an upsert — existing reference entities are updated if the ontology data changed in the new
version, unchanged entities produce no write (and no provenance event). See §5.4.

**Version tracking:** The installed version of each reference loader is stored in `mosaic_meta`:

```json
// mosaic_meta key: "reference_versions"
{
  "mosaic-reference-fma": "3.3",
  "mosaic-reference-ensembl": "GRCh38.109",
  "mosaic-reference-go": "2024-01-01"
}
```

`mosaic reference list` reads this and compares against available versions from each loader.

---

### 5.4 Upsert Strategy and Idempotency

All ingestion in Hippo defaults to **upsert-by-identity** — writing the same data twice
produces the same result, with no duplicate entities created.

#### Identity resolution

Entities are matched by identity in this priority order:

1. **Explicit `id` field** — if the input record carries a Hippo UUID, that entity is
   updated if it exists, or created with that UUID if it doesn't
2. **ExternalID lookup** — if the input record carries an `external_id` value and a `system`
   identifier, Hippo looks up the entity by ExternalID. Found → update. Not found → create
   with a new UUID and register the ExternalID
3. **No identity** — create a new entity with a generated UUID

This means Cappella's core idempotency strategy (upsert-by-ExternalID) is a first-class
ingestion feature, not a Cappella-specific concern. Cappella supplies `system` and
`external_id` fields; Hippo resolves identity automatically.

#### Change detection

When an existing entity is found, the incoming fields are compared to the current state:

- **No change** — no write, no provenance event; `IngestResult.unchanged` incremented
- **Change detected** — write proceeds; an `EntityUpdated` provenance event is recorded with
  `changed_fields`, `previous_state`, and `new_state`

This makes ingestion pipelines naturally idempotent: re-running a sync that produced no new
data in the source system results in zero writes to Hippo.

#### Batch transactions

**Opinionated decision:** Flat-file ingestion processes records **individually by default**,
not as an atomic batch. Each record is an independent write transaction. Failures on
individual records are collected in `IngestResult.errors` and ingestion continues unless
`--fail-fast` is set.

**Rationale:** Atomic batch semantics ("all or nothing") are more complex to implement and
rarely match what callers actually need — partial success with error reporting is more useful
for large batch loads where a few malformed rows should not roll back thousands of successful
writes.

**`--atomic` flag (future):** A future `--atomic` flag will wrap the entire batch in a single
transaction, rolling back all writes if any record fails. Deferred from v0.1.

> **Batch unit-of-work (issue #84).** The flat-file `--atomic` idea generalizes to an SDK-level
> **batch unit-of-work**: `MosaicClient.validate_batch()` (whole-set dry-run validation, no writes)
> and `MosaicClient.batch_put()` (atomic multi-entity commit via the existing
> `staged_transaction()`, with intra-batch reference resolution). Exposed over the transports as
> REST `POST /ingest/validate` + `POST /ingest/batch` and GraphQL `validateBatch` / `ingestBatch`.
> Driven by the Aperture write loop (`aperture/design/portal-requirements.md` X4). See
> `openspec/changes/batch-unit-of-work/`.

---

### 5.5 Relationship Ingestion

Relationships between entities can be declared inline in flat-file ingestion via a special
field naming convention, or created explicitly via the SDK.

#### Inline relationship fields

A field named `<relationship_name>_id` (for to-one relationships) or `<relationship_name>_ids`
(comma-separated, for to-many) in the input file creates relationship edges automatically:

```csv
id,tissue_type,donated_by_id
sample-001,brain,subject-abc
sample-002,liver,subject-def
```

The relationship name must match a declared relationship in schema config. The referenced
entity must already exist in Hippo; if not found, the row fails with an `EntityNotFoundError`.

#### SDK relationship write

```python
client.relate(
    relationship="donated",
    from_type="Subject", from_id="subject-abc",
    to_type="Sample",   to_id="sample-001",
    actor="data-team",
    properties={"method": "surgical biopsy"}
)
```

---

### 5.6 ExternalID Registration During Ingestion

When an input record contains `external_system` and `external_id` fields, the pipeline
automatically registers them as ExternalID records for the entity. Multiple ExternalID
registrations per record are supported via a list:

```jsonl
{"tissue_type": "brain", "external_ids": [
    {"system": "starlims", "id": "SL-12345"},
    {"system": "halo", "id": "HALO-998"}
]}
```

If an ExternalID for the given `(entity_id, system)` pair already exists with the same value,
no write occurs. If the value differs, an `ExternalIdSuperseded` event is written (see
sec6 §6.3).

---

### 5.7 Ingestion Error Handling

Errors during ingestion are captured in `IngestResult.errors` and never silently swallowed.
Each error carries the row number, the entity ID (if known), and the full error detail
including which validator rejected the row.

**Default behaviour (no `--fail-fast`):** Process all rows; collect all errors; report at end.
Partially-failed ingestions are valid — successfully written rows are committed.

**`--fail-fast` behaviour:** Stop on the first error; roll back the current row's transaction;
report the error. Previously written rows are committed (ingestion is not atomic by default).

**Validation errors vs. adapter errors:** `ValidationError` subclasses (schema or rule
failures) are surfaced as `IngestError` entries. `AdapterError` (storage backend failures)
are re-raised immediately and halt ingestion — they indicate infrastructure problems, not
data quality issues.

---

### 5.8 Open Questions

| Question | Priority | Notes |
|---|---|---|
| `--atomic` batch flag | Medium | Wrap entire flat-file ingest in one transaction. Deferred from v0.1. |
| Streaming ingestion | Low | For very large files (>1M rows), a streaming JSONL reader that doesn't load the file into memory. Standard Python generators should suffice; design at implementation time. |
| Webhook-triggered ingestion (Cappella) | High — when live integrations are scoped | Webhook retry deduplication (short-window digest cache), out-of-order delivery protection via source timestamps. Deferred from MVP. ExternalID upsert is the stable foundation. |

---
