"""Test helpers for building minimal LinkML schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from mosaic.linkml_bridge import SchemaRegistry


def schema_dict(
    classes: dict[str, Any],
    enums: Optional[dict[str, Any]] = None,
    default_range: str = "string",
    schema_id: str = "https://example.org/mosaic/test",
    schema_name: str = "test_schema",
) -> dict[str, Any]:
    """Return a minimal, valid LinkML schema dict around the given classes/enums."""
    schema: dict[str, Any] = {
        "id": schema_id,
        "name": schema_name,
        "prefixes": {"linkml": "https://w3id.org/linkml/"},
        "imports": ["linkml:types"],
        "default_range": default_range,
        "classes": classes,
    }
    if enums:
        schema["enums"] = enums
    return schema


def build_registry(
    classes: dict[str, Any],
    enums: Optional[dict[str, Any]] = None,
    default_range: str = "string",
    schema_id: str = "https://example.org/mosaic/test",
    schema_name: str = "test_schema",
) -> SchemaRegistry:
    """Build a ``SchemaRegistry`` from a compact dict spec."""
    return SchemaRegistry.from_dict(
        schema_dict(
            classes,
            enums=enums,
            default_range=default_range,
            schema_id=schema_id,
            schema_name=schema_name,
        )
    )


def write_schema_file(
    path: Path,
    classes: dict[str, Any],
    *,
    enums: Optional[dict[str, Any]] = None,
    schema_name: Optional[str] = None,
) -> Path:
    """Write a minimal LinkML schema YAML to ``path`` and return it.

    ``path`` may be a directory (file defaults to ``<schema_name>.yaml``) or a
    file path. All Mosaic-specific slot metadata must be expressed via LinkML
    annotations under ``mosaic_*`` keys.
    """
    if path.is_dir():
        assert schema_name, "schema_name required when path is a directory"
        target = path / f"{schema_name}.yaml"
    else:
        target = path
    doc = schema_dict(
        classes, enums=enums, schema_name=schema_name or target.stem
    )
    target.write_text(yaml.safe_dump(doc, sort_keys=False))
    return target
