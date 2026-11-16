"""Postgres parity for a reverse edge declared on the forward slot (ADR-0012).

Requires a running PostgreSQL instance (``MOSAIC_DATABASE_URL``), like the
rest of ``tests/integration/test_postgres_*``. The schema is the
import-layered pair from ``tests/core/test_inverse_slots_forward.py``:
``person.yaml`` (``Donor``) never names ``Sample``; ``tissue.yaml`` declares
``Sample.donor: {range: Donor, inverse: samples}`` on an abstract ``Sample``.
Expectations match the SQLite suite: hydration from the forward FK key over
the concrete closure, ``some``/``none``, ``count_relationship``, and writes
carrying the synthesized slot being ignored.
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
def client(tmp_path):
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
    from mosaic.linkml_bridge import SchemaRegistry
    from tests.core.test_inverse_slots_forward import PERSON_YAML, TISSUE_YAML

    (tmp_path / "person.yaml").write_text(PERSON_YAML)
    (tmp_path / "tissue.yaml").write_text(TISSUE_YAML)
    registry = SchemaRegistry.from_path(tmp_path / "tissue.yaml")
    (inv,) = registry.inverse_reference_slots("Donor")
    assert inv.declared_on == "forward"

    adapter = PostgresAdapter(
        database_url=POSTGRES_URL,
        schema_registry=registry,
        min_pool_size=1,
        max_pool_size=5,
    )
    c = MosaicClient(storage=adapter, registry=registry)
    c.put("Donor", {"id": "d1", "name": "D1", "cohort": "case"})
    c.put("Donor", {"id": "d2", "name": "D2", "cohort": "control"})
    c.put("Donor", {"id": "d3", "name": "D3", "cohort": "case"})
    c.put("Brain", {"id": "b1", "label": "brain", "volume": 10, "donor": "d1"})
    c.put("CSF", {"id": "f1", "label": "csf", "volume": 2, "donor": "d1"})
    c.put("CSF", {"id": "f2", "label": "csf", "volume": 1, "donor": "d2"})
    c.put("Brain", {"id": "b9", "label": "orphan", "volume": 5})
    yield c
    with adapter._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM relationships")
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")
    adapter.close()


def ids(client, where, entity_type="Donor") -> set[str]:
    return {i["id"] for i in client.query(entity_type, where=where).items}


def some(sub):
    return {"edge": "samples", "quantifier": "some", "where": sub}


def none(sub):
    return {"edge": "samples", "quantifier": "none", "where": sub}


class TestPostgresForwardDeclaredHydration:
    def test_get_hydrates_across_the_concrete_closure(self, client):
        assert sorted(client.get("Donor", "d1")["data"]["samples"]) == ["b1", "f1"]
        assert client.get("Donor", "d2")["data"]["samples"] == ["f2"]
        assert "samples" not in client.get("Donor", "d3")["data"]

    def test_query_hydrates_in_batch(self, client):
        by_id = {i["id"]: i["data"] for i in client.query("Donor").items}
        assert sorted(by_id["d1"]["samples"]) == ["b1", "f1"]
        assert by_id["d2"]["samples"] == ["f2"]

    def test_unavailable_target_is_excluded(self, client):
        client.delete("CSF", "f1")
        assert client.get("Donor", "d1")["data"]["samples"] == ["b1"]

    def test_forward_update_moves_reverse_membership(self, client):
        client.update("Brain", "b9", {"label": "orphan", "volume": 5, "donor": "d3"})
        assert client.get("Donor", "d3")["data"]["samples"] == ["b9"]


class TestPostgresForwardDeclaredWritesIgnored:
    def test_put_carrying_the_synthesized_slot_writes_nothing_for_it(self, client):
        client.put("Donor", {"id": "d4", "name": "D4", "samples": ["b9", "b1"]})
        assert client.relationships.find_relationships(source_id="d4") == []
        assert "samples" not in client.get("Donor", "d4")["data"]
        assert client.get("Brain", "b9")["data"].get("donor") is None

    def test_get_then_put_round_trip_is_stable(self, client):
        client.put("Donor", client.get("Donor", "d1")["data"])
        assert sorted(client.get("Donor", "d1")["data"]["samples"]) == ["b1", "f1"]
        assert client.relationships.find_relationships(source_id="d1") == []


class TestPostgresForwardDeclaredFilterAndCount:
    def test_some(self, client):
        assert ids(client, some({"field": "label", "op": "eq", "value": "csf"})) == {"d1", "d2"}
        assert ids(client, some({"field": "volume", "op": "gte", "value": 10})) == {"d1"}

    def test_none_includes_sampleless(self, client):
        assert ids(client, none({"field": "label", "op": "eq", "value": "brain"})) == {"d2", "d3"}

    def test_count_relationship(self, client):
        assert client.count_relationship("Donor", "d1", "samples") == 2
        assert client.count_relationship("Donor", "d2", "samples") == 1
        assert client.count_relationship("Donor", "d3", "samples") == 0
