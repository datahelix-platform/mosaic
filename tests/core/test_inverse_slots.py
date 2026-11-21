"""Storage contract for ``inverse``-declared slots on SQLite (ADR-0011 / #204).

``Donor.samples: {range: Sample, multivalued: true, inverse: donor}`` is a
*virtual* reverse edge over ``Sample.donor``'s FK column:

- hydrated on read from the forward column (available targets only);
- filterable as a to-many relationship predicate (``some``/``none``)
  compiled against the target table, not the relationships link table;
- countable via ``count_relationship``;
- ignored on write — never a column, never relationship rows, never in
  the provenance patch.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationError
from mosaic.core.storage import Query
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

SCHEMA = """\
id: https://example.org/inverse_storage
name: inverse_storage
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
      cohort:
      samples:
        range: Sample
        multivalued: true
        inverse: donor
  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      tissue:
      volume:
        range: integer
      donor:
        range: Donor
"""


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "inverse.db"), schema_registry=registry
        )
        c = MosaicClient(storage=storage, bypass_validation=True)
        c.put("Donor", {"id": "d1", "name": "D1", "cohort": "case"})
        c.put("Donor", {"id": "d2", "name": "D2", "cohort": "control"})
        c.put("Donor", {"id": "d3", "name": "D3", "cohort": "case"})  # no samples
        c.put("Sample", {"id": "s1", "name": "S1", "tissue": "brain", "volume": 10, "donor": "d1"})
        c.put("Sample", {"id": "s2", "name": "S2", "tissue": "liver", "volume": 20, "donor": "d1"})
        c.put("Sample", {"id": "s3", "name": "S3", "tissue": "brain", "volume": 30, "donor": "d2"})
        c.put("Sample", {"id": "s4", "name": "S4", "tissue": "blood", "volume": 5})  # orphan
        yield c


def ids(client, where, entity_type="Donor") -> set[str]:
    return {i["id"] for i in client.query(entity_type, where=where).items}


def some(sub):
    return {"edge": "samples", "quantifier": "some", "where": sub}


def none(sub):
    return {"edge": "samples", "quantifier": "none", "where": sub}


class TestHydration:
    def test_get_hydrates_reverse_ids_from_forward_fk(self, client):
        assert client.get("Donor", "d1")["data"]["samples"] == ["s1", "s2"]
        assert client.get("Donor", "d2")["data"]["samples"] == ["s3"]

    def test_donor_without_samples_has_no_key(self, client):
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

    def test_forward_side_still_reads_the_plain_fk(self, client):
        assert client.get("Sample", "s1")["data"]["donor"] == "d1"


class TestWritesAreIgnored:
    def test_put_carrying_inverse_slot_writes_no_relationship_rows(self, client):
        client.put("Donor", {"id": "d4", "name": "D4", "samples": ["s4", "s1"]})
        assert client.relationships.find_relationships(source_id="d4") == []
        # Hydration reflects the forward FKs, not the ignored payload.
        assert "samples" not in client.get("Donor", "d4")["data"]

    def test_get_then_put_round_trip_is_stable(self, client):
        got = client.get("Donor", "d1")
        assert got["data"]["samples"] == ["s1", "s2"]
        client.put("Donor", {"id": "d1", **got["data"]})
        again = client.get("Donor", "d1")
        assert again["data"]["samples"] == ["s1", "s2"]
        assert again["data"]["name"] == "D1"
        assert client.relationships.find_relationships(source_id="d1") == []

    @staticmethod
    def _patches(client, entity_id: str) -> list[dict]:
        out = []
        for record in client.history(entity_id):
            patch = record.get("patch") or record.get("state_snapshot") or {}
            out.append(patch if isinstance(patch, dict) else {})
        return out

    def test_inverse_slot_never_lands_in_the_provenance_patch(self, client):
        client.put("Donor", {"id": "d1", "name": "D1", "cohort": "case", "samples": ["s1"]})
        patches = self._patches(client, "d1")
        assert len(patches) >= 2  # create + update
        assert all("samples" not in p for p in patches)
        assert any(p.get("cohort") == "case" for p in patches)

    def test_delete_snapshot_omits_the_derived_list(self, client):
        client.delete("Donor", "d1")
        patches = self._patches(client, "d1")
        assert any(p.get("name") == "D1" for p in patches)
        assert all("samples" not in p for p in patches)

    def test_as_of_state_never_carries_the_derived_list(self, client):
        client.put("Donor", {"id": "d1", "name": "D1", "cohort": "case", "samples": ["s1"]})
        state = client.state_at("d1", "2999-01-01T00:00:00+00:00")
        assert state is not None
        data = state.get("data", state)
        assert "samples" not in data


class TestReversePredicate:
    def test_some_matches_donors_with_a_qualifying_sample(self, client):
        assert ids(client, some({"field": "tissue", "value": "brain"})) == {"d1", "d2"}
        assert ids(client, some({"field": "tissue", "value": "liver"})) == {"d1"}

    def test_none_is_the_complement_and_includes_sampleless(self, client):
        assert ids(client, none({"field": "tissue", "value": "liver"})) == {"d2", "d3"}

    def test_some_with_comparison_on_target(self, client):
        assert ids(client, some({"field": "volume", "op": "gte", "value": 25})) == {"d2"}

    def test_existence_check_via_identifier_is_null(self, client):
        exists = some({"field": "id", "op": "is_null", "value": False})
        assert ids(client, exists) == {"d1", "d2"}
        assert ids(client, none({"field": "id", "op": "is_null", "value": False})) == {"d3"}

    def test_composes_with_scalar_and_combinators(self, client):
        where = {"and": [
            {"field": "cohort", "value": "case"},
            some({"field": "tissue", "value": "brain"}),
        ]}
        assert ids(client, where) == {"d1"}

    def test_unavailable_target_never_satisfies_some(self, client):
        client.delete("Sample", "s3")
        assert ids(client, some({"field": "tissue", "value": "brain"})) == {"d1"}

    def test_count_sees_the_reverse_predicate(self, client):
        assert client.count("Donor", where=some({"field": "tissue", "value": "brain"})) == 2

    def test_reverse_edge_nests_inside_a_forward_edge(self, client):
        # Sample -> donor (to-one, forward) -> samples (reverse): samples
        # whose donor also has a liver sample.
        where = {"edge": "donor", "where": some({"field": "tissue", "value": "liver"})}
        assert ids(client, where, entity_type="Sample") == {"s1", "s2"}

    def test_reverse_edge_requires_a_quantifier(self, client):
        with pytest.raises(ValidationError):
            client.query("Donor", where={"edge": "samples", "where": {"field": "tissue", "value": "brain"}})


class TestCount:
    def test_count_relationship_counts_available_targets(self, client):
        assert client.count_relationship("Donor", "d1", "samples") == 2
        assert client.count_relationship("Donor", "d2", "samples") == 1
        assert client.count_relationship("Donor", "d3", "samples") == 0

    def test_count_relationship_excludes_unavailable(self, client):
        client.delete("Sample", "s1")
        assert client.count_relationship("Donor", "d1", "samples") == 1


class TestNoStorageOfItsOwn:
    def test_no_column_no_link_table(self, client):
        import sqlite3

        conn = sqlite3.connect(str(client.storage.database_path))
        cols = {r[1] for r in conn.execute('PRAGMA table_info("Donor")')}
        assert "samples" not in cols
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "Donor_samples" not in tables
