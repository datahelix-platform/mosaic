"""Pydantic models for Mosaic runtime configuration (``mosaic.yaml``).

Entity/class schemas are no longer modeled here — those live in LinkML YAML
and are accessed via ``mosaic.linkml_bridge.SchemaRegistry``.
"""

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class ValidatorDefinition(BaseModel):
    """Definition of a validator in the mosaic.yaml config."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str
    type: str
    enabled: bool = True
    priority: int = 0
    config: Optional[dict[str, Any]] = None


class MosaicConfig(BaseModel):
    """Top-level mosaic.yaml runtime config."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    schema_path: Path
    storage_backend: Optional[str] = None
    database_url: Optional[str] = None
    validation_enabled: bool = True
    validation_fail_fast: bool = True
    write_path_validation_enabled: bool = True
    write_path_validation_timeout: Optional[float] = None
    validators_path: Optional[Path] = None
    cors_allow_origins: Optional[list[str]] = None

    @field_validator("schema_path", mode="before")
    @classmethod
    def validate_schema_path(cls, v):
        if isinstance(v, str):
            return Path(v)
        return v

