"""Reverse edges over multivalued (relationship-row) forward slots (ADR-0013, #231).

ADR-0013 lifts ADR-0011's deferral: an ``inverse`` whose forward slot is
multivalued is a virtual reverse resolved through the ADR-0002
``relationships`` table, joining on ``target_id`` and scoped by
``relationship_type`` to the forward slot. Its 2026-11-10 amendment pins
which side of an ``inverse`` pair is stored:

- ``Assay.inputs: {multivalued: true, inverse: assays}`` with no
  ``Sample.assays`` — ``Assay.inputs`` is **stored** (writes persist as
  relationship rows) and ``Sample.assays`` is synthesized;
- ``Cohort.members: {inverse: cohort}`` over a real ``Person.cohort`` that
  doesn't declare back — ``Person.cohort`` is stored, ``Cohort.members``
  virtual;
- two multivalued slots declaring ``inverse`` on each other — ``SchemaError``.

Counts and ``some``/``none`` mirror the forward edge exactly.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import SchemaError
from mosaic.core.schema_typing import build_type_model
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

HEADER = """\
id: https://example.org/inverse_mv
name: inverse_mv
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
"""

# Forward-side declaration on a multivalued slot; the range is abstract
# (``Sample`` with two leaves), so the synthesized reverse lands on
# ``Sample`` and is inherited by ``Brain`` / ``CSF``. ``Run.inputs`` has
# the same slot name as ``Assay.inputs`` to prove ``relationship_type``
# scoping alone doesn't mix them (the source table closure does).
# ``Cohort.members`` is the derived-side declaration over ``Person.cohort``.
SCHEMA = HEADER + """\
  Sample:
    is_a: Entity
    abstract: true
    attributes:
      label:
  Brain:
    is_a: Sample
  CSF:
    is_a: Sample
  Assay:
    is_a: Entity
    attributes:
      kind:
      inputs:
        range: Sample
        multivalued: true
        inverse: assays
  Run:
    is_a: Entity
    attributes:
      inputs:
        range: Sample
        multivalued: true
  Cohort:
    is_a: Entity
    attributes:
      name:
      members:
        range: Person
        multivalued: true
        inverse: cohort
  Person:
    is_a: Entity
    attributes:
      name:
      cohort:
        range: Cohort
        multivalued: true
"""


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "inverse_mv.db"), schema_registry=registry
        )
        c = MosaicClient(storage=storage, bypass_validation=True)
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


def ids(client, entity_type, where) -> set[str]:
    return {i["id"] for i in client.query(entity_type, where=where).items}


class TestRegistryPairRule:
    def test_multivalued_forward_slot_is_stored(self, registry):
        assert registry.inverse_reference_slots("Assay") == []
        assert ("inputs", "Sample") in registry.multivalued_reference_slots("Assay")

    def test_reverse_is_synthesized_on_the_range(self, registry):
        (inv,) = registry.inverse_reference_slots("Sample")
        assert (inv.name, inv.target_class, inv.forward_slot) == (
            "assays", "Assay", "inputs",
        )
        assert inv.declared_on == "forward"
        assert inv.forward_multivalued is True
        (leaf,) = registry.inverse_reference_slots("Brain")
        assert leaf.name == "assays"

    def test_derived_side_declaration_over_multivalued_forward(self, registry):
        (inv,) = registry.inverse_reference_slots("Cohort")
        assert (inv.name, inv.forward_slot, inv.declared_on) == (
            "members", "cohort", "reverse",
        )
        assert inv.forward_multivalued is True
        assert ("cohort", "Cohort") in registry.multivalued_reference_slots("Person")

    def test_virtual_side_owns_no_column(self, registry):
        assert "assays" in registry.non_column_slot_names("Brain")
        assert "members" in registry.non_column_slot_names("Cohort")

    def test_type_model_marks_only_the_virtual_side(self, registry):
        model = build_type_model(registry)
        fields = {f.name: f for f in model["Assay"].fields}
        assert fields["inputs"].inverse_of is None
        fields = {f.name: f for f in model["Brain"].fields}
        assert fields["assays"].inverse_of == "inputs"
        fields = {f.name: f for f in model["Cohort"].fields}
        assert fields["members"].inverse_of == "cohort"
        fields = {f.name: f for f in model["Person"].fields}
        assert fields["cohort"].inverse_of is None

    def test_both_multivalued_mutual_declaration_is_rejected(self):
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(
                HEADER
                + """\
  Sample:
    is_a: Entity
    attributes:
      assays:
        range: Assay
        multivalued: true
        inverse: inputs
  Assay:
    is_a: Entity
    attributes:
      inputs:
        range: Sample
        multivalued: true
        inverse: assays
"""
            )
        assert exc.value.error_code == "INVERSE_SLOT"
        msg = str(exc.value)
        assert "declare `inverse` on one side only" in msg
        assert msg.count("on one side only") == 1

    def test_mutual_single_and_multivalued_stores_the_single_valued_side(self):
        registry = SchemaRegistry.from_yaml(
            HEADER
            + """\
  Donor:
    is_a: Entity
    attributes:
      samples:
        range: Sample
        multivalued: true
        inverse: donor
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
"""
        )
        (inv,) = registry.inverse_reference_slots("Donor")
        assert inv.forward_slot == "donor" and inv.forward_multivalued is False
        assert registry.inverse_reference_slots("Sample") == []

    def test_reverse_naming_another_reverse_is_rejected(self):
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(
                HEADER
                + """\
  Donor:
    is_a: Entity
    attributes:
      samples:
        range: Sample
        multivalued: true
        inverse: donor
      cohort_list:
        range: Sample
        multivalued: true
        inverse: samples
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
      samples:
        range: Donor
        multivalued: true
        inverse: tissues
  Other:
    is_a: Entity
