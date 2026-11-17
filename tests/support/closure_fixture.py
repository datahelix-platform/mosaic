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
  ``Person.measurements`` (← ``Measurement.person``);
- leaf-only reference slots ``Blood.drawn_from`` → ``Person`` and
  ``BankBrain.supervisor`` → ``Person`` (the D1 read-as-NULL cases).

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
      supervisor:
        range: Person
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
      drawn_from:
        range: Person
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
      person ``p1``, the control;
    - ``bl1`` was drawn from ``p1`` and ``bb1`` is supervised by ``p1``
      (leaf-only reference slots).
    """
    client.put("Person", {"id": "p1", "name": "Pat"})
    client.put("Donor", {"id": "d1", "name": "Dee", "cohort": "case"})
    client.put("Process", {"id": "pr1", "name": "plain", "operator": "d1"})
    client.put("BankBrain", {"id": "bb1", "name": "banked", "site": "A",
                             "operator": "d1", "supervisor": "p1"})
    client.put("Brain", {"id": "br1", "label": "brain", "hemisphere": "L", "produced_by": "bb1"})
    client.put("Blood", {"id": "bl1", "label": "blood", "volume_ml": 2.0,
                         "produced_by": "pr1", "drawn_from": "p1"})
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

    # -- ADR-0014 Decision 1 amendment (D1 = read-as-NULL) -----------------
    # Under a closure edge, a field or reference edge only some classes of
    # the range have reads as NULL / memberless on the others. Inverse
    # closure edges follow the same rule; a name no class has still raises.

    def test_forward_not_on_leaf_only_field(self, client):
        # bl1 has no hemisphere: `hemisphere = L` is false there, so `not`
        # holds and a1 matches through its Blood input.
        where = {"edge": "inputs", "quantifier": "some",
                 "where": {"not": {"field": "hemisphere", "value": "L"}}}
        assert _ids(client, "Assay", where) == {"a1"}
        where = {"edge": "inputs", "quantifier": "none",
                 "where": {"not": {"field": "hemisphere", "value": "L"}}}
        assert _ids(client, "Assay", where) == set()

    def test_forward_nested_leaf_only_reference_slot(self, client):
        # Only Blood has drawn_from; br1 has no members on it.
        where = {"edge": "inputs", "quantifier": "some",
                 "where": {"edge": "drawn_from",
                           "where": {"field": "name", "value": "Pat"}}}
        assert _ids(client, "Assay", where) == {"a1"}
        where = {"edge": "inputs", "quantifier": "none",
                 "where": {"edge": "drawn_from",
                           "where": {"field": "name", "value": "Pat"}}}
        assert _ids(client, "Assay", where) == set()
        # `not` over the absent edge holds on br1 (no members).
        where = {"edge": "inputs", "quantifier": "some",
                 "where": {"not": {"edge": "drawn_from",
                                   "where": {"field": "name", "value": "Pat"}}}}
        assert _ids(client, "Assay", where) == {"a1"}

    def test_inverse_is_null_on_leaf_only_field(self, client):
        # Person.processes spans {Process, BankBrain}; only BankBrain has
        # site, so pr1 reads it as NULL.
        where = {"edge": "processes", "quantifier": "some",
                 "where": {"field": "site", "op": "is_null", "value": True}}
        assert _ids(client, "Person", where) == {"d1"}
        where = {"edge": "processes", "quantifier": "some",
                 "where": {"field": "site", "value": "A"}}
        assert _ids(client, "Person", where) == {"d1"}

    def test_inverse_not_on_leaf_only_field(self, client):
        where = {"edge": "processes", "quantifier": "some",
                 "where": {"not": {"field": "site", "value": "A"}}}
        assert _ids(client, "Person", where) == {"d1"}  # via pr1
        where = {"edge": "processes", "quantifier": "none",
                 "where": {"field": "site", "value": "A"}}
        assert _ids(client, "Person", where) == {"p1"}  # p1 has no processes

    def test_inverse_nested_leaf_only_reference_slot(self, client):
        # Only BankBrain has supervisor; pr1 has no members on it.
        where = {"edge": "processes", "quantifier": "some",
                 "where": {"edge": "supervisor",
                           "where": {"field": "name", "value": "Pat"}}}
        assert _ids(client, "Person", where) == {"d1"}
        where = {"edge": "processes", "quantifier": "none",
                 "where": {"edge": "supervisor",
                           "where": {"field": "name", "value": "Pat"}}}
        assert _ids(client, "Person", where) == {"p1"}

    def test_closure_edge_unknown_name_still_raises(self, client):
        import pytest

        from mosaic.core.exceptions import ValidationError

        for nested in (
            {"field": "no_such_field", "value": "x"},
            {"edge": "no_such_edge", "where": {"field": "name", "value": "x"}},
        ):
            for edge in ("inputs", "processes"):
                base = "Assay" if edge == "inputs" else "Person"
                where = {"edge": edge, "quantifier": "some", "where": nested}
                with pytest.raises(ValidationError):
                    _ids(client, base, where)

    # -- ADR-0014 Decision 1 amendment (D3 = closure-aware by-id lookup) ---

    def test_get_through_concrete_parent_finds_subclass_row(self, client):
        row = client.get("Process", "bb1")
        assert row["entity_type"] == "BankBrain"
        assert row["data"]["site"] == "A"
        assert client.get("Person", "d1")["entity_type"] == "Donor"
        # The parent's own rows and the leaf's own lookup are unchanged.
        assert client.get("Process", "pr1")["entity_type"] == "Process"
        assert client.get("BankBrain", "bb1")["entity_type"] == "BankBrain"

    def test_get_through_parent_stays_type_checked(self, client):
        import pytest

        from mosaic.core.exceptions import EntityNotFoundError

        # Opt-out keeps the type-exact lookup.
        with pytest.raises(EntityNotFoundError):
            client.get("Process", "bb1", include_subclasses=False)
        # A leaf does not answer for its parent's row, nor a sibling class.
        with pytest.raises(EntityNotFoundError):
            client.get("BankBrain", "pr1")
        with pytest.raises(EntityNotFoundError):
            client.get("Process", "d1")

    # -- ADR-0014 Decisions 2-3 (#228): a family's own collection -----------
    # ``Sample`` is abstract: its collection is its kinds' rows (Brain br1,
    # Blood bl1), each keeping its own class; it has no rows of its own.

    def test_family_collection_lists_every_kind(self, client):
        page = client.query("Sample")
        assert {i["id"]: i["entity_type"] for i in page.items} == {
            "br1": "Brain", "bl1": "Blood"
        }
        assert page.total == 2
        assert client.count("Sample") == 2
        # Each row keeps its own stored slots.
        bl1 = next(i for i in page.items if i["id"] == "bl1")
        assert bl1["data"]["volume_ml"] == 2.0

    def test_family_include_subclasses_false_reads_nothing(self, client):
        # The family names a class with no rows of its own: a one-class-at-a-
        # time reader (a migration transform) sees none of its kinds' rows.
        assert client.query("Sample", include_subclasses=False).total == 0
        assert client.count("Sample", include_subclasses=False) == 0

    def test_family_get_by_id_returns_the_kind(self, client):
        import pytest

        from mosaic.core.exceptions import EntityNotFoundError

        row = client.get("Sample", "br1")
        assert row["entity_type"] == "Brain"
        assert row["data"]["hemisphere"] == "L"
        assert client.get("Sample", "bl1")["entity_type"] == "Blood"
        with pytest.raises(EntityNotFoundError):
            client.get("Sample", "br1", include_subclasses=False)
        with pytest.raises(EntityNotFoundError):
            client.get("Sample", "a1")  # not a kind of Sample

    def test_family_filters_shared_and_kind_only_fields(self, client):
        import pytest

        from mosaic.core.exceptions import ValidationError

        assert _ids(client, "Sample", {"field": "label", "value": "blood"}) == {"bl1"}
        # A field only one kind has reads as NULL on the others (the
        # 2026-11-17 read-as-NULL rule, applied to the family's collection).
        assert _ids(client, "Sample", {"field": "hemisphere", "value": "L"}) == {"br1"}
        assert _ids(client, "Sample", {
            "field": "hemisphere", "op": "is_null", "value": True}) == {"bl1"}
        assert _ids(client, "Sample", {
            "field": "volume_ml", "op": "gt", "value": 1.0}) == {"bl1"}
        assert _ids(client, "Sample", {"not": {"field": "hemisphere", "value": "L"}}) == {"bl1"}
        # A shared reference filters too.
        where = {"edge": "produced_by", "where": {"field": "name", "value": "banked"}}
        assert _ids(client, "Sample", where) == {"br1"}
        # A name no kind has is still rejected.
        with pytest.raises(ValidationError):
            _ids(client, "Sample", {"field": "no_such_field", "value": "x"})

    def test_family_order_facets_and_range(self, client):
        page = client.query("Sample", order_by="label")
        assert [i["id"] for i in page.items] == ["bl1", "br1"]
        assert dict(client.facet_counts("Sample", "label")) == {"blood": 1, "brain": 1}
        assert client.field_range("Sample", "volume_ml") == (2.0, 2.0)

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

    def test_by_id_through_concrete_parent_finds_subclass_row(self, gql):
        data = gql('{ process(id: "bb1") { id name entityType __typename } '
                   'person(id: "d1") { id entityType } '
                   'plain: process(id: "pr1") { entityType } '
                   'wrong: process(id: "d1") { id } }')
        assert data["process"] == {
            "id": "bb1", "name": "banked", "entityType": "BankBrain",
            "__typename": "Process",
        }
        assert data["person"] == {"id": "d1", "entityType": "Donor"}
        assert data["plain"] == {"entityType": "Process"}
        assert data["wrong"] is None

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

    # -- ADR-0014 Decisions 2-3 (#228): families in GraphQL -----------------

    def test_reference_into_family_returns_each_kind(self, gql):
        # No raw `inputsIds` fallback: the edge resolves to the interface and
        # each row is its own kind; a shared field needs no fragment.
        data = gql('{ assays { items { id inputs { __typename id label } inputsCount } } }')
        (row,) = data["assays"]["items"]
        assert sorted(row["inputs"], key=lambda i: i["id"]) == [
            {"__typename": "Blood", "id": "bl1", "label": "blood"},
            {"__typename": "Brain", "id": "br1", "label": "brain"},
        ]
        assert row["inputsCount"] == 2

    def test_family_interface_and_kinds(self, gql):
        data = gql('{ __type(name: "Sample") { kind fields { name } '
                   'possibleTypes { name } } }')
        t = data["__type"]
        assert t["kind"] == "INTERFACE"
        assert sorted(p["name"] for p in t["possibleTypes"]) == ["Blood", "Brain"]
        names = {f["name"] for f in t["fields"]}
        # Shared fields, including a shared reference; kind-only ones are not.
        assert {"id", "label", "producedBy"} <= names
        assert "hemisphere" not in names and "volumeMl" not in names
        assay = gql('{ __type(name: "Assay") { fields { name } } }')["__type"]
        assay_fields = {f["name"] for f in assay["fields"]}
        assert "inputs" in assay_fields and "inputsIds" not in assay_fields

    def test_family_collection_count_and_get(self, gql):
        data = gql('{ samples(orderBy: LABEL) { total items { __typename id '
                   '... on Brain { hemisphere } ... on Blood { volumeMl } } } '
                   'samplesCount '
                   'one: sample(id: "bl1") { __typename id producedBy { id } } '
                   'none: sample(id: "a1") { id } }')
        assert data["samples"]["total"] == 2
        assert data["samples"]["items"] == [
            {"__typename": "Blood", "id": "bl1", "volumeMl": 2.0},
            {"__typename": "Brain", "id": "br1", "hemisphere": "L"},
        ]
        assert data["samplesCount"] == 2
        assert data["one"] == {"__typename": "Blood", "id": "bl1", "producedBy": {"id": "pr1"}}
        assert data["none"] is None

    def test_family_where_shared_kind_only_and_through_edge(self, gql):
        data = gql('{ a: samples(where: {hemisphere: {eq: "L"}}) { items { id } } '
                   'b: samples(where: {label: {eq: "blood"}}) { items { id } } '
                   'c: samplesCount(where: {volumeMl: {gt: 1.0}}) '
                   'd: assays(where: {inputs: {some: {label: {eq: "blood"}}}}) { items { id } } '
                   'e: assays(where: {inputs: {some: {hemisphere: {eq: "R"}}}}) { items { id } } '
                   'f: samplesFacetCounts(field: "label") { value count } }')
        assert data["a"]["items"] == [{"id": "br1"}]
        assert data["b"]["items"] == [{"id": "bl1"}]
        assert data["c"] == 1
        assert data["d"]["items"] == [{"id": "a1"}]
        assert data["e"]["items"] == []
        assert {f["value"]: f["count"] for f in data["f"]} == {"blood": 1, "brain": 1}

    def test_family_schema_description(self, gql):
        data = gql('{ s: hippoEntityType(name: "Sample") { abstract parent kinds } '
                   'b: hippoEntityType(name: "Brain") { abstract parent kinds } '
                   'p: hippoEntityType(name: "Process") { abstract kinds } }')
        assert data["s"] == {"abstract": True, "parent": None, "kinds": ["Blood", "Brain"]}
        assert data["b"] == {"abstract": False, "parent": "Sample", "kinds": []}
        assert data["p"] == {"abstract": False, "kinds": ["Process", "BankBrain"]}
