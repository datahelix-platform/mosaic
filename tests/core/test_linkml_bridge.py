"""Parity tests for the LinkML-backed SchemaRegistry."""

from pathlib import Path

import pytest

from mosaic.linkml_bridge import (
    MOSAIC_SEARCH,
    MOSAIC_INDEX,
    MOSAIC_INDEX_PARTIAL,
    TREE_ROOT_CLASS_NAME,
    SchemaRegistry,
    default_accessor,
)


FIXTURE = Path(__file__).parent.parent / "fixtures" / "schemas" / "sample_schema.yaml"


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_path(FIXTURE)


class TestSchemaIntrospection:
    def test_class_names_includes_defined_classes(self, registry: SchemaRegistry):
        names = set(registry.class_names())
        assert {"Entity", "Project", "Sample"}.issubset(names)

    def test_identifier_slot_resolves_through_inheritance(
        self, registry: SchemaRegistry
    ):
        ident = registry.identifier_slot("Sample")
        assert ident is not None
        assert ident.name == "id"

    def test_induced_slots_include_inherited_id(self, registry: SchemaRegistry):
        slot_names = {s.name for s in registry.induced_slots("Sample")}
        assert "id" in slot_names
        assert "name" in slot_names
        assert "project_id" in slot_names

    def test_required_slots(self, registry: SchemaRegistry):
        required = {s.name for s in registry.required_slots("Sample")}
        assert "id" in required
        assert "name" in required
        assert "volume_ml" not in required


class TestHippoAnnotations:
    def test_searchable_slots_reads_mosaic_search_annotation(
        self, registry: SchemaRegistry
    ):
        searchable = registry.searchable_slots("Sample")
        by_name = {slot.name: mode for slot, mode in searchable}
        assert by_name == {"name": "fts5"}

    def test_indexed_slots_distinguishes_partial(self, registry: SchemaRegistry):
        indexed = {slot.name: partial for slot, partial in registry.indexed_slots("Sample")}
        assert indexed == {"project_id": False, "collected_at": True}

    def test_project_has_two_searchable_slots(self, registry: SchemaRegistry):
        searchable = {slot.name for slot, _ in registry.searchable_slots("Project")}
        assert searchable == {"name", "description"}


class TestReferenceSlots:
    def test_reference_slot_points_to_class_range(self, registry: SchemaRegistry):
        refs = registry.reference_slots("Sample")
        assert ("project_id", "Project") in refs

    def test_non_class_ranges_not_returned_as_references(
        self, registry: SchemaRegistry
    ):
        refs = dict(registry.reference_slots("Sample"))
        assert "name" not in refs
        assert "volume_ml" not in refs
        assert "status" not in refs  # enum, not class


class TestValidation:
    """Tests use sample_schema.yaml which imports mosaic_core (sec9). Sample
    inherits `is_available` from Entity, so every valid instance must include
    it. Typically the SDK fills it in on create; these tests exercise the raw
    validator interface directly and therefore supply it explicitly.
    """

    def test_valid_instance_passes(self, registry: SchemaRegistry):
        errors = registry.validate(
            {"id": "s1", "name": "tissue A", "is_available": True}, "Sample"
        )
        assert errors == []

    def test_missing_required_field_rejected(self, registry: SchemaRegistry):
        errors = registry.validate({"id": "s1", "is_available": True}, "Sample")
        assert len(errors) == 1
        assert "'name'" in errors[0]
        assert "required" in errors[0]

    def test_wrong_type_rejected(self, registry: SchemaRegistry):
        errors = registry.validate(
            {
                "id": "s1",
                "name": "ok",
                "is_available": True,
                "volume_ml": "not-a-number",
            },
            "Sample",
        )
        assert any("not of type" in e for e in errors)

    def test_bad_enum_value_rejected(self, registry: SchemaRegistry):
        errors = registry.validate(
            {"id": "s1", "name": "ok", "is_available": True, "status": "bogus"},
            "Sample",
        )
        assert any("not one of" in e for e in errors)
        assert any("active" in e for e in errors)

    def test_closed_schema_rejects_extra_fields(self, registry: SchemaRegistry):
        errors = registry.validate(
            {"id": "s1", "name": "ok", "is_available": True, "nope": 1}, "Sample"
        )
        assert any("Additional properties" in e for e in errors)


class TestLoaders:
    def test_from_dict_matches_from_path(self):
        import yaml

        path_reg = SchemaRegistry.from_path(FIXTURE)
        data = yaml.safe_load(FIXTURE.read_text())
        dict_reg = SchemaRegistry.from_dict(data)
        assert set(path_reg.class_names()) == set(dict_reg.class_names())

    def test_from_yaml_string(self):
        yaml_text = FIXTURE.read_text()
        reg = SchemaRegistry.from_yaml(yaml_text)
        assert "Sample" in reg.class_names()