"""
            )
        assert exc.value.error_code == "INVERSE_SLOT"


class TestForwardWritesPersist:
    def test_write_to_multivalued_forward_slot_persists_and_hydrates(self, client):
        assert client.get("Assay", "a1")["data"]["inputs"] == ["b1", "f1"]
        assert client.get("Assay", "a2")["data"]["inputs"] == ["b1"]
        assert client.get("Person", "p1")["data"]["cohort"] == ["c1", "c2"]

    def test_forward_count_into_concrete_range(self, client):
        # (The forward count into the abstract ``Sample`` range is #241's
        # closure bug, ADR-0014 — not exercised here.)
        assert client.count_relationship("Person", "p1", "cohort") == 2


class TestReverseHydration:
    def test_get_hydrates_reverse_over_relationship_rows(self, client):
        assert client.get("Brain", "b1")["data"]["assays"] == ["a1", "a2"]
        assert client.get("CSF", "f1")["data"]["assays"] == ["a1"]
        assert client.get("Brain", "b2")["data"]["assays"] == ["a3"]

    def test_same_slot_name_on_another_class_does_not_leak(self, client):
        # Run.inputs also has relationship_type "inputs"; f9 is only a Run input.
        assert "assays" not in client.get("CSF", "f9")["data"]

    def test_query_hydrates_in_batch(self, client):
        by_id = {i["id"]: i["data"] for i in client.query("Brain").items}
        assert by_id["b1"]["assays"] == ["a1", "a2"]
        assert by_id["b2"]["assays"] == ["a3"]

    def test_derived_side_hydrates(self, client):
        assert sorted(client.get("Cohort", "c1")["data"]["members"]) == ["p1", "p2"]
        assert client.get("Cohort", "c2")["data"]["members"] == ["p1"]
        assert "members" not in client.get("Cohort", "c3")["data"]

    def test_unavailable_source_is_excluded(self, client):
        client.delete("Assay", "a2")
        assert client.get("Brain", "b1")["data"]["assays"] == ["a1"]

    def test_removed_edge_is_excluded(self, client):
        client.update("Assay", "a1", {"inputs": ["b1"]})
        assert "assays" not in client.get("CSF", "f1")["data"]

    def test_write_carrying_the_virtual_slot_is_ignored(self, client):
        client.update("Brain", "b2", {"label": "renamed", "assays": ["a1"]})
        got = client.get("Brain", "b2")["data"]
        assert got["label"] == "renamed"
        assert got["assays"] == ["a3"]


class TestReversePredicatesMirrorForward:
    def test_some(self, client):
        where = {
            "edge": "assays", "quantifier": "some",
            "where": {"field": "kind", "op": "eq", "value": "rnaseq"},
        }
        assert ids(client, "Brain", where) == {"b1", "b2"}
        assert ids(client, "CSF", where) == {"f1"}

    def test_some_matches_forward_direction(self, client):
        # Brain b1 has an atac assay iff some atac Assay lists b1 as input.
        rev = ids(client, "Brain", {
            "edge": "assays", "quantifier": "some",
            "where": {"field": "kind", "op": "eq", "value": "atac"},
        })
        fwd = {
            s
            for a in client.query(
                "Assay", where={"field": "kind", "op": "eq", "value": "atac"}
            ).items
            for s in a["data"].get("inputs", [])
        }
        assert rev == fwd & {"b1", "b2"} == {"b1"}

    def test_none(self, client):
        where = {
            "edge": "assays", "quantifier": "none",
            "where": {"field": "kind", "op": "eq", "value": "atac"},
        }
        assert ids(client, "Brain", where) == {"b2"}
        assert ids(client, "CSF", where) == {"f1", "f9"}

    def test_derived_side_some_none(self, client):
        some = {
            "edge": "members", "quantifier": "some",
            "where": {"field": "name", "op": "eq", "value": "P2"},
        }
        assert ids(client, "Cohort", some) == {"c1"}
        assert ids(client, "Cohort", {**some, "quantifier": "none"}) == {"c2", "c3"}

    def test_unavailable_source_does_not_match(self, client):
        client.delete("Assay", "a3")
        where = {
            "edge": "assays", "quantifier": "some",
            "where": {"field": "kind", "op": "eq", "value": "rnaseq"},
        }
        assert ids(client, "Brain", where) == {"b1"}


class TestReverseCounts:
    def test_counts(self, client):
        assert client.count_relationship("Brain", "b1", "assays") == 2
        assert client.count_relationship("CSF", "f1", "assays") == 1
        assert client.count_relationship("CSF", "f9", "assays") == 0
        assert client.count_relationship("Cohort", "c1", "members") == 2
        assert client.count_relationship("Cohort", "c3", "members") == 0

    def test_counts_match_hydration(self, client):
        for item in client.query("Brain").items:
            assert client.count_relationship("Brain", item["id"], "assays") == len(
                item["data"].get("assays", [])
            )

    def test_count_excludes_unavailable_source(self, client):
        client.delete("Assay", "a1")
        assert client.count_relationship("Brain", "b1", "assays") == 1
