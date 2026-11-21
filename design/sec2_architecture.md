## 2. Architecture

### 2.1 Component Overview

Hippo is structured as three concentric layers. The Core SDK contains all business logic and is
the only layer required for local deployment. The Transport Layer is optional and adds network
accessibility. The Infrastructure Layer is optional and adds cloud-scale storage backends.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Transport Layer (optional)               │
│                                                                 │
│   ┌─────────────────────────┐   ┌───────────────────────────┐  │
│   │      REST API           │   │    GraphQL API (optional) │  │
│   │  hippo.rest (FastAPI)   │   │   hippo.graphql           │  │
│   │  `mosaic serve`         │   │                           │  │
│   └────────────┬────────────┘   └─────────────┬─────────────┘  │
└────────────────│─────────────────────────────│─────────────────┘
                 │  both call SDK directly      │
┌────────────────▼─────────────────────────────▼─────────────────┐
│                         Core Python SDK                         │
│                                                                 │
│   ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│   │  QueryEngine │  │IngestionPipe │  │  ProvenanceManager  │  │
│   └──────────────┘  └──────────────┘  └─────────────────────┘  │
│   ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│   │ SchemaConfig │  │  EntityStore │  │  MosaicClient       │  │
│   │  (YAML)      │  │  (interface) │  │  (public API)       │  │
│   └──────────────┘  └──────┬───────┘  └─────────────────────┘  │
└──────────────────────────── │ ───────────────────────────────── ┘
                              │  adapter interface
┌─────────────────────────────▼───────────────────────────────────┐
│                    Infrastructure Layer (adapters)              │
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐    │
│  │   SQLite    │  │  PostgreSQL │  │  DynamoDB (future)   │    │
│  │  (v0.1)     │  │  (future)   │  │                      │    │
│  └─────────────┘  └─────────────┘  └──────────────────────┘    │
│                                                                 │
│  (External system connectors live in Cappella, not Hippo.        │
│   Hippo defines ExternalSourceAdapter ABC only — see §2.3)       │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Package Structure

```
hippo/
│
├── core/                        # All business logic — no I/O
│   ├── client.py                # MosaicClient: primary public interface
│   ├── query.py                 # QueryEngine: filter, join, resolve, search
│   ├── ingestion.py             # IngestionPipeline: validate & write entities
│   ├── provenance.py            # ProvenanceManager: change tracking
│   ├── schema.py                # SchemaConfig: load, validate, inheritance resolution
│   └── models.py                # Pydantic models for all core entities
│
├── adapters/
│   ├── base.py                  # Abstract base classes (EntityStore, ExternalSourceAdapter)
│   └── storage/
│       ├── sqlite.py            # SQLite adapter (v0.1)
│       ├── postgres.py          # PostgreSQL adapter (future, stub in v0.1)
│       └── dynamodb.py          # DynamoDB adapter (future, stub in v0.1)
│       # External system connectors (STARLIMS, HALO, etc.) live in Cappella, not here.
│       # Hippo defines ExternalSourceAdapter ABC in base.py only.
│
├── validators/
│   ├── base.py                  # WriteValidator ABC, WriteOperation, ValidationResult
│   ├── registry.py              # Validator registry: entry point discovery + config instantiation
│   ├── cel_engine.py            # CEL evaluation: expand path resolution, context building
│   ├── expander.py              # Entity graph expander: path parsing, batch fetch, cycle detection
│   └── builtins/
│       ├── ref_check.py         # ref_check preset
│       ├── count_constraint.py  # count_constraint preset
│       ├── immutable_field.py   # immutable_field preset
│       ├── field_required_if.py # field_required_if preset
│       └── no_self_ref.py       # no_self_ref preset
│
├── reference/
│   ├── base.py                  # ReferenceLoader ABC, LoadResult
│   └── registry.py              # Reference loader entry point discovery + version tracking
│
├── rest/
│   ├── app.py                   # FastAPI app object (primary artifact)
│   ├── routers/
│   │   ├── entities.py          # Generic entity CRUD — dispatches by entity_type
│   │   ├── relationships.py     # Relationship operations
│   │   ├── search.py            # Fuzzy search endpoints
│   │   └── ingestion.py         # Batch ingestion endpoints
│   ├── dependencies.py          # Dependency injection (SDK client, config)
│   ├── auth.py                  # Auth middleware stub (no-op in v0.1)
│   └── schemas.py               # Pydantic request/response models
│
├── graphql/                     # GraphQL transport (optional `graphql` extra; sec4 §4.7)
│   ├── __init__.py              # Lazy entry points + extra detection
│   ├── schema_builder.py        # schema_typing type model → strawberry types
│   ├── resolvers.py             # Query/Mutation roots; thin MosaicClient delegation
│   └── router.py                # FastAPI GraphQLRouter (GraphiQL, bearer auth)
│
├── cli/
│   └── main.py                  # Typer CLI: init, serve, ingest, validate, reference
│
├── config/
│   ├── loader.py                # Config file loading & validation
│   └── defaults.py              # Default config values
│
└── schema/
    └── example_schema.yaml      # Example schema for reference
```

### 2.3 Adapter Pattern

All storage and external system integrations implement abstract base classes defined in
`hippo/adapters/base.py`. The active adapter is selected at runtime based on the `adapter.type`
field in the Hippo config file — no code changes required to switch backends.

**EntityStore ABC** (storage adapters must implement):

```python
class EntityStore(ABC):
    @abstractmethod
    def get(self, entity_type: str, entity_id: str) -> dict: ...

    @abstractmethod
    def query(self, entity_type: str, filters: list[Filter],
              include_unavailable: bool = False) -> list[dict]: ...

    @abstractmethod
    def put(self, entity_type: str, entity: dict, actor: str,
            provenance_context: dict | None = None) -> ProvenanceRecord: ...

    @abstractmethod
    def history(self, entity_type: str, entity_id: str) -> list[ProvenanceRecord]: ...

    @abstractmethod
    def search(self, entity_type: str, field: str, query: str,
               limit: int = 10, min_score: float = 0.0) -> list[ScoredMatch]: ...

    @abstractmethod
    def search_capabilities(self) -> set[str]: ...
    # Returns supported search modes e.g. {"fts", "embedding", "synonym"}
    # Hippo validates at startup that all schema-declared search modes are in this set.
```

