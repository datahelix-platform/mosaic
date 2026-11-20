"""Shared fixtures for the GraphQL transport tests.

The whole package is skipped when ``strawberry-graphql`` (the optional
``graphql`` extra) is not installed — mirrors how ``tests/tui`` skips
without textual. CI installs only ``.[dev]``, so these tests run
locally and in graphql-enabled environments.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any, Optional

import pytest

# Skip the entire package if strawberry is not installed.
strawberry = pytest.importorskip(
    "strawberry", reason="strawberry not installed; run: pip install datahelix-mosaic[graphql]"
)

from fastapi.testclient import TestClient

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.serve import create_default_app

# Exercises every generation rule: required/optional scalars of each
# range, an enum, a single-valued reference with the ``_id`` naming
# convention, a self-referential relationship without the suffix, a
# multivalued reference, and a full-text-searchable slot.
GRAPHQL_TEST_SCHEMA = """
id: https://example.org/mosaic/test_graphql
name: test_graphql
description: Schema exercising the GraphQL generation rules.
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
      donor_id:
        range: Donor
      parent:
        range: Sample
      collected_at:
        range: datetime
      volume_ml:
        range: float
      replicate_count:
        range: integer
      is_tumor:
        range: boolean
      notes:
        annotations:
          mosaic_search: fts5

  Study:
    is_a: Entity
    attributes:
      title:
        required: true
      sample_ids:
        range: Sample
        multivalued: true

enums:
  SexEnum:
    description: Reported sex of a donor.
    permissible_values:
      male: {}
      female: {}
      unknown: {}
"""

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="session")
def registry() -> SchemaRegistry:
    """Session-scoped — SchemaRegistry construction is expensive."""
    return SchemaRegistry.from_yaml(GRAPHQL_TEST_SCHEMA)


@pytest.fixture
def hippo_client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "graphql_test.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        client = MosaicClient(storage=storage, registry=registry)
        # FTS virtual tables for the `mosaic_search`-annotated slots (the
        # adapter does not create them; mirrors tests/integration).
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
def hippo_app(hippo_client: MosaicClient):
    """FastAPI app with REST + GraphQL transports over one client."""
    return create_default_app(mosaic_client=hippo_client, graphql=True)


@pytest.fixture
def client(hippo_app):
    """FastAPI test client with the GraphQL transport mounted.

    Entered as a context manager so every request runs on the single
    portal thread — one thread-local SQLite connection, matching how a
    real (single event loop) ``mosaic serve`` process behaves.
    """
    with TestClient(hippo_app) as test_client:
        yield test_client


@pytest.fixture
def gql(client: TestClient):
    """POST a GraphQL operation with bearer auth; returns the JSON body."""

    def _gql(query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        response = client.post(
            "/graphql",
            json={"query": query, "variables": variables or {}},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _gql
