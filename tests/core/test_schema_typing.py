"""Tests for the shared schema-typing core (issue #47)."""

from __future__ import annotations

from pathlib import Path

from mosaic.core.schema_typing import (
    INFRASTRUCTURE_CLASSES,
    SYSTEM_FIELDS,
    EntityTypeModel,
    FieldRole,
    FilterOp,
    SlotKind,
    build_capability_manifest,
    build_type_model,
    exposed_class_names,
)
from mosaic.linkml_bridge import SchemaRegistry

_FIXTURE_SCHEMA = (
    Path(__file__).parents[1] / "fixtures" / "schemas" / "sample_schema.yaml"
)


def _registry() -> SchemaRegistry:
    return SchemaRegistry.from_path(_FIXTURE_SCHEMA)


def test_exposed_classes_excludes_infrastructure_and_abstract():
    names = exposed_class_names(_registry())
    # Behaviour-preserving: matches the typed-client / GraphQL selection.
    # ExternalID is a concrete hippo_core class NOT in the 5-class
    # infrastructure set, so it is exposed today (see issue #47 follow-up on
    # whether it should be treated as infrastructure — the TUI excludes it).
    assert names == ["ExternalID", "Project", "Sample"]
    assert not (set(names) & INFRASTRUCTURE_CLASSES)


def test_build_type_model_keys():
    model = build_type_model(_registry())
    assert set(model) == {"ExternalID", "Project", "Sample"}
    assert all(isinstance(v, EntityTypeModel) for v in model.values())


def test_scalar_slot_classification():
    sample = build_type_model(_registry())["Sample"]
    by_name = {f.name: f for f in sample.fields}
    assert by_name["volume_ml"].kind is SlotKind.SCALAR
    assert by_name["volume_ml"].range == "float"
    assert by_name["name"].required is True
    assert by_name["name"].role is FieldRole.USER


def test_enum_slot_classification():
    sample = build_type_model(_registry())["Sample"]
    status = {f.name: f for f in sample.fields}["status"]
    assert status.kind is SlotKind.ENUM
    assert status.enum_name == "SampleStatus"
    assert status.enum_values == ("active", "archived", "distributed")


def test_reference_slot_classification():
    sample = build_type_model(_registry())["Sample"]
    project_id = {f.name: f for f in sample.fields}["project_id"]
    assert project_id.kind is SlotKind.REFERENCE
    assert project_id.target_class == "Project"
    # And it surfaces via the relationships convenience.
    assert "project_id" in {r.name for r in sample.relationships}


def test_system_fields_partitioned_from_user_fields():
    sample = build_type_model(_registry())["Sample"]
    system = {f.name for f in sample.system_fields}
    assert "id" in system and "is_available" in system
    assert system <= SYSTEM_FIELDS
    # User fields exclude system fields.
    assert "id" not in {f.name for f in sample.user_fields}
    assert "name" in {f.name for f in sample.user_fields}


def test_accessor_name_present():
    project = build_type_model(_registry())["Project"]
    assert project.accessor_name  # canonical plural accessor, non-empty


def test_has_default_reflects_ifabsent():
    registry = SchemaRegistry.from_yaml(
        """
id: https://example.org/hippo/test_ifabsent
name: test_ifabsent
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Widget:
    is_a: Entity
    attributes:
      name:
        required: true
      kind:
        required: true
        ifabsent: string(generic)
"""
    )
    widget = build_type_model(registry)["Widget"]
    by_name = {f.name: f for f in widget.fields}
    assert by_name["kind"].has_default is True
    assert by_name["name"].has_default is False


# -- capability manifest (issue #181) ----------------------------------------


def test_reference_field_has_no_direct_filter_ops_but_is_predicate_filterable():
    manifest = build_capability_manifest(_registry())
    project_id = manifest["Sample"].fields_by_name["project_id"]
    # Resolves the schema_builder/resolvers FilterOp divergence in favor of
    # the `where:` contract (ADR-0006 M5a/M5b): no direct FilterOp on a
    # reference field, but predicate-filterable via the nested edge, since
    # its target (Project) is exposed.
    assert project_id.filter_ops == ()
    assert project_id.predicate is True
    assert project_id.orderable is False
    assert project_id.aggregatable is False
    assert project_id.range_queryable is False


