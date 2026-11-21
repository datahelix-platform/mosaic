## 7. Non-Functional Requirements

**Document status:** Draft v0.1
**Depends on:** sec2_architecture.md, sec4_api_layer.md
**Feeds into:** Implementation

---

### 7.1 Performance

#### Target workloads

Hippo v0.1 is designed for **small-to-medium research deployments**. Performance targets are
set for these workloads and are not intended to cover enterprise-scale deployments (which
would use PostgreSQL or a cloud adapter).

| Workload profile | Entity count | Concurrent users | Write rate |
|---|---|---|---|
| **Small** (single researcher, local) | < 100k | 1 | < 10 writes/min |
| **Medium** (small team, shared server) | 100k – 2M | 2–10 | < 100 writes/min |
| **Large** (institutional, PostgreSQL) | 2M+ | 10+ | Adapter-dependent |

#### Performance targets (SQLite adapter, v0.1)

| Operation | Target | Notes |
|---|---|---|
| Single entity read (`client.get`) | < 5ms p99 | Indexed UUID lookup |
| Filtered query (100 results) | < 50ms p99 | With partial index on filter field |
| Single entity write (`client.put`) | < 20ms p99 | Includes schema validation + provenance |
| Batch ingest (1000 entities) | < 30s | ~30ms/entity including validation |
| Fuzzy search (FTS5, 10 results) | < 100ms p99 | SQLite FTS5; field must have `search: fts` |
| `query_updated_since` (500 entities) | < 200ms p99 | Via provenance summary view |
| `client.history` (100 events) | < 50ms p99 | Indexed by entity_id |

**Degradation**: Performance degrades gracefully as entity count grows past the medium tier.
Queries that do not use indexed fields will perform full table scans — this is expected and
documented behaviour. Callers are responsible for declaring appropriate `indexed: true` fields
on query-critical columns.

#### Performance anti-patterns to avoid

- N+1 provenance queries: use the `entity_provenance_summary` view for batch reads
- Unbounded queries: always supply a `limit`; the SDK enforces a hard max of 10,000
- Unexpanded `ref` fields in CEL validators on large collections: set `max_expand_list_size`
  appropriately; default is 200

---

### 7.2 Reliability and Data Integrity

#### Transaction guarantees

All writes (entity create/update, relationship creation, provenance recording) are wrapped in
a single database transaction. Either all succeed together or none are written. This applies
to:
- Single entity writes (entity data + provenance event — atomic)
- Supersession (availability change + relationship edge + provenance — atomic)
- ExternalID correction (old invalidation + new creation + provenance — atomic)

#### SQLite durability

The SQLite adapter uses WAL (Write-Ahead Logging) mode with `PRAGMA synchronous = NORMAL`.
This provides:
- Crash recovery: in-progress transactions are rolled back on restart
- No data loss on clean shutdown
- Acceptable durability for research workloads (not ACID-full; `synchronous = FULL` is
  available as a config option for deployments requiring stronger durability guarantees)

```yaml
# mosaic.yaml — optional stricter durability
adapter:
  type: sqlite
  sqlite:
    path: ./mosaic.db
    synchronous: FULL    # default: NORMAL
```

#### Provenance immutability enforcement

The storage adapter enforces immutability of provenance records at the database level via
triggers (see sec6 §6.6). This cannot be bypassed by the SDK or REST layer.

#### Schema validation is non-bypassable

Schema validation (Tier 1) runs on every write regardless of caller. There is no `--force`
or bypass flag. Business rule validators (Tier 2) can be omitted by not configuring a
`validators.yaml`, but field-level schema validation is always active.

---

### 7.3 Scalability

#### Vertical scaling

The SQLite adapter scales vertically (faster CPU, more memory) within its single-writer
constraint. For most research deployments, a modest server (4 cores, 16GB RAM) is sufficient
for the medium workload profile.

#### Horizontal scaling path

| When | Action |
|---|---|
| Write throughput saturates SQLite | Migrate to PostgreSQL adapter |
| Data volume exceeds practical SQLite limits (~10GB) | Migrate to PostgreSQL adapter |
| Multi-region or cloud-native requirement | Migrate to DynamoDB or PostgreSQL on RDS |

Migration path: `mosaic migrate` applies schema to new adapter; data migration is a one-time
`hippo export / mosaic import` operation (deferred tooling; not in v0.1).

#### REST server scaling

The REST server (Uvicorn + FastAPI) is **fully stateless** — no in-process mutable state
spans requests. Multiple instances behind a load balancer are transparent to callers.

All distributed-systems concerns (transaction atomicity, upsert race conditions, replication,
failover) are delegated entirely to the storage adapter. The API layer has no distributed
logic of its own.

