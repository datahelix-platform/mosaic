"""Families: abstract classes with exposed kinds (ADR-0014 Decisions 2-6, #228).

The storage and GraphQL cases run on both adapters through
``tests/support/closure_fixture.py``. This module covers what is
transport-independent: the family model, the planner-facing capability
manifest and QuerySpec (Decision 4), the ``hippo_anchor`` /
``hippo_manifest`` opt-outs (Decision 5), a migration transform's
one-class-at-a-time default, and full-text search over a family.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.loaders.domain_module import MigrationReadView
from mosaic.core.query_spec import parse_query_spec, validate_query_spec
from mosaic.core.query_spec_compiler import compile_query_spec
from mosaic.core.schema_typing import build_capability_manifest, build_family_model
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.mcp.serialize import entity_capability_to_dict
from tests.support.closure_fixture import SCHEMA, build_closure_registry, seed


def _with_annotation(cls: str, key: str) -> SchemaRegistry:
    """The closure fixture with ``<key>: false`` on class ``cls``."""
    marker = f"  {cls}:\n"
    assert marker in SCHEMA
    return SchemaRegistry.from_yaml(
        SCHEMA.replace(marker, f"{marker}    annotations:\n      {key}: false\n", 1)
    )


@pytest.fixture
def client():
    registry = build_closure_registry()
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(os.path.join(tmpdir, "families.db"), schema_registry=registry)
        c = MosaicClient(storage=storage, registry=registry, bypass_validation=True)
        seed(c)
        yield c


class TestFamilyModel:
    def test_sample_is_a_family_with_shared_and_kind_only_fields(self):
        families = build_family_model(build_closure_registry())
        assert set(families) == {"Sample"}  # Entity is infrastructure
        sample = families["Sample"]
        assert sample.kinds == ("Blood", "Brain")
        assert sample.parent is None
        assert sample.accessor_name == "samples"
        assert {f.name for f in sample.fields} >= {"id", "label", "produced_by"}
        assert dict(sample.kind_field_owners) == {
            "hemisphere": ("Brain",),
            "volume_ml": ("Blood",),
            "drawn_from": ("Blood",),
        }

    def test_slot_typed_differently_per_kind_is_neither_shared_nor_kind_only(self):
        registry = SchemaRegistry.from_yaml(
            SCHEMA.replace(
                "      hemisphere:\n",
                "      hemisphere:\n      grade:\n        range: integer\n",
            ).replace(
                "      volume_ml:\n        range: float\n",
                "      volume_ml:\n        range: float\n      grade:\n",
            )
        )
        sample = build_family_model(registry)["Sample"]
        assert "grade" not in {f.name for f in sample.fields}
        assert "grade" not in dict(sample.kind_field_owners)


class TestCapabilityManifest:
    def test_family_entry_carries_the_hierarchy(self):
        manifest = build_capability_manifest(build_closure_registry())
        sample = manifest["Sample"]
        assert sample.abstract is True
        assert sample.kinds == ("Blood", "Brain")
        assert sample.parent is None
        assert sample.anchor is True
        fields = sample.fields_by_name
        assert fields["label"].kinds == ()
        assert fields["hemisphere"].kinds == ("Brain",)
        # Leaves and concrete parents name their place in the hierarchy.
        assert manifest["Brain"].parent == "Sample"
        assert manifest["Brain"].kinds == ()
        assert manifest["Donor"].parent == "Person"
        assert manifest["Process"].kinds == ("Process", "BankBrain")
        assert manifest["Process"].abstract is False

    def test_reference_into_a_family_is_predicate_filterable(self):
        manifest = build_capability_manifest(build_closure_registry())
        assert manifest["Assay"].fields_by_name["inputs"].predicate is True

    def test_serialized_for_mcp(self):
        manifest = build_capability_manifest(build_closure_registry())
        payload = entity_capability_to_dict(manifest["Sample"])
        assert payload["abstract"] is True
        assert payload["kinds"] == ["Blood", "Brain"]
        assert payload["parent"] is None
        assert payload["anchor"] is True
        only = {f["name"]: f["only_kinds"] for f in payload["fields"]}
        assert only["hemisphere"] == ["Brain"]
        assert only["label"] == []


class TestQuerySpecOverFamilies:
    def _run(self, client, raw):
        manifest = build_capability_manifest(client.registry)
        spec = parse_query_spec(raw)
        result = validate_query_spec(spec, manifest)
        assert not result.errors, result.errors
        compiled = compile_query_spec(spec, manifest)
        page = client.query(entity_type=compiled.entity_type, where=compiled.where)
        return {i["id"] for i in page.items}

    def test_family_anchor(self, client):
        assert self._run(client, {"v": 1, "anchor": "Sample", "mode": "AND",
                                  "criteria": []}) == {"br1", "bl1"}
        assert self._run(client, {
            "v": 1, "anchor": "Sample", "mode": "AND",
            "criteria": [{"kind": "field", "slot": "hemisphere", "op": "eq", "value": "L"}],
        }) == {"br1"}

    def test_related_condition_into_a_family(self, client):
        assert self._run(client, {
            "v": 1, "anchor": "Assay", "mode": "AND",
            "criteria": [{"kind": "related", "edge": "inputs", "quantifier": "some",
                          "criteria": [{"kind": "field", "slot": "volume_ml",
                                        "op": "gt", "value": 1.0}]}],
        }) == {"a1"}


class TestOptOuts:
    def test_hippo_anchor_false_keeps_the_interface_but_no_collection(self):
        from mosaic.graphql import build_graphql_schema

        registry = _with_annotation("Sample", "hippo_anchor")
        manifest = build_capability_manifest(registry)
        assert manifest["Sample"].anchor is False
        result = validate_query_spec(
            parse_query_spec({"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []}),
            manifest,
        )
        assert [e.code for e in result.errors] == ["UNKNOWN_ANCHOR"]
        # Still an edge target for the planner, still an interface in GraphQL.
        assert manifest["Assay"].fields_by_name["inputs"].predicate is True
        sdl = str(build_graphql_schema(registry))
        assert "interface Sample" in sdl
        assert "samples(" not in sdl and "sample(id" not in sdl
        assert "inputs: [Sample!]!" in sdl

    def test_hippo_anchor_false_on_a_concrete_class_removes_only_queryspec_anchoring(self):
        from mosaic.graphql import build_graphql_schema

        registry = _with_annotation("Assay", "hippo_anchor")
        manifest = build_capability_manifest(registry)
        assert manifest["Assay"].anchor is False
        assert "assays(" in str(build_graphql_schema(registry))

    def test_hippo_manifest_false_hides_the_family_from_the_planner(self):
        from mosaic.graphql import build_graphql_schema

        registry = _with_annotation("Sample", "hippo_manifest")
        manifest = build_capability_manifest(registry)
        assert "Sample" not in manifest
        assert manifest["Assay"].fields_by_name["inputs"].predicate is False
        assert manifest["Brain"].parent is None
        # GraphQL and the SDK are unaffected.
        assert "samples(" in str(build_graphql_schema(registry))


class TestMigrationReadView:
    def test_transform_reads_a_family_one_class_at_a_time(self, client):
        view = MigrationReadView(client)
        assert view.query("Sample").total == 0
        assert view.count("Sample") == 0
        page = view.query("Sample", include_subclasses=True)
        assert {i["id"] for i in page.items} == {"br1", "bl1"}


SEARCH_SCHEMA = """\
id: https://example.org/family_search
name: family_search
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Note:
    is_a: Entity
    abstract: true
    attributes:
      title:
  Memo:
    is_a: Note
    attributes:
      body:
        annotations:
          hippo_search: fts5
  Letter:
    is_a: Note
    attributes:
      text:
        annotations:
          hippo_search: fts5
"""


class TestFamilySearch:
    def test_search_spans_every_kind(self):
        registry = SchemaRegistry.from_yaml(SEARCH_SCHEMA)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "family_search.db")
            c = MosaicClient(
                storage=SQLiteAdapter(db_path, schema_registry=registry), registry=registry
            )
            conn = sqlite3.connect(db_path)
            for tables in c._fts_table_metadata.values():
                for meta in tables:
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {meta.table_name} "
                        "USING fts5(entity_id, content)"
                    )
            conn.commit()
            conn.close()
            c.put("Memo", {"id": "m1", "title": "a", "body": "hippocampus slice"})
            c.put("Letter", {"id": "l1", "title": "b", "text": "about the hippocampus"})
            c.put("Memo", {"id": "m2", "title": "c", "body": "unrelated"})
            page = c.search("Note", "hippocampus")
            assert {i["id"]: i["entity_type"] for i in page.items} == {
                "m1": "Memo", "l1": "Letter"
            }
            assert page.total == 2
            manifest = build_capability_manifest(registry)
            assert manifest["Note"].search_available is True
