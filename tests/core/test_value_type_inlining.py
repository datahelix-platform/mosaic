"""Schema-driven inline value types (issue #90).

A LinkML class with no identifier slot used as a slot range — a value object
such as ``Mass {value, unit}`` modeled as a subclass of an abstract
``Quantity`` — must be treated as an inline value type exactly like the
framework's ``ExternalReference``: stored as one JSON TEXT column on the
owning entity, never reified into its own table with a synthetic FK.

Before the fix, value-type handling was a hardcoded allowlist of one class
(``ExternalReference``). Any other identifier-less class got its own table
and the owner slot became a ``_id`` FK; on ingest the inline dict matched no
entity id, so the value was dropped silently (``errors=0``, FK NULL, value
table empty). These tests pin both facets: (a) detection is schema-driven,
and (b) the inline object round-trips with no data loss.
"""

import os
import sqlite3
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.schema_typing import SlotKind, build_type_model, exposed_class_names
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.storage.ddl_generator import DDLGenerator
from mosaic.linkml_bridge import SchemaRegistry, is_value_type

# Minimal reproduction from issue #90: an abstract Quantity, an
# identifier-less Mass value object, and a Widget entity that inlines it.
QUANTITY_SCHEMA = """
id: https://example.org/quantity-repro
name: quantity_repro
prefixes:
  linkml: https://w3id.org/linkml/
  ex: https://example.org/quantity-repro/
default_prefix: ex
default_range: string
imports: [linkml:types, mosaic_core]
enums:
  MassUnit:
    permissible_values: {g: {}, mg: {}}
classes:
  Quantity:
    abstract: true
    attributes:
      value: {range: float}
  Mass:
    is_a: Quantity
    attributes:
      unit: {range: MassUnit}
  Widget:
    is_a: Entity
    attributes:
      name: {required: true}
      mass: {range: Mass, inlined: true}
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(QUANTITY_SCHEMA)


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "value_type.db")


@pytest.fixture
def client(registry, db_path) -> MosaicClient:
    storage = SQLiteAdapter(db_path, schema_registry=registry)
    return MosaicClient(storage=storage, registry=registry)


class TestDetection:
    def test_identifier_less_classes_are_value_types(self, registry):
        vts = registry.value_type_classes()
        # Mass (concrete) and Quantity (abstract base) both lack an
        # identifier slot and are not tree-roots.
        assert "Mass" in vts
        assert "Quantity" in vts
        # The framework value type is still detected schema-driven.
        assert "ExternalReference" in vts

    def test_entities_are_not_value_types(self, registry):
        vts = registry.value_type_classes()
        # Widget is_a Entity -> inherits the `id` identifier.
        assert "Widget" not in vts
        # mosaic_core entity classes carry identity (id / name).
        assert "Entity" not in vts
        assert "ProvenanceRecord" not in vts

    def test_is_value_type_helper(self, registry):
        sv = registry.schema_view
        assert is_value_type(sv, "Mass") is True
        assert is_value_type(sv, "Widget") is False
        assert is_value_type(sv, "DoesNotExist") is False


class TestDDL:
    def test_value_object_gets_no_table(self, registry):
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
        assert "Mass" not in tables
        assert "Quantity" not in tables
        assert "Widget" in tables

    def test_owner_slot_is_text_not_fk(self, registry):
        ddl = DDLGenerator().generate(registry)
        conn = sqlite3.connect(":memory:")
        for stmt in ddl:
            conn.execute(stmt)
        cols = {
            row[1]: (row[2] or "").upper()
            for row in conn.execute("PRAGMA table_info('Widget')")
        }
        assert cols.get("mass") == "TEXT"
        assert "mass_id" not in cols


class TestRoundTrip:
    def test_inline_value_object_round_trips(self, client):
        """Facet (b): the inline object must persist, not vanish silently."""
        created = client.create(
            "Widget",
            {"name": "Widget One", "mass": {"value": 5.0, "unit": "g"}},
        )
        eid = created["id"]
        fetched = client.get("Widget", eid)
        assert fetched["data"]["mass"] == {"value": 5.0, "unit": "g"}

        # Surfaces through query() too.
        item = client.query("Widget").items[0]
        assert item["data"]["mass"] == {"value": 5.0, "unit": "g"}


class TestTyping:
    def test_value_object_not_exposed_as_entity(self, registry):
        exposed = exposed_class_names(registry)
        assert "Widget" in exposed
        assert "Mass" not in exposed
        assert "Quantity" not in exposed

    def test_owner_slot_classifies_as_structured(self, registry):
        model = build_type_model(registry)
        mass_field = next(f for f in model["Widget"].fields if f.name == "mass")
        assert mass_field.kind is SlotKind.STRUCTURED
        assert mass_field.target_class == "Mass"
