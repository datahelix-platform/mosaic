"""To-one relationship predicates (ADR-0006 M5a, issue #155 increment 3).

A ``where`` tree node ``{"edge": <to-one reference slot>, "where": subtree}``
compiles to ONE correlated EXISTS against the target's table keyed on the
FK column. Covers composition with scalar predicates and combinators,
nesting (edge inside edge, self-referential edges), target availability,
the loud errors (unknown edge, multivalued edge → M5b message), and the
as-of gate. Postgres parity lives in
``tests/integration/test_postgres_adapter.py``.
"""

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationError
from mosaic.core.storage import has_relationship_predicate, normalize_where
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

FUTURE = "2999-01-01T00:00:00+00:00"

SCHEMA = """
id: https://example.org/mosaic/test_relationship_predicates
name: test_relationship_predicates
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Facility:
    is_a: Entity
    attributes:
      name:
        required: true
      city: {}

  Donor:
    is_a: Entity
    attributes:
      name:
        required: true
      age:
        range: integer
      facility_id:
        range: Facility

  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      tissue: {}
      donor_id:
        range: Donor
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
        db_path = os.path.join(tmpdir, "test_relationship_predicates.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, bypass_validation=True)
        c.put("Facility", {"id": "f1", "name": "North", "city": "Boston"})
        c.put("Facility", {"id": "f2", "name": "South", "city": "NYC"})
        c.put("Donor", {"id": "d1", "name": "Ada", "age": 76, "facility_id": "f1"})
        c.put("Donor", {"id": "d2", "name": "Alan", "age": 41, "facility_id": "f2"})
        c.put("Donor", {"id": "d3", "name": "Grace", "age": 85})  # no facility
        c.put("Sample", {"id": "s1", "name": "S1", "tissue": "brain", "donor_id": "d1"})
        c.put("Sample", {"id": "s2", "name": "S2", "tissue": "brain", "donor_id": "d2"})
        c.put("Sample", {"id": "s3", "name": "S3", "tissue": "liver", "donor_id": "d1"})
        c.put("Sample", {"id": "s4", "name": "S4", "tissue": "brain"})  # no donor
        c.put("Sample", {"id": "s5", "name": "S5", "tissue": "liver", "parent": "s1"})
        yield c


def ids(client, entity_type="Sample", **kwargs) -> set[str]:
    return {i["id"] for i in client.query(entity_type, **kwargs).items}


class TestToOnePredicates:
    def test_basic_edge_predicate(self, client):
        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        assert ids(client, where=where) == {"s1", "s3"}

    def test_edge_composes_with_scalar_by_and(self, client):
        where = {
            "and": [
                {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}},
                {"field": "tissue", "value": "brain"},
            ]
        }
        assert ids(client, where=where) == {"s1"}

    def test_edge_under_or(self, client):
        where = {
            "or": [
                {"edge": "donor_id", "where": {"field": "age", "op": "lt", "value": 50}},
                {"field": "tissue", "value": "liver"},
            ]
        }
        assert ids(client, where=where) == {"s2", "s3", "s5"}

    def test_not_edge_includes_refless_entities(self, client):
        # NOT EXISTS: entities with no referenced target (s4, s5) satisfy
        # the negation — two-valued semantics, same rule as scalar `not`.
        where = {
            "not": {
                "edge": "donor_id",
                "where": {"field": "age", "op": "gt", "value": 60},
            }
        }
        assert ids(client, where=where) == {"s2", "s4", "s5"}

    def test_nested_edge_inside_edge(self, client):
        where = {
            "edge": "donor_id",
            "where": {
                "edge": "facility_id",
                "where": {"field": "city", "value": "Boston"},
            },
        }
        assert ids(client, where=where) == {"s1", "s3"}

    def test_self_referential_edge(self, client):
        where = {"edge": "parent", "where": {"field": "tissue", "value": "brain"}}
        assert ids(client, where=where) == {"s5"}

    def test_edge_predicate_on_donors_via_facility(self, client):
        where = {"edge": "facility_id", "where": {"field": "city", "value": "NYC"}}
        assert ids(client, "Donor", where=where) == {"d2"}


class TestAvailabilityConsistency:
    def test_unavailable_target_never_matches(self, client):
        client.set_availability_bulk(
            entity_type="Donor", entity_ids=["d2"],
            is_available=False, reason="test",
        )
        where = {"edge": "donor_id", "where": {"field": "age", "op": "lt", "value": 50}}
        assert ids(client, where=where) == set()
        # …and the complement flips accordingly: s2's target is now
        # invisible, so s2 satisfies the negation.
        neg = {"not": where}
        assert ids(client, where=neg) == {"s1", "s2", "s3", "s4", "s5"}


class TestAggregatesSeeEdgePredicates:
    """The shared-predicate rule (ADR-0007): count/facets/range run under
    the identical WHERE, relationship predicates included."""

    def test_count_with_edge_predicate(self, client):
        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        assert client.count("Sample", where=where) == 2

    def test_facets_with_edge_predicate(self, client):
        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        assert client.facet_counts("Sample", "tissue", where=where) == [
            ("brain", 1),
            ("liver", 1),
        ]


class TestLoudErrors:
    def test_unknown_edge_raises(self, client):
        with pytest.raises(ValidationError, match="reference slots"):
            ids(client, where={"edge": "nope", "where": {"field": "age", "value": 1}})

    def test_multivalued_edge_raises_with_m5b_pointer(self, client):
        with pytest.raises(ValidationError, match="M5b"):
            ids(
                client,
                "Study",
                where={"edge": "sample_ids", "where": {"field": "tissue", "value": "brain"}},
            )

    def test_malformed_edge_node_raises(self, client):
        with pytest.raises(ValidationError, match="edge"):
            normalize_where({"edge": "donor_id"})  # missing "where"
        with pytest.raises(ValidationError, match="edge"):
            normalize_where({"edge": "donor_id", "where": {"field": "x", "value": 1}, "op": "eq"})

    def test_as_of_with_edge_predicate_raises(self, client):
        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        with pytest.raises(ValidationError, match="as_of"):
            client.query("Sample", where=where, as_of=FUTURE)
        with pytest.raises(ValidationError, match="as_of"):
            client.count("Sample", where=where, as_of=FUTURE)

    def test_scalar_where_under_as_of_still_works(self, client):
        # The gate is specific to relationship predicates.
        live = ids(client, where={"field": "tissue", "value": "brain"})
        asof = ids(client, where={"field": "tissue", "value": "brain"}, as_of=FUTURE)
        assert live == asof


class TestTreeHelpers:
    def test_has_relationship_predicate(self):
        edge = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 1}}
        assert has_relationship_predicate(normalize_where(edge))
        assert has_relationship_predicate(
            normalize_where({"not": {"and": [edge, {"field": "x", "value": 1}]}})
        )
        assert not has_relationship_predicate(
            normalize_where({"field": "x", "value": 1})
        )
