"""Schema-layer contract for ``inverse``-declared slots (ADR-0011 / issue #204).

An ``inverse`` slot is a *virtual* reverse edge over the forward FK column:
the registry recognizes it, validates it at construction, and keeps it out
of every "this slot owns storage" set (relationship materialization, DDL
columns, schema diff).
"""

from __future__ import annotations

import pytest

from mosaic.core.exceptions import SchemaError
from mosaic.core.storage.ddl_generator import DDLGenerator
from mosaic.linkml_bridge import InverseSlot, SchemaRegistry

_HEADER = """\
id: https://example.org/inverse
name: inverse
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
"""

VALID = _HEADER + """\
  Donor:
    is_a: Entity
    attributes:
      name:
        required: true
      samples:
        range: Sample
        multivalued: true
        inverse: donor
  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      donor:
        range: Donor
        required: true
"""


def _registry(text: str) -> SchemaRegistry:
    return SchemaRegistry.from_yaml(text)


class TestRecognition:
    def test_inverse_reference_slots_resolves_forward_slot(self):
        reg = _registry(VALID)
        assert reg.inverse_reference_slots("Donor") == [
            InverseSlot(
                name="samples",
                target_class="Sample",
                forward_slot="donor",
                target_classes=("Sample",),
            )
        ]
        assert reg.inverse_reference_slots("Sample") == []

    def test_inverse_slot_is_not_a_relationships_backed_multivalued_ref(self):
        reg = _registry(VALID)
        assert reg.multivalued_reference_slots("Donor") == []

    def test_inverse_slot_owns_no_column(self):
        reg = _registry(VALID)
        assert reg.non_column_slot_names("Donor") == {"samples"}
        assert reg.non_column_slot_names("Sample") == set()

    def test_forward_slot_still_reported_as_plain_reference(self):
        reg = _registry(VALID)
        assert ("donor", "Donor") in reg.reference_slots("Sample")


class TestDDL:
    def test_sqlite_ddl_emits_no_column_and_no_link_table(self):
        statements = DDLGenerator().generate(_registry(VALID))
        tables = [s for s in statements if "CREATE TABLE" in s]
        names = {s.split('"')[1] for s in tables}
        assert "Donor_samples" not in names
        donor = next(s for s in tables if '"Donor"' in s.split("(")[0])
        assert "samples" not in donor



class TestValidation:
    def _rejects(self, text: str, *needles: str) -> None:
        with pytest.raises(SchemaError) as exc:
            _registry(text)
        assert exc.value.error_code == "INVERSE_SLOT"
        for n in needles:
            assert n in str(exc.value)

    def test_forward_slot_must_exist(self):
        self._rejects(
            VALID.replace("inverse: donor", "inverse: patient"),
            "Donor.samples", "no slot named 'patient'",
        )

    def test_forward_slot_must_be_single_valued(self):
        text = _HEADER + """\
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
"""
        self._rejects(text, "Sample.workflows", "is multivalued", "single-valued")

    def test_forward_slot_must_point_back_at_declaring_class(self):
        text = _HEADER + """\
  Donor:
    is_a: Entity
    attributes:
      name:
      samples:
        range: Sample
        multivalued: true
        inverse: study
  Study:
    is_a: Entity
    attributes:
      name:
  Sample:
    is_a: Entity
    attributes:
      study:
        range: Study
"""
        self._rejects(text, "Donor.samples", "ranged on 'Study'")

    def test_inverse_slot_cannot_be_required(self):
        self._rejects(
            VALID.replace("        inverse: donor", "        inverse: donor\n        required: true"),
            "cannot be required",
        )

    def test_forward_slot_must_not_be_inlined(self):
        text = _HEADER + """\
  Donor:
    is_a: Entity
    attributes:
      name:
      samples:
        range: Sample
        multivalued: true
        inverse: donor
  Sample:
    is_a: Entity
    attributes:
      name:
      donor:
        range: Donor
        inlined: true
"""
        self._rejects(text, "Donor.samples", "is inlined")

    def test_inverse_on_non_class_range_rejected(self):
        text = _HEADER + """\
  Donor:
    is_a: Entity
    attributes:
      tags:
        range: string
        multivalued: true
        inverse: donor
"""
        self._rejects(text, "Donor.tags", "not an entity class")

    def test_single_valued_side_carrying_inverse_is_left_alone(self):
        # LinkML permits the symmetric declaration; the stored side is not
        # a virtual slot and is not validated as one.
        text = VALID.replace(
            "      donor:\n        range: Donor\n        required: true",
            "      donor:\n        range: Donor\n        required: true\n        inverse: samples",
        )
        reg = _registry(text)
        assert reg.inverse_reference_slots("Sample") == []
        assert reg.inverse_reference_slots("Donor")[0].forward_slot == "donor"

    def test_forward_range_may_be_an_ancestor_of_the_declaring_class(self):
        text = _HEADER + """\
  Person:
    is_a: Entity
    attributes:
      name:
  Donor:
    is_a: Person
    attributes:
      samples:
        range: Sample
        multivalued: true
        inverse: subject
  Sample:
    is_a: Entity
    attributes:
      subject:
        range: Person
"""
        reg = _registry(text)
        assert reg.inverse_reference_slots("Donor")[0].forward_slot == "subject"

    def test_multiple_failures_aggregate(self):
        text = VALID.replace("inverse: donor", "inverse: nope").replace(
            "      donor:\n        range: Donor\n        required: true",
            "      donor:\n        range: Donor\n        required: true\n      studies:\n        range: string\n        multivalued: true\n        inverse: x",
        )
        with pytest.raises(SchemaError) as exc:
            _registry(text)
        assert "2 inverse-slot error(s)" in str(exc.value)


class TestTypeModel:
    def test_inverse_slot_is_a_reference_marked_inverse_of(self):
        from mosaic.core.schema_typing import SlotKind, build_type_model

        model = build_type_model(_registry(VALID))
        samples = next(f for f in model["Donor"].fields if f.name == "samples")
        assert samples.kind is SlotKind.REFERENCE
        assert samples.multivalued is True
        assert samples.target_class == "Sample"
        assert samples.inverse_of == "donor"
        # The stored (forward) side is an ordinary reference.
        donor = next(f for f in model["Sample"].fields if f.name == "donor")
        assert donor.inverse_of is None

    def test_inverse_slot_is_predicate_filterable_in_the_manifest(self):
        from mosaic.core.schema_typing import build_capability_manifest

        manifest = build_capability_manifest(_registry(VALID))
        field = manifest["Donor"].fields_by_name["samples"]
        assert field.predicate is True
        assert field.filter_ops == ()
        assert field.orderable is False

    def test_mcp_serialization_carries_inverse_of(self):
        from mosaic.core.schema_typing import build_capability_manifest
        from mosaic.mcp.serialize import entity_capability_to_dict

        manifest = build_capability_manifest(_registry(VALID))
        fields = {f["name"]: f for f in entity_capability_to_dict(manifest["Donor"])["fields"]}
        assert fields["samples"]["inverse_of"] == "donor"
        assert fields["samples"]["predicate"] is True
        assert fields["name"]["inverse_of"] is None

    def test_openapi_renders_inverse_slot_read_only(self):
        from mosaic.api.openapi import _slot_schema
        from mosaic.core.schema_typing import build_type_model

        model = build_type_model(_registry(VALID))
        samples = next(f for f in model["Donor"].fields if f.name == "samples")
        rendered = _slot_schema(samples)
        assert rendered["readOnly"] is True
        assert rendered["type"] == "array"
        assert "donor" in rendered["description"]
