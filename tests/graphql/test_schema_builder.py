"""Schema-generation tests: LinkML in, generated GraphQL SDL/types out.

No storage involved — these assert the shape of the strawberry schema
rendered from the shared type model (``mosaic.core.schema_typing``) for
the fixture LinkML schema in ``conftest``.
"""

from __future__ import annotations

import re

import pytest

from mosaic.core.schema_typing import (
    build_capability_manifest,
    build_type_model,
    exposed_class_names,
)
from mosaic.graphql import build_graphql_schema
from mosaic.graphql.schema_builder import (
    INFRASTRUCTURE_CLASSES,
    GraphQLTypeBuilder,
    camel_case,
    snake_case,
)
from mosaic.linkml_bridge import SchemaRegistry


@pytest.fixture(scope="module")
def sdl(registry: SchemaRegistry) -> str:
    return str(build_graphql_schema(registry))


def _block(sdl: str, header: str) -> str:
    """Extract one ``type X { ... }`` block from the SDL."""
    match = re.search(rf"^{re.escape(header)} \{{\n(.*?)^\}}", sdl, re.M | re.S)
    assert match is not None, f"{header} not found in SDL"
    return match.group(1)


class TestObjectTypeGeneration:
    def test_concrete_entity_classes_get_object_types(self, sdl):
        for name in ("Donor", "Sample", "Study"):
            assert f"type {name} " in sdl or f"type {name} {{" in sdl

    def test_infrastructure_classes_are_excluded(self, sdl):
        for name in INFRASTRUCTURE_CLASSES:
            assert f"type {name} {{" not in sdl

    def test_external_id_is_exposed_like_typed_client(self, sdl):
        # ExternalID is a concrete hippo_core class exposed by the
        # schema-typing core (issues #47/#48) — same class set as the
        # typed client; GraphQL mirrors that.
        assert "type ExternalID" in sdl

    def test_scalar_range_mappings(self, sdl):
        sample = _block(sdl, "type Sample")
        assert "name: String!" in sample  # required slot
        assert "volumeMl: Float" in sample
        assert "replicateCount: Int" in sample
        assert "isTumor: Boolean" in sample
        assert "collectedAt: DateTime" in sample

    def test_system_and_temporal_fields(self, sdl):
        sample = _block(sdl, "type Sample")
        assert "id: ID!" in sample
        assert "isAvailable: Boolean!" in sample
        for computed in (
            "version: Int",
            "createdAt: DateTime",
            "updatedAt: DateTime",
            "schemaVersion: String",
            "createdBy: String",
            "updatedBy: String",
            "supersededBy: ID",
        ):
            assert computed in sample, computed

    def test_enum_generation(self, sdl):
        enum_block = _block(sdl, "enum SexEnum")
        for member in ("male", "female", "unknown"):
            assert member in enum_block
        donor = _block(sdl, "type Donor")
        assert "sex: SexEnum" in donor


class TestRelationshipFields:
    def test_reference_emits_edge_only(self, sdl):
        # Edge-only (ADR-0005): the resolved relationship is the ONLY field;
        # the raw *_id scalar is a hidden Private carrier, absent from the SDL.
        sample = _block(sdl, "type Sample")
        assert "donor: Donor" in sample  # resolved graph edge
        assert "donorId" not in sample  # raw FK id is not exposed

    def test_self_reference_emits_edge_only(self, sdl):
        sample = _block(sdl, "type Sample")
        # Slot named `parent` (no _id suffix): the resolved field keeps the
        # natural name; the raw parentId field is not exposed.
        assert "parent: Sample" in sample
        assert "parentId" not in sample

    def test_multivalued_reference_emits_edge_only(self, sdl):
        study = _block(sdl, "type Study")
        assert "samples: [Sample!]!" in study
        assert "sampleIds" not in study


class TestPageTypes:
    def test_page_type_mirrors_paginated_result(self, sdl):
        page = _block(sdl, "type SamplePage")
        assert "items: [Sample!]!" in page
        assert "total: Int!" in page
        assert "limit: Int!" in page
        assert "offset: Int!" in page


class TestInputTypes:
    def test_create_input_keeps_required_slots_required(self, sdl):
        block = _block(sdl, "input SampleCreateInput")
        assert "name: String!" in block

    def test_update_input_is_fully_optional(self, sdl):
        block = _block(sdl, "input SampleUpdateInput")
        assert "name: String!" not in block
        assert "name: String" in block

    def test_id_is_never_required_on_inputs(self, sdl):
        block = _block(sdl, "input SampleCreateInput")
        assert "id: String!" not in block

    def test_reference_inputs_use_slot_names(self, sdl):
        # Inputs key on the LinkML slot name (donorId / parent), taking
        # the target UUID — the write payload maps 1:1 onto slot names.
        block = _block(sdl, "input SampleCreateInput")
        assert "donorId: ID" in block
        assert "parent: ID" in block


