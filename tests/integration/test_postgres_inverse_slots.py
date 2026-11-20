"""Postgres parity for ``inverse``-declared slots (ADR-0011 / issue #204).

Requires a running PostgreSQL instance (``MOSAIC_DATABASE_URL``), like the
rest of ``tests/integration/test_postgres_*``. Expectations intentionally
match ``tests/core/test_inverse_slots.py`` — the JSONB-document compiler
must agree with the SQLite per-class-table compiler on every case:
hydration from the forward FK key, ``some``/``none`` over the target
type's rows, ``count_relationship``, and writes carrying the derived slot
being ignored.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)


@pytest.fixture
def client():
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
    from tests.support.linkml_schemas import build_registry

    registry = build_registry(
        {
            "Donor": {
                "attributes": {
                    "id": {"identifier": True},
                    "name": {"range": "string", "required": True},
                    "cohort": {"range": "string"},
                    "samples": {"range": "Sample", "multivalued": True, "inverse": "donor"},
                }
            },
            "Sample": {
                "attributes": {
                    "id": {"identifier": True},
                    "name": {"range": "string", "required": True},
                    "tissue": {"range": "string"},
                    "volume": {"range": "integer"},
                    "donor": {"range": "Donor"},
                }
            },
        }
    )
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
    c.put("Sample", {"id": "s1", "name": "S1", "tissue": "brain", "volume": 10, "donor": "d1"})
    c.put("Sample", {"id": "s2", "name": "S2", "tissue": "liver", "volume": 20, "donor": "d1"})
    c.put("Sample", {"id": "s3", "name": "S3", "tissue": "brain", "volume": 30, "donor": "d2"})
    c.put("Sample", {"id": "s4", "name": "S4", "tissue": "blood", "volume": 5})
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


class TestPostgresInverseHydration:
    def test_get_hydrates_reverse_ids(self, client):
        assert client.get("Donor", "d1")["data"]["samples"] == ["s1", "s2"]
        assert client.get("Donor", "d2")["data"]["samples"] == ["s3"]
        assert "samples" not in client.get("Donor", "d3")["data"]

    def test_query_hydrates_in_batch(self, client):
        by_id = {i["id"]: i["data"] for i in client.query("Donor").items}
        assert by_id["d1"]["samples"] == ["s1", "s2"]
        assert by_id["d2"]["samples"] == ["s3"]
        assert "samples" not in by_id["d3"]

    def test_unavailable_target_is_excluded(self, client):
        client.delete("Sample", "s1")
        assert client.get("Donor", "d1")["data"]["samples"] == ["s2"]

    def test_forward_update_moves_reverse_membership(self, client):
        client.update("Sample", "s3", {"name": "S3", "tissue": "brain", "volume": 30, "donor": "d1"})
        assert client.get("Donor", "d1")["data"]["samples"] == ["s1", "s2", "s3"]
        assert "samples" not in client.get("Donor", "d2")["data"]


class TestPostgresInverseWritesIgnored:
    def test_put_carrying_inverse_slot_writes_nothing_for_it(self, client):
        client.put("Donor", {"id": "d4", "name": "D4", "samples": ["s4", "s1"]})
        assert client.relationships.find_relationships(source_id="d4") == []
        assert "samples" not in client.get("Donor", "d4")["data"]

    def test_get_then_put_round_trip_is_stable(self, client):
        got = client.get("Donor", "d1")
        client.put("Donor", {"id": "d1", **got["data"]})
        again = client.get("Donor", "d1")
        assert again["data"]["samples"] == ["s1", "s2"]
        assert client.relationships.find_relationships(source_id="d1") == []

    def test_provenance_patch_never_carries_the_derived_list(self, client):
        client.put("Donor", {"id": "d1", "name": "D1", "cohort": "case", "samples": ["s1"]})
        for record in client.history("d1"):
            patch = record.get("patch") or record.get("state_snapshot") or {}
            assert "samples" not in (patch if isinstance(patch, dict) else {})


class TestPostgresReversePredicate:
    def test_some(self, client):
        assert ids(client, some({"field": "tissue", "value": "brain"})) == {"d1", "d2"}
        assert ids(client, some({"field": "tissue", "value": "liver"})) == {"d1"}

    def test_none_includes_sampleless(self, client):
        assert ids(client, none({"field": "tissue", "value": "liver"})) == {"d2", "d3"}

    def test_comparison_on_target(self, client):
        assert ids(client, some({"field": "volume", "op": "gte", "value": 25})) == {"d2"}

    def test_existence_via_identifier_is_null(self, client):
        exists = some({"field": "id", "op": "is_null", "value": False})
        assert ids(client, exists) == {"d1", "d2"}
        assert ids(client, none({"field": "id", "op": "is_null", "value": False})) == {"d3"}

    def test_composes_with_scalar(self, client):
        where = {"and": [{"field": "cohort", "value": "case"}, some({"field": "tissue", "value": "brain"})]}
        assert ids(client, where) == {"d1"}

    def test_unavailable_target_never_satisfies_some(self, client):
        client.delete("Sample", "s3")
        assert ids(client, some({"field": "tissue", "value": "brain"})) == {"d1"}

    def test_count_sees_the_reverse_predicate(self, client):
        assert client.count("Donor", where=some({"field": "tissue", "value": "brain"})) == 2

    def test_reverse_edge_nests_inside_a_forward_edge(self, client):
        where = {"edge": "donor", "where": some({"field": "tissue", "value": "liver"})}
        assert ids(client, where, entity_type="Sample") == {"s1", "s2"}


class TestPostgresInverseCount:
    def test_count_relationship(self, client):
        assert client.count_relationship("Donor", "d1", "samples") == 2
        assert client.count_relationship("Donor", "d2", "samples") == 1
        assert client.count_relationship("Donor", "d3", "samples") == 0

    def test_count_relationship_excludes_unavailable(self, client):
        client.delete("Sample", "s1")
        assert client.count_relationship("Donor", "d1", "samples") == 1
