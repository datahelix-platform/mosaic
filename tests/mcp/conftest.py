"""Shared fixtures for the MCP transport tests.

The whole package is skipped when the ``mcp`` package (the optional
``mcp`` extra) is not installed — mirrors ``tests/graphql``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

# Skip the entire package if the mcp SDK is not installed.
pytest.importorskip("mcp", reason="mcp not installed; run: pip install datahelix-mosaic[mcp]")

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.serve import create_default_app

# Reuses the exact fixture schema the GraphQL suite exercises (Donor/
# Sample/Study, a to-one and a to-many reference, an enum, a searchable
# slot) so the two transports' capability manifests are directly
# comparable in intent even though this package builds its own registry
# instance (session-scoped instances must not cross package boundaries).
MCP_TEST_SCHEMA = """
id: https://example.org/mosaic/test_mcp
name: test_mcp
description: Schema exercising the MCP resource generation.
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Donor:
    is_a: Entity
    description: A tissue donor.
    attributes:
      name:
        required: true
      sex:
        range: SexEnum

  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
        annotations:
          mosaic_search: fts5
      donor_id:
        range: Donor
      volume_ml:
        range: float

enums:
  SexEnum:
    permissible_values:
      male: {}
      female: {}
      unknown: {}
"""


@pytest.fixture(scope="session")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(MCP_TEST_SCHEMA)


@pytest.fixture
def hippo_client(registry: SchemaRegistry):
    """Storage-backed (not registry-only): the execute_query_spec tests
    need to write and then query real entities, not just introspect the
    schema. Mirrors tests/graphql/conftest.py's fixture exactly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "mcp_test.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        client = MosaicClient(storage=storage, registry=registry)
        conn = sqlite3.connect(db_path)
        for tables in client._fts_table_metadata.values():
            for meta in tables:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {meta.table_name} "
                    "USING fts5(entity_id, content)"
                )
        conn.commit()
        conn.close()
        yield client


@pytest.fixture
def mcp_app(hippo_client: MosaicClient):
    return create_default_app(mosaic_client=hippo_client, mcp=True)