class TestAnnotationConstants:
    def test_annotation_keys_use_flat_mosaic_prefix(self):
        assert MOSAIC_SEARCH == "mosaic_search"
        assert MOSAIC_INDEX == "mosaic_index"
        assert MOSAIC_INDEX_PARTIAL == "mosaic_index_partial"


class TestMosaicExtValidation:
    """SchemaRegistry validates every mosaic_* annotation against mosaic_ext at load.

    See sec9 §9.4 and design/reference_mosaic_ext.md for the declared vocabulary.
    """

    def _build(self, extra_annotations: dict, on: str = "slot") -> str:
        """Construct a minimal user-schema YAML with the given annotations.

        `on` is either "slot" (attach to `Thing.name`) or "class" (attach to
        `Thing` itself).
        """
        def _fmt(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, str):
                return f'"{v}"'
            return str(v)

        if on == "slot":
            # annotations under Thing.name require 10-space indent
            ann_yaml = "\n".join(
                f"          {k}: {_fmt(v)}" for k, v in extra_annotations.items()
            )
            return (
                "id: https://example.org/test\n"
                "name: test\n"
                "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
                "default_range: string\n"
                "imports: [linkml:types]\n"
                "classes:\n"
                "  Thing:\n"
                "    attributes:\n"
                "      id: {identifier: true}\n"
                "      name:\n"
                "        range: string\n"
                "        annotations:\n"
                f"{ann_yaml}\n"
            )
        # class-level annotations on Thing require 6-space indent
        ann_yaml = "\n".join(
            f"      {k}: {_fmt(v)}" for k, v in extra_annotations.items()
        )
        return (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types]\n"
            "classes:\n"
            "  Thing:\n"
            "    annotations:\n"
            f"{ann_yaml}\n"
            "    attributes:\n"
            "      id: {identifier: true}\n"
        )

    def test_declared_annotation_passes(self):
        # mosaic_index: true is valid on a slot
        yaml_text = self._build({"mosaic_index": True})
        reg = SchemaRegistry.from_yaml(yaml_text)
        assert "Thing" in reg.class_names()

    def test_undeclared_annotation_fails_with_actionable_message(self):
        from mosaic.core.exceptions import SchemaError

        yaml_text = self._build({"mosaic_bogus": True})
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        msg = str(exc.value)
        assert "mosaic_bogus" in msg
        assert "Thing.name" in msg
        assert "not declared in mosaic_ext" in msg

    def test_wrong_value_type_fails(self):
        from mosaic.core.exceptions import SchemaError

        # mosaic_index expects boolean, got string
        yaml_text = self._build({"mosaic_index": "yes"})
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        msg = str(exc.value)
        assert "mosaic_index" in msg
        assert "expected boolean" in msg

    def test_class_annotation_on_slot_fails(self):
        # mosaic_append_only is a class-only annotation, but in Wave 1 it's
        # not yet declared in mosaic_ext — so this surfaces as "undeclared"
        # rather than "wrong applies_to". When Wave 2 lands and declares
        # mosaic_append_only, this test would shift to the wrong-target case.
        from mosaic.core.exceptions import SchemaError

        yaml_text = self._build({"mosaic_append_only": True})
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        msg = str(exc.value)
        assert "mosaic_append_only" in msg

    def test_slot_annotation_on_class_fails(self):
        # mosaic_index is a slot annotation; attaching it to a class should fail.
        from mosaic.core.exceptions import SchemaError

        yaml_text = self._build({"mosaic_index": True}, on="class")
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        msg = str(exc.value)
        assert "mosaic_index" in msg
        # The error message mentions the annotation may only attach to slots
        assert "slot_annotation" in msg or "slot" in msg.lower()

    def test_multiple_errors_aggregate_into_one_exception(self):
        from mosaic.core.exceptions import SchemaError

        yaml_text = self._build(
            {"mosaic_bogus1": True, "mosaic_bogus2": True, "mosaic_index": "not-bool"}
        )
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        msg = str(exc.value)
        assert "3 mosaic_* annotation error(s)" in msg
        assert "mosaic_bogus1" in msg
        assert "mosaic_bogus2" in msg
        assert "mosaic_index" in msg

    def test_retired_hippo_slot_key_names_its_replacement(self):
        # Platform ADR-0007: Mosaic reads only mosaic_* keys; a stale
        # hippo_* key fails loudly with the new name instead of being
        # silently ignored.
        from mosaic.core.exceptions import SchemaError

        yaml_text = self._build({"hippo_index": True})
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        assert exc.value.error_code == "EXT_VALIDATION"
        msg = str(exc.value)
        assert "`hippo_index`" in msg
        assert "Thing.name" in msg
        assert "rename it to `mosaic_index`" in msg

    def test_retired_hippo_class_key_names_its_replacement(self):
        from mosaic.core.exceptions import SchemaError

        yaml_text = self._build({"hippo_anchor": False}, on="class")
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        msg = str(exc.value)
        assert "`hippo_anchor`" in msg
        assert "rename it to `mosaic_anchor`" in msg

    def test_retired_hippo_core_import_names_its_replacement(self):
        from mosaic.core.exceptions import SchemaError

        yaml_text = (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types, hippo_core]\n"
            "classes:\n"
            "  Thing:\n"
            "    is_a: Entity\n"
        )
        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(yaml_text)
        assert "`hippo_core` (rename it to `mosaic_core`)" in str(exc.value)

    def test_non_mosaic_annotations_are_not_validated(self):
        # Only mosaic_* keys get validated against mosaic_ext. Custom user
        # annotations in other namespaces (prov:, skos:, custom_*) pass
        # through unchecked.
        yaml_text = self._build({"custom_tag": "anything"})
        reg = SchemaRegistry.from_yaml(yaml_text)
        assert "Thing" in reg.class_names()

    def test_mosaic_search_accepts_any_string_value(self):
        # Per Decision 9.4.C: mosaic_search range is `string`, not an enum.
        # The adapter is the authority on which modes it supports; schema-level
        # validation is permissive.
        yaml_text = self._build({"mosaic_search": "fts5"})
        reg1 = SchemaRegistry.from_yaml(yaml_text)
        assert "Thing" in reg1.class_names()

        # Non-canonical mode also passes at schema load (adapter would fail
        # at startup if it doesn't support this mode).
        yaml_text2 = self._build({"mosaic_search": "embedding"})
        reg2 = SchemaRegistry.from_yaml(yaml_text2)
        assert "Thing" in reg2.class_names()


