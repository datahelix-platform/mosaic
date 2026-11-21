"""Multivalued slots persist and round-trip (issue #79 / ADR-0002).

Before the fix, multivalued slots got neither a per-class column nor a
junction table (the DDL generator filters LinkML's linktables), so
``put`` silently dropped them: not in ``entity["data"]`` and not as
relationships. This covers the two storage rules introduced by ADR-0002:

- multivalued **reference** slots (range is an entity class) persist as
  relationships keyed by the slot name, hydrated back on read;
- multivalued **non-reference** slots (scalars/enums) persist inline as a
  single JSON TEXT column.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mosaic


_SCHEMA = """\
id: https://example.org/mv
name: mv
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
      sample_id:
        range: string
  Assay:
    is_a: Entity
    attributes:
      platform:
        range: string
      inputs:                 # multivalued reference → relationships
        range: Sample
        multivalued: true
      tags:                   # multivalued scalar → inline JSON column
        range: string
        multivalued: true
"""


@pytest.fixture
def client(tmp_path: Path):
    schema = tmp_path / "schema.yaml"
    schema.write_text(_SCHEMA)
    return mosaic.client_for_schema(
        schema, database_url=str(tmp_path / "h.db")
    )


class TestMultivaluedReferenceSlots:
    def test_get_round_trips_multivalued_reference(self, client):
        client.put("Sample", {"sample_id": "S1"}, entity_id="S1")
        client.put("Sample", {"sample_id": "S2"}, entity_id="S2")
        client.put(
            "Assay",
            {"platform": "Illumina", "inputs": ["S1", "S2"]},
            entity_id="A1",
        )

        got = client.get("Assay", "A1")
        assert got["data"]["platform"] == "Illumina"
        assert got["data"]["inputs"] == ["S1", "S2"]  # order preserved

    def test_query_round_trips_multivalued_reference(self, client):
        client.put("Sample", {"sample_id": "S1"}, entity_id="S1")
        client.put("Assay", {"inputs": ["S1"]}, entity_id="A1")

        item = client.query("Assay", limit=1).items[0]
        assert item["data"]["inputs"] == ["S1"]

    def test_reference_materializes_as_relationships(self, client):
        client.put("Sample", {"sample_id": "S1"}, entity_id="S1")
        client.put("Assay", {"inputs": ["S1"]}, entity_id="A1")

        rels = client.relationships.find_relationships(source_id="A1")
        assert [(r["relationship_type"], r["target_id"]) for r in rels] == [
            ("inputs", "S1")
        ]

    def test_forward_reference_is_stored_without_target(self, client):
        # Target does not exist yet (bulk-ingest ordering); the edge must
        # still persist, matching single-valued-ref behavior.
        client.put("Assay", {"inputs": ["S_FUTURE"]}, entity_id="A1")

        rels = client.relationships.find_relationships(source_id="A1")
        assert [r["target_id"] for r in rels] == ["S_FUTURE"]

    def test_update_reconciles_edges(self, client):
        client.put("Sample", {"sample_id": "S1"}, entity_id="S1")
        client.put("Sample", {"sample_id": "S2"}, entity_id="S2")
        client.put("Assay", {"inputs": ["S1", "S2"]}, entity_id="A1")

        # Drop S2 on update — its edge must go away, S1 stays.
        client.put("Assay", {"inputs": ["S1"]}, entity_id="A1")

        got = client.get("Assay", "A1")
        assert got["data"]["inputs"] == ["S1"]
        rels = client.relationships.find_relationships(source_id="A1")
        assert [r["target_id"] for r in rels] == ["S1"]

    def test_update_omitting_slot_clears_edges(self, client):
        client.put("Sample", {"sample_id": "S1"}, entity_id="S1")
        client.put("Assay", {"inputs": ["S1"]}, entity_id="A1")

        # Omitting the slot entirely clears its column (NULL); edges follow.
        client.put("Assay", {"platform": "X"}, entity_id="A1")

        got = client.get("Assay", "A1")
        assert "inputs" not in got["data"]
        assert client.relationships.find_relationships(source_id="A1") == []

    def test_scalar_value_is_unaffected(self, client):
        # The contrast case from the issue: scalar slots always worked.
        client.put("Sample", {"sample_id": "S1"}, entity_id="S1")
        client.put("Assay", {"platform": "Illumina"}, entity_id="A1")
        assert client.get("Assay", "A1")["data"]["platform"] == "Illumina"


class TestMultivaluedScalarSlots:
    def test_get_round_trips_multivalued_scalar(self, client):
        client.put("Assay", {"tags": ["x", "y"]}, entity_id="A1")
        assert client.get("Assay", "A1")["data"]["tags"] == ["x", "y"]

    def test_scalar_slot_is_not_a_relationship(self, client):
        client.put("Assay", {"tags": ["x", "y"]}, entity_id="A1")
        # A non-reference multivalued slot stores inline, NOT as edges.
        assert client.relationships.find_relationships(source_id="A1") == []
