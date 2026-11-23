"""Core SDK types for Mosaic.

The core module provides the fundamental types and abstractions for the Mosaic
Metadata Tracking Service SDK.

Key Components:
- Validation: SchemaValidator, ValidationResult, WriteOperation
- Storage: EntityStore, ValidatingEntityStore
- Types: ProvenanceRecord, IngestResult, Filter

Quick Start - Validation:
```python
from mosaic.core.validation import SchemaValidator, SchemaValidationConfig
from mosaic.linkml_bridge import SchemaRegistry

registry = SchemaRegistry.from_path("schemas/")
config = SchemaValidationConfig(registry=registry)
validator = SchemaValidator(config)

from mosaic.core.validation import WriteOperation
op = WriteOperation(
    operation="insert",
    entity_type="Sample",
    data={"id": "123", "name": "Test"},
)
result = validator.validate(op)
if not result.is_valid:
    print(f"Errors: {result.errors}")
```
"""

from mosaic.core.client import MosaicClient
from mosaic.core.ingestion_service import IngestionService
from mosaic.core.provenance_service import ProvenanceService
from mosaic.core.query_service import QueryService
from mosaic.core.schema_manager import SchemaManager
from mosaic.core.exceptions import (
    AdapterError,
    ConfigError,
    EntityAlreadySupersededError,
    EntityKindMismatchError,
    EntityNotFoundError,
    EntityTypeConflictError,
    MosaicError,
    SchemaError,
    ValidationError,
    ValidationFailure,
)
from mosaic.core.middleware import (
    AuthMiddleware,
    PassThroughAuthMiddleware,
    RequestContext,
    create_auth_middleware,
)
from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.relationship import (
    RelationshipExistsError,
    RelationshipManager,
    RelationshipNotFoundError,
)
from mosaic.core.types import (
    Filter,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    IngestResult,
    IngestStatus,
    LogicalOperator,
    PaginatedResult,
    ProvenanceRecord,
    ScoredMatch,
    ValidationError as ValidationErrorModel,
    ValidationResult,
    WriteOperation,
)

__all__ = [
    "MosaicClient",
    "IngestionService",
    "ProvenanceService",
    "QueryService",
    "SchemaManager",
    "MosaicError",
    "ConfigError",
    "SchemaError",
    "ValidationError",
    "ValidationFailure",
    "EntityKindMismatchError",
    "EntityNotFoundError",
    "EntityTypeConflictError",
    "EntityAlreadySupersededError",
    "AdapterError",
    "ValidationPipeline",
    "RelationshipManager",
    "RelationshipExistsError",
    "RelationshipNotFoundError",
    "AuthMiddleware",
    "PassThroughAuthMiddleware",
    "RequestContext",
    "create_auth_middleware",
    "Filter",
    "FilterCondition",
    "FilterGroup",
    "FilterOperator",
    "IngestResult",
    "IngestStatus",
    "LogicalOperator",
    "PaginatedResult",
    "ProvenanceRecord",
    "ScoredMatch",
    "ValidationErrorModel",
    "ValidationResult",
    "WriteOperation",
]
