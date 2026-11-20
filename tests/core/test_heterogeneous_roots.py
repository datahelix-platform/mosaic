"""Heterogeneous roots (issue #158): cross-class searchAll + neighbors.

SDK layer: ``client.search_all`` (rank-merged cross-class FTS with
batched-by-type materialization) and ``client.neighbors`` (depth-bounded
subgraph over BOTH edge stores — the headline gap test asserts a
link-table edge and a column-stored single-valued reference on the same
entity both appear). Postgres parity lives in
``tests/integration/test_postgres_adapter.py``.
"""

import os
import sqlite3
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import EntityNotFoundError
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

FUTURE = "2999-01-01T00:00:00+00:00"

SCHEMA = """
id: https://example.org/mosaic/test_heterogeneous_roots
name: test_heterogeneous_roots
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Donor:
    is_a: Entity
    attributes:
      name:
        required: true
      bio:
        annotations:
          mosaic_search: fts5

  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      donor_id:
        range: Donor
      notes:
        annotations:
          mosaic_search: fts5

  Study:
    is_a: Entity
    attributes:
      title:
        required: true
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_heterogeneous_roots.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, registry=registry)
        conn = sqlite3.connect(db_path)
        for tables in c._fts_table_metadata.values():
            for meta in tables:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {meta.table_name} "
                    "USING fts5(entity_id, content)"
                )
        conn.commit()
        conn.close()

        c.put("Donor", {"id": "d1", "name": "Ada",
                        "bio": "cortex cortex cortex researcher"})
        c.put("Donor", {"id": "d2", "name": "Ben",
                        "bio": "hippocampus specialist here"})
        c.put("Sample", {"id": "s1", "name": "S1", "donor_id": "d1",
                         "notes": "cortex cortex lesion sample"})
        c.put("Sample", {"id": "s2", "name": "S2", "donor_id": "d1",
                         "notes": "cortex intact tissue sample"})
        c.put("Sample", {"id": "s3", "name": "S3",
                         "notes": "unrelated hippocampus material"})
        c.put("Study", {"id": "st1", "title": "Cortex Study"})
        # Link-table edge beside s1's column-stored donor_id — the ADR-0002
        # two-stores fixture the gap test needs.
        c.relationships.relate("st1", "s1", "includes_sample")
        yield c


class TestSearchAll:
    def test_rank_merged_across_classes(self, client):
        hits = client.search_all("cortex")
        # d1 (3× cortex) outranks s1 (2×) outranks s2 (1×); Study has no
        # FTS slot so st1 never appears despite "Cortex" in its title.
        assert [(h["entity_type"], h["id"]) for h in hits] == [
            ("Donor", "d1"), ("Sample", "s1"), ("Sample", "s2"),
        ]
        assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]
        assert hits[0]["data"]["name"] == "Ada"
        assert hits[0]["created_at"] is not None

    def test_limit_truncates_after_merge(self, client):
        hits = client.search_all("cortex", limit=2)
        assert [(h["entity_type"], h["id"]) for h in hits] == [
            ("Donor", "d1"), ("Sample", "s1"),
        ]
        assert client.search_all("cortex", limit=0) == []  # #130 discipline

    def test_no_hits_is_empty(self, client):
        assert client.search_all("nonexistentterm12345") == []

    def test_availability_parity(self, client):
        client.set_availability_bulk(
            entity_type="Sample", entity_ids=["s1"],
            is_available=False, reason="test",
        )
        hits = client.search_all("cortex")
        assert [(h["entity_type"], h["id"]) for h in hits] == [
            ("Donor", "d1"), ("Sample", "s2"),
        ]

    def test_batched_materialization(self, client, monkeypatch):
        storage = client._storage
        calls = {"find": 0, "read": 0}
        real_find, real_read = storage.find, storage.read
        monkeypatch.setattr(
            storage, "find",
            lambda *a, **k: (calls.__setitem__("find", calls["find"] + 1),
                             real_find(*a, **k))[1],
        )
        monkeypatch.setattr(
            storage, "read",
            lambda *a, **k: (calls.__setitem__("read", calls["read"] + 1),
                             real_read(*a, **k))[1],
        )
        hits = client.search_all("cortex")
        assert len(hits) == 3
        # One composed read per class present in the page (Donor, Sample),
        # never per-hit.
        assert calls == {"find": 2, "read": 0}


class TestNeighbors:
    def test_gap_both_edge_stores_present(self, client):
        # THE headline assertion (issue #158): s1 carries a column-stored
        # donor_id AND a link-table edge from st1 — both must appear.
        graph = client.neighbors("s1")
        edges = {(e["source"], e["target"], e["edge_source"])
                 for e in graph["edges"]}
        assert ("s1", "d1", "COLUMN") in edges
        assert ("st1", "s1", "LINK_TABLE") in edges
        assert {n["entity_id"] for n in graph["nodes"]} == {"s1", "d1", "st1"}
        assert set(graph["edge_sources"]) == {"LINK_TABLE", "COLUMN"}
        assert graph["notices"] == []

    def test_reverse_column_edges(self, client):
        # From the donor's side, the samples referencing it via the FK
        # column must appear (schema-driven reverse edges).
        graph = client.neighbors("d1")
        edges = {(e["source"], e["target"], e["type"]) for e in graph["edges"]}
        assert ("s1", "d1", "donor_id") in edges
        assert ("s2", "d1", "donor_id") in edges

    def test_depth_bound(self, client):
        # depth=1 from st1 reaches s1 only; depth=2 adds s1's donor.
        one = client.neighbors("st1", depth=1)
        assert {n["entity_id"] for n in one["nodes"]} == {"st1", "s1"}
        two = client.neighbors("st1", depth=2)
        assert {n["entity_id"] for n in two["nodes"]} == {"st1", "s1", "d1"}

    def test_edges_renderable_without_further_queries(self, client):
        graph = client.neighbors("s1")
        node_ids = {n["entity_id"] for n in graph["nodes"]}
        for e in graph["edges"]:
            assert e["source"] in node_ids and e["target"] in node_ids
            assert e["type"]

    def test_unavailable_neighbor_dropped_with_its_edges(self, client):
        client.set_availability_bulk(
            entity_type="Donor", entity_ids=["d1"],
            is_available=False, reason="test",
        )
        graph = client.neighbors("s1")
        assert {n["entity_id"] for n in graph["nodes"]} == {"s1", "st1"}
        assert all(e["target"] != "d1" for e in graph["edges"])

    def test_unknown_start_raises(self, client):
        with pytest.raises(EntityNotFoundError):
            client.neighbors("nope")

    def test_batched_node_materialization(self, client, monkeypatch):
        storage = client._storage
        calls = {"read": 0}
        real_read = storage.read
        monkeypatch.setattr(
            storage, "read",
            lambda *a, **k: (calls.__setitem__("read", calls["read"] + 1),
                             real_read(*a, **k))[1],
        )
        graph = client.neighbors("s1")
        assert len(graph["nodes"]) == 3
        # One existence check for the start node; materialization itself is
        # batched by type via find(), never get/read-per-node.
        assert calls["read"] == 1

    def test_as_of_link_edges_only_with_disclosure(self, client):
        graph = client.neighbors("s1", depth=2, as_of=FUTURE)
        # The link-table edge replays from provenance…
        assert {(e["source"], e["target"]) for e in graph["edges"]} == {
            ("st1", "s1")
        }
        assert all(e["edge_source"] == "LINK_TABLE" for e in graph["edges"])
        # …and the column-edge scoping is disclosed, never silent.
        assert graph["edge_sources"] == ["LINK_TABLE"]
        assert any("hippo#71" in n for n in graph["notices"])
        node_ids = {n["entity_id"] for n in graph["nodes"]}
        assert node_ids == {"s1", "st1"}
        s1 = next(n for n in graph["nodes"] if n["entity_id"] == "s1")
        assert s1["data"]["name"] == "S1"  # state reconstructed at as_of
