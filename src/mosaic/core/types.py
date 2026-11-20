"""Core SDK types for Mosaic."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class FilterOperator(str, Enum):
    """Comparison operators for filter conditions."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class FilterCondition(BaseModel):
    """A single filter condition."""

    field: str = Field(description="The field name to filter on")
    operator: FilterOperator = Field(description="The comparison operator")
    value: Any = Field(description="The value to compare against")


class LogicalOperator(str, Enum):
    """Logical operators for combining filter conditions."""

    AND = "and"
    OR = "or"


class FilterGroup(BaseModel):
    """A group of filter conditions combined with a logical operator."""

    conditions: list["FilterCondition"] = Field(default_factory=list)
    groups: list["FilterGroup"] = Field(default_factory=list)
    logical_operator: LogicalOperator = Field(default=LogicalOperator.AND)


class Filter(BaseModel):
    """Filter type for querying data with multiple conditions.

    Supports nested condition groups with AND/OR logic for complex queries.
    """

    root: FilterGroup = Field(default_factory=FilterGroup)

    def model_dump(self, **kwargs) -> dict[str, Any]:
        """Serialize the filter to a dictionary."""
        return super().model_dump(**kwargs)


class PaginatedResult(BaseModel):
    """Paginated result type with metadata.

    Used for returning paged query results with pagination information.
    Returned by client.query() instead of a bare list[dict].
    """

    items: list[Any] = Field(default_factory=list, description="The items on this page")
    total: int = Field(
        description="Total number of matching items across all pages (ignoring limit/offset)"
    )
    limit: int = Field(
        description="Maximum number of items per page (0 means no limit)"
    )
    offset: int = Field(default=0, description="Number of items skipped")


class ScoredMatch(BaseModel):
    """Scored match type for search results with relevance scoring.

    Used when returning search results that include relevance scores.
    """

    score: float = Field(description="Relevance score (higher is more relevant)")
    match_data: dict[str, Any] = Field(
        default_factory=dict, description="The matched data"
    )
    matched_fields: list[str] = Field(
        default_factory=list, description="Fields that matched the query"
    )


class WriteOperation(BaseModel):
    """Write operation result type with success status and metadata.

    Used for data insertion, update, and delete operations.
    """

    success: bool = Field(description="Whether the operation succeeded")
    operation: str = Field(description="Type of operation (insert, update, delete)")
    entity_type: str = Field(description="The type of entity affected")
    entity_id: Optional[str] = Field(default=None, description="ID of affected entity")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional operation metadata"
    )


class ValidationError(BaseModel):
    """A single validation error."""

    field: str = Field(description="The field that failed validation")
    message: str = Field(description="Human-readable error message")


class ValidationResult(BaseModel):
    """Validation result type with success/failure status and errors.

    Used for validating data before write operations.
    """

    valid: bool = Field(description="Whether validation passed")
    errors: list[ValidationError] = Field(
        default_factory=list, description="List of validation errors if any"
    )

    @property
    def is_valid(self) -> bool:
        """Alias for valid property."""
        return self.valid


class TemporalRecord(BaseModel):
    """Read-time computed temporal state for an entity (sec9 §9.7).

    Populated by ``StorageAdapter.get_temporal`` aggregating over
    ``ProvenanceRecord`` entries for a given entity. All five fields are
    always present when the entity has any provenance; an entity with no
    provenance raises ``ProvenanceIntegrityError`` rather than yielding a
    ``TemporalRecord`` with null fields.
    """

    created_at: Optional[str] = Field(
        default=None,
        description="Timestamp of the earliest 'create' record",
    )
    updated_at: Optional[str] = Field(
        default=None, description="Timestamp of the latest record (any operation)"
    )
    schema_version: Optional[str] = Field(
        default=None, description="schema_version from the latest record"
    )
    created_by: Optional[str] = Field(
        default=None, description="actor_id from the 'create' record"
    )
    updated_by: Optional[str] = Field(
        default=None, description="actor_id from the latest record"
    )


class Operation(str, Enum):
    """Kind of operation recorded on a ProvenanceRecord.

    Mirrors the ``Operation`` enum declared in ``mosaic_core.yaml`` (sec9 §9.6).
    The canonical source is the LinkML declaration; this mirror exists so
    Python callers can pass enum values without going through the registry.
    """

    create = "create"
    update = "update"
    availability_change = "availability_change"
    supersede = "supersede"
    relationship_add = "relationship_add"
    relationship_remove = "relationship_remove"
    external_id_add = "external_id_add"
    external_id_remove = "external_id_remove"
    migration_applied = "migration_applied"
    reference_data_installed = "reference_data_installed"
    recipe_imported = "recipe_imported"


class ProvenanceRecord(BaseModel):
    """Provenance record for tracking data lineage (sec9 §9.6 shape).

    In-memory representation returned by ``ProvenanceStore.record()``.
    Corresponds 1:1 with the ``ProvenanceRecord`` class declared in
    ``mosaic_core.yaml``.
    """

    id: Optional[str] = Field(
        default=None, description="UUID primary key; assigned by the store on write"
    )
    entity_id: Optional[str] = Field(
        default=None,
        description="UUID of the targeted entity; null for system operations",
    )
    entity_type: Optional[str] = Field(
        default=None, description="Fully-qualified class name of the target entity"
    )
    operation: Operation = Field(description="Kind of operation (sec9 §9.6)")
    actor_id: str = Field(
        description="UUID of the entity responsible for the operation (sec9 §9.5)"
    )
    timestamp: datetime = Field(description="UTC wall-clock time of completion")
    schema_version: str = Field(
        description="Version of the merged schema at write time"
    )
    derived_from_id: Optional[str] = Field(
        default=None,
        description="For supersede operations, UUID of the previous entity version",
    )
    process_id: Optional[str] = Field(
        default=None, description="Enclosing Process UUID, if any"
    )
    patch: Optional[dict[str, Any]] = Field(
        default=None,
        description="Operation-specific change payload as a JSON-serializable dict",
    )
    context: Optional[dict[str, Any]] = Field(
        default=None, description="Caller-supplied contextual metadata"
    )


class IngestStatus(str, Enum):
    """Status of an ingestion operation."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class IngestResult(BaseModel):
    """Ingest result type with status, counts, and errors.

    Used for data ingestion operations that process multiple items.
    """

    status: IngestStatus = Field(description="Overall ingestion status")
    total_processed: int = Field(description="Total items processed")
    successful: int = Field(description="Number of successfully processed items")
    failed: int = Field(description="Number of failed items")
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description="List of error details for failed items"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional result metadata"
    )
