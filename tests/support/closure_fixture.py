"""Shared fixture and cases for closure-aware reference resolution (#241).

ADR-0014 Decision 1: a reference ranged on class ``C`` resolves across
``concrete_class_closure(C)``, and so do ``count_relationship``, edge
predicates and a concrete parent's own collection. Before that landed,
rows that belong to a subclass, or to an abstract range's leaves, were
lost; the cases below pin each lost-row case (they were strict xfails in
#274 and pass since the fix).

The fixture is the minimal one #241 names:

- ``Person`` with concrete subclass ``Donor``;
- ``Process`` with concrete subclass ``BankBrain``;
- abstract ``Sample`` with leaves ``Brain`` and ``Blood``;
- to-many ``Assay.inputs`` → ``Sample`` (abstract range);
- ``Measurement.person`` → ``Person``;
- inverses ``Person.processes`` (← ``Process.operator``) and
  ``Person.measurements`` (← ``Measurement.person``).

The SDK cases are mixed into a SQLite test class (``tests/core``) and a
Postgres one (``tests/integration``), and so are the GraphQL cases
(``tests/graphql`` and ``tests/integration``). Each test module supplies
the ``client`` fixture.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mosaic.linkml_bridge import SchemaRegistry

SCHEMA = """\
id: https://example.org/closure241
name: closure241
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Person:
    is_a: Entity
    attributes:
      name:
      measurements:
        range: Measurement
        multivalued: true
        inverse: person
      processes:
        range: Process
        multivalued: true
        inverse: operator
  Donor:
    is_a: Person
    attributes:
      cohort:
  Process:
    is_a: Entity
    annotations:
      hippo_accessor: processes
    attributes:
      name:
      operator:
        range: Person
  BankBrain:
    is_a: Process
    attributes:
      site:
  Sample:
    is_a: Entity
    abstract: true
    attributes:
      label:
      produced_by:
        range: Process
  Brain:
    is_a: Sample
    attributes:
      hemisphere:
  Blood:
    is_a: Sample
    attributes:
      volume_ml:
        range: float
  Assay:
    is_a: Entity
    attributes:
      name:
      inputs:
        range: Sample
        multivalued: true
  Measurement:
    is_a: Entity
    attributes:
      value:
      person:
        range: Person
