# Mosaic API Reference

## SDK Validation API

Validation runs automatically when you call `create`, `update`, or `delete` on `MosaicClient`. The pipeline executes registered validators in order and raises `ValidationFailure` on the first failure. You do not need to call validators directly unless you are building custom validation logic.

### ValidationPipeline

The `ValidationPipeline` class provides sequential execution of validators with fail-fast behavior.

```python
from mosaic.core.pipeline import ValidationPipeline, create_pipeline
from mosaic.core.validation import WriteOperation, WriteValidator, ValidationResult
```

**Core Methods:**

| Method | Description |
|--------|-------------|
| `add_validator(validator)` | Register a validator to the pipeline |
| `execute(operation)` | Execute validators in fail-fast mode (stop on first failure) |
| `execute_all(operation)` | Execute all validators and report all failures |
| `get_validators()` | Get list of registered validators in order |
| `get_validator_count()` | Get number of registered validators |
| `clear_validators()` | Clear all registered validators |

**Usage Example:**

```python
# Create a pipeline
pipeline = ValidationPipeline()

# Add validators
class RequiredFieldsValidator(WriteValidator):
    def validate(self, operation: WriteOperation) -> ValidationResult:
        if not operation.data.get("name"):
            return ValidationResult(is_valid=False, errors=["name is required"])
        return ValidationResult(is_valid=True)

class UniqueIdValidator(WriteValidator):
    def validate(self, operation: WriteOperation) -> ValidationResult:
        # Check for duplicate IDs
        return ValidationResult(is_valid=True)

pipeline.add_validator(RequiredFieldsValidator())
pipeline.add_validator(UniqueIdValidator())

# Validate an operation (fail-fast)
operation = WriteOperation(
    operation="insert",
    entity_type="Sample",
    data={"id": "123", "name": "Test"}
)
result = pipeline.execute(operation)

# Validate all (report all failures)
result = pipeline.execute_all(operation)
```

**Fail-Fast Behavior:**
- `execute()` stops on first validation failure and returns immediately
- `execute_all()` runs all validators and aggregates all errors

### MosaicClient

The main SDK client for Mosaic with integrated validation pipeline support.

```python
from mosaic import MosaicClient
```

**Constructor:**

```python
client = MosaicClient(
    pipeline=None,           # Optional ValidationPipeline instance
    bypass_validation=False  # DEPRECATED: Skip validation
)
```

**Methods:**

| Method | Description |
|--------|-------------|
| `validate(operation)` | Validate a write operation using the pipeline |
| `add_validator(validator)` | Add a validator to the client's pipeline |
| `create(entity_type, data, bypass_validation=None)` | Create an entity with validation |
| `update(entity_type, entity_id, data, bypass_validation=None, actor=None, provenance_context=None)` | Update an entity with validation |
| `delete(entity_type, entity_id, bypass_validation=None)` | Delete an entity with validation |

**Usage Example:**

```python
from mosaic import MosaicClient
from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.validation import WriteOperation, WriteValidator, ValidationResult

# Create a client with a pipeline
pipeline = ValidationPipeline()
pipeline.add_validator(MyCustomValidator())
client = MosaicClient(pipeline=pipeline)

# Create an entity (validation runs automatically)
try:
    entity = client.create("Sample", {"id": "123", "name": "Test"})
except ValidationFailure as e:
    print(f"Validation failed: {e.format_detailed_message()}")

# Update an entity
entity = client.update("Sample", "123", {"name": "Updated"})

# Update with actor and provenance context
entity = client.update(
    "Sample", "123",
    {"name": "Updated"},
    actor="pipeline-run-456",
    provenance_context={"reason": "Corrected tissue annotation"}
)

# Delete an entity
client.delete("Sample", "123")
```

### ValidationFailure

Exception raised when a write operation fails validation.

