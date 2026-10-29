"""``inverse`` declared on the *forward* slot (ADR-0012 / issue #230).

ADR-0011 reads ``inverse`` on the derived, multivalued side. In an
import-layered schema that points a lower module at a higher one: brain
bank's ``person.yaml`` (``Donor``) imports only ``core`` and cannot name
``Sample`` from ``tissue.yaml``. ADR-0012 lets the forward slot carry the
declaration — ``Sample.donor: {range: Donor, inverse: samples}`` — and
Mosaic synthesizes ``Donor.samples`` onto the range class with exactly
ADR-0011's storage, query, manifest and transport semantics, flagged
``declared_on="forward"`` on ``InverseSlot``.

The fixture here is two LinkML modules where the range module does **not**
import the declaring one, loaded both through import resolution
(``from_path(tissue.yaml)``) and merged (``from_path(dir)``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import SchemaError
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.storage.ddl_generator import DDLGenerator
from mosaic.linkml_bridge import InverseSlot, SchemaRegistry

PERSON_YAML = """\
id: https://example.org/person
name: person
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Donor:
    is_a: Entity
    attributes:
      name:
        required: true
      cohort:
"""

TISSUE_YAML = """\
id: https://example.org/tissue
name: tissue
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - person
default_range: string
classes:
  Sample:
    is_a: Entity
    abstract: true
    attributes:
      label:
      volume:
        range: integer
      donor:
        range: Donor
        inverse: samples        # forward side: person.yaml never names Sample
  Brain:
    is_a: Sample
    attributes:
      hemisphere:
  CSF:
    is_a: Sample