`ScoredMatch` is a core SDK type (adapter-agnostic):

```python
@dataclass
class ScoredMatch:
    entity_id: str
    entity_type: str
    field: str
    value: str
    score: float          # 0.0–1.0
    match_mode: str       # "fts" | "embedding" | "synonym" | "exact"
```

**ExternalSourceAdapter ABC** (external ingestion adapters must implement — implementations
live in Cappella, not Hippo; this ABC defines the contract only):

```python
class ExternalSourceAdapter(ABC):
    @abstractmethod
    def fetch_batch(self, since: datetime | None = None) -> list[dict]: ...

    @abstractmethod
    def validate(self, record: dict) -> ValidationResult: ...
```

Adapters are registered in a central registry keyed by adapter type string. When Hippo loads
its config, it resolves the configured adapter type to its implementation class and instantiates
it with the adapter-specific config block:

```yaml
# mosaic.yaml
adapter:
  type: sqlite
  sqlite:
    path: /data/hippo.db

# Future postgres example:
# adapter:
#   type: postgres
#   postgres:
#     host: localhost
#     port: 5432
#     database: hippo
#     pool_size: 10
```

New storage backends can be added by implementing `EntityStore` and registering the class — no
changes to core SDK logic required.

**Multi-instance adapter contract:** Storage adapters used in multi-instance deployments
(i.e. any adapter other than SQLite) **must** implement entity creation and ExternalID
registration as atomic server-side upserts — not as application-level read-then-write
sequences. Specifically:

- Entity creation: `INSERT ... ON CONFLICT (id) DO UPDATE` (PostgreSQL) or equivalent
  conditional write (DynamoDB)
- ExternalID registration: atomic on `(entity_id, system, external_id)` uniqueness constraint

This requirement exists because multiple Hippo instances can receive concurrent writes for the
same entity or ExternalID. Application-level read-then-write creates a race window that leads
to duplicate records. The SQLite adapter is exempt because SQLite's single-writer WAL mode
serialises all writes naturally — concurrent multi-instance use of SQLite is not supported
(see sec7 §7.3).

#### Plugin System

Hippo supports third-party adapter packages via Python entry points (`importlib.metadata`).
This allows the community to develop, publish, and share adapters as standalone pip-installable
packages without forking Hippo or contributing to the core repository.

Hippo defines four entry point groups. Third-party packages can contribute to any of them:

| Entry point group | Purpose | Naming convention |
|---|---|---|
| `mosaic.storage_adapters` | Storage backend implementations | `mosaic-adapter-<name>` |
| `mosaic.external_adapters` | External system connector implementations (Cappella ships these) | `cappella-adapter-<name>` |
| `mosaic.write_validators` | Write-path business rule validators | `mosaic-validators-<name>` |
| `mosaic.reference_loaders` | Reference ontology data loaders | `mosaic-reference-<name>` |