**Multi-instance operational constraint (v0.1):** Schema changes require a **restart-on-migrate**
procedure: drain all instances, run `mosaic migrate` against the storage backend, restart
all instances. This is standard operational practice (30–60s downtime window) and acceptable
for the expected v0.1 schema change frequency (infrequent, deliberate).

The schema is loaded into memory at startup and held for the lifetime of the process. There
is no dynamic schema reload in v0.1.

#### Planned schema sync roadmap (post-v0.1)

The foundation for zero-downtime schema changes is already in place (additive-only migrations,
schema version tracked in `mosaic_meta`). The roadmap to eliminate restart-on-migrate:

| Phase | Change | Complexity |
|---|---|---|
| **v0.2** | Schema version check on every write: compare instance's cached version against `mosaic_meta`. Return `503 Service Unavailable` + `Retry-After: 5s` if mismatch. Eliminates silent data inconsistency risk; operators see 503s as a restart signal. | Low |
| **v0.3** | Schema version polling: each instance polls `mosaic_meta` on a short interval (default 10s) and reloads schema in-memory if the version changes. No new infrastructure dependencies — polling uses the existing storage adapter. | Moderate |
| **v0.3+** | `mosaic migrate` enforces the expand-contract convention for new required fields: required fields must be introduced as optional in one migration and made required in a subsequent one. Eliminates the remaining validation-inconsistency window during transitions. | Moderate |

These phases build incrementally on the existing design without architectural changes.

---

### 7.4 Security

#### v0.1 posture

Authentication and authorisation are **explicitly out of scope for v0.1**. The REST API
accepts all requests with no credential check. The auth middleware stub passes all requests
through.

**Recommended deployment for v0.1**: Run `mosaic serve` on `localhost` or within a private
network only. Do not expose the REST API to the public internet without an external auth proxy.

#### Actor field

All write operations require an `actor` string. In v0.1 this is advisory (not authenticated).
When auth is added in a future version, the transport layer will validate the caller's
identity and override the `actor` field — callers will not be able to impersonate other actors.

#### Future auth design (deferred)

The auth middleware stub in `hippo/rest/auth.py` defines the interface that real auth will
implement:

```python
class AuthMiddleware(ABC):
    @abstractmethod
    def authenticate(self, request: Request) -> str:
        # Returns actor identity or raises 401
        ...

    @abstractmethod
    def authorize(self, actor: str, operation: str, entity_type: str) -> bool:
        # Returns True if allowed or raises 403
        ...
```

RBAC, JWT validation, and API key management are deferred. The stub ensures the app
structure accommodates auth without restructuring.

#### Data sensitivity

Hippo stores metadata and provenance records. In bioinformatics research deployments, this
may include sensitive clinical or phenotypic data. Deployers are responsible for:
- Access controls at the network/OS level in v0.1
- Disk encryption for the SQLite database file
- Compliance with applicable regulations (HIPAA, GDPR, etc.) at the deployment level

Hippo makes no compliance guarantees in v0.1.

---

### 7.5 Observability

#### Logging

Hippo uses Python's stdlib `logging` module. Structured JSON logging is configurable:

```yaml
# mosaic.yaml
logging:
  level: INFO          # DEBUG | INFO | WARNING | ERROR
  format: json         # json | text (default: text for local, json for server)
```

Key log events:
- Every write operation: entity type, entity id, actor, duration, validator names run
- Every validation failure: validator name, entity type, error message
- Every ingestion batch: source, records processed, created/updated/unchanged/errors
- Startup: adapter type, schema version, plugins loaded, search capability validation

#### Metrics (future)

A `/metrics` endpoint (Prometheus format) is deferred from v0.1. The `/status` endpoint
provides human-readable summary statistics.

---

### 7.6 Testability

The SDK-first architecture makes Hippo highly testable in isolation:

- All business logic is in `hippo/core/` with no I/O dependencies
- `EntityStore` and other ABCs can be mocked via standard Python `unittest.mock`
- `IngestionPipeline` accepts a `MosaicClient` — test against an in-memory SQLite adapter
- `WriteValidator` implementations are pure functions (input: `WriteOperation` + mock client)
- CEL expressions can be tested without running the full Hippo server

The test suite uses in-memory SQLite (`:memory:` path) for all integration tests. No external
services are required to run the full test suite.

---

### 7.7 Compatibility

#### Python version

Minimum Python 3.10. Type hints, `match` statements, and `importlib.metadata` entry points
are used throughout.

#### Schema backwards compatibility

Schema migrations are additive-only in v0.1 (see sec3 §3.8). Deployed schemas can be
extended but not destructively modified. This provides strong backwards compatibility for
clients built against a given schema version.

#### API versioning

The REST API is versioned at the URL level (`/api/v1`). v0.1 ships only v1. Breaking changes
to the API will increment the version prefix.

---