"""


def _write_layered(root: Path) -> Path:
    (root / "person.yaml").write_text(PERSON_YAML)
    (root / "tissue.yaml").write_text(TISSUE_YAML)
    return root


@pytest.fixture(params=["imports", "merged"])
def registry(tmp_path: Path, request) -> SchemaRegistry:
    root = _write_layered(tmp_path)
    if request.param == "imports":
        return SchemaRegistry.from_path(root / "tissue.yaml")
    return SchemaRegistry.from_path(root)


@pytest.fixture
def client(registry: SchemaRegistry, tmp_path: Path):
    storage = SQLiteAdapter(os.path.join(tmp_path, "fwd.db"), schema_registry=registry)
    c = MosaicClient(storage=storage, registry=registry)
    c.put("Donor", {"id": "d1", "name": "D1", "cohort": "case"})
    c.put("Donor", {"id": "d2", "name": "D2", "cohort": "control"})
    c.put("Donor", {"id": "d3", "name": "D3", "cohort": "case"})  # no samples
    c.put("Brain", {"id": "b1", "label": "brain", "volume": 10, "donor": "d1"})
    c.put("CSF", {"id": "f1", "label": "csf", "volume": 2, "donor": "d1"})
    c.put("CSF", {"id": "f2", "label": "csf", "volume": 1, "donor": "d2"})
    c.put("Brain", {"id": "b9", "label": "orphan", "volume": 5})
    yield c


def _ids(page) -> list[str]:
    return sorted(e["id"] for e in page.items)


class TestRecognition:
    def test_forward_declaration_synthesizes_the_reverse(self, registry):
        assert registry.inverse_reference_slots("Donor") == [
            InverseSlot(
                name="samples",
                target_class="Sample",
                forward_slot="donor",
                target_classes=("Brain", "CSF"),
                declared_on="forward",
            )
        ]
        (inv,) = registry.inverse_reference_slots("Donor")
        assert inv.synthesized is True
        assert inv.tables == ("Brain", "CSF")

    def test_inherited_forward_declaration_synthesizes_once(self, registry):
        # Brain and CSF induce `donor` with the same `inverse`, but only the
        # abstract declaring class places the reverse; the subclasses are
        # reached through the concrete closure (#224).
        for cls in ("Sample", "Brain", "CSF"):
            assert registry.inverse_reference_slots(cls) == []
        names = [s.name for s in registry.induced_slots("Donor")]
        assert names.count("samples") == 1

    def test_synthesized_slot_owns_no_storage(self, registry):
        assert registry.multivalued_reference_slots("Donor") == []
        assert registry.non_column_slot_names("Donor") == {"samples"}
        assert ("donor", "Donor") in registry.reference_slots("Brain")

    def test_sqlite_ddl_emits_no_column_and_no_link_table(self, registry):
        statements = DDLGenerator().generate(registry)
        tables = [s for s in statements if "CREATE TABLE" in s]
        names = {s.split('"')[1] for s in tables}
        assert "Donor_samples" not in names
        donor = next(s for s in tables if '"Donor"' in s.split("(")[0])
        assert "samples" not in donor

    def test_rebuilding_a_registry_on_the_same_view_is_idempotent(self, registry):
        again = SchemaRegistry(registry.schema_view)
        (inv,) = again.inverse_reference_slots("Donor")
        assert inv.declared_on == "forward"
        assert [s.name for s in again.induced_slots("Donor")].count("samples") == 1

    def test_reverse_side_declaration_is_reported_as_declared_on_reverse(self):
        reg = SchemaRegistry.from_yaml(
            PERSON_YAML.replace(
                "      cohort:\n",
                "      cohort:\n      samples:\n        range: Sample\n        multivalued: true\n        inverse: donor\n",
            )
            + TISSUE_YAML.split("classes:\n", 1)[1].replace("        inverse: samples        # forward side: person.yaml never names Sample\n", "")
        )
        (inv,) = reg.inverse_reference_slots("Donor")
        assert inv.declared_on == "reverse"
        assert inv.synthesized is False

    def test_both_sides_declared_and_agreeing_keeps_the_reverse_declaration(self):
        text = PERSON_YAML.replace(
            "      cohort:\n",
            "      cohort:\n      samples:\n        range: Sample\n        multivalued: true\n        inverse: donor\n",
        ) + TISSUE_YAML.split("classes:\n", 1)[1]
        reg = SchemaRegistry.from_yaml(text)
        (inv,) = reg.inverse_reference_slots("Donor")
        assert inv.declared_on == "reverse"
        assert [s.name for s in reg.induced_slots("Donor")].count("samples") == 1


class TestTypeModelAndManifest:
    def test_type_model_marks_the_synthesized_slot_inverse_of(self, registry):
        from mosaic.core.schema_typing import SlotKind, build_type_model

        model = build_type_model(registry)
        samples = next(f for f in model["Donor"].fields if f.name == "samples")
        assert samples.kind is SlotKind.REFERENCE
        assert samples.multivalued is True
        assert samples.target_class == "Sample"
        assert samples.inverse_of == "donor"
        assert "ADR-0012" in (samples.description or "")
        donor = next(f for f in model["Brain"].fields if f.name == "donor")
        assert donor.inverse_of is None
        assert donor.multivalued is False

    def test_manifest_and_mcp_serialization(self, registry):
        from mosaic.core.schema_typing import build_capability_manifest
        from mosaic.mcp.serialize import entity_capability_to_dict

        manifest = build_capability_manifest(registry)
        fields = {f["name"]: f for f in entity_capability_to_dict(manifest["Donor"])["fields"]}
        assert fields["samples"]["inverse_of"] == "donor"
        assert fields["samples"]["multivalued"] is True
        assert fields["samples"]["filter_ops"] == []

    def test_openapi_renders_the_synthesized_slot_read_only(self, registry):
        from mosaic.api.openapi import _slot_schema
        from mosaic.core.schema_typing import build_type_model

        model = build_type_model(registry)
        samples = next(f for f in model["Donor"].fields if f.name == "samples")
        rendered = _slot_schema(samples)
        assert rendered["readOnly"] is True
        assert rendered["type"] == "array"


class TestStorage:
    def test_get_hydrates_across_the_concrete_closure(self, client):
        assert sorted(client.get("Donor", "d1")["data"]["samples"]) == ["b1", "f1"]
        assert client.get("Donor", "d2")["data"]["samples"] == ["f2"]
        assert not client.get("Donor", "d3")["data"].get("samples")

    def test_query_hydrates_in_batch(self, client):
        by_id = {i["id"]: i["data"] for i in client.query("Donor").items}
        assert sorted(by_id["d1"]["samples"]) == ["b1", "f1"]
        assert by_id["d2"]["samples"] == ["f2"]

    def test_unavailable_targets_are_excluded(self, client):
        client.delete("CSF", "f1")
        assert client.get("Donor", "d1")["data"]["samples"] == ["b1"]

    def test_count_relationship(self, client):
        assert client.count_relationship("Donor", "d1", "samples") == 2
        assert client.count_relationship("Donor", "d2", "samples") == 1
        assert client.count_relationship("Donor", "d3", "samples") == 0

    def test_some_quantifier(self, client):
        where = {"edge": "samples", "quantifier": "some",
                 "where": {"field": "label", "op": "eq", "value": "csf"}}
        assert _ids(client.query("Donor", where=where)) == ["d1", "d2"]
        where = {"edge": "samples", "quantifier": "some",
                 "where": {"field": "volume", "op": "gte", "value": 10}}
        assert _ids(client.query("Donor", where=where)) == ["d1"]

    def test_none_quantifier_includes_sampleless_donors(self, client):
        where = {"edge": "samples", "quantifier": "none",
                 "where": {"field": "label", "op": "eq", "value": "brain"}}
        assert _ids(client.query("Donor", where=where)) == ["d2", "d3"]

    def test_write_carrying_the_synthesized_slot_is_ignored(self, client):
        client.put("Donor", {"id": "d4", "name": "D4", "samples": ["b9", "b1"]})
        assert client.relationships.find_relationships(source_id="d4") == []
        assert not client.get("Donor", "d4")["data"].get("samples")
        # Membership still follows the forward FK only.
        assert client.get("Brain", "b9")["data"].get("donor") is None

    def test_get_then_put_round_trip_is_stable(self, client):
        before = client.get("Donor", "d1")
        client.put("Donor", before["data"])
        after = client.get("Donor", "d1")
        assert sorted(after["data"]["samples"]) == ["b1", "f1"]
        assert client.relationships.find_relationships(source_id="d1") == []

    def test_forward_update_moves_reverse_membership(self, client):
        client.update("Brain", "b9", {"label": "orphan", "volume": 5, "donor": "d3"})
        assert client.get("Donor", "d3")["data"]["samples"] == ["b9"]


_HEADER = """\
id: https://example.org/fwd_errors
name: fwd_errors
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
"""


def _rejects(text: str, *needles: str) -> None:
    with pytest.raises(SchemaError) as exc:
        SchemaRegistry.from_yaml(_HEADER + text)
    assert exc.value.error_code == "INVERSE_SLOT"
    for n in needles:
        assert n in str(exc.value), str(exc.value)


class TestErrors:
    def test_both_sides_declared_must_name_each_other(self):
        _rejects(
            """\
  Donor:
    is_a: Entity
    attributes:
      name:
      specimens:
        range: Sample
        multivalued: true
        inverse: donor
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
""",
            "Sample.donor declares `inverse: samples`",
            "Donor.specimens declares `inverse: donor`",
            "must name each other",
            "ADR-0012",
        )

    def test_same_name_but_pointing_elsewhere_is_a_disagreement(self):
        _rejects(
            """\
  Donor:
    is_a: Entity
    attributes:
      name:
      samples:
        range: Sample
        multivalued: true
        inverse: collector
  Sample:
    is_a: Entity
    attributes:
      collector:
        range: Donor
      donor:
        range: Donor
        inverse: samples