```python
from mosaic.core.exceptions import ValidationFailure
```

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `message` | `str` | Error message |
| `rule_id` | `str \| None` | Identifier of the validation rule that failed |
| `input_context` | `dict` | Input data that caused the failure |
| `entity_type` | `str \| None` | Type of entity being validated |
| `entity_id` | `str \| None` | ID of entity being validated |

**Methods:**

| Method | Description |
|--------|-------------|
| `format_detailed_message()` | Returns formatted string with all failure details |

**Usage Example:**

```python
from mosaic.core.exceptions import ValidationFailure

try:
    client.create("Sample", {"id": "123"})
except ValidationFailure as e:
    print(f"Rule: {e.rule_id}")
    print(f"Message: {e.message}")
    print(f"Context: {e.input_context}")
    print(f"Detailed: {e.format_detailed_message()}")
```

### RelationshipManager

The `RelationshipManager` class provides methods for managing relationships between entities.

```python
from mosaic.core import RelationshipManager
```

**Constructor:**

```python
manager = RelationshipManager(
    storage=sqlite_adapter,  # Optional: SQLiteAdapter instance
    user_context="user-123"   # Optional: User ID for audit
)
```

**Access via MosaicClient:**

```python
from mosaic import MosaicClient

client = MosaicClient(storage=sqlite_adapter)
manager = client.relationships
```

**Methods:**

| Method | Description |
|--------|-------------|
| `relate(source_id, target_id, relationship_type, metadata=None)` | Create a relationship between two entities |
| `unrelate(source_id, target_id, relationship_type)` | Remove a relationship between entities |
| `traverse(source_id, relationship_type=None, max_depth=10)` | Traverse relationships from a starting entity |
| `find_relationships(source_id=None, target_id=None, relationship_type=None)` | Find relationships matching criteria |

**relate()**

Creates a relationship between two entities.

```python
result = client.relationships.relate(
    source_id="entity-1",
    target_id="entity-2",
    relationship_type="contains",
    metadata={"note": "Primary container"}
)
```

**unrelate()**

Removes a relationship between entities.

```python
client.relationships.unrelate(
    source_id="entity-1",
    target_id="entity-2",
    relationship_type="contains"
)
```

**traverse()**

Traverses relationships from a starting entity using recursive query.

```python
results = client.relationships.traverse(
    source_id="entity-1",
    relationship_type="contains",  # Optional: filter by type
    max_depth=5                     # Optional: limit depth (default: 10, max: 100)
)
```

**Exceptions:**

| Exception | Description |
|-----------|-------------|
| `EntityNotFoundError` | Source or target entity doesn't exist |
| `RelationshipExistsError` | Relationship already exists (if duplicate check needed) |
| `RelationshipNotFoundError` | Relationship doesn't exist when trying to remove |

## Error Handling

All Mosaic SDK exceptions inherit from `MosaicError`. Catch the base class to handle any SDK error, or catch specific subclasses for finer-grained control.

**Exception Hierarchy:**

| Exception | Parent | Description |
|-----------|--------|-------------|
| `MosaicError` | `Exception` | Base class for all Mosaic SDK errors |
| `ConfigError` | `MosaicError` | Configuration loading and validation errors |
| `SchemaError` | `MosaicError` | Schema parsing and processing errors |
| `ValidationError` | `MosaicError` | Internal data validation errors |
| `EntityNotFoundError` | `MosaicError` | Entity not found in the system |
| `AdapterError` | `MosaicError` | Storage or external adapter errors |
| `ValidationFailure` | `MosaicError` | Write operation failed validation pipeline |

**Example:**

```python
from mosaic import MosaicClient
from mosaic.core.exceptions import MosaicError, EntityNotFoundError, ValidationFailure

client = MosaicClient()

# Catch any SDK error
try:
    entity = client.get("Sample", "sample-123")
except MosaicError as e:
    print(f"Mosaic error: {e.message}")

# Catch specific errors
try:
    entity = client.get("Sample", "sample-123")
except EntityNotFoundError as e:
    print(f"Entity not found: {e.entity_type} / {e.entity_id}")

try:
    client.create("Sample", {"id": "123"})
except ValidationFailure as e:
    print(f"Validation failed: {e.format_detailed_message()}")
    print(f"  Rule:    {e.rule_id}")
    print(f"  Context: {e.input_context}")
```