**Declaring plugins** (in the third-party package's `pyproject.toml`):

```toml
[project.entry-points."mosaic.storage_adapters"]
myadapter = "mypkg.adapters.storage:MyStorageAdapter"

[project.entry-points."mosaic.write_validators"]
my_rules = "mypkg.validators:MyBusinessRuleValidator"

[project.entry-points."mosaic.reference_loaders"]
my_ontology = "mypkg.reference:MyOntologyLoader"
```

Once the package is `pip install`ed alongside Hippo, its contributions are available with no
other registration step:

```yaml
adapter:
  type: myadapter
  myadapter:
    some_setting: value
```

**Discovery and conflict handling:** All four entry point groups are loaded at startup via
`importlib.metadata`. Built-in contributions (sqlite adapter, bundled write validators,
bundled reference loaders) are registered via the same entry points mechanism in Hippo's own
`pyproject.toml` — no separate internal registration path. If two packages register the same
name within the same group, Hippo raises a `ConfigError` at startup with a clear conflict
message identifying both packages.

**Startup validation:** After loading all plugins, Hippo performs cross-validation:
- Schema-declared `search:` modes are checked against `adapter.search_capabilities()` — fail
  fast if a mode is declared but not supported by the active adapter
- Schema-declared `requires:` loader names are checked against installed reference loaders —
  fail fast with a clear install suggestion if a required loader is missing

**Interface stability:** The ABCs for all four plugin groups are part of Hippo's public API.
Once v1.0 is released, breaking changes constitute a semver-major bump. Pre-1.0, breaking
changes may occur with deprecation notices. Plugin authors should pin to compatible Hippo
versions using `hippo>=0.x,<0.(x+1)` until v1.0 is reached.

**Community registry:** A curated list of known community plugins will be maintained in the
Hippo repository README. The naming conventions above (`mosaic-adapter-<name>`,
`mosaic-reference-<name>`, etc.) ensure discoverability via PyPI search.

### 2.4 Config System

All Hippo configuration lives in a single YAML file (`mosaic.yaml` by default, overridable via
`MOSAIC_CONFIG` environment variable). The config file has three top-level sections:

```yaml
# mosaic.yaml

adapter:                         # Which storage backend to use and its settings
  type: sqlite
  sqlite:
    path: ./mosaic.db

schema:                          # Path to entity schema definition (required — see Section 3)
  path: ./schema.yaml            # LinkML format; no default schema is bundled

validators:                      # Path to business rule validators config (optional)
  path: ./validators.yaml        # If omitted, only schema-level validation runs

server:                          # REST API server settings (only needed for mosaic serve)
  host: 0.0.0.0
  port: 8000
  workers: 4
```

Config is loaded once at startup, validated against a Pydantic model, and injected throughout
the SDK via a `MosaicConfig` object. Components never read config files directly — they receive
a config object at instantiation. This keeps components testable in isolation.

### 2.5 Dependency Injection

The `MosaicClient` is the single entry point to all SDK functionality. It is instantiated with
a `MosaicConfig` and internally constructs the appropriate adapter, query engine, and provenance
manager. In the REST layer, a single `MosaicClient` instance is created at server startup and
injected into route handlers via FastAPI's dependency system:

```python
# hippo/rest/dependencies.py
def get_client() -> MosaicClient:
    return _client  # module-level singleton initialized at startup

# hippo/rest/routers/entities.py
@router.get("/{entity_type}")
def list_entities(entity_type: str, filters: dict, client: MosaicClient = Depends(get_client)):
    return client.query(entity_type, **filters)
```

For local SDK usage, the caller instantiates `MosaicClient` directly:

```python
from mosaic import MosaicClient, MosaicConfig

client = MosaicClient(MosaicConfig.from_file("mosaic.yaml"))
results = client.query("Item", category="task", completion_state="pending")
```

### 2.6 Transport Layer

#### REST API

The REST API is a FastAPI application built by the app factories in `mosaic/serve/` and
`mosaic/api/factory.py`. `mosaic serve` is a thin CLI wrapper that runs it via Uvicorn:

```python
# mosaic serve is equivalent to:
uvicorn mosaic.serve:create_app_from_env --factory --host 0.0.0.0 --port 8000 --workers 4
```

Embedding in a larger application is a first-class use case:

```python
# Future workflow executor or data portal:
from mosaic.serve import create_default_app
platform_app.mount("/mosaic", create_default_app())
```

The REST API exposes an OpenAPI schema automatically via FastAPI at `/docs` and `/openapi.json`.

#### GraphQL

`hippo/graphql/` (reserved-empty in v0.1, implemented since — issue #45) is a Strawberry-based
GraphQL transport adapter that wraps the SDK directly — not via REST. The GraphQL schema is
autogenerated at startup by rendering the shared LinkML type model
(`mosaic.core.schema_typing`, issue #47), so the GraphQL and typed-client surfaces cannot
drift; resolvers are thin `MosaicClient` delegations reading the same `app.state.mosaic_client`
the REST routers use. Strawberry ships in the optional `graphql` extra;
`mosaic serve --graphql` mounts the transport at `/graphql` on the same FastAPI app. Auth
middleware is shared between REST and GraphQL transports (see 2.7).
Full design: sec4 §4.7.

### 2.7 Auth Placeholder

Authentication and authorization are explicitly out of scope for v0.1. The REST layer includes
a no-op auth middleware stub in `hippo/rest/auth.py` that passes all requests through. This
stub defines the interface that real auth middleware will implement, so adding JWT/RBAC in a
future version requires replacing the stub — not restructuring the app.

The SDK is auth-unaware by design. Auth is validated at the transport layer before the SDK is
called. The `actor` field on all write operations accepts a string identity passed in by the
transport layer (defaulting to `"anonymous"` in v0.1).

### 2.8 CLI

The Hippo CLI is implemented with Typer and installed as the `hippo` command:

| Command | Description |
|---|---|
| `mosaic init` | Scaffold a new `mosaic.yaml`, `schema.yaml`, and `validators.yaml` in the current directory |
| `mosaic serve` | Start the REST API server using settings from `mosaic.yaml` |
| `mosaic ingest <source> <file>` | Run a batch ingestion from a flat file (CSV/JSON/JSONL) |
| `mosaic validate [--schema <path>]` | Validate config, schema, and validators files without starting the server; `--schema` runs the full namespace graph validation pass (cross-namespace reference checks, duplicate entity detection, circular dependency detection) and reports all errors with file locations |
| `mosaic migrate` | Apply any pending schema migrations to the current storage backend; includes namespace graph validation pass before applying any changes |
| `mosaic status` | Show current config, adapter type, entity counts, schema version, and installed plugins |
| `mosaic validate-schema` | Validate a LinkML schema file |
| `mosaic reference list` | List all installed reference loaders and their installed versions |
| `mosaic reference install <name> [--version <v>]` | Install a reference dataset; merges schema fragment and runs migrate |
| `mosaic reference update <name> [--version <v>]` | Update an installed reference dataset to a newer release |

**Namespace validation in `mosaic validate --schema`**

When `--schema` is provided (or the schema path is read from `mosaic.yaml`), the validator
runs the full namespace graph validation pass:

1. Discovers all schema files recursively from the schema path (file or directory)
2. Builds the `NamespaceRegistry` from all discovered files
3. Checks for duplicate `(namespace, entity_name)` pairs across files
4. Validates all `references.entity_type` FQNs against the registry
5. Detects circular namespace dependencies via topological sort

Each error identifies the unresolved FQN or circular dependency path and the file where it
originates:

```
SchemaValidationError: Unresolved FQN reference 'ghost.Entity' in field 'sample.ghost_id'
  declared in: schemas/tissue.yaml

SchemaValidationError: Circular namespace dependency detected: tissue → omics → tissue
  first reference: schemas/omics.yaml field 'datafile.sample_id' (references tissue.Sample)

SchemaValidationError: Duplicate entity 'Sample' in namespace 'tissue'
  first declared in: schemas/tissue.yaml
  also declared in: schemas/tissue_legacy.yaml
```

**Namespace validation in `mosaic migrate`**

`mosaic migrate` runs the namespace graph validation pass before generating the migration
plan. If namespace validation fails, the migration is aborted and errors are reported before
any changes are applied. This ensures no schema with broken namespace references can be
applied to storage.

### 2.9 Deployment Tiers

| Tier | Storage Adapter | Transport | Auth | Typical Use |
|---|---|---|---|---|
| **Local** | SQLite | None (SDK direct) | None | Single researcher, notebook |
| **Single-host** | SQLite or PostgreSQL | REST (`mosaic serve`) | None (v0.1) / JWT (future) | Small team, shared server |
| **Cloud (AWS)** | PostgreSQL (RDS) or DynamoDB | REST behind ALB | JWT + RBAC | Enterprise deployment |

All tiers use the same `hippo` Python package. Tier is determined entirely by config.

### 2.10 Concurrency Model

In v0.1 with the SQLite adapter, Hippo uses SQLite's WAL (Write-Ahead Logging) mode, which
supports concurrent reads with a single writer. The REST server runs with multiple Uvicorn
workers (configurable, default 4), each with its own SQLite connection. Write operations acquire
a short-lived lock. This is sufficient for the expected v0.1 workload of infrequent batch
writes and moderate concurrent reads from pipelines.

Connection behavior per adapter:

| Adapter | Concurrency Model |
|---|---|
| SQLite (v0.1) | WAL mode, per-worker connection, short write lock |
| PostgreSQL (future) | Connection pool via `psycopg2`, configurable pool size |
| DynamoDB (future) | Native AWS concurrent access, no connection management |

### 2.11 Error Handling

Errors propagate outward from adapter → SDK → transport layer in a consistent hierarchy:

```
MosaicError (base)
├── EntityNotFoundError       # Requested entity does not exist
├── ValidationError           # Entity failed validation
│   ├── SchemaValidationError     # Failed schema-level field/type/enum check
│   └── RuleValidationError       # Failed a write validator rule (carries validator name)
├── IngestError               # Batch ingestion failure (with row context)
├── AdapterError              # Storage backend error (wraps underlying exception)
├── SearchCapabilityError     # Schema declares a search mode the active adapter doesn't support
└── ConfigError               # Invalid or missing configuration
```

The SDK raises typed `MosaicError` subclasses. The REST layer catches these and maps them to
appropriate HTTP status codes (404, 422, 500, etc.) with a consistent JSON error body:

```json
{
  "error": "EntityNotFoundError",
  "message": "Sample 'abc-123' not found",
  "detail": {}
}
```

Raw adapter exceptions (e.g., SQLite errors) are always wrapped in `AdapterError` before
surfacing — internal implementation details never leak through the SDK boundary.

### 2.12 Dependencies

**Core SDK (required):**

| Package | Purpose |
|---|---|
| `pydantic>=2.0` | Entity models, config validation, schema enforcement |
| `pyyaml` | Config and schema file parsing |
| `typer` | CLI framework |
| `cel-python` | CEL expression language for config-driven validators |

**SQLite adapter (v0.1, included in core):**

| Package | Purpose |
|---|---|
| (stdlib `sqlite3`) | No additional dependency — uses Python standard library |

**REST API (optional install extra):**

| Package | Purpose |
|---|---|
| `fastapi` | REST framework |
| `uvicorn[standard]` | ASGI server |

**References extra (optional):**

| Package | Purpose |
|---|---|
| Bundled `mosaic-reference-*` loaders | FMA, Ensembl, GO, etc. (each is a separate package) |

**Installation extras:**

```bash
pip install hippo                   # Core SDK + SQLite + CEL validators
pip install hippo[rest]             # Adds REST API server
pip install hippo[postgres]         # Adds PostgreSQL adapter (future)
pip install hippo[references]       # Adds bundled reference loader packages
pip install hippo[all]              # Everything

# Community plugins installed alongside:
pip install datahelix-mosaic mosaic-adapter-redshift    # hypothetical storage adapter
pip install datahelix-mosaic mosaic-reference-reactome  # community reference loader
```

Plugin packages should follow the naming convention `mosaic-adapter-<name>` for discoverability.
The Hippo repository provides a `mosaic-adapter-template` cookiecutter for bootstrapping new
adapter packages with the correct entry point declarations, ABC implementations, and test
scaffolding pre-wired.

---


### 2.13 Validation Infrastructure

Hippo's write path enforces two tiers of validation before committing any change.

#### Tier 1: Schema validation (built-in)

Runs on every write. Enforces structural constraints declared in `schema.yaml`:
- Required fields present
- Field types and value ranges
- Enum values within declared set
- `ref` fields point to an existing, available entity of the correct type
- Relationship cardinality constraints

Raises `SchemaValidationError` on failure. Cannot be disabled.

#### Tier 2: Config-driven business rule validators (`validators.yaml`)

Optional. Loaded from the path declared in `mosaic.yaml`. If the key is absent, this tier is
skipped entirely.

**`validators.yaml` config format:**

```yaml
validators:
  - name: <unique identifier used in error messages>
    entity_types: [EntityType, ...]     # omit or null to match all types
    on: [create, update, availability_change, relationship]  # default: all
    expand:                             # paths to pre-fetch before CEL evaluation
      - field_name
      - field_name.child
      - field_name[].child_field        # [] = iterate over relationship collection
    when: '<CEL expression>'            # skip this validator if false
    condition: '<CEL expression>'       # must be true to pass
    requires: [field_a, field_b]        # shorthand: fields must be non-null
    error: "message with {entity.field} and {existing.field} substitutions"
    max_expand_list_size: 200           # default 200, hard cap 1000
```

**CEL evaluation context:**

| Variable | Contents |
|---|---|
| `entity` | Proposed new state (with expand paths pre-fetched and populated in place) |
| `existing` | Current state before write; `null` for creates |

**Expand path mechanics:**
- `field` — scalar `ref` field: replaces the ID value with the full referenced entity dict
- `field.child` — chain: expands `child` on the fetched entity
- `field[]` — relationship collection: replaces list of IDs with list of entity dicts (one batch query)
- `field[].child` — expands `child` on each element of the collection
- Paths sharing a prefix are deduplicated — a shared entity is fetched only once
- Cycle detection via visited set prevents infinite loops on self-referential relationships
- `max_expand_list_size` prevents runaway expansion on large collections

**Write validator execution order:**
```
1. Schema validation (Tier 1, always first → SchemaValidationError on failure)
2. Config-driven validators (Tier 2, declaration order → RuleValidationError on failure)
3. Plugin validators (Tier 3, priority order → RuleValidationError on failure)
4. Commit write + record provenance event
```
The entire sequence is atomic — any failure rolls back the transaction and no provenance event
is written.

**`WriteValidator` ABC** (Tier 3 — Python plugin validators):

```python
class WriteValidator(ABC):
    name: str                           # used in error messages and logs
    entity_types: list[str] | None      # None = run for all types; subtype-aware
    priority: int = 0                   # lower runs first; schema validation = -1

    @abstractmethod
    def validate(self, operation: WriteOperation,
                 client: MosaicClient) -> ValidationResult: ...

@dataclass
class WriteOperation:
    kind: Literal["create", "update", "availability_change", "relationship"]
    entity_type: str
    entity_id: str
    proposed: dict
    existing: dict | None               # None for creates
    actor: str
    provenance_context: dict | None     # structured context from caller (e.g. workflow run)

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
```

**`entity_types` and schema inheritance:** With polymorphic inheritance, `entity_types: [Sample]`
covers `Sample` and all subtypes. `entity_types: [BrainSample]` targets only that subtype.
Redundant entries (child listed with parent) emit a startup warning.

**Built-in validator presets** (ergonomic shortcuts in `validators.yaml`, not separate code paths):

| Preset `type:` | Equivalent pattern |
|---|---|
| `ref_check` | `expand` + `condition` checking a referenced entity |
| `count_constraint` | `expand` with `[]` + `size()` CEL condition |
| `immutable_field` | `on: [update]` + `condition: entity.field == existing.field` |
| `field_required_if` | `when` pre-condition + `requires` |
| `no_self_ref` | `condition` checking entity does not reference itself |

---

### 2.14 Reference Loader System

Reference loaders install community-standard ontology data as regular Hippo entities.
Distributed as `mosaic-reference-<name>` packages, discovered via two entry point groups:
`mosaic.reference_loaders` (the ABC subclass) and `mosaic.reference_loader_cli` (an optional
`typer.Typer` app mounted under `mosaic reference <name> ...`).

**v2 status.** The contract documented in this section is the v2 ReferenceLoader contract.
v1 (locked in [PTS-215](/PTS/issues/PTS-215) against GitHub #6) shipped with two follow-up
gaps that the v1 design left undecided: opt-in `LoadResult.entity_ids` as the `--prune-old`
substrate did not scale to production references (GitHub #15), and the
`entity_type` + `entity_ids` sibling-field shape was ambiguous for multi-class loaders
(GitHub #16). v2 closes both. The substrate moves to a Hippo-internal write log
(§2.14.9). The `LoadResult` shape adopts `EntityRef` and drops the v1 sibling fields
(§2.14.8). v2 is a clean break — the migration touches in-tree fixtures only because no
downstream `mosaic-reference-*` package has shipped against v1. Decisions are recorded in
[sec9 §2.14.J, §2.14.K, §2.14.L](sec9_decisions.md).

**`ReferenceLoader` ABC:**

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel
import typer

class ReferenceLoader(ABC):
    name: str           # e.g. "fma", "ensembl", "go" — must match entry point key
    description: str

    # Optional: Typer app mounted as `mosaic reference <name> <subcmd> ...`
    # Register via the mosaic.reference_loader_cli entry point.
    subcommands_app: typer.Typer | None = None

    # Optional: Pydantic v2 model for load() parameters.
    # When declared, the CLI auto-renders --flag args and validates user input.
    load_params_schema: type[BaseModel] | None = None

    @abstractmethod
    def versions(self) -> list[str]: ...
    # Available version slugs. Format is loader-defined and opaque to Hippo.
    # Loaders SHOULD include "test" as a pseudo-version (see §2.14.7).

    @abstractmethod
    def entity_types(self) -> list[str]: ...
    # Entity type names this loader creates (declarative — for provenance/discovery only).

    @abstractmethod
    def schema_fragment(self) -> dict: ...
    # LinkML fragment defining this loader's entity types and relationships.
    # Must declare default_prefix: matching the loader name (see §2.14.5).

    @abstractmethod
    def load(self, client: MosaicClient, version: str,
             params: BaseModel | None = None) -> LoadResult: ...
    # Ingest the reference dataset at the given version.
    # params is an instance of load_params_schema, or None if not declared.

    def upgrade(self, client: MosaicClient, from_version: str, to_version: str,
                params: BaseModel | None = None) -> LoadResult:
        # Default: runs load(to_version, params). Override for efficient diffs.
        return self.load(client, to_version, params)

    def validate(self, user_artifact) -> ValidationResult:
        # Recommended, not required. Validate a user-supplied artifact against this
        # loader's schema. Default raises NotImplementedError; the CLI surfaces a
        # "this loader doesn't expose a validator" message in that case.
        raise NotImplementedError(f"{self.name} does not implement validate()")
```

**Entry points** (in the loader package's `pyproject.toml`):

```toml
[project.entry-points."mosaic.reference_loaders"]
ensembl = "mosaic_reference_ensembl.loader:EnsemblLoader"

# Optional — only needed if the loader exposes subcommands:
[project.entry-points."mosaic.reference_loader_cli"]
ensembl = "mosaic_reference_ensembl.cli:app"
```

**Install lifecycle (`mosaic reference install <name> [--version <v>]`):**
1. Resolve loader from `mosaic.reference_loaders` entry points
2. Call `loader.schema_fragment()` and merge into deployed schema (see §2.14.5)
3. Run `mosaic migrate` (additive = non-interactive; structural = prompt for confirmation)
4. Call `loader.load(client, version, params)` — source data fetched via `client.cached_fetch`
   (see §2.14.3)
5. Record `{loader_name: version}` in `mosaic_meta` under key `reference_versions`

**Upgrade lifecycle (`mosaic reference upgrade <name> [--version <v>] [--prune-old]`):**
Calls `loader.upgrade(client, from_version, to_version, params)`. The new version's entities
are ingested additively by default; pass `--prune-old` to remove the prior version's rows
after the new install succeeds (see §2.14.4).

**User schema dependency declaration:**

```yaml
# schema.yaml — exact-match pins only for v1 (range comparators are deferred)
requires:
  - mosaic-reference-fma==3.3
  - mosaic-reference-ensembl==mus_musculus.GRCm39.115
```

`mosaic validate` fails fast with a clear install suggestion if a required loader is missing.
Users reference loader-provided entity types by name without redeclaring them.

**Coupling notes (D1 ↔ D4 ↔ D5):**

These three decisions interact. Implementers of the ABC surface must understand all three:

- **D1 → D5**: Sub-command apps declared via `subcommands_app` receive `client` via Typer
  context injection. This means the loader CLI shares the same `MosaicClient` instance (and
  thus the same `client.cache_dir_for(...)` resolution) as `load()`. Any subcommand that fetches source data MUST
  use `client.cached_fetch` — not a private download path.
- **D4 → D1**: `load_params_schema` is shared between `load()` and the CLI. When a subcommand
  triggers a load, it renders `--flag` args from the same Pydantic model, validates them, and
  passes the validated instance to `load()`. The schema is the single source of truth for both
  surfaces.
- **D4 → D5**: `cached_fetch` is the canonical download path for all `load()` calls regardless
  of how they are invoked — directly via SDK, CLI, or subcommand.

---

#### 2.14.1 Version Semantics

Version strings returned by `versions()` are **opaque slugs** whose format is entirely
loader-defined. Hippo does not parse or compare them.

Examples of valid version slugs:
- `"GRCh38.109"` — a single-organism release
- `"mus_musculus.GRCm39.115"` — organism + assembly + release
- `"2024-01-15"` — date-stamped snapshot

**v1 constraint — exact-match `requires:` only.** The `requires:` directive in `schema.yaml`
accepts only exact-match pins (`mosaic-reference-ensembl==mus_musculus.GRCm39.115`). Range
comparators (`>=`, `~=`, `^=`) are deferred to v2, which may introduce an optional
`parse_version(s) -> tuple` ABC method letting Hippo evaluate ranges. Until then, users pin
exact versions; the install check is string equality only.

Users who need a range constraint should pin the lowest acceptable version and upgrade
explicitly with `mosaic reference upgrade`.

---

#### 2.14.2 Load Parameters

Loaders that accept runtime parameters (e.g., organism, gene-biotype filter) declare them via:

```python
class EnsemblParams(BaseModel):
    organism: str = "homo_sapiens"
    release: str | None = None
    gene_biotypes: list[str] = ["protein_coding"]

class EnsemblLoader(ReferenceLoader):
    load_params_schema = EnsemblParams

    def load(self, client, version, params=None):
        p = params or EnsemblParams()
        ...
```

When `load_params_schema` is declared:
- `mosaic reference install ensembl --organism mus_musculus` renders `--flag` args from the
  model and validates them before invoking `load()`.
- `load()` always receives a validated model instance (or `None` if no schema is declared).
- The same model is used for `upgrade()` calls.

Loaders that do not declare a schema receive `params=None` in `load()` and `upgrade()`.

---

#### 2.14.3 Caching Contract

`MosaicClient` exposes two caching primitives for loaders:

```python
client.cache_dir_for(loader_name: str) -> Path
# Resolves to $MOSAIC_CACHE_DIR/<loader_name>/ if set,
# else ~/.cache/mosaic/references/<loader_name>/. Auto-mkdir.

client.cached_fetch(url: str, *, expected_sha256: str | None = None,
                    loader_name: str) -> Path
# Content-addressable HTTP fetch. Returns the local path to the cached
# file. The path is keyed by sha256 of the URL inside
# cache_dir_for(loader_name) so repeated calls are stable. When
# expected_sha256 is supplied the digest is verified on download AND on
# cache hit; mismatch raises CacheIntegrityError and removes the
# offending file so the next call re-downloads cleanly.
```

`MosaicClient` is loader-agnostic — per-loader scoping happens via the `loader_name`
argument so a single client instance can serve many loaders without stateful
per-loader binding.

**MUST rule:** Loaders MUST use `client.cached_fetch` for any network download larger than
1 MB. This enables:
- `mosaic reference clean-cache` to manage disk usage via a single location
- CI mounts of the cache root for deterministic, network-free re-runs
- Reproducible audits (content-addressed, optionally hash-verified)

Loaders that access small static files (<1 MB, bundled in the package) may skip
`cached_fetch` for those files only. All large reference archives MUST go through
`cached_fetch`.

---

#### 2.14.4 Upgrade Semantics

**Default — additive.** `upgrade()` loads the new version's entities alongside the existing
ones. Entities from the old version remain queryable until explicitly pruned. This preserves
existing FK references in user data.

**`--prune-old` — opt-in destructive.** When passed to `mosaic reference upgrade`, Hippo removes
the prior version's rows *after* the new install succeeds. Destructive; gated behind an
explicit flag. Never the default.

The prune substrate is the **reference write log** (§2.14.9), not `LoadResult`. Loader authors
do not opt into prune support — `--prune-old` works for every loader because Hippo records every
`client.put()` call made inside the lifecycle wrapper (§2.14.9). The v1 `LoadResult.entity_ids`
opt-in is removed in v2 (see §2.14.8 transition).

**`upgrade()` ABC method.** The default implementation calls `load(to_version, params)`. Loaders
with efficient diff logic override it:

```python
def upgrade(self, client, from_version, to_version, params=None):
    # e.g., fetch only changed records since from_version
    ...
```

Migrate-with-deprecation semantics (marking old rows as superseded without deleting them) are
deferred to v3 once entity write paths support row-superseded marking.

---

#### 2.14.5 Schema-Fragment Merge Rules

Every loader's `schema_fragment()` return value is a LinkML YAML dict. Three rules govern
how fragments merge into the deployed schema:

**Rule 1 — Mandatory per-loader prefix.** Every fragment MUST declare
`default_prefix: <loader_name>:` (e.g., `default_prefix: ensembl:`). All classes and slots in
the fragment carry that prefix. User schemas reference loader-provided types as `ensembl:Gene`.
Two loaders declaring the same prefix → `ConfigError` at install time with both package names
identified.

**Rule 2 — `imports:` policy.** Loader fragments MUST NOT redeclare `linkml:types` or any
prefix already present in the deployed schema. Hippo strips colliding top-level imports before
merging and supplies them from the merged registry. Loader-private imports (URLs unique to this
loader) pass through unchanged.

**Rule 3 — Traceability annotation.** On merge, Hippo injects
`annotations: { provided_by: { value: "<loader_name>@<package_version>" } }` on every class
and slot introduced by the fragment. This annotation survives in `SchemaView` for runtime
introspection (e.g., `mosaic status` can show which loader owns which classes).

**Collision detection** (existing rule, now superseded by Rule 1): with mandatory unique
prefixes per loader, class-name collisions across loaders can no longer occur. Two loaders
declaring the same prefix → `ConfigError` at install.

---

#### 2.14.6 Cross-Loader Foreign Keys

v1 does **not** validate cross-loader foreign keys and does **not** transitively pull in
loader dependencies via `requires:`.

Loaders that reference entity types from another loader SHOULD annotate their fragment:

```yaml
# In the loader's schema_fragment() return value
annotations:
  loader_depends_on:
    value: "fma"   # or a comma-separated list: "fma,go"
```

At install/validate time, Hippo emits a **warning** (not an error) if a declared
`loader_depends_on` loader is not installed. This warns the developer without blocking the
install.

v2 may promote this to a hard FK with transitive `requires:` resolution once real-world usage
warrants it. This is a known v1 limitation.

---

#### 2.14.7 Test Fixtures

Loaders SHOULD implement a `"test"` pseudo-version:

```python
def versions(self) -> list[str]:
    return ["test", "mus_musculus.GRCm39.115", "homo_sapiens.GRCh38.110"]
```

The `"test"` version SHOULD:
- Return a deterministic, network-free subset bundled in the package (e.g.,
  `<package>/fixtures/tiny.tar.gz`)
- Be usable without credentials or network access
- Be stable across package versions (no external data fetch)

CI pipelines use `mosaic reference install <name> --version test` to install the fixture
dataset without a live network connection. This is a convention, not enforced by the ABC;
loaders that omit `"test"` will not break Hippo, but their downstream consumers lose the
hermetic test path.

`"test"` is a **reserved slug** — Hippo will never generate it as a real release version.

---

#### 2.14.8 `LoadResult` and `EntityRef` (v2 shape)

The v1 `LoadResult` paired `entity_type: str | None` with `entity_ids: list[str]` as sibling
fields whose invariant ("every id shares the declared type") was implicit and easy to violate
in multi-class loaders. v2 replaces that pair with a typed list of `EntityRef` handles. Counter
fields are unchanged — they answer "what happened," while `entities` answers "what was written."

**`EntityRef` — public dataclass:**

```python
# hippo/core/loaders/reference.py
from dataclasses import dataclass

@dataclass(frozen=True)
class EntityRef:
    """Lightweight (id, type) handle to a written entity.

    Returned by Hippo helpers that surface a written entity to a loader,
    and accumulated into ``LoadResult.entities`` so multi-class loaders
    can report heterogeneous writes without an iterable return type.
    """
    id: str
    type: str

    @classmethod
    def from_put_result(cls, rec: dict) -> "EntityRef":
        """Bridge from the dict returned by ``MosaicClient.put`` to an
        ``EntityRef``. Use inside ``load()`` when assembling
        ``LoadResult.entities`` for the advisory inspection list."""
        return cls(id=rec["id"], type=rec["entity_type"])
```

**`LoadResult` v2:**

```python
@dataclass
class LoadResult:
    # Counters — authoritative for "what happened" (D2.14.K-2).
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)

    # Advisory inspection list — see "Advisory contract" below.
    entities: list[EntityRef] = field(default_factory=list)
```

**Advisory contract for `entities`.** `LoadResult.entities` is **advisory only**. Loaders MAY
populate it for inspection / CLI breakdown; consumers MUST NOT treat it as authoritative for
any operation. In particular:

- `--prune-old` is driven by the reference write log (§2.14.9), not by `entities`.
- `entities` MAY be empty (large loaders are encouraged to leave it empty to avoid in-memory
  growth) or partial (a loader may sample one EntityRef per type).
- The CLI breakdown shown by `mosaic reference install` derives counts from `entities` when
  non-empty, otherwise it queries the write log scoped to `(loader_name, version)` to render
  the same per-type counts. Loader authors do not have to choose between "ergonomic CLI" and
  "scale" — both paths produce the same display.

**Recommended loader patterns:**

Small loaders (≤ ~10⁴ entities, single-class or few-class) populate `entities` directly:

```python
def load(self, client, version, params=None):
    entities = []
    for row in parse_fixture():
        entities.append(EntityRef.from_put_result(client.put("FakeTerm", row)))
    return LoadResult(created=len(entities), entities=entities)
```

Large loaders (~10⁵+ entities) skip the in-memory accumulation. The write log captures every
write transparently:

```python
def load(self, client, version, params=None):
    counts = Counter()  # only what the loader needs for its own reporting
    for row in parse_gtf(...):
        client.put("Gene", row)
        counts["Gene"] += 1
    # No EntityRef accumulation; write log is authoritative for prune.
    return LoadResult(created=sum(counts.values()))
```

**Transition from v1 (D2.14.L).** Clean break. `LoadResult.entity_type` and
`LoadResult.entity_ids` are removed in v2. The v1 → v2 migration is mechanical and limited to
in-tree fixtures (`FakeReferenceLoader`, `RichParamsLoader`, `BareParamsLoader`) plus the
lifecycle code in `hippo/cli/commands/reference.py`. Downstream loader count at the point of
v2 cut-over is zero (the Ensembl prototype team held off shipping per the PTS-215 thread), so
the deprecation-window cost would be unrecovered overhead.

**`MosaicClient.put()` return shape — deferred.** GitHub #16 proposes bundling `MosaicClient.put()
-> EntityRef` with this change. v2 declines the bundle: `MosaicClient.put()` retains its
`dict[str, Any]` return (`id`, `entity_type`, `data`, `version`, `created_at`, `updated_at`)
because tests, `TypedClient`, and the ingestion pipeline all index those keys today. A future
v3 decision may revisit `put()`'s return type with its own migration story. The
`EntityRef.from_put_result()` classmethod bridges cleanly in the meantime.

---

#### 2.14.9 Reference Write Log

The reference write log is Hippo's internal substrate for `--prune-old`. It records every
`client.put()` call made while a loader's `load()` or `upgrade()` is active, so prune can
discover the prior version's entity IDs and types without depending on loader-author
diligence. Loader code does not interact with the log directly.

**Schema:**

```sql
CREATE TABLE reference_write_log (
    loader_name TEXT NOT NULL,
    version     TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    written_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (loader_name, version, entity_id)
);

CREATE INDEX idx_reference_write_log_lookup
    ON reference_write_log (loader_name, version);
```

The composite PK collapses repeat writes to the same `(loader_name, version, entity_id)` to a
single row (idempotent for re-runs and for `upgrade()` that re-writes a stable id). The
`(loader_name, version)` index serves bulk prune queries grouped by `entity_type`.

**Loader-context plumbing.** The lifecycle code in `hippo/cli/commands/reference.py` wraps
each `loader.load()` / `loader.upgrade()` invocation in an explicit context manager:

```python
# In install_reference() and upgrade_reference()
with client.load_context(loader_name=name, version=resolved_version):
    load_result = loader.load(client, resolved_version, params)
```

`MosaicClient.load_context()` is the **only** sanctioned way to enter logging mode. Inside the
block, every successful `client.put()` records a row in `reference_write_log` keyed by
`(loader_name, version, entity_id, entity_type)`. Outside the block, `client.put()` is a
no-op for the log (user-data writes, ingestion CLI, REST handler writes, etc., never hit the
log).

Rationale for explicit `with` over ContextVar: lifecycle entry/exit is visible at the call
site, the binding is one stack frame deep, and the scope of "what counts as a load" is
auditable in code review. ContextVar would obscure the boundary; a wrapper-client would force
`MosaicClient` to be wrap-friendly for one caller's benefit.

**Failure recovery.** Write-log inserts share the same SQL transaction as the entity write.
If `loader.load()` raises midway, the in-flight transaction rolls back and no orphan log rows
persist beyond committed entity writes. If a previously committed batch then a later batch
raises (loader does its own batching with explicit commits), committed log rows correspond
exactly to committed entity writes — `--prune-old` on a failed-then-retried install therefore
cleans only what was actually written during the failed attempt. Loader authors who batch
explicitly own that recovery story; Hippo does not retry on their behalf.

**Garbage collection.**

- `mosaic reference upgrade --prune-old` deletes the prior version's entities AND the prior
  version's log rows in the same transaction. After a successful prune, the only log rows
  remaining for `loader_name` correspond to the current installed version.
- `mosaic reference uninstall <name>` (existing verb) deletes all log rows where
  `loader_name = <name>`.
- Log rows for a `loader_name` not currently registered in `mosaic_meta.reference_versions`
  (e.g., loader was force-removed without `uninstall`) are GC candidates. `mosaic reference
  doctor` (existing diagnostic command) reports such orphans; cleanup is manual until v3.

**Upgrade overlap.** When `upgrade()` re-writes a stable `entity_id` under a new version slug,
the log records a new `(loader_name, new_version, entity_id, entity_type)` row alongside the
existing `(loader_name, old_version, entity_id, entity_type)` row. Both rows persist until
prune. This is the intended behavior for the Ensembl `Gene.stable_id` pattern, where the same
gene appears across releases — prune for `old_version` will then attempt to delete the entity
even though the new version still references the same id. Loaders that rely on
stable-id upgrade overlap MUST override `upgrade()` to skip writing rows that are unchanged
across versions, OR accept that `--prune-old` on overlap removes the entity (the new install
will re-create it). v3 may add a `keep_if_unchanged` policy; v2 documents the constraint.

**Replaces `mosaic_meta.reference_entity_ids`.** The v1 implementation persisted entity IDs
as a JSON blob in `mosaic_meta.reference_entity_ids`. v2 deletes that key and migrates to the
write-log table. The migration runs once on first v2 startup: existing JSON entries are
read, written into `reference_write_log` (with `entity_type` resolved by per-entity lookup),
and the `mosaic_meta` key is deleted. Migration is idempotent; subsequent startups are no-ops.

---

### 2.15 Error Hierarchy

All Hippo exceptions inherit from `MosaicError`. The full hierarchy is specified in
`appendix_b_implementation_guide.md §B.3`. Summary:

**Configuration errors (startup time):**

| Exception | Trigger | HTTP |
|---|---|---|
| `ConfigError` | Invalid/missing `mosaic.yaml` fields; unknown config keys | — |
| `SchemaError` | Invalid `schema.yaml`; cycles; unknown type references | — |
| `MigrationError` | Destructive migration attempt | — |
| `SearchCapabilityError` | Schema declares unsupported search mode | — |
| `AdapterNotFoundError` | No adapter registered for configured type | — |

**Runtime errors (request time):**

| Exception | Trigger | HTTP |
|---|---|---|
| `ValidationError` | Write rejected by any validator tier | 422 |
| `EntityNotFoundError` | Entity UUID not found or unavailable | 404 |
| `RelationshipNotFoundError` | Relationship edge UUID not found | 404 |
| `ExternalIdNotFoundError` | No entity for `(system, external_id)` | 404 |
| `ExternalIdConflictError` | ExternalID already belongs to a different entity | 409 |
| `SchemaVersionMismatchError` | Instance schema differs from storage (future v0.2) | 503 |
| `AdapterError` | Storage backend failure; wraps underlying exception | 500 |

**Wrapping rule:** Adapters must catch all backend-specific exceptions and re-raise as
`AdapterError(cause=original)`. Raw `sqlite3` or `psycopg2` exceptions must never
reach the caller. See `appendix_b_implementation_guide.md §B.3` for full class definitions.

---