def test_reference_to_unexposed_class_is_not_predicate_filterable():
    registry = SchemaRegistry.from_yaml(
        """
id: https://example.org/hippo/test_dangling_ref
name: test_dangling_ref
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Widget:
    is_a: Entity
    abstract: true
    attributes:
      name:
        required: true
  Gadget:
    is_a: Entity
    attributes:
      name:
        required: true
      widget_id:
        range: Widget
"""
    )
    manifest = build_capability_manifest(registry)
    widget_id = manifest["Gadget"].fields_by_name["widget_id"]
    assert widget_id.filter_ops == ()
    assert widget_id.predicate is False


def test_ordered_scalar_field_gets_full_comparison_ops_and_is_range_queryable():
    manifest = build_capability_manifest(_registry())
    volume = manifest["Sample"].fields_by_name["volume_ml"]
    assert set(volume.filter_ops) == {
        FilterOp.EQ,
        FilterOp.NEQ,
        FilterOp.IN,
        FilterOp.GT,
        FilterOp.GTE,
        FilterOp.LT,
        FilterOp.LTE,
        FilterOp.IS_NULL,
    }
    assert volume.orderable is True
    assert volume.aggregatable is True
    assert volume.range_queryable is True


def test_string_field_gets_contains_but_is_not_range_queryable():
    manifest = build_capability_manifest(_registry())
    name = manifest["Sample"].fields_by_name["name"]
    assert set(name.filter_ops) == {
        FilterOp.EQ,
        FilterOp.NEQ,
        FilterOp.IN,
        FilterOp.CONTAINS,
        FilterOp.IS_NULL,
    }
    assert name.orderable is True
    assert name.aggregatable is True
    assert name.range_queryable is False


def test_enum_field_is_aggregatable_but_not_range_queryable():
    manifest = build_capability_manifest(_registry())
    status = manifest["Sample"].fields_by_name["status"]
    assert set(status.filter_ops) == {
        FilterOp.EQ,
        FilterOp.NEQ,
        FilterOp.IN,
        FilterOp.IS_NULL,
    }
    assert status.orderable is True
    assert status.aggregatable is True
    assert status.range_queryable is False


def test_search_available_reflects_hippo_search_annotation_presence():
    manifest = build_capability_manifest(_registry())
    # Project.name/description carry `hippo_search: fts5` in the fixture.
    assert manifest["Project"].search_available is True
    assert manifest["Project"].fields_by_name["name"].searchable is True


def test_search_available_false_with_no_searchable_slots():
    registry = SchemaRegistry.from_yaml(
        """
id: https://example.org/hippo/test_no_search
name: test_no_search
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Widget:
    is_a: Entity
    attributes:
      name:
        required: true
"""
    )
    manifest = build_capability_manifest(registry)
    assert manifest["Widget"].search_available is False
    assert manifest["Widget"].fields_by_name["name"].searchable is False


_OWN_PROCESS_SCHEMA = """
id: https://example.org/own_process
name: own_process
prefixes:
  linkml: https://w3id.org/linkml/
  own: https://example.org/own_process/
default_prefix: own
default_range: string
imports:
  - linkml:types
classes:
  Process:
    attributes:
      id:
        identifier: true
  Sample:
    attributes:
      id:
        identifier: true
      produced_by:
        range: Process
"""


def test_domain_class_named_like_infrastructure_is_exposed():
    # A schema that defines its own ``Process`` without importing hippo_core:
    # the name collides with a framework class but the identity does not.
    registry = SchemaRegistry.from_yaml(_OWN_PROCESS_SCHEMA)
    assert exposed_class_names(registry) == ["Process", "Sample"]
    sample = build_type_model(registry)["Sample"]
    produced_by = {f.name: f for f in sample.fields}["produced_by"]
    assert produced_by.kind is SlotKind.REFERENCE
    assert produced_by.target_class == "Process"
