"""Tests for the ExternalReference value type + mosaic_external_xref
annotation (issue #48, non-breaking phase).

Covers:
- DDL: inline JSON TEXT storage for ExternalReference-ranged slots
  (single- and multivalued), mosaic_xref_index side table emission, and
  startup failure for a misplaced annotation.
- Index maintenance across create / update / replace / availability-off /
  availability-on / delete / supersede, in the same transaction as the
  entity write.
- Global (system, value) uniqueness among available entities with a
  clear XrefUniquenessError naming the conflict.
- SDK find_by_xref / list_xrefs round-trips.
- Typing core: SlotKind.STRUCTURED + SlotModel.is_external_xref;
  ExternalReference excluded from exposure.
- Deprecation warnings on the legacy ExternalID client methods.
"""

import os
import sqlite3
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import SchemaError, XrefUniquenessError
from mosaic.core.schema_typing import (
    SlotKind,
    build_type_model,
    exposed_class_names,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.storage.ddl_generator import DDLGenerator
from mosaic.core.storage.xref import XREF_TABLE, extract_xref_pairs
from mosaic.linkml_bridge import SchemaRegistry

XREF_TEST_SCHEMA = """
id: https://example.org/mosaic/test_xref
name: test_xref
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Sample:
    is_a: Entity
    description: A sample with external references.
    attributes:
      name:
        required: true
      starlims_ref:
        description: Single-valued reverse-lookup key.
        range: ExternalReference
        inlined: true
        annotations:
          mosaic_external_xref: true
      other_refs:
        description: Multivalued reverse-lookup keys.
        range: ExternalReference
        multivalued: true
        inlined: true
        inlined_as_list: true
        annotations:
          mosaic_external_xref: true
      note_ref:
        description: Plain structured value — NOT a lookup key.
        range: ExternalReference
        inlined: true

  Donor:
    is_a: Entity
    description: Second class sharing the global xref namespace.
    attributes:
      label:
        required: true
      registry_ref:
        range: ExternalReference
        inlined: true
        annotations:
          mosaic_external_xref: true
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(XREF_TEST_SCHEMA)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "xref_test.db")


@pytest.fixture
def storage(registry, db_path) -> SQLiteAdapter:
    return SQLiteAdapter(db_path, schema_registry=registry)


@pytest.fixture
def client(registry, storage) -> MosaicClient:
    return MosaicClient(storage=storage, registry=registry)


def _xref_rows(db_path: str) -> set[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            tuple(row)
            for row in conn.execute(
                f"SELECT entity_id, entity_type, slot, system, value "
                f"FROM {XREF_TABLE}"
            )
        }
    finally:
        conn.close()


class TestExtractXrefPairs:
    def test_none_and_garbage(self):
        assert extract_xref_pairs(None) == []
        assert extract_xref_pairs("not json") == []
        assert extract_xref_pairs(42) == []
        assert extract_xref_pairs([1, "x"]) == []

    def test_single_dict(self):
        assert extract_xref_pairs({"system": "A", "value": "1"}) == [("A", "1")]

    def test_list_of_dicts(self):
        pairs = extract_xref_pairs(
            [{"system": "A", "value": "1"}, {"system": "B", "value": "2"}]
        )
        assert pairs == [("A", "1"), ("B", "2")]

    def test_json_text_forms(self):
        assert extract_xref_pairs('{"system": "A", "value": "1"}') == [("A", "1")]
        assert extract_xref_pairs('[{"system": "A", "value": "1"}]') == [("A", "1")]

    def test_missing_fields_skipped(self):
        assert extract_xref_pairs({"system": "A"}) == []
        assert extract_xref_pairs({"value": "1"}) == []
        assert extract_xref_pairs({"system": "", "value": "1"}) == []


class TestDDL:
    def test_value_type_slots_become_text_columns(self, registry):
        ddl = DDLGenerator().generate(registry)
        conn = sqlite3.connect(":memory:")
        for stmt in ddl:
            conn.execute(stmt)
        cols = {
            row[1]: row[2].upper()
            for row in conn.execute("PRAGMA table_info('Sample')")
        }
        # single-valued, multivalued, and unannotated value-type slots all
        # land as plain TEXT columns (JSON storage) — no FK, no linktable.
        assert cols["starlims_ref"] == "TEXT"
        assert cols["other_refs"] == "TEXT"
        assert cols["note_ref"] == "TEXT"
        assert "starlims_ref_id" not in cols

    def test_no_external_reference_table(self, registry):
        ddl = DDLGenerator().generate(registry)
        conn = sqlite3.connect(":memory:")
        for stmt in ddl:
            conn.execute(stmt)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "ExternalReference" not in tables
        assert XREF_TABLE in tables

    def test_xref_unique_index(self, registry):
        ddl = DDLGenerator().generate(registry)
        conn = sqlite3.connect(":memory:")
        for stmt in ddl:
            conn.execute(stmt)
        conn.execute(
            f"INSERT INTO {XREF_TABLE} VALUES ('e1', 'Sample', 's', 'A', '1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {XREF_TABLE} VALUES ('e2', 'Donor', 'r', 'A', '1')"
            )

    def test_no_xref_table_without_annotation(self):
        plain = SchemaRegistry.from_yaml(
            """
id: https://example.org/mosaic/test_plain
name: test_plain
prefixes:
  linkml: https://w3id.org/linkml/
imports: [linkml:types, mosaic_core]
default_range: string
classes:
  Thing:
    is_a: Entity
    attributes:
      name: {}
"""
        )
        ddl = DDLGenerator().generate(plain)
        assert not any(XREF_TABLE in stmt for stmt in ddl)

    def test_annotation_on_wrong_range_fails_at_startup(self):
        bad = SchemaRegistry.from_yaml(
            """
id: https://example.org/mosaic/test_bad
name: test_bad
prefixes:
  linkml: https://w3id.org/linkml/
imports: [linkml:types, mosaic_core]
default_range: string
classes:
  Thing:
    is_a: Entity
    attributes:
      name:
        annotations:
          mosaic_external_xref: true
"""
        )
        with pytest.raises(SchemaError, match="mosaic_external_xref"):
            DDLGenerator().generate(bad)


class TestIndexMaintenance:
    def test_create_indexes_annotated_slots_only(self, client, storage, db_path):
        created = client.create(
            "Sample",
            {
                "name": "s1",
                "starlims_ref": {"system": "STARLIMS", "value": "BC-1"},
                "other_refs": [
                    {"system": "HALO", "value": "H-1"},
                    {"system": "DONOR_DB", "value": "D-1"},
                ],
                "note_ref": {"system": "NOTES", "value": "N-1"},
            },
        )
        eid = created["id"]
        rows = _xref_rows(db_path)
        assert (eid, "Sample", "starlims_ref", "STARLIMS", "BC-1") in rows
        assert (eid, "Sample", "other_refs", "HALO", "H-1") in rows
        assert (eid, "Sample", "other_refs", "DONOR_DB", "D-1") in rows
        # note_ref carries an ExternalReference but is NOT annotated.
        assert not any(r[2] == "note_ref" for r in rows)
        assert len(rows) == 3

    def test_update_rederives_rows(self, client, storage, db_path):
        eid = client.create(
            "Sample",
            {
                "name": "s1",
                "starlims_ref": {"system": "STARLIMS", "value": "BC-1"},
                "other_refs": [{"system": "HALO", "value": "H-1"}],
            },
        )["id"]
        client.update(
            "Sample",
            eid,
            {
                "name": "s1",
                "starlims_ref": {"system": "STARLIMS", "value": "BC-2"},
            },
        )
        rows = _xref_rows(db_path)
        assert rows == {(eid, "Sample", "starlims_ref", "STARLIMS", "BC-2")}

    def test_replace_rederives_rows(self, client, storage, db_path):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        client.replace(
            "Sample",
            eid,
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-9"}},
        )
        rows = _xref_rows(db_path)
        assert rows == {(eid, "Sample", "starlims_ref", "STARLIMS", "BC-9")}

    def test_availability_off_removes_rows(self, client, storage, db_path):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        result = client.set_availability_bulk(
            "Sample", [eid], is_available=False, reason="archived"
        )
        assert result["failed"] == 0
        assert _xref_rows(db_path) == set()

    def test_availability_on_restores_rows(self, client, storage, db_path):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        client.set_availability_bulk("Sample", [eid], is_available=False)
        client.set_availability_bulk("Sample", [eid], is_available=True)
        rows = _xref_rows(db_path)
        assert rows == {(eid, "Sample", "starlims_ref", "STARLIMS", "BC-1")}

    def test_delete_removes_rows(self, client, storage, db_path):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        client.delete("Sample", eid)
        assert _xref_rows(db_path) == set()

    def test_supersede_removes_rows_from_predecessor(
        self, client, storage, db_path
    ):
        old = client.create(
            "Sample",
            {"name": "old", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        new = client.create("Sample", {"name": "new"})["id"]
        client.supersede_entity(old, new)
        assert _xref_rows(db_path) == set()
        # ...and the freed pair is claimable by the replacement.
        client.update(
            "Sample",
            new,
            {"name": "new", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )
        assert (new, "Sample", "starlims_ref", "STARLIMS", "BC-1") in _xref_rows(
            db_path
        )


class TestUniqueness:
    def test_create_conflict_rejected_and_rolled_back(self, client):
        client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )
        with pytest.raises(XrefUniquenessError) as excinfo:
            client.create(
                "Sample",
                {
                    "name": "s2",
                    "starlims_ref": {"system": "STARLIMS", "value": "BC-1"},
                },
            )
        msg = str(excinfo.value)
        assert "STARLIMS" in msg and "BC-1" in msg
        assert excinfo.value.conflicting_entity_id is not None
        # The conflicting create rolled back entirely — only s1 exists.
        names = {item["data"]["name"] for item in client.query("Sample").items}
        assert names == {"s1"}

    def test_conflict_across_classes(self, client):
        client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "REG", "value": "R-1"}},
        )
        with pytest.raises(XrefUniquenessError, match="globally unique"):
            client.create(
                "Donor",
                {"label": "d1", "registry_ref": {"system": "REG", "value": "R-1"}},
            )

    def test_duplicate_within_one_entity_rejected(self, client):
        with pytest.raises(XrefUniquenessError, match="duplicated within"):
            client.create(
                "Sample",
                {
                    "name": "s1",
                    "starlims_ref": {"system": "A", "value": "1"},
                    "other_refs": [{"system": "A", "value": "1"}],
                },
            )

    def test_unannotated_slot_not_constrained(self, client):
        client.create(
            "Sample",
            {"name": "s1", "note_ref": {"system": "NOTES", "value": "N-1"}},
        )
        # Same pair on the unannotated slot of another entity is fine.
        client.create(
            "Sample",
            {"name": "s2", "note_ref": {"system": "NOTES", "value": "N-1"}},
        )

    def test_reactivation_conflict_rejected(self, client):
        first = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        client.set_availability_bulk("Sample", [first], is_available=False)
        client.create(
            "Sample",
            {"name": "s2", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )
        # Bringing the first entity back would re-claim a taken pair.
        result = client.set_availability_bulk(
            "Sample", [first], is_available=True
        )
        assert result["failed"] == 1
        assert "BC-1" in result["failures"][0]["error"]


class TestFindByXref:
    def test_round_trip(self, client):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        envelope = client.find_by_xref("STARLIMS", "BC-1")
        assert envelope is not None
        assert envelope["id"] == eid
        assert envelope["entity_type"] == "Sample"
        assert envelope["data"]["name"] == "s1"

    def test_unknown_pair_returns_none(self, client):
        assert client.find_by_xref("NOPE", "missing") is None

    def test_round_trip_after_update(self, client):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        client.update(
            "Sample",
            eid,
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-2"}},
        )
        assert client.find_by_xref("STARLIMS", "BC-1") is None
        after = client.find_by_xref("STARLIMS", "BC-2")
        assert after is not None and after["id"] == eid

    def test_multivalued_lookup(self, client):
        eid = client.create(
            "Sample",
            {
                "name": "s1",
                "other_refs": [
                    {"system": "HALO", "value": "H-1"},
                    {"system": "DONOR_DB", "value": "D-1"},
                ],
            },
        )["id"]
        for system, value in (("HALO", "H-1"), ("DONOR_DB", "D-1")):
            envelope = client.find_by_xref(system, value)
            assert envelope is not None and envelope["id"] == eid

    def test_unavailable_entity_not_found(self, client):
        eid = client.create(
            "Sample",
            {"name": "s1", "starlims_ref": {"system": "STARLIMS", "value": "BC-1"}},
        )["id"]
        client.set_availability_bulk("Sample", [eid], is_available=False)
        assert client.find_by_xref("STARLIMS", "BC-1") is None

    def test_list_xrefs(self, client):
        eid = client.create(
            "Sample",
            {
                "name": "s1",
                "starlims_ref": {"system": "STARLIMS", "value": "BC-1"},
                "other_refs": [{"system": "HALO", "value": "H-1"}],
                "note_ref": {"system": "NOTES", "value": "N-1"},
            },
        )["id"]
        xrefs = client.list_xrefs(eid)
        assert {(x["slot"], x["system"], x["value"]) for x in xrefs} == {
            ("starlims_ref", "STARLIMS", "BC-1"),
            ("other_refs", "HALO", "H-1"),
        }

    def test_list_xrefs_unknown_entity(self, client):
        assert client.list_xrefs("does-not-exist") == []


class TestSchemaTyping:
    def test_external_reference_not_exposed(self, registry):
        names = exposed_class_names(registry)
        assert "ExternalReference" not in names
        # ExternalID stays exposed (deprecated but non-breaking).
        assert "ExternalID" in names
        assert "Sample" in names and "Donor" in names

    def test_structured_kind_and_xref_flag(self, registry):
        model = build_type_model(registry)
        fields = {f.name: f for f in model["Sample"].fields}

        single = fields["starlims_ref"]
        assert single.kind is SlotKind.STRUCTURED
        assert single.target_class == "ExternalReference"
        assert single.is_external_xref is True
        assert single.multivalued is False

        multi = fields["other_refs"]
        assert multi.kind is SlotKind.STRUCTURED
        assert multi.is_external_xref is True
        assert multi.multivalued is True

        plain = fields["note_ref"]
        assert plain.kind is SlotKind.STRUCTURED
        assert plain.is_external_xref is False

    def test_scalar_slots_unaffected(self, registry):
        model = build_type_model(registry)
        fields = {f.name: f for f in model["Sample"].fields}
        assert fields["name"].kind is SlotKind.SCALAR
        assert fields["name"].is_external_xref is False


class TestDeprecationShims:
    def test_register_external_id_warns(self, client):
        eid = client.create("Sample", {"name": "s1"})["id"]
        with pytest.warns(DeprecationWarning, match="register_external_id"):
            client.register_external_id(eid, "EXT-1", source_system="LEGACY")

    def test_get_by_external_id_warns(self, client):
        eid = client.create("Sample", {"name": "s1"})["id"]
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", DeprecationWarning)
            client.register_external_id(eid, "EXT-1", source_system="LEGACY")
        with pytest.warns(DeprecationWarning, match="find_by_xref"):
            resolved = client.get_by_external_id("EXT-1")
        assert resolved["id"] == eid

    def test_list_external_ids_warns(self, client):
        eid = client.create("Sample", {"name": "s1"})["id"]
        with pytest.warns(DeprecationWarning, match="list_xrefs"):
            client.list_external_ids(eid)

    def test_mapping_supersede_warns(self, client):
        eid = client.create("Sample", {"name": "s1"})["id"]
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", DeprecationWarning)
            client.register_external_id(eid, "EXT-1", source_system="LEGACY")
        with pytest.warns(DeprecationWarning, match="supersede"):
            client.supersede(eid, "EXT-1", "EXT-2", source_system="LEGACY")

    def test_entity_level_supersede_does_not_warn(self, client, recwarn):
        old = client.create("Sample", {"name": "old"})["id"]
        new = client.create("Sample", {"name": "new"})["id"]
        client.supersede_entity(old, new)
        assert not [
            w for w in recwarn.list if issubclass(w.category, DeprecationWarning)
        ]
