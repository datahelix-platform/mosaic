"""Postgres parity for reverses over multivalued forward slots (ADR-0013, #231).

Requires a running PostgreSQL instance (``MOSAIC_DATABASE_URL``), like the
rest of ``tests/integration/test_postgres_*``. Same schema and expectations
as ``tests/core/test_inverse_slots_multivalued.py``: ``Assay.inputs`` carries
``inverse: assays`` and stays the stored side (relationship rows);
``Sample.assays`` is synthesized; ``Cohort.members`` is declared on the
derived side over the multivalued ``Person.cohort``. Hydration, ``some`` /
``none`` and ``count_relationship`` resolve through ``relationships``
joined on ``target_id`` and scoped by ``relationship_type``.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL") or os.environ.get(
    "HIPPO_DATABASE_URL"
)

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)


@pytest.fixture
def client():
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
    from mosaic.linkml_bridge import SchemaRegistry
    from tests.core.test_inverse_slots_multivalued import SCHEMA

    registry = SchemaRegistry.from_yaml(SCHEMA)
    adapter = PostgresAdapter(
        database_url=POSTGRES_URL,
        schema_registry=registry,
        min_pool_size=1,
        max_pool_size=5,
    )
    c = MosaicClient(storage=adapter, registry=registry, bypass_validation=True)
    c.put("Brain", {"id": "b1", "label": "brain-1"})
    c.put("Brain", {"id": "b2", "label": "brain-2"})
    c.put("CSF", {"id": "f1", "label": "csf-1"})
    c.put("CSF", {"id": "f9", "label": "unused"})
    c.put("Assay", {"id": "a1", "kind": "rnaseq", "inputs": ["b1", "f1"]})
    c.put("Assay", {"id": "a2", "kind": "atac", "inputs": ["b1"]})
    c.put("Assay", {"id": "a3", "kind": "rnaseq", "inputs": ["b2"]})
    c.put("Run", {"id": "r1", "inputs": ["b1", "f9"]})
    c.put("Cohort", {"id": "c1", "name": "case"})
    c.put("Cohort", {"id": "c2", "name": "control"})
    c.put("Cohort", {"id": "c3", "name": "empty"})
    c.put("Person", {"id": "p1", "name": "P1", "cohort": ["c1", "c2"]})
    c.put("Person", {"id": "p2", "name": "P2", "cohort": ["c1"]})
    yield c
    with adapter._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM relationships")
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")
    adapter.close()


def ids(client, entity_type, where) -> set[str]:
    return {i["id"] for i in client.query(entity_type, where=where).items}


def _kind(op, value, quantifier="some", edge="assays", field="kind"):
    return {
        "edge": edge, "quantifier": quantifier,
        "where": {"field": field, "op": op, "value": value},
    }


class TestPostgresReverseOfMultivalued:
    def test_forward_write_persists(self, client):
        assert client.get("Assay", "a1")["data"]["inputs"] == ["b1", "f1"]
        assert client.get("Person", "p1")["data"]["cohort"] == ["c1", "c2"]

    def test_hydration(self, client):
        assert client.get("Brain", "b1")["data"]["assays"] == ["a1", "a2"]
        assert client.get("CSF", "f1")["data"]["assays"] == ["a1"]
        assert "assays" not in client.get("CSF", "f9")["data"]
        assert client.get("Cohort", "c1")["data"]["members"] == ["p1", "p2"]
        assert "members" not in client.get("Cohort", "c3")["data"]

    def test_query_hydrates_in_batch(self, client):
        by_id = {i["id"]: i["data"] for i in client.query("Brain").items}
        assert by_id["b1"]["assays"] == ["a1", "a2"]
        assert by_id["b2"]["assays"] == ["a3"]

    def test_unavailable_source_is_excluded(self, client):
        client.delete("Assay", "a2")
        assert client.get("Brain", "b1")["data"]["assays"] == ["a1"]
        assert client.count_relationship("Brain", "b1", "assays") == 1

    def test_write_carrying_the_virtual_slot_is_ignored(self, client):
        client.update("Brain", "b2", {"label": "renamed", "assays": ["a1"]})
        assert client.get("Brain", "b2")["data"]["assays"] == ["a3"]

    def test_some_none(self, client):
        assert ids(client, "Brain", _kind("eq", "rnaseq")) == {"b1", "b2"}
        assert ids(client, "Brain", _kind("eq", "atac", "none")) == {"b2"}
        assert ids(client, "CSF", _kind("eq", "atac", "none")) == {"f1", "f9"}
        assert ids(client, "Cohort", _kind("eq", "P2", edge="members", field="name")) == {"c1"}

    def test_counts(self, client):
        assert client.count_relationship("Brain", "b1", "assays") == 2
        assert client.count_relationship("CSF", "f9", "assays") == 0
        assert client.count_relationship("Cohort", "c1", "members") == 2
        assert client.count_relationship("Cohort", "c3", "members") == 0
