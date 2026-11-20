# tui-backend Specification

## Purpose
TBD - created by archiving change tui-browser-schema-explorer. Update Purpose after archive.
## Requirements
### Requirement: TUIBackend protocol defines the data access contract
The system SHALL define a `TUIBackend` `typing.Protocol` in `src/mosaic/tui/backend/protocol.py`
exposing the following methods: `list_entity_types()`, `list_entities(type, page, filter)`,
`get_entity(type, id)`, `get_schema()`, and `get_provenance(type, id)`. All methods SHALL be
async. All TUI views SHALL call only `TUIBackend` methods and SHALL NOT import SDK or HTTP
client classes directly.

#### Scenario: Views remain backend-agnostic
- **WHEN** a TUI view calls `backend.list_entity_types()`
- **THEN** the view receives a `list[EntityTypeSummary]` regardless of whether the backend is `SDKBackend` or `RESTBackend`

#### Scenario: Mock backend satisfies the protocol
- **WHEN** a test provides an object implementing all `TUIBackend` methods
- **THEN** any TUI view or widget can accept it without modification

---

### Requirement: SDKBackend wraps MosaicClient for SDK-direct mode
The system SHALL provide an `SDKBackend` class in `src/mosaic/tui/backend/sdk.py` that
implements `TUIBackend`. `SDKBackend` SHALL instantiate `MosaicClient` with a `SQLiteAdapter`
using the configured `db_path`. All synchronous `MosaicClient` calls SHALL be dispatched via
`asyncio.to_thread()` so that Textual's async event loop is never blocked.

#### Scenario: SDK backend resolves db_path from config
- **WHEN** `SDKBackend` is constructed without an explicit `db_path`
- **THEN** it reads `db_path` from `config.json` in the current working directory

#### Scenario: SDK backend resolves db_path from explicit flag
- **WHEN** `SDKBackend` is constructed with an explicit `db_path` argument
- **THEN** it uses that path and ignores `config.json`

#### Scenario: Synchronous MosaicClient call does not block event loop
- **WHEN** `SDKBackend.list_entities()` is awaited during TUI rendering
- **THEN** the call executes in a thread via `asyncio.to_thread()` and the UI event loop remains responsive

---

### Requirement: RESTBackend calls a running mosaic serve instance
The system SHALL provide a `RESTBackend` class in `src/mosaic/tui/backend/rest.py` that
implements `TUIBackend` using `httpx.AsyncClient`. The default base URL SHALL be
`http://127.0.0.1:8000`. The default auth token SHALL be `dev-token`, overridable via `--token`
CLI flag or the `MOSAIC_TUI_TOKEN` environment variable. The `MOSAIC_TUI_TOKEN` environment
variable SHALL take lower precedence than the `--token` flag.

#### Scenario: REST backend uses Bearer token from flag
- **WHEN** `mosaic tui --backend rest --token mytoken` is invoked
- **THEN** `RESTBackend` sends `Authorization: Bearer mytoken` on every request

#### Scenario: REST backend falls back to env variable
- **WHEN** `MOSAIC_TUI_TOKEN=envtoken` is set and `--token` is not provided
- **THEN** `RESTBackend` sends `Authorization: Bearer envtoken`

#### Scenario: REST backend uses default token when neither flag nor env is set
- **WHEN** neither `--token` nor `MOSAIC_TUI_TOKEN` is provided
- **THEN** `RESTBackend` sends `Authorization: Bearer dev-token`

#### Scenario: REST backend reports connection failure gracefully
- **WHEN** the `mosaic serve` instance is unreachable
- **THEN** the TUI displays an error message in the status bar and does not crash

---

### Requirement: Backend factory selects implementation from --backend flag
The system SHALL provide a `create_backend(mode, **kwargs) -> TUIBackend` factory function.
When `mode` is `"sdk"` it SHALL return `SDKBackend`; when `mode` is `"rest"` it SHALL return
`RESTBackend`. The factory SHALL raise `ValueError` for unrecognised modes.

#### Scenario: Factory returns SDKBackend for sdk mode
- **WHEN** `create_backend("sdk", db_path="hippo.db")` is called
- **THEN** the returned object is an `SDKBackend` instance

#### Scenario: Factory returns RESTBackend for rest mode
- **WHEN** `create_backend("rest", url="http://localhost:8000", token="t")` is called
- **THEN** the returned object is a `RESTBackend` instance

#### Scenario: Factory raises for unknown mode
- **WHEN** `create_backend("graphql")` is called
- **THEN** a `ValueError` is raised with a descriptive message

---

### Requirement: Shared data types are defined in the protocol module
The system SHALL define the following dataclasses or typed dicts in `protocol.py`:
`EntityTypeSummary` (name, count), `PagedResult` (items, page, total_pages),
`EntityDetail` (id, entity_type, fields, relationships), `SchemaView` (entity_types, relationships),
and `ProvenanceEvent` (event_type, timestamp, diff). These types SHALL be the sole data
transfer objects between backends and views.

#### Scenario: EntityDetail includes relationships
- **WHEN** `get_entity(type, id)` is called
- **THEN** the returned `EntityDetail.relationships` contains a list of related entity references with type and id

#### Scenario: PagedResult carries total page count
- **WHEN** `list_entities()` returns results for a type with more than 20 entities
- **THEN** `PagedResult.total_pages` is greater than 1