"""


def build_closure_registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


def seed(client: Any) -> None:
    """Rows chosen so every lost-row case has one subclass row and one control.

    - ``pr1`` (a plain ``Process``) and ``bb1`` (a ``BankBrain``), both
      operated by donor ``d1``;
    - ``br1`` (``Brain``) produced by ``bb1``, the subclass row;
      ``bl1`` (``Blood``) produced by ``pr1``, the control;
    - ``a1`` (``Assay``) with ``inputs`` ``[br1, bl1]``, one per leaf;
    - ``m1`` measures donor ``d1``, the subclass row; ``m2`` measures
      person ``p1``, the control.
    """
    client.put("Person", {"id": "p1", "name": "Pat"})
    client.put("Donor", {"id": "d1", "name": "Dee", "cohort": "case"})
    client.put("Process", {"id": "pr1", "name": "plain", "operator": "d1"})
    client.put("BankBrain", {"id": "bb1", "name": "banked", "site": "A", "operator": "d1"})
    client.put("Brain", {"id": "br1", "label": "brain", "hemisphere": "L", "produced_by": "bb1"})
    client.put("Blood", {"id": "bl1", "label": "blood", "volume_ml": 2.0, "produced_by": "pr1"})
    client.put("Assay", {"id": "a1", "name": "rna", "inputs": ["br1", "bl1"]})
    client.put("Measurement", {"id": "m1", "value": "5", "person": "d1"})
    client.put("Measurement", {"id": "m2", "value": "6", "person": "p1"})


def _ids(client: Any, entity_type: str, where: Any = None) -> set[str]:
    return {item["id"] for item in client.query(entity_type, where=where).items}


class ClosureSDKCases:
    """SDK cases from #241 plus a concrete parent's own collection (ADR-0014)."""

    # -- controls: these hold today and must keep holding ----------------

    def test_control_forward_filter_to_base_row(self, client):
        where = {"edge": "produced_by", "where": {"field": "name", "value": "plain"}}
        assert _ids(client, "Blood", where) == {"bl1"}

    def test_control_forward_to_many_hydrates_both_leaves(self, client):
        assert sorted(client.get("Assay", "a1")["data"]["inputs"]) == ["bl1", "br1"]

    def test_control_inverse_on_concrete_base_lists_and_counts_subclass_row(self, client):
        assert sorted(client.get("Donor", "d1")["data"]["processes"]) == ["bb1", "pr1"]
        assert client.count_relationship("Donor", "d1", "processes") == 2

    # -- #241 lost-row cases ----------------------------------------------

    def test_forward_filter_through_concrete_base_reaches_subclass_row(self, client):
        where = {"edge": "produced_by", "where": {"field": "name", "value": "banked"}}
        assert _ids(client, "Brain", where) == {"br1"}

    def test_count_relationship_forward_into_abstract_range(self, client):
        assert client.count_relationship("Assay", "a1", "inputs") == 2

    def test_edge_filter_into_abstract_range_on_leaf_field(self, client):
        where = {
            "edge": "inputs",
            "quantifier": "some",
            "where": {"field": "hemisphere", "value": "L"},
        }
        assert _ids(client, "Assay", where) == {"a1"}

    # -- ADR-0014 Decision 1: a concrete parent's own collection ----------

    def test_concrete_parent_collection_includes_subclass_rows(self, client):
        page = client.query("Process")
        by_id = {item["id"]: item for item in page.items}
        assert set(by_id) == {"pr1", "bb1"}
        assert by_id["bb1"]["entity_type"] == "BankBrain"
        assert by_id["pr1"]["entity_type"] == "Process"

    def test_concrete_parent_count_total_and_aggregates_span_closure(self, client):
        assert client.query("Process").total == 2
        assert client.count("Process") == 2
        assert dict(client.facet_counts("Process", "name")) == {"plain": 1, "banked": 1}
        # Filters and ordering address the base's slots, over every row.
        page = client.query("Process", order_by="name", limit=1)
        assert [i["id"] for i in page.items] == ["bb1"]
        assert page.total == 2
        assert {i["id"] for i in client.query(
            "Process", where={"field": "operator", "value": "d1"}
        ).items} == {"pr1", "bb1"}
        # A subclass row keeps its own stored slots in the SDK envelope.
        bb1 = next(i for i in client.query("Process").items if i["id"] == "bb1")
        assert bb1["data"]["site"] == "A"

    def test_include_subclasses_false_keeps_type_exact_rows(self, client):
        assert _ids(client, "Process") == {"pr1", "bb1"}
        exact = client.query("Process", include_subclasses=False)
        assert {i["id"] for i in exact.items} == {"pr1"}
        assert client.count("Process", include_subclasses=False) == 1
        # A leaf's collection is unchanged.
        assert _ids(client, "BankBrain") == {"bb1"}

    def test_edge_filter_into_abstract_range_leaf_field_none_and_shared(self, client):
        # Shared slot on the abstract range: rows of either leaf.
        where = {"edge": "inputs", "quantifier": "some",
                 "where": {"field": "label", "value": "blood"}}
        assert _ids(client, "Assay", where) == {"a1"}
        # A leaf-only field reads as absent on the other leaf: no Blood has a
        # hemisphere, so `none` holds for a1 only if no Brain input is L.
        where = {"edge": "inputs", "quantifier": "none",
                 "where": {"field": "hemisphere", "value": "L"}}
        assert _ids(client, "Assay", where) == set()
        where = {"edge": "inputs", "quantifier": "some",
                 "where": {"field": "hemisphere", "op": "is_null", "value": True}}
        assert _ids(client, "Assay", where) == {"a1"}  # bl1 has none

    def test_neighbors_reverse_column_edge_reaches_subclass_row(self, client):
        result = client.neighbors("bb1", depth=1)
        edges = {(e["source"], e["target"], e["type"]) for e in result["edges"]}
        assert ("br1", "bb1", "produced_by") in edges


def run_graphql(schema: Any, builder: Any, client: Any, query: str) -> dict[str, Any]:
    """Execute ``query`` and return ``data``; a GraphQL error is a RuntimeError."""
    result = asyncio.run(
        schema.execute(query, context_value={"client": client, "builder": builder})
    )
    if result.errors:
        raise RuntimeError(f"GraphQL errors: {result.errors}")
    return result.data