class TestMosaicCoreImport:
    """Tests that `imports: [mosaic_core]` resolves and user classes can
    `is_a: Entity`. See sec9 §9.3 and §9.5.
    """

    def _user_schema(self, include_mosaic_core_import: bool = True) -> str:
        imports = ["linkml:types"]
        if include_mosaic_core_import:
            imports.append("mosaic_core")
        imports_block = "\n".join(f"  - {i}" for i in imports)
        return (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports:\n"
            f"{imports_block}\n"
            "classes:\n"
            "  MySample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      barcode:\n"
            "        range: string\n"
            "        required: true\n"
        )

    def test_mosaic_core_importable_and_entity_resolves(self):
        reg = SchemaRegistry.from_yaml(self._user_schema())
        # Entity flows in from mosaic_core; MySample inherits id + is_available.
        names = set(reg.class_names())
        assert "Entity" in names
        assert "MySample" in names
        slot_names = {s.name for s in reg.induced_slots("MySample")}
        assert {"id", "is_available", "barcode"}.issubset(slot_names)

    def test_mosaic_core_enums_available_in_user_view(self):
        reg = SchemaRegistry.from_yaml(self._user_schema())
        sv = reg.schema_view
        enums = sv.all_enums()
        assert "Status" in enums
        assert "Operation" in enums
        op_values = set(enums["Operation"].permissible_values.keys())
        assert {"create", "update", "supersede", "migration_applied"}.issubset(
            op_values
        )

    def test_without_mosaic_core_import_entity_unresolved(self):
        # Without `imports: mosaic_core`, SchemaView loads successfully but
        # LinkML can't resolve `is_a: Entity` when induced slots are
        # requested — SchemaView raises ValueError("No such class: Entity").
        # This is acceptable load-time failure behavior; the error points at
        # the missing import.
        reg = SchemaRegistry.from_yaml(
            self._user_schema(include_mosaic_core_import=False)
        )
        with pytest.raises(ValueError, match="Entity"):
            reg.induced_slots("MySample")

    def test_entity_is_available_inherited_with_default(self):
        reg = SchemaRegistry.from_yaml(self._user_schema())
        slots = {s.name: s for s in reg.induced_slots("MySample")}
        assert "is_available" in slots
        is_avail = slots["is_available"]
        assert is_avail.required is True
        # ifabsent is stored as a string form in LinkML; slot_default coerces
        # boolean-ranged strings back to Python bools.
        from mosaic.linkml_bridge import slot_default
        assert slot_default(is_avail) is True


