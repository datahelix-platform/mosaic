"""Cheap cardinality of a multivalued reference edge (issue #132, deferred
from ADR-0005 — the interim resolve-to-count optimization).

``MosaicClient.count_relationship`` answers "how many members does this
edge have" with a single indexed ``COUNT(*)`` over the ADR-0002
relationships table joined to the target's table, without resolving any
member object — reusing the exact edge-resolution machinery ADR-0006 M5b
added (``_reference_edge``), so the errors and availability semantics
match the ``some``/``none`` quantifier path exactly. Postgres parity lives
in ``tests/integration/test_postgres_adapter.py``.
"""

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationError
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

SCHEMA = """
id: https://example.org/mosaic/test_relationship_count
name: test_relationship_count
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      tissue: {}
      parent:
        range: Sample

  Study:
    is_a: Entity
    attributes:
      title:
        required: true
      sample_ids:
        range: Sample
        multivalued: true
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_relationship_count.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, bypass_validation=True)
        c.put("Sample", {"id": "s1", "name": "S1", "tissue": "brain"})
        c.put("Sample", {"id": "s2", "name": "S2", "tissue": "liver"})
        c.put("Sample", {"id": "s3", "name": "S3", "tissue": "brain"})
        c.put("Study", {"id": "st1", "title": "Brain+Liver",
                        "sample_ids": ["s1", "s2"]})
        c.put("Study", {"id": "st2", "title": "Empty"})  # no edges
        yield c


class TestCountRelationship:
    def test_counts_without_resolving(self, client):
        assert client.count_relationship("Study", "st1", "sample_ids") == 2

    def test_edgeless_entity_counts_zero(self, client):
        assert client.count_relationship("Study", "st2", "sample_ids") == 0

    def test_nonexistent_source_counts_zero(self, client):
        assert client.count_relationship("Study", "does-not-exist", "sample_ids") == 0

    def test_unavailable_target_not_counted(self, client):
        client.set_availability_bulk(
            entity_type="Sample", entity_ids=["s2"],
            is_available=False, reason="test",
        )
        assert client.count_relationship("Study", "st1", "sample_ids") == 1

    def test_removed_edge_not_counted(self, client):
        client.relationships.unrelate("st1", "s1", "sample_ids")
        assert client.count_relationship("Study", "st1", "sample_ids") == 1


class TestLoudErrors:
    def test_unknown_edge_raises(self, client):
        with pytest.raises(ValidationError, match="reference slots"):
            client.count_relationship("Study", "st1", "nope")

    def test_to_one_edge_raises(self, client):
        with pytest.raises(ValidationError, match="to-one"):
            client.count_relationship("Sample", "s1", "parent")
