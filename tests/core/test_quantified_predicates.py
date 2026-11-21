"""To-many quantified relationship predicates (ADR-0006 M5b, issue #155
increment 4 — the final typed-filter increment).

``{"edge": <multivalued reference slot>, "quantifier": "some"|"none",
"where": subtree}`` compiles to EXISTS/NOT EXISTS against the ADR-0002
``relationships`` link table joined to the target's table. Covers the
quantifier semantics (some/none, edgeless entities, availability of edge
and target), composition, quantifier/cardinality mismatch errors, and the
as-of gate. Postgres parity lives in
``tests/integration/test_postgres_adapter.py``.
"""

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationError
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

FUTURE = "2999-01-01T00:00:00+00:00"

SCHEMA = """
id: https://example.org/mosaic/test_quantified_predicates
name: test_quantified_predicates
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
      volume:
        range: integer
      parent:
        range: Sample

  Study:
    is_a: Entity
    attributes:
      title:
        required: true
      status: {}
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
        db_path = os.path.join(tmpdir, "test_quantified_predicates.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, bypass_validation=True)
        c.put("Sample", {"id": "s1", "name": "S1", "tissue": "brain", "volume": 10})
        c.put("Sample", {"id": "s2", "name": "S2", "tissue": "liver", "volume": 20})
        c.put("Sample", {"id": "s3", "name": "S3", "tissue": "brain", "volume": 30})
        c.put("Study", {"id": "st1", "title": "Brain+Liver",
                        "sample_ids": ["s1", "s2"]})
        c.put("Study", {"id": "st2", "title": "LiverOnly",
                        "sample_ids": ["s2"]})
        c.put("Study", {"id": "st3", "title": "Empty"})  # no edges
        c.put("Study", {"id": "st4", "title": "BrainOnly",
                        "sample_ids": ["s3"]})
        yield c


def ids(client, where, entity_type="Study") -> set[str]:
    return {i["id"] for i in client.query(entity_type, where=where).items}


def some(sub):
    return {"edge": "sample_ids", "quantifier": "some", "where": sub}


def none(sub):
    return {"edge": "sample_ids", "quantifier": "none", "where": sub}


class TestQuantifiers:
    def test_some_matches_any_linked_target(self, client):
        assert ids(client, some({"field": "tissue", "value": "brain"})) == {
            "st1", "st4",
        }

    def test_none_is_the_complement_and_includes_edgeless(self, client):
        # `none` matches studies with NO linked brain sample — including
        # st3, which has no edges at all.
        assert ids(client, none({"field": "tissue", "value": "brain"})) == {
            "st2", "st3",
        }

    def test_some_with_comparison_on_target(self, client):
        assert ids(
            client, some({"field": "volume", "op": "gte", "value": 25})
        ) == {"st4"}

    def test_quantifier_composes_with_scalar_and_combinators(self, client):
        where = {
            "and": [
                some({"field": "tissue", "value": "brain"}),
                {"not": some({"field": "tissue", "value": "liver"})},
            ]
        }
        assert ids(client, where) == {"st4"}

    def test_some_and_none_together(self, client):
        where = {
            "and": [
                some({"field": "tissue", "value": "liver"}),
                none({"field": "tissue", "value": "brain"}),
            ]
        }
        assert ids(client, where) == {"st2"}

    def test_aggregates_see_quantified_predicates(self, client):
        assert client.count(
            "Study", where=some({"field": "tissue", "value": "brain"})
        ) == 2


class TestAvailability:
    def test_unavailable_target_never_satisfies_some(self, client):
        client.set_availability_bulk(
            entity_type="Sample", entity_ids=["s3"],
            is_available=False, reason="test",
        )
        assert ids(client, some({"field": "tissue", "value": "brain"})) == {
            "st1",
        }
        # …and `none` gains st4, whose only brain sample is now invisible.
        assert ids(client, none({"field": "tissue", "value": "brain"})) == {
            "st2", "st3", "st4",
        }


class TestLoudErrors:
    def test_multivalued_edge_without_quantifier_raises(self, client):
        with pytest.raises(ValidationError, match="some"):
            ids(client, {"edge": "sample_ids",
                         "where": {"field": "tissue", "value": "brain"}})

    def test_quantifier_on_to_one_edge_raises(self, client):
        with pytest.raises(ValidationError, match="to-one"):
            ids(
                client,
                {"edge": "parent", "quantifier": "some",
                 "where": {"field": "tissue", "value": "brain"}},
                entity_type="Sample",
            )

    def test_unknown_quantified_edge_raises(self, client):
        with pytest.raises(ValidationError, match="reference slots"):
            ids(client, {"edge": "nope", "quantifier": "some",
                         "where": {"field": "tissue", "value": "brain"}})

    def test_invalid_quantifier_raises(self, client):
        with pytest.raises(ValidationError, match="quantifier"):
            ids(client, {"edge": "sample_ids", "quantifier": "all",
                         "where": {"field": "tissue", "value": "brain"}})

    def test_as_of_with_quantifier_raises(self, client):
        with pytest.raises(ValidationError, match="as_of"):
            client.query(
                "Study",
                where=some({"field": "tissue", "value": "brain"}),
                as_of=FUTURE,
            )