class TestHippoCoreProcess:
    """Process class shipped in mosaic_core (sec9 §9.5). Composite activity
    grouping that callers can `is_a: Process` for domain-specific executions
    like Cappella PipelineRuns.
    """

    def _schema_with_process_subclass(self) -> str:
        return (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports:\n"
            "  - linkml:types\n"
            "  - mosaic_core\n"
            "classes:\n"
            "  PipelineRun:\n"
            "    is_a: Process\n"
            "    attributes:\n"
            "      pipeline_name:\n"
            "        range: string\n"
            "        required: true\n"
        )

    def test_process_class_present_in_mosaic_core(self):
        # A user schema that imports mosaic_core sees Process available.
        reg = SchemaRegistry.from_yaml(
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports:\n  - linkml:types\n  - mosaic_core\n"
            "classes: {}\n"
        )
        assert "Process" in reg.class_names()

    def test_process_inherits_entity_slots(self):
        reg = SchemaRegistry.from_yaml(self._schema_with_process_subclass())
        slot_names = {s.name for s in reg.induced_slots("Process")}
        # id + is_available from Entity
        assert "id" in slot_names
        assert "is_available" in slot_names
        # Process-specific slots
        expected = {
            "parent_process_id",
            "operation_kind",
            "started_at",
            "ended_at",
            "actor_id",
        }
        assert expected.issubset(slot_names)

    def test_process_subclass_inherits_all_slots(self):
        reg = SchemaRegistry.from_yaml(self._schema_with_process_subclass())
        slot_names = {s.name for s in reg.induced_slots("PipelineRun")}
        # Entity-inherited
        assert "id" in slot_names
        assert "is_available" in slot_names
        # Process-inherited
        assert "operation_kind" in slot_names
        assert "started_at" in slot_names
        assert "actor_id" in slot_names
        assert "parent_process_id" in slot_names
        # PipelineRun-declared
        assert "pipeline_name" in slot_names

    def test_process_class_uri_is_prov_activity(self):
        reg = SchemaRegistry.from_yaml(self._schema_with_process_subclass())
        proc_cls = reg.get_class("Process")
        assert proc_cls is not None
        # class_uri may be returned as the literal string or the expanded form;
        # accept either.
        assert (
            proc_cls.class_uri == "prov:Activity"
            or "Activity" in (proc_cls.class_uri or "")
        )

    def test_parent_process_id_is_self_reference(self):
        reg = SchemaRegistry.from_yaml(self._schema_with_process_subclass())
        slots = {s.name: s for s in reg.induced_slots("Process")}
        ppid = slots["parent_process_id"]
        assert ppid.range == "Process"
        assert ppid.required is False

    def test_process_operation_kind_is_indexed(self):
        # operation_kind annotated mosaic_index: true — verify annotation reaches
        # the induced slot via the existing annotation_value helper.
        reg = SchemaRegistry.from_yaml(self._schema_with_process_subclass())
        slots = {s.name: s for s in reg.induced_slots("Process")}
        from mosaic.linkml_bridge import annotation_value, MOSAIC_INDEX
        assert annotation_value(slots["operation_kind"], MOSAIC_INDEX) is True
        assert annotation_value(slots["started_at"], MOSAIC_INDEX) is True


