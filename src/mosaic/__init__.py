"""Mosaic SDK — a runtime for LinkML schemas (formerly Hippo; ADR-0004)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("datahelix-mosaic")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

from pathlib import Path
from typing import Any, Optional, Union

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationFailure
from mosaic.core.pipeline import ValidationPipeline
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
    ValidationError,
    ValidationResult,
    WriteOperation,
)

_PathLike = Union[str, Path]


def registry_for_schema(schema_path: _PathLike) -> Any:
    """Build a ``SchemaRegistry`` spanning ``schema_path`` and its loaders.

    Resolves the schema's ``requires:`` block and merges every installed
    reference loader it pins, returning a registry that knows both the
    user's own classes and the reference loaders' classes. The public,
    one-call answer to "a registry spanning my schema + the installed
    reference loaders" (issue #67).

    Raises :class:`~mosaic.core.exceptions.SchemaError` when a declared
    loader is not installed or its version disagrees with the pin.
    """
    from mosaic.core.factory import build_schema_registry

    return build_schema_registry(schema_path, merge_requires=True)


def client_for_schema(
    schema_path: _PathLike,
    *,
    database_url: Optional[_PathLike] = None,
    storage_backend: Optional[str] = None,
    validators_path: Optional[_PathLike] = None,
    validation_enabled: bool = True,
) -> MosaicClient:
    """Build a ``MosaicClient`` whose registry spans ``schema_path`` + loaders.

    The public path for a consumer that links its own entities to an
    installed reference loader (issue #67): resolves the schema's
    ``requires:`` block, merges the installed loaders' schema fragments,
    and returns a ready client — no hand-assembled registry code. Querying
    a consumer entity joined to a reference class, or looking a reference
    entity up by its identifier through the SDK, works because the client's
    registry knows both.

    Raises :class:`~mosaic.core.exceptions.SchemaError` when a declared
    loader is not installed or its version disagrees with the pin.
    """
    from mosaic.core.factory import create_client

    return create_client(
        storage_backend=storage_backend,
        database_url=str(database_url) if database_url is not None else None,
        schema_path=schema_path,
        validators_path=validators_path,
        validation_enabled=validation_enabled,
        merge_requires=True,
    )


__all__ = [
    "MosaicClient",
    "client_for_schema",
    "registry_for_schema",
    "ValidationFailure",
    "ValidationPipeline",
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
    "ValidationError",
    "ValidationResult",
    "WriteOperation",
]