class TestRootTypes:
    def test_query_root_per_entity(self, sdl):
        query = _block(sdl, "type Query")
        assert "sample(id: ID!): Sample" in query
        assert "samples(" in query and "SamplePage!" in query
        assert "filterMode: FilterMode! = AND" in query
        assert "limit: Int! = 100" in query
        assert "offset: Int! = 0" in query
        assert "entityHistory(entityId: ID!): [ProvenanceEntry!]!" in query

    def test_search_query_per_entity(self, sdl):
        query = _block(sdl, "type Query")
        assert "searchSamples(q: String!" in query
        assert "searchDonors(q: String!" in query
        # Search twins return the Page envelope and take the list
        # surface's filter arguments (issue #157) — no more bare lists.
        assert "searchSamples(q: String!, filters: [FilterInput!]" in query
        assert "): SamplePage!" in query

    def test_supersession_query(self, sdl):
        query = _block(sdl, "type Query")
        assert "supersededBy(id: ID!): SupersessionInfo!" in query
        info = _block(sdl, "type SupersessionInfo")
        assert "entityId: ID!" in info
        assert "supersededBy: ID" in info
        assert "chain: [ID!]!" in info

    def test_hippo_schema_introspection_queries(self, sdl):
        query = _block(sdl, "type Query")
        assert "hippoSchema: [MosaicEntityTypeInfo!]!" in query
        assert "hippoEntityType(name: String!): MosaicEntityTypeInfo" in query
        info = _block(sdl, "type MosaicEntityTypeInfo")
        assert "fields: [MosaicSlotInfo!]!" in info
        assert "relationships: [MosaicReferenceInfo!]!" in info

    def test_mosaic_slot_info_carries_every_slot_model_attribute(self, sdl):
        # Regression for issue #211: hasDefault and isExternalXref were
        # declared on SlotModel but never surfaced on this GraphQL type.
        slot_info = _block(sdl, "type MosaicSlotInfo")
        assert "hasDefault: Boolean!" in slot_info
        assert "isExternalXref: Boolean!" in slot_info

    def test_mutation_root_per_entity(self, sdl):
        mutation = _block(sdl, "type Mutation")
        assert "createSample(data: SampleCreateInput!): Sample!" in mutation
        assert (
            "updateSample(id: ID!, data: SampleUpdateInput!): Sample!" in mutation
        )
        assert "setSampleAvailability(" in mutation
        assert "supersedeSample(" in mutation

    def test_bulk_availability_mutation_per_entity(self, sdl):
        mutation = _block(sdl, "type Mutation")
        assert (
            "setSampleAvailabilityBulk(ids: [ID!]!, isAvailable: Boolean!"
            in mutation
        )
        result = _block(sdl, "type BulkAvailabilityResult")
        assert "total: Int!" in result
        assert "succeeded: Int!" in result
        assert "failed: Int!" in result
        assert "failures: [BulkAvailabilityFailure!]!" in result

    def test_no_delete_mutations(self, sdl):
        # No hard deletes — availability transitions only (sec3).
        mutation = _block(sdl, "type Mutation")
        assert "deleteSample" not in mutation
        assert "deleteDonor" not in mutation


class TestTypingCoreAlignment:
    """The GraphQL surface renders the shared type model verbatim."""

    def test_entity_set_is_the_typing_core_class_set(self, registry):
        builder = GraphQLTypeBuilder(registry).build()
        assert sorted(builder.entities) == exposed_class_names(registry)

    def test_plural_names_come_from_the_shared_accessor(self, registry):
        builder = GraphQLTypeBuilder(registry).build()
        model = build_type_model(registry)
        for class_name, entity in builder.entities.items():
            assert entity.plural_name == model[class_name].accessor_name

    def test_slot_specs_mirror_the_model_slots(self, registry):
        builder = GraphQLTypeBuilder(registry).build()
        model = build_type_model(registry)
        for class_name, entity in builder.entities.items():
            assert [s.slot_name for s in entity.slots] == [
                f.name for f in model[class_name].fields
            ]
            assert [s.kind for s in entity.slots] == [
                f.kind.value for f in model[class_name].fields
            ]

    def test_capability_manifest_agrees_with_the_generated_where_contract(
        self, registry
    ):
        """The manifest (issue #181) must not silently drift from what
        ``where:`` (ADR-0006 M5a/M5b) actually generates and accepts."""
        builder = GraphQLTypeBuilder(registry).build()
        manifest = build_capability_manifest(registry)
        for class_name, entity in builder.entities.items():
            capability = manifest[class_name]
            edge_slot_names = {spec.slot_name for _attr, spec, *_ in entity.filter_edges}
            filter_field_slot_names = {
                spec.slot_name for _attr, spec in entity.filter_fields
            }
            for field in capability.fields:
                slot_name = field.slot.name
                if slot_name in edge_slot_names:
                    # Reference slot whose target is exposed: predicate-
                    # filterable via a nested edge, never a direct FilterOp.
                    assert field.predicate is True
                    assert field.filter_ops == ()
                elif slot_name in filter_field_slot_names:
                    # Everything the real `where:` builder gave a FilterOp
                    # input to, the manifest must report as filterable.
                    assert field.filter_ops != ()
                    assert field.predicate is False
                elif field.slot.kind.name == "REFERENCE":
                    # Reference to a non-exposed class: unfilterable either way.
                    assert field.filter_ops == ()
                    assert field.predicate is False