class TestHippoCoreProvenanceRecord:
    """ProvenanceRecord class shipped in mosaic_core (sec9 §9.6). Declared for
    introspection and downstream use (typed-client, REST surface). The
    adapter-side enforcement and ProvenanceStore migration land with the
    `provenance-migration` change per Decision 9.6.A.
    """

    def _schema_importing_mosaic_core(self) -> str:
        return (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports:\n"
            "  - linkml:types\n"
            "  - mosaic_core\n"
            "classes: {}\n"
        )

    def test_provenance_record_present_in_mosaic_core(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        assert "ProvenanceRecord" in reg.class_names()

    def test_provenance_record_is_a_entity(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slot_names = {s.name for s in reg.induced_slots("ProvenanceRecord")}
        # Inherited from Entity
        assert "id" in slot_names
        assert "is_available" in slot_names

    def test_provenance_record_has_sec9_slots(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slot_names = {s.name for s in reg.induced_slots("ProvenanceRecord")}
        expected = {
            "entity_id",
            "entity_type",
            "operation",
            "actor_id",
            "timestamp",
            "schema_version",
            "derived_from_id",
            "process_id",
            "patch",
            "context",
        }
        assert expected.issubset(slot_names)

    def test_provenance_record_class_uri_is_prov_activity(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        cls = reg.get_class("ProvenanceRecord")
        assert cls is not None
        assert cls.class_uri == "prov:Activity" or "Activity" in (
            cls.class_uri or ""
        )

    def test_provenance_record_has_mosaic_append_only_annotation(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        cls = reg.get_class("ProvenanceRecord")
        assert cls is not None
        from mosaic.linkml_bridge import annotation_value
        assert annotation_value(cls, "mosaic_append_only") is True

    def test_provenance_record_slots_are_indexed(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slots = {s.name: s for s in reg.induced_slots("ProvenanceRecord")}
        from mosaic.linkml_bridge import annotation_value, MOSAIC_INDEX
        # Slots that sec9 §9.6 and §9.7 say should be indexed for the
        # canonical query paths
        assert annotation_value(slots["entity_id"], MOSAIC_INDEX) is True
        assert annotation_value(slots["timestamp"], MOSAIC_INDEX) is True
        assert annotation_value(slots["operation"], MOSAIC_INDEX) is True
        assert annotation_value(slots["process_id"], MOSAIC_INDEX) is True

    def test_operation_enum_available(self):
        # ProvenanceRecord.operation references the Operation enum declared
        # in mosaic_core. The user view should see both.
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        sv = reg.schema_view
        enums = sv.all_enums()
        assert "Operation" in enums


class TestHippoAppendOnlyAnnotation:
    """Validates the mosaic_append_only annotation declaration in mosaic_ext."""

    def _schema_with_annotation(self, target: str, value: object) -> str:
        """Build a schema attaching mosaic_append_only to a class or slot."""
        if target == "class":
            return (
                "id: https://example.org/t\n"
                "name: t\n"
                "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
                "default_range: string\n"
                "imports: [linkml:types]\n"
                "classes:\n"
                "  LogEntry:\n"
                "    annotations:\n"
                f"      mosaic_append_only: {str(value).lower()}\n"
                "    attributes:\n"
                "      id: {identifier: true}\n"
            )
        # target == "slot"
        return (
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types]\n"
            "classes:\n"
            "  Thing:\n"
            "    attributes:\n"
            "      id: {identifier: true}\n"
            "      name:\n"
            "        range: string\n"
            "        annotations:\n"
            f"          mosaic_append_only: {str(value).lower()}\n"
        )

    def test_mosaic_append_only_true_on_class_passes(self):
        reg = SchemaRegistry.from_yaml(
            self._schema_with_annotation("class", True)
        )
        cls = reg.get_class("LogEntry")
        from mosaic.linkml_bridge import annotation_value
        assert annotation_value(cls, "mosaic_append_only") is True

    def test_mosaic_append_only_false_on_class_passes(self):
        reg = SchemaRegistry.from_yaml(
            self._schema_with_annotation("class", False)
        )
        cls = reg.get_class("LogEntry")
        from mosaic.linkml_bridge import annotation_value
        assert annotation_value(cls, "mosaic_append_only") is False

    def test_mosaic_append_only_on_slot_fails(self):
        # mosaic_append_only is a class_annotation; attaching it to a slot
        # must fail at schema load with an applies_to violation.
        from mosaic.core.exceptions import SchemaError

        with pytest.raises(SchemaError) as exc:
            SchemaRegistry.from_yaml(
                self._schema_with_annotation("slot", True)
            )
        msg = str(exc.value)
        assert "mosaic_append_only" in msg
        assert "slot" in msg.lower() or "class_annotation" in msg


class TestAppendOnlyClassesHelper:
    """SchemaRegistry.append_only_classes() — consulted by adapters to
    reject UPDATE/DELETE against append-only tables (sec9 §9.4 / §9.6).
    """

    def _schema_with_classes(self, annotated: dict[str, bool]) -> str:
        """Build a schema where each class is optionally mosaic_append_only."""
        lines = [
            "id: https://example.org/t",
            "name: t",
            "prefixes: {linkml: 'https://w3id.org/linkml/'}",
            "default_range: string",
            "imports: [linkml:types]",
            "classes:",
        ]
        for name, is_append_only in annotated.items():
            lines.append(f"  {name}:")
            if is_append_only:
                lines.append("    annotations:")
                lines.append("      mosaic_append_only: true")
            lines.append("    attributes:")
            lines.append("      id: {identifier: true}")
        return "\n".join(lines) + "\n"

    def test_empty_when_no_classes_annotated(self):
        reg = SchemaRegistry.from_yaml(
            self._schema_with_classes({"A": False, "B": False})
        )
        assert reg.append_only_classes() == set()

    def test_includes_annotated_class(self):
        reg = SchemaRegistry.from_yaml(
            self._schema_with_classes({"A": True, "B": False})
        )
        assert reg.append_only_classes() == {"A"}

    def test_includes_provenance_record_from_mosaic_core(self):
        reg = SchemaRegistry.from_yaml(
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types, mosaic_core]\n"
            "classes: {}\n"
        )
        assert "ProvenanceRecord" in reg.append_only_classes()

    def test_excludes_abstract_classes(self):
        # A mosaic_append_only annotation on an abstract class is declarative
        # only; adapters don't need it in the enforcement set because there
        # is no backing table.
        reg = SchemaRegistry.from_yaml(
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types]\n"
            "classes:\n"
            "  AbstractLog:\n"
            "    abstract: true\n"
            "    annotations:\n"
            "      mosaic_append_only: true\n"
            "    attributes:\n"
            "      id: {identifier: true}\n"
        )
        assert "AbstractLog" not in reg.append_only_classes()


class TestIsPolymorphicBase:
    """SchemaRegistry.is_polymorphic_base() — drives FK stripping in the DDL
    generators so a reference ranged on a base whose subtype instances live in
    their own tables is stored as plain TEXT rather than a failing FK (#93)."""

    def _reg(self) -> SchemaRegistry:
        return SchemaRegistry.from_yaml(
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types]\n"
            "classes:\n"
            "  AbstractBase:\n"
            "    abstract: true\n"
            "    attributes: {id: {identifier: true}}\n"
            "  ConcreteBase:\n"
            "    attributes: {id: {identifier: true}}\n"
            "  Sub:\n"
            "    is_a: ConcreteBase\n"
            "  Leaf:\n"
            "    attributes: {id: {identifier: true}}\n"
        )

    def test_abstract_base_is_polymorphic(self):
        assert self._reg().is_polymorphic_base("AbstractBase") is True

    def test_concrete_base_with_subclass_is_polymorphic(self):
        assert self._reg().is_polymorphic_base("ConcreteBase") is True

    def test_concrete_leaf_is_not_polymorphic(self):
        # A leaf's instances all live in its own table — its FK is safe.
        assert self._reg().is_polymorphic_base("Leaf") is False
        assert self._reg().is_polymorphic_base("Sub") is False

    def test_unknown_class_is_not_polymorphic(self):
        assert self._reg().is_polymorphic_base("Nope") is False


class TestHippoCoreExternalID:
    """ExternalID class shipped in mosaic_core (sec3 §3.4, β-refactor PR 1.1).
    First-class lifecycle-tracked entity for cross-system ID mapping. The
    legacy external_id_store is not retired yet — that happens in Phase 2.
    """

    def _schema_importing_mosaic_core(self) -> str:
        return (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports:\n"
            "  - linkml:types\n"
            "  - mosaic_core\n"
            "classes: {}\n"
        )

    def test_external_id_present_in_mosaic_core(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        assert "ExternalID" in reg.class_names()

    def test_external_id_inherits_entity_slots(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slot_names = {s.name for s in reg.induced_slots("ExternalID")}
        # Inherited from Entity
        assert "id" in slot_names
        assert "is_available" in slot_names

    def test_external_id_has_declared_slots(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slot_names = {s.name for s in reg.induced_slots("ExternalID")}
        expected = {"value", "source_system", "entity", "is_active"}
        assert expected.issubset(slot_names)

    def test_value_slot_has_mosaic_index_annotation(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slots = {s.name: s for s in reg.induced_slots("ExternalID")}
        from mosaic.linkml_bridge import annotation_value, MOSAIC_INDEX
        assert annotation_value(slots["value"], MOSAIC_INDEX) is True

    def test_source_system_slot_not_indexed(self):
        # Only `value` is marked mosaic_index per PR 1.1 spec; source_system
        # benefits from the composite unique_keys index, not a standalone one.
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slots = {s.name: s for s in reg.induced_slots("ExternalID")}
        from mosaic.linkml_bridge import annotation_value, MOSAIC_INDEX
        assert annotation_value(slots["source_system"], MOSAIC_INDEX) is not True

    def test_is_active_default_is_true(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slots = {s.name: s for s in reg.induced_slots("ExternalID")}
        from mosaic.linkml_bridge import slot_default
        assert slot_default(slots["is_active"]) is True

    def test_entity_slot_range_is_entity(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        slots = {s.name: s for s in reg.induced_slots("ExternalID")}
        assert slots["entity"].range == "Entity"

    def test_unique_keys_block_declared(self):
        reg = SchemaRegistry.from_yaml(self._schema_importing_mosaic_core())
        sv = reg.schema_view
        cls = sv.get_class("ExternalID")
        assert cls is not None
        assert cls.unique_keys
        key_names = set(cls.unique_keys.keys())
        assert "source_system_value_active" in key_names
        uk = cls.unique_keys["source_system_value_active"]
        assert set(uk.unique_key_slots) == {"source_system", "value"}


class TestReferenceLoadersHelper:
    """SchemaRegistry.reference_loaders() — returns concrete subclasses of
    ReferenceLoader in the merged schema (sec9 §9.5, reference-loader-shape).
    """

    _MOSAIC_CORE_IMPORT_SCHEMA = (
        "id: https://example.org/t\n"
        "name: t\n"
        "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
        "default_range: string\n"
        "imports: [linkml:types, mosaic_core]\n"
        "classes: {}\n"
    )

    def test_empty_when_no_subclasses_declared(self):
        reg = SchemaRegistry.from_yaml(self._MOSAIC_CORE_IMPORT_SCHEMA)
        assert reg.reference_loaders() == []

    def test_includes_subclass_of_reference_loader(self):
        schema = (
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types, mosaic_core]\n"
            "classes:\n"
            "  ChemicalLoader:\n"
            "    is_a: ReferenceLoader\n"
            "    attributes:\n"
            "      id: {identifier: true}\n"
        )
        reg = SchemaRegistry.from_yaml(schema)
        assert "ChemicalLoader" in reg.reference_loaders()

    def test_excludes_reference_loader_itself(self):
        reg = SchemaRegistry.from_yaml(self._MOSAIC_CORE_IMPORT_SCHEMA)
        assert "ReferenceLoader" not in reg.reference_loaders()

    def test_excludes_abstract_subclass(self):
        schema = (
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types, mosaic_core]\n"
            "classes:\n"
            "  AbstractLoader:\n"
            "    is_a: ReferenceLoader\n"
            "    abstract: true\n"
            "    attributes:\n"
            "      id: {identifier: true}\n"
        )
        reg = SchemaRegistry.from_yaml(schema)
        assert "AbstractLoader" not in reg.reference_loaders()


class TestTreeRootSynthesis:
    """`_MosaicInstanceBundle` is synthesized at SchemaRegistry construction
    with one multivalued slot per concrete class (sec9 PR 3.1).

    The synthesized class is held on the registry and injected only into
    the validator schema — it is NOT added to the SchemaView, so DDL
    generation, schema-diff, and the typed-client surface remain unaware
    of it. Use the dedicated accessors (``tree_root_class_name``,
    ``tree_root_class``, ``tree_root_slots``) for introspection.
    """

    def test_tree_root_class_name_accessor(self, registry: SchemaRegistry):
        assert registry.tree_root_class_name() == "_MosaicInstanceBundle"
        assert registry.tree_root_class_name() == TREE_ROOT_CLASS_NAME

    def test_tree_root_not_in_class_names(self, registry: SchemaRegistry):
        # The synthetic class is held off the SchemaView so DDL/diff/
        # typed-client never see it.
        assert TREE_ROOT_CLASS_NAME not in registry.class_names()
        assert registry.has_class(TREE_ROOT_CLASS_NAME) is False
        assert registry.get_class(TREE_ROOT_CLASS_NAME) is None

    def test_tree_root_flag_is_set(self, registry: SchemaRegistry):
        cls = registry.tree_root_class()
        assert cls.tree_root is True
        assert cls.name == TREE_ROOT_CLASS_NAME

    def test_user_concrete_classes_have_slots(self, registry: SchemaRegistry):
        slot_names = {s.name for s in registry.tree_root_slots()}
        assert "projects" in slot_names
        assert "samples" in slot_names

    def test_bundled_concrete_classes_have_slots(self, registry: SchemaRegistry):
        slot_names = {s.name for s in registry.tree_root_slots()}
        # ProvenanceRecord, Validator, ReferenceLoader, ExternalID come from
        # the imported mosaic_core schema.
        assert "provenance_records" in slot_names
        assert "validators" in slot_names
        assert "reference_loaders" in slot_names
        assert "external_ids" in slot_names

    def test_abstract_entity_excluded(self, registry: SchemaRegistry):
        slot_names = {s.name for s in registry.tree_root_slots()}
        # `Entity` is abstract — must not get a tree-root slot.
        assert "entitys" not in slot_names
        assert "entities" not in slot_names

    def test_mosaic_accessor_override_used_for_process(
        self, registry: SchemaRegistry
    ):
        # `mosaic_core.yaml` annotates `Process` with
        # `mosaic_accessor: processes` so the naive `+ "s"` pluralizer
        # (which would yield `processs`) is overridden.
        slot_names = {s.name for s in registry.tree_root_slots()}
        assert "processes" in slot_names
        assert "processs" not in slot_names

    def test_slots_are_multivalued_and_inlined(self, registry: SchemaRegistry):
        slots_by_name = {s.name: s for s in registry.tree_root_slots()}
        samples = slots_by_name["samples"]
        assert samples.multivalued is True
        assert samples.inlined is True
        assert samples.inlined_as_list is True
        assert samples.range == "Sample"

    def test_validate_against_tree_root_accepts_valid_bundle(
        self, registry: SchemaRegistry
    ):
        bundle = {
            "projects": [{"id": "p1", "name": "Proj", "is_available": True}],
            "samples": [
                {
                    "id": "s1",
                    "name": "Tissue A",
                    "project_id": "p1",
                    "is_available": True,
                }
            ],
        }
        errors = registry.validate(bundle, registry.tree_root_class_name())
        assert errors == []

    def test_validate_against_tree_root_rejects_unknown_slot(
        self, registry: SchemaRegistry
    ):
        bundle = {"not_a_class": [{"id": "x"}]}
        errors = registry.validate(bundle, registry.tree_root_class_name())
        assert errors  # closed schema rejects undeclared slot

    def test_validate_against_tree_root_propagates_class_errors(
        self, registry: SchemaRegistry
    ):
        # Sample requires `name`. Validation must catch missing fields on
        # nested instances inside the tree.
        bundle = {"samples": [{"id": "s1", "is_available": True}]}
        errors = registry.validate(bundle, registry.tree_root_class_name())
        assert errors
        assert any("'name'" in e and "required" in e for e in errors)

    def test_default_accessor_helper_matches_slot_names(self):
        # Sanity-check the algorithm the synthesis relies on.
        assert default_accessor("Sample") == "samples"
        assert default_accessor("TissueType") == "tissue_types"
        assert default_accessor("DNASample") == "dna_samples"
        assert default_accessor("ExternalID") == "external_ids"
        assert default_accessor("ProvenanceRecord") == "provenance_records"

    def test_collision_with_reserved_class_name_raises(self):
        from mosaic.core.exceptions import SchemaError

        schema = (
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types, mosaic_core]\n"
            "classes:\n"
            "  _MosaicInstanceBundle:\n"
            "    description: oops\n"
            "    attributes:\n"
            "      id: {identifier: true}\n"
        )
        with pytest.raises(SchemaError):
            SchemaRegistry.from_yaml(schema)

    def test_slot_name_collision_raises(self):
        from mosaic.core.exceptions import SchemaError

        # Two classes whose snake_case_plural would collide and neither
        # overrides via mosaic_accessor → synthesis must refuse.
        schema = (
            "id: https://example.org/t\n"
            "name: t\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports: [linkml:types, mosaic_core]\n"
            "classes:\n"
            "  WidgetCount:\n"
            "    is_a: Entity\n"
            "    annotations: {mosaic_accessor: counts}\n"
            "  GadgetCount:\n"
            "    is_a: Entity\n"
            "    annotations: {mosaic_accessor: counts}\n"
        )
        with pytest.raises(SchemaError):
            SchemaRegistry.from_yaml(schema)


class TestRelativeImportResolution:
    """Relative sibling ``imports:`` resolve relative to the referencing
    schema, not the process CWD (datahelix #66)."""

    # A tree-root file importing a sibling in the same directory — the standard
    # LinkML "schema split across files" pattern.
    _ROOT = (
        "id: https://example.org/main\n"
        "name: main\n"
        "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
        "default_range: string\n"
        "imports: [linkml:types, pathology]\n"
        "classes:\n"
        "  Donor:\n"
        "    tree_root: true\n"
        "    attributes:\n"
        "      id: {identifier: true}\n"
        "      diagnosis: {range: Pathology}\n"
    )
    _SIBLING = (
        "id: https://example.org/pathology\n"
        "name: pathology\n"
        "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
        "default_range: string\n"
        "imports: [linkml:types]\n"
        "classes:\n"
        "  Pathology:\n"
        "    attributes:\n"
        "      code: {range: string}\n"
    )

    @pytest.fixture
    def schema_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "schemas"
        d.mkdir()
        (d / "main.yaml").write_text(self._ROOT)
        (d / "pathology.yaml").write_text(self._SIBLING)
        return d

    def test_directory_load_resolves_siblings_from_foreign_cwd(
        self, schema_dir: Path, monkeypatch, tmp_path: Path
    ):
        # cwd is the *parent* of the schema dir — mirrors a container invoking
        # `mosaic migrate --schema-dir schemas` from its WORKDIR.
        monkeypatch.chdir(tmp_path)
        reg = SchemaRegistry.from_path(schema_dir)
        assert {"Donor", "Pathology"}.issubset(set(reg.class_names()))

    def test_single_file_with_requires_resolves_siblings_from_foreign_cwd(
        self, schema_dir: Path, monkeypatch, tmp_path: Path
    ):
        # A `requires:` directive forces string-construction; the sibling
        # import must still resolve relative to the schema file.
        (schema_dir / "main.yaml").write_text(
            self._ROOT + "requires: [some_loader==1.0.0]\n"
        )
        monkeypatch.chdir(tmp_path)
        reg = SchemaRegistry.from_path(schema_dir / "main.yaml")
        assert {"Donor", "Pathology"}.issubset(set(reg.class_names()))

    def test_single_file_without_requires_resolves_siblings_from_foreign_cwd(
        self, schema_dir: Path, monkeypatch, tmp_path: Path
    ):
        monkeypatch.chdir(tmp_path)
        reg = SchemaRegistry.from_path(schema_dir / "main.yaml")
        assert {"Donor", "Pathology"}.issubset(set(reg.class_names()))