**HTTP status mapping (REST layer):**

When an SDK exception escapes a REST handler, the API factory maps it to a
meaningful HTTP status with the standard error body (`{"error": ..., "detail":
...}`), so clients can distinguish causes rather than seeing an anonymous 500
(sec4 §4.3). Status codes are resolved by the exception's class hierarchy
(most-specific match wins):

| Exception | HTTP status | `error` |
|-----------|-------------|---------|
| `EntityNotFoundError` | 404 | `Entity Not Found` |
| `EntityAlreadySupersededError` | 409 | `Entity Already Superseded` |
| `ConfigError` (e.g. adapter conflict) | 409 | `Configuration Error` |
| `ValidationError` | 422 | `Validation Error` |
| `ValidationFailed` | 422 | `Validation Failed` (tier-tagged envelope, sec9 §9.9) |
| `ValidationFailure` | 422 | `Validation Failed` |
| `IngestionError` (incl. `IngestionValidationError`) | 400 | `Ingestion Error` |
| `SearchCapabilityError` | 400 | `Search Capability Error` |
| `TemporalQueryError` | 400 | `Temporal Query Error` |
| `SchemaError` | 400 | `Schema Error` |
| `AdapterError` | 500 | `Storage Adapter Error` |
| `ProvenanceIntegrityError` | 500 | `Provenance Integrity Error` |
| Any other `MosaicError` (recipe/migration/...) | 500 | The exception class name |
| Any non-Mosaic exception | 500 | `Internal Server Error` (detail withheld) |

All 5xx responses are logged server-side with full tracebacks.

## REST API

All REST endpoints (except health checks) require authentication via Bearer token header:

```
Authorization: Bearer <token>
```

Requests missing this header or using an invalid format receive a `401 Unauthorized` response.

### Request Headers for Writes

All write endpoints (`POST`, `PUT`, `DELETE`) accept the following optional headers for provenance tracking:

| Header | Required | Description |
|--------|----------|-------------|
| `X-DataHelix-Actor` | Required on writes | Identity of the actor performing the write. Defaults to `"anonymous"` in v0.1 if omitted |
| `X-Mosaic-Context` | Optional | JSON-encoded provenance context (e.g., `{"pipeline": "rnaseq-v2", "run_id": "abc"}`) |

**Example:**

```bash
curl -X POST http://127.0.0.1:8000/ingest \
  -H "Authorization: Bearer dev-token" \
  -H "X-DataHelix-Actor: actor:pipeline-run-789" \
  -H "X-Mosaic-Context: {\"pipeline\": \"rnaseq-v2\", \"run_id\": \"abc\"}" \
  -H "Content-Type: application/json" \
  -d '{"entity_type": "Sample", "data": {...}}'
```

### Response Envelope

All REST endpoints return responses in a standard envelope format:

```json
{
  "data": { ... },
  "error": null,
  "meta": {
    "schema_version": "1.1",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `data` | `object \| array \| null` | The response payload. `null` when an error occurs |
| `error` | `object \| null` | Error details when the request fails. `null` on success |
| `meta` | `object` | Request metadata including `schema_version` and a unique `request_id` |

Error responses use the same envelope with `data` set to `null`:

```json
{
  "data": null,
  "error": {
    "code": 404,
    "message": "Entity not found: sample-999"
  },
  "meta": {
    "schema_version": "1.1",
    "request_id": "550e8400-e29b-41d4-a716-446655440001"
  }
}
```

---

### Health & Status

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check (unauthenticated) |
| GET | `/status` | System status: adapter, schema version, entity counts, capabilities |
| GET | `/` | API root with version info |

**GET /health**

Returns health status of the service.

*Response:*
```json
{
  "data": {
    "status": "healthy",
    "service": "mosaic"
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

**GET /**

Returns API information.

*Response:*
```json
{
  "data": {
    "service": "Mosaic API",
    "version": "0.5.0",
    "docs": "/docs"
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes: None (unauthenticated)*

**GET /status**

Returns a system status summary. Authenticated — unlike `/health`, the
response describes the deployment's data. Mirrors the SDK's
`client.status()`.

*Response:*
```json
{
  "service": "mosaic",
  "version": "0.8.0",
  "adapter": "SQLiteAdapter",
  "schema_version": "1.2.3",
  "entity_types": ["Donor", "Sample"],
  "entity_counts": { "Donor": 12, "Sample": 240 },
  "capabilities": {
    "search": ["fts"],
    "staged_transaction": true
  }
}
```

Entity counts include unavailable (soft-deleted / superseded) entities —
Mosaic never hard-deletes. `adapter` and `schema_version` are `null` when
the server runs without a storage adapter or schema registry.

```python
# SDK equivalent
status = client.status()
```

---

### Entities

| Method | Path | Description |
|--------|------|-------------|
| GET | `/entities` | List entities with filtering |
| GET | `/entities/{entity_id}` | Get entity by ID |
| PUT | `/entities/{entity_type}/{entity_id}` | Update an existing entity |
| DELETE | `/entities/{entity_id}` | Soft delete an entity |

**GET /entities**

List entities with optional filtering and pagination.

*Query Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_type` | string | Filter by entity type |
| `limit` | int | Max results (1-1000, default: 100) |
| `offset` | int | Results to skip (default: 0) |
| `tissue_type` | string | Filter by field value (repeat for OR: `?tissue_type=brain&tissue_type=liver`) |
| `filter` | string | CEL filter expression (URL-encoded). Example: `?filter=data.age_at_collection%20%3E%2018` |
| `updated_since` | string | ISO 8601 watermark for polling: only entities whose provenance-derived `updated_at` is strictly greater are returned, ordered by `updated_at` ascending. Invalid timestamps return `400 Temporal Query Error` |

*Multi-value parameters (OR within a field):*

Repeat a query parameter to match any of the provided values (OR logic):

```
GET /entities?entity_type=Sample&tissue_type=brain&tissue_type=liver
```

This returns samples where `tissue_type` is `"brain"` OR `"liver"`.

*CEL filter parameter:*

Use the `filter` parameter for advanced filtering with CEL (Common Expression Language) expressions:

```
GET /entities?entity_type=Sample&filter=data.age_at_collection%20%3E%2018
```

The CEL expression context provides the following variables:

| Variable | Type | Description |
|----------|------|-------------|
| `data` | object | The entity's user-defined data fields |
| `id` | string | The entity's internal ID |
| `is_available` | bool | The entity's availability status |
| `created_at` | timestamp | When the entity was created |
| `updated_at` | timestamp | When the entity was last modified |

*Polling for changes (`updated_since`):*

Pass an ISO 8601 watermark to fetch only entities modified after it. Results
are ordered by `updated_at` ascending; persist the `updated_at` of the last
entity processed as the watermark for the next poll. The comparison uses
Mosaic's server-side provenance timestamps (UTC), so callers never need to
trust their own clock. Omit `entity_type` to poll across all entity types:

```
GET /entities?entity_type=Sample&updated_since=2024-01-01T00:00:00Z&limit=500
```

```python
# SDK equivalent
recent = client.query_updated_since(
    entity_type="Sample",
    since="2024-01-01T00:00:00Z",
    limit=500,
)
```

*SDK equivalents:*

```python
# Multi-value OR filter
results = client.query("Sample", tissue_type=["brain", "liver"])

# CEL filter
results = client.query("Sample", filter='data.age_at_collection > 18')

# Combined
results = client.query(
    "Sample",
    tissue_type=["brain", "liver"],
    filter='data.rin_score >= 7.0'
)
```

*Response:*
```json
{
  "data": {
    "items": [],
    "total": 0,
    "limit": 100,
    "offset": 0
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

**GET /entities/{entity_id}**

Get an entity by ID.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Query Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `expand` | string | Expand path for related entities |

*Response:* Entity object with all fields

*Error Codes:* `404` - Entity not found

**PUT /entities/{entity_type}/{entity_id}**

Explicit update of an existing entity. Unlike the `POST /ingest` upsert endpoint, this requires the entity to already exist and returns `404` if it does not. Supports partial update semantics — only the fields included in the request body are modified.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_type` | string | Entity type (e.g., `Sample`, `Donor`) |
| `entity_id` | string | Entity ID |

*Request Headers:*
| Header | Required | Description |
|--------|----------|-------------|
| `X-DataHelix-Actor` | Required | Identity of the actor performing the update |
| `X-Mosaic-Context` | Optional | JSON-encoded provenance context |

*Request Body:*
```json
{
  "data": {
    "brain_region": "Temporal Cortex",
    "notes": "Updated region annotation"
  }
}
```

*Response:*
```json
{
  "data": {
    "id": "donor-1",
    "entity_type": "Donor",
    "data": {
      "donor_id": "AD-001",
      "name": "Subject AD-001",
      "age": 78,
      "sex": "M",
      "diagnosis": "Alzheimer's Disease",
      "brain_region": "Temporal Cortex",
      "notes": "Updated region annotation"
    },
    "version": 2,
    "created_at": "2026-03-17T10:30:00Z",
    "updated_at": "2026-03-17T11:15:00Z",
    "is_available": true
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes:*
- `404` - Entity not found (entity must already exist)
- `422` - Validation failure

*SDK Equivalent:*

```python
# Update an existing entity
entity = client.update(
    "Donor", "donor-1",
    {"brain_region": "Temporal Cortex", "notes": "Updated region annotation"},
    actor="user@example.com",
    provenance_context={"reason": "Corrected region annotation"}
)
```

**DELETE /entities/{entity_id}**

Soft delete an entity (sets `is_available` to false).

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Response:*
```json
{
  "data": {
    "status": "deleted",
    "entity_id": "entity-123"
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes:* `404` - Entity not found

---

### Search

| Method | Path | Description |
|--------|------|-------------|
| GET | `/search` | Full-text search of entities |

**GET /search**

Search entities using full-text search.

*Query Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_type` | string | Entity type to search (required) |
| `q` | string | Search query string (required) |
| `limit` | int | Max results (1-1000, default: 100) |
| `offset` | int | Results to skip (default: 0) |

*Response:* Array of matching entities

---

### History

| Method | Path | Description |
|--------|------|-------------|
| GET | `/entities/{entity_id}/history` | Get entity change history |

**GET /entities/{entity_id}/history**

Get the change history/provenance log for an entity.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Response:* Array of history records with timestamps and changes

*Error Codes:* `404` - Entity not found

---

### Relationships

| Method | Path | Description |
|--------|------|-------------|
| GET | `/entities/{entity_id}/relationships` | List entity relationships |
| POST | `/entities/{entity_id}/relationships` | Create a relationship |
| DELETE | `/entities/{entity_id}/relationships/{rel_id}` | Delete a relationship |

**GET /entities/{entity_id}/relationships**

Get all relationships for an entity.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Response:* Array of relationship objects

*Error Codes:* `404` - Entity not found

**POST /entities/{entity_id}/relationships**

Create a relationship between two entities.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Source entity ID |

*Request Body:*
```json
{
  "target_entity_id": "entity-456",
  "relationship_type": "contains"
}
```

*Response:*
```json
{
  "data": {
    "id": "entity-123->entity-456:contains",
    "source_entity_id": "entity-123",
    "target_entity_id": "entity-456",
    "relationship_type": "contains"
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes:* `404` - Source or target entity not found

**DELETE /entities/{entity_id}/relationships/{rel_id}**

Delete a relationship by its ID.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Source entity ID |
| `rel_id` | string | Relationship ID (e.g. `entity-123->entity-456:contains`) |

*Response:*
```json
{
  "data": {
    "status": "deleted",
    "relationship_id": "entity-123->entity-456:contains"
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

---

### Ingest

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ingest` | Create or upsert an entity |

**POST /ingest**

Create a new entity via the ingestion endpoint. If an entity with the same ID already exists, this performs an upsert.

*Request Body:*
```json
{
  "entity_type": "Sample",
  "data": {
    "id": "sample-123",
    "name": "Test Sample",
    "status": "active"
  }
}
```

*Response:* Created entity with generated ID and timestamps

*Error Codes:*
- `400` - Invalid JSON format
- `422` - Missing required fields or validation failure

---

### Schemas

| Method | Path | Description |
|--------|------|-------------|
| GET | `/schemas` | List all schemas |
| GET | `/schemas/{name}` | Get schema by name |
| POST | `/schemas` | Create a new schema |

**GET /schemas**

List all available entity schemas.

*Response:* Array of schema definitions

**GET /schemas/{name}**

Get a schema by name.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Schema name |

*Response:* Schema definition with fields

*Error Codes:* `404` - Schema not found

**POST /schemas**

Create a new schema.

*Request Body:*
```json
{
  "name": "Sample",
  "version": "1.0",
  "fields": [
    {"name": "id", "field_type": "string", "required": true},
    {"name": "name", "field_type": "string", "required": true}
  ]
}
```

*Response:*
```json
{
  "data": {
    "name": "Sample",
    "version": "1.0",
    "status": "created"
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

---

### Supersession

| Method | Path | Description |
|--------|------|-------------|
| POST | `/entities/{entity_id}/supersede` | Supersede an entity's external ID |
| GET | `/entities/{entity_id}/superseded` | Get superseded external IDs |

**POST /entities/{entity_id}/supersede**

Supersede an entity's external ID with a new one.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Request Body:*
```json
{
  "old_external_id": "EXT-001",
  "new_external_id": "EXT-002"
}
```

*Response:* New external ID record

*Error Codes:* `404` - Entity not found

**GET /entities/{entity_id}/superseded**

Get superseded external IDs for an entity.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Response:* Array of superseded external ID records

*Error Codes:* `404` - Entity not found

---

### External References (xref)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/xref/{system}/{value}` | Resolve an external reference to its entity |

**GET /xref/{system}/{value}**

Reverse lookup over `mosaic_external_xref`-annotated `ExternalReference`
slots: returns the full envelope of the single AVAILABLE entity whose
annotated slot carries the `(system, value)` pair. An entity's external
references themselves are ordinary slot data on the entity endpoints.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `system` | string | External system name (e.g., "STARLIMS") |
| `value` | string | Identifier value in that system |

*Response:* Entity envelope (same shape as `GET /entities/{id}`)

*Error Codes:* `404` - No available entity holds the pair; `501` - storage adapter does not implement the xref index (PostgreSQL)

---

### External IDs (deprecated)

> **Deprecated (issue #48):** these endpoints are backed by the deprecated
> `ExternalID` entity and are marked `deprecated` in OpenAPI. Use
> `ExternalReference` slots and `GET /xref/{system}/{value}` instead.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/external-ids/{id_type}/{external_id}` | Get entity by external ID |
| GET | `/entities/{entity_id}/external-ids` | List entity's external IDs |
| POST | `/entities/{entity_id}/external-ids` | Register external ID |

**GET /external-ids/{id_type}/{external_id}**

Get an entity by its external ID.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `id_type` | string | External ID type (e.g., "lims", "barcode") |
| `external_id` | string | External ID value |

*Response:* Entity object

*Error Codes:* `404` - Entity not found

**GET /entities/{entity_id}/external-ids**

List all external IDs for an entity.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Query Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `include_superseded` | bool | Include superseded IDs (default: false) |

*Response:* Array of external ID records

*Error Codes:* `404` - Entity not found

**POST /entities/{entity_id}/external-ids**

Register an external ID for an entity.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Request Body:*
```json
{
  "external_id": "LIMS-12345"
}
```

*Response:* Created external ID record

*Error Codes:* `404` - Entity not found

---

### Availability

| Method | Path | Description |
|--------|------|-------------|
| GET | `/entities/{entity_id}/availability` | Get entity availability status |
| POST | `/entities/{entity_id}/availability` | Set entity availability status |

**GET /entities/{entity_id}/availability**

Get the availability status of an entity.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Response:*
```json
{
  "data": {
    "entity_id": "entity-123",
    "entity_type": "Sample",
    "is_available": true
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes:* `404` - Entity not found

**POST /entities/{entity_id}/availability**

Set the availability status of an entity, either way, as its own type. The
change and its reason are recorded as an `availability_change` provenance entry.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_id` | string | Entity ID |

*Request Body:*
```json
{
  "is_available": false,
  "reason": "depleted"
}
```

`reason` is optional.

*Response:*
```json
{
  "data": {
    "entity_id": "entity-123",
    "entity_type": "Sample",
    "is_available": false
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes:* `404` - Entity not found

---

### Bulk Availability

| Method | Path | Description |
|--------|------|-------------|
| POST | `/entities/{entity_type}/bulk-availability` | Set availability on multiple entities |

**POST /entities/{entity_type}/bulk-availability**

Set the availability status on multiple entities of the same type in a single request. Errors are isolated per record — a failure on one entity does not prevent others from being updated.

*Path Parameters:*
| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_type` | string | Entity type (e.g., `Sample`, `Donor`) |

*Request Headers:*
| Header | Required | Description |
|--------|----------|-------------|
| `X-DataHelix-Actor` | Required | Identity of the actor performing the operation |
| `X-Mosaic-Context` | Optional | JSON-encoded provenance context |

*Request Body:*
```json
{
  "entity_ids": ["sample-1", "sample-2", "sample-3"],
  "available": false,
  "reason": "Batch archived after QC failure",
  "actor": "qc-pipeline-v3"
}
```

*Response (all succeeded):*
```json
{
  "data": {
    "updated": 3,
    "errors": []
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Response (partial failure):*
```json
{
  "data": {
    "updated": 2,
    "errors": [
      {
        "entity_id": "sample-3",
        "code": 404,
        "message": "Entity not found: sample-3"
      }
    ]
  },
  "error": null,
  "meta": { "schema_version": "1.1", "request_id": "uuid" }
}
```

*Error Codes:*
- `200` - All entities updated successfully
- `207` - Partial success (some entities failed; see `errors` array)
- `404` - Unknown entity type

*SDK Equivalent:*

```python
# Set availability on multiple entities
result = client.set_availability_bulk(
    entity_type="Sample",
    entity_ids=["sample-1", "sample-2", "sample-3"],
    available=False,
    reason="Batch archived after QC failure",
    actor="qc-pipeline-v3"
)

print(f"Updated: {result['updated']}")
for err in result['errors']:
    print(f"  Failed: {err['entity_id']} — {err['message']}")
```

---

### Error Codes Summary

| Code | Description |
|------|-------------|
| `200` | OK - Request succeeded |
| `207` | Multi-Status - Partial success (bulk operations) |
| `400` | Bad Request - Invalid JSON format |
| `401` | Unauthorized - Missing or invalid Bearer token |
| `404` | Not Found - Entity/Schema not found |
| `422` | Unprocessable Entity - Validation failed |
| `500` | Internal Server Error |
