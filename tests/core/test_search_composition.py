"""Search composition (issue #157): page envelope + id-set composition.

Covers the SDK layer: ``client.search`` returning the ``PaginatedResult``
envelope, FTS-rank default ordering with explicit ``order_by`` override,
composition with ``filters``/``where`` (intersection semantics — the
ranked id set always bounds the result, whatever ``filter_mode`` says),
the ``offset >= limit`` regression, the no-N+1 batched read, and
availability consistency. Transport shapes are pinned in
``tests/graphql/test_search_composition.py`` and the REST tests.
"""

import os
import sqlite3
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

SCHEMA = """
id: https://example.org/mosaic/test_search_composition
name: test_search_composition
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Note:
    is_a: Entity
    attributes:
      name:
        required: true
      tissue: {}
      priority:
        range: integer
      body:
        annotations:
          mosaic_search: fts5

  Plain:
    is_a: Entity
    attributes:
      name:
        required: true
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_search_composition.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, registry=registry)
        # FTS virtual tables for the mosaic_search-annotated slots (the
        # adapter does not create them; put() syncs content once they exist).
        conn = sqlite3.connect(db_path)
        for tables in c._fts_table_metadata.values():
            for meta in tables:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {meta.table_name} "
                    "USING fts5(entity_id, content)"
                )
        conn.commit()
        conn.close()

        # Equal-length bodies so bm25 term frequency alone decides rank:
        # n1 (3× cortex) > n2 (2×) > n3 (1×).
        c.put("Note", {"id": "n1", "name": "One", "tissue": "brain",
                       "priority": 1, "body": "cortex cortex cortex alpha"})
        c.put("Note", {"id": "n2", "name": "Two", "tissue": "brain",
                       "priority": 2, "body": "cortex cortex beta filler"})
        c.put("Note", {"id": "n3", "name": "Three", "tissue": "liver",
                       "priority": 3, "body": "cortex gamma delta filler"})
        c.put("Note", {"id": "n4", "name": "Four", "tissue": "brain",
                       "priority": 4, "body": "hippocampus only here now"})
        yield c


def ids(result) -> list[str]:
    return [item["id"] for item in result.items]


class TestEnvelopeAndRank:
    def test_page_envelope_in_rank_order(self, client):
        result = client.search("Note", "cortex")
        assert ids(result) == ["n1", "n2", "n3"]  # bm25 rank, not insert order
        assert result.total == 3
        assert result.limit == 100 and result.offset == 0

    def test_items_carry_computed_temporal_fields(self, client):
        result = client.search("Note", "cortex", limit=1)
        assert result.items[0]["created_at"] is not None

    def test_no_searchable_slots_is_empty_envelope(self, client):
        client.put("Plain", {"id": "p1", "name": "P"})
        result = client.search("Plain", "anything")
        assert result.items == [] and result.total == 0

    def test_no_hits_is_empty_envelope(self, client):
        result = client.search("Note", "nonexistentterm12345")
        assert result.items == [] and result.total == 0


class TestPaging:
    def test_offset_ge_limit_returns_correct_page(self, client):
        # Regression (issue #157): the old resolver sliced
        # results[offset : offset + limit] over a limit-bounded fetch, so
        # offset >= limit always returned [].
        result = client.search("Note", "cortex", limit=1, offset=2)
        assert ids(result) == ["n3"]
        assert result.total == 3

    def test_page_through_visits_every_hit_once(self, client):
        seen: list[str] = []
        for offset in range(0, 3):
            seen += ids(client.search("Note", "cortex", limit=1, offset=offset))
        assert seen == ["n1", "n2", "n3"]

    def test_limit_zero_returns_no_rows_with_total(self, client):
        result = client.search("Note", "cortex", limit=0)
        assert result.items == []
        assert result.total == 3  # issue #130 discipline


class TestComposition:
    def test_flat_filters_intersect_ranked_hits(self, client):
        result = client.search(
            "Note", "cortex", filters=[{"field": "tissue", "value": "brain"}]
        )
        assert ids(result) == ["n1", "n2"]  # n3 is liver; rank preserved
        assert result.total == 2

    def test_where_tree_intersects_ranked_hits(self, client):
        result = client.search(
            "Note", "cortex",
            where={"field": "priority", "op": "gte", "value": 2},
        )
        assert ids(result) == ["n2", "n3"]
        assert result.total == 2

    def test_or_mode_never_escapes_the_hit_set(self, client):
        # OR applies among the caller's flat filters only; the ranked id
        # set ANDs in via the where tree — n4 matches priority>=1 but is
        # not an FTS hit, so it must not surface.
        result = client.search(
            "Note", "cortex",
            filters=[
                {"field": "tissue", "value": "liver"},
                {"field": "priority", "value": 1},
            ],
            filter_mode="or",
        )
        assert ids(result) == ["n1", "n3"]

    def test_unknown_flat_filter_field_matches_zero_rows(self, client):
        # Legacy #149 SDK behavior (GraphQL rejects far earlier with a
        # coded error).
        result = client.search("Note", "cortex", filters=[{"nope": "x"}])
        assert result.items == [] and result.total == 0


class TestOrderByOverridesRank:
    def test_order_by_overrides_rank(self, client):
        result = client.search(
            "Note", "cortex", order_by="priority", order_dir="desc"
        )
        assert ids(result) == ["n3", "n2", "n1"]
        assert result.total == 3

    def test_order_by_pushdown_pages_correctly(self, client):
        result = client.search(
            "Note", "cortex", limit=1, offset=1,
            order_by="priority", order_dir="desc",
        )
        assert ids(result) == ["n2"]
        assert result.total == 3

    def test_invalid_order_dir_raises(self, client):
        from mosaic.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="order_dir"):
            client.search("Note", "cortex", order_by="priority", order_dir="up")


class TestAvailabilityConsistency:
    def test_unavailable_entities_never_surface(self, client):
        client.put("Note", {"id": "n5", "name": "Ghost", "tissue": "brain",
                            "priority": 5, "body": "cortex ghost entry gone"})
        assert "n5" in ids(client.search("Note", "cortex"))
        client.set_availability_bulk(
            entity_type="Note", entity_ids=["n5"],
            is_available=False, reason="test",
        )
        result = client.search("Note", "cortex")
        assert "n5" not in ids(result)
        assert result.total == 3


class TestNoNPlusOne:
    def test_one_batched_read_per_page(self, client, monkeypatch):
        storage = client._storage
        calls = {"find": 0, "read": 0, "get_temporal": 0}

        real_find, real_read, real_temporal = (
            storage.find, storage.read, storage.get_temporal
        )
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
        monkeypatch.setattr(
            storage, "get_temporal",
            lambda *a, **k: (calls.__setitem__(
                "get_temporal", calls["get_temporal"] + 1),
                real_temporal(*a, **k))[1],
        )

        result = client.search("Note", "cortex")
        assert result.total == 3
        # One composed find() for the page and one batched temporal
        # aggregation — never a per-hit get()/read() loop.
        assert calls == {"find": 1, "read": 0, "get_temporal": 1}