class TestBuilder:
    def test_build_is_idempotent(self, registry):
        builder = GraphQLTypeBuilder(registry).build()
        first = dict(builder.entities)
        builder.build()
        assert builder.entities == first

    def test_empty_schema_raises(self):
        # hippo_core alone has exactly one concrete non-infrastructure
        # class (ExternalID); a registry can never really be empty, so
        # exercise the guard through a builder with no entities.
        from mosaic.graphql.resolvers import build_graphql_schema as build_with_builder

        registry = SchemaRegistry.from_yaml(GRAPHQL_CORE_ONLY)
        builder = GraphQLTypeBuilder(registry)
        builder._built = True  # bypass generation: simulate no entities
        with pytest.raises(ValueError, match="no concrete entity classes"):
            build_with_builder(registry, builder=builder)


GRAPHQL_CORE_ONLY = """
id: https://example.org/hippo/test_graphql_core_only
name: test_graphql_core_only
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
"""


#: A reference whose range is an ABSTRACT polymorphic base (ADR-0003). The
#: base has no generated object type, so the reference can't resolve to an edge.
EDGE_ONLY_POLYMORPHIC_SCHEMA = """
id: https://example.org/hippo/test_edge_only_polymorphic
name: test_edge_only_polymorphic
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string

classes:
  Container:
    is_a: Entity
    abstract: true
    attributes:
      label:
        required: true
  Box:
    is_a: Container
  Item:
    is_a: Entity
    attributes:
      name:
        required: true
      container:
        range: Container
"""

#: A resolved relationship field name that collides with a sibling scalar slot.
EDGE_ONLY_COLLISION_SCHEMA = """
id: https://example.org/hippo/test_edge_only_collision
name: test_edge_only_collision
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
  Widget:
    is_a: Entity
    attributes:
      donor_id:
        range: Donor
      donor:
        range: string
"""


class TestEdgeOnlyReferenceEmission:
    """ADR-0005: references emit only the resolved edge; the raw id is hidden.

    An abstract target with an exposed concrete kind is a family and resolves
    to its interface (ADR-0014 Decision 2). The two cases that still can't
    produce an edge are handled explicitly — a target with NO exposed kind
    keeps a raw id and warns; a resolved-name collision is a build-time
    error (no silent raw-id fallback).
    """

    def test_abstract_target_with_exposed_kind_resolves_to_interface(self):
        import warnings

        registry = SchemaRegistry.from_yaml(EDGE_ONLY_POLYMORPHIC_SCHEMA)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # no raw-id fallback warning
            sdl = str(build_graphql_schema(registry))
        item = _block(sdl, "type Item")
        assert "container: Container" in item
        assert "containerId" not in item
        assert "interface Container" in sdl
        assert re.search(r"type Box implements Container", sdl)

    def test_abstract_target_without_exposed_kind_retains_raw_id_and_warns(self):
        registry = SchemaRegistry.from_yaml(
            EDGE_ONLY_POLYMORPHIC_SCHEMA.replace(
                "  Box:\n    is_a: Container\n", ""
            )
        )
        with pytest.warns(UserWarning, match="no generated GraphQL type"):
            sdl = str(build_graphql_schema(registry))
        item = _block(sdl, "type Item")
        # No kind to resolve to → raw id retained, no edge, no interface.
        assert "containerId: ID" in item
        assert "container: Container" not in item
        assert "interface Container" not in sdl

    def test_resolved_name_collision_is_build_error(self):
        registry = SchemaRegistry.from_yaml(EDGE_ONLY_COLLISION_SCHEMA)
        with pytest.raises(ValueError, match="collides"):
            build_graphql_schema(registry)


class TestNamingHelpers:
    def test_snake_case(self):
        assert snake_case("DNASample") == "dna_sample"
        assert snake_case("Sample") == "sample"
        assert snake_case("ExternalID") == "external_id"

    def test_camel_case(self):
        assert camel_case("create_sample") == "createSample"
        assert camel_case("samples") == "samples"
        assert camel_case("set_dna_sample_availability") == "setDnaSampleAvailability"