class ClosureGraphQLCases:
    """GraphQL cases from #241 plus a concrete parent's own collection.

    The using module supplies ``client`` and a ``gql(query) -> data``
    fixture.
    """

    # -- controls ----------------------------------------------------------

    def test_control_forward_reference_to_base_row(self, gql):
        data = gql('{ bloods { items { id producedBy { id name __typename } } } }')
        assert data["bloods"]["items"] == [
            {"id": "bl1", "producedBy": {"id": "pr1", "name": "plain", "__typename": "Process"}}
        ]

    def test_control_forward_reference_to_base_person(self, gql):
        data = gql('{ measurements { items { id person { id name } } } }')
        by_id = {i["id"]: i for i in data["measurements"]["items"]}
        assert by_id["m2"]["person"] == {"id": "p1", "name": "Pat"}

    # -- #241 lost-row cases ----------------------------------------------

    def test_forward_reference_to_subclass_row(self, gql):
        data = gql('{ brains { items { id producedBy { id name __typename } } } }')
        (row,) = data["brains"]["items"]
        assert row["producedBy"] is not None, "producedBy is null (the #241 bug)"
        # Decision 1: rendered as the base object type.
        assert row["producedBy"] == {"id": "bb1", "name": "banked", "__typename": "Process"}

    def test_forward_reference_to_subclass_person(self, gql):
        data = gql('{ measurements { items { id person { id name __typename } } } }')
        by_id = {i["id"]: i for i in data["measurements"]["items"]}
        assert by_id["m1"]["person"] is not None, "person is null (the #241 bug)"
        assert by_id["m1"]["person"] == {"id": "d1", "name": "Dee", "__typename": "Person"}

    def test_inverse_on_concrete_base_list_matches_count(self, gql):
        data = gql('{ donors { items { id processes { id } processesCount } } }')
        (row,) = data["donors"]["items"]
        assert row["processesCount"] == 2
        assert sorted(p["id"] for p in row["processes"]) == ["bb1", "pr1"]

    # -- ADR-0014 Decision 1: a concrete parent's own collection ----------

    def test_concrete_parent_collection_includes_subclass_rows(self, gql):
        data = gql('{ processes { total items { id __typename } } }')
        page = data["processes"]
        assert page["total"] == 2
        assert sorted(i["id"] for i in page["items"]) == ["bb1", "pr1"]
        assert {i["__typename"] for i in page["items"]} == {"Process"}

    # -- ADR-0014 Decision 1: entityType on a concrete parent --------------

    def test_concrete_parent_entity_type_names_row_class(self, gql):
        data = gql('{ processes { items { id entityType } } }')
        by_id = {i["id"]: i["entityType"] for i in data["processes"]["items"]}
        assert by_id == {"pr1": "Process", "bb1": "BankBrain"}

    def test_reference_entity_type_names_subclass(self, gql):
        data = gql(
            '{ brains { items { producedBy { id entityType __typename } } } }'
        )
        (row,) = data["brains"]["items"]
        assert row["producedBy"] == {
            "id": "bb1", "entityType": "BankBrain", "__typename": "Process"
        }
        data = gql('{ measurements { items { id person { entityType } } } }')
        by_id = {i["id"]: i["person"]["entityType"] for i in data["measurements"]["items"]}
        assert by_id == {"m1": "Donor", "m2": "Person"}

    def test_entity_type_is_non_null_and_only_on_concrete_parents(self, gql):
        def fields(type_name):
            data = gql(
                '{ __type(name: "%s") { fields { name type { kind ofType { name } } } } }'
                % type_name
            )
            return {f["name"]: f["type"] for f in data["__type"]["fields"]}

        for parent in ("Process", "Person"):
            assert fields(parent)["entityType"] == {
                "kind": "NON_NULL", "ofType": {"name": "String"}
            }
        # Leaves (and classes without exposed subclasses) are unchanged.
        for leaf in ("BankBrain", "Donor", "Brain", "Assay", "Measurement"):
            assert "entityType" not in fields(leaf)