""",
            "Sample.donor declares `inverse: samples`",
            "Donor.samples declares `inverse: collector`",
            "must name each other",
        )

    def test_name_collision_with_a_real_slot(self):
        _rejects(
            """\
  Donor:
    is_a: Entity
    attributes:
      name:
      samples:
        range: string
        multivalued: true
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
""",
            "Sample.donor declares `inverse: samples`",
            "already has a slot named 'samples'",
            "collide",
        )

    def test_collision_with_a_single_valued_real_slot(self):
        _rejects(
            """\
  Donor:
    is_a: Entity
    attributes:
      name:
      samples:
        range: integer
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
""",
            "already has a slot named 'samples' (range 'integer')",
        )

    def test_forward_slot_multivalued_keeps_the_adr_0011_rejection(self):
        # ADR-0011's existing shape and message: the reverse side names a
        # relationships-backed multivalued forward slot (ADR-0013 territory).
        _rejects(
            """\
  Workflow:
    is_a: Entity
    attributes:
      input_samples:
        range: Sample
        multivalued: true
  Sample:
    is_a: Entity
    attributes:
      workflows:
        range: Workflow
        multivalued: true
        inverse: input_samples
""",
            "Sample.workflows", "is multivalued", "single-valued",
        )

    def test_multivalued_slot_carrying_inverse_is_still_read_as_the_derived_side(self):
        # A multivalued slot with `inverse` is ADR-0011's derived side, not a
        # forward declaration: nothing is synthesized and the existing
        # validation message stands (the reverse of a multivalued forward
        # slot is ADR-0013).
        _rejects(
            """\
  Workflow:
    is_a: Entity
    attributes:
      input_samples:
        range: Sample
        multivalued: true
        inverse: workflows
  Sample:
    is_a: Entity
    attributes:
      name:
""",
            "Workflow.input_samples declares `inverse: workflows`",
            "Sample has no slot named 'workflows'",
        )

    def test_forward_range_must_be_an_entity_class(self):
        _rejects(
            """\
  Sample:
    is_a: Entity
    attributes:
      label:
        range: string
        inverse: samples
""",
            "Sample.label declares `inverse: samples`", "not an entity class", "ADR-0012",
        )

    def test_inlined_forward_slot_names_the_forward_declaration(self):
        _rejects(
            """\
  Donor:
    is_a: Entity
    attributes:
      name:
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inlined: true
        inverse: samples
""",
            "Donor.samples (synthesized from Sample.donor's `inverse`, ADR-0012)",
            "is inlined",
        )

    def test_two_unrelated_classes_cannot_share_one_synthesized_reverse(self):
        _rejects(
            """\
  Donor:
    is_a: Entity
    attributes:
      name:
  Brain:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
  CSF:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
""",
            "CSF.donor declares `inverse: samples`",
            "already synthesized from Brain.donor",
            "shared ancestor",
        )

    def test_failures_aggregate(self):
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(
                _HEADER
                + """\
  Donor:
    is_a: Entity
    attributes:
      name:
      samples:
        range: integer
  Sample:
    is_a: Entity
    attributes:
      donor:
        range: Donor
        inverse: samples
      label:
        range: string
        inverse: labelled
"""
            )
        assert "2 inverse-slot error(s) in schema (ADR-0012)" in str(exc.value)


class TestSlotUsage:
    SCHEMA = _HEADER + """\
  Person:
    is_a: Entity
    attributes:
      name:
  Observation:
    is_a: Entity
    abstract: true
    attributes:
      person:
        range: Person
      note:
  Diagnosis:
    is_a: Observation
    slot_usage:
      person:
        inverse: diagnoses
    attributes:
      code:
  Measurement:
    is_a: Observation
    slot_usage:
      person:
        inverse: measurements
    attributes:
      value:
        range: float
  Remark:
    is_a: Observation
"""

    def test_inverse_set_through_slot_usage_ranges_the_reverse_on_the_subclass(self):
        reg = SchemaRegistry.from_yaml(self.SCHEMA)
        inv = {i.name: i for i in reg.inverse_reference_slots("Person")}
        assert set(inv) == {"diagnoses", "measurements"}
        assert inv["diagnoses"].target_class == "Diagnosis"
        assert inv["diagnoses"].forward_slot == "person"
        assert inv["diagnoses"].tables == ("Diagnosis",)
        assert inv["diagnoses"].declared_on == "forward"
        assert inv["measurements"].tables == ("Measurement",)
        # The un-narrowed sibling and the base introduce no reverse.
        assert reg.inverse_reference_slots("Remark") == []

    def test_slot_usage_reverse_resolves_in_storage(self, tmp_path):
        reg = SchemaRegistry.from_yaml(self.SCHEMA)
        storage = SQLiteAdapter(os.path.join(tmp_path, "usage.db"), schema_registry=reg)
        c = MosaicClient(storage=storage, registry=reg)
        c.put("Person", {"id": "p1", "name": "P1"})
        c.put("Person", {"id": "p2", "name": "P2"})
        c.put("Diagnosis", {"id": "dx1", "person": "p1", "code": "F43.1"})
        c.put("Measurement", {"id": "m1", "person": "p1", "value": 1.5})
        c.put("Remark", {"id": "r1", "person": "p1", "note": "n"})
        data = c.get("Person", "p1")["data"]
        assert data["diagnoses"] == ["dx1"]
        assert data["measurements"] == ["m1"]
        assert c.count_relationship("Person", "p1", "diagnoses") == 1
        where = {"edge": "diagnoses", "quantifier": "some",
                 "where": {"field": "code", "op": "eq", "value": "F43.1"}}
        assert _ids(c.query("Person", where=where)) == ["p1"]
        where["quantifier"] = "none"
        assert _ids(c.query("Person", where=where)) == ["p2"]

    def test_base_declaration_plus_subclass_override_yields_both_reverses(self):
        text = self.SCHEMA.replace(
            "      person:\n        range: Person\n      note:",
            "      person:\n        range: Person\n        inverse: observations\n      note:",
        )
        reg = SchemaRegistry.from_yaml(text)
        inv = {i.name: i for i in reg.inverse_reference_slots("Person")}
        assert set(inv) == {"observations", "diagnoses", "measurements"}
        assert inv["observations"].target_class == "Observation"
        assert inv["observations"].tables == ("Diagnosis", "Measurement", "Remark")
