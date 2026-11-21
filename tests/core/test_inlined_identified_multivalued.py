"""``inlined_as_list`` multivalued slot ranged on an identified class (issue #121).

A multivalued slot declared ``inlined_as_list: true`` whose range class *also*
declares an identifier was misclassified as a multivalued reference by
``SchemaRegistry.multivalued_reference_slots`` — it ignored ``slot.inlined``/
``slot.inlined_as_list`` entirely. The SQLite adapter then persisted it via
the id-normalization relationship path (``str(v)`` on each element), so an
inline object like ``{"pid": "P1", "pname": "a"}`` round-tripped as the
stringified Python dict ``"{'pid': 'P1', 'pname': 'a'}"`` instead of a
structured object — silent data corruption with ``errors=0``.

Identifier-less inline value objects (``Mass``/``Volume``/``Concentration``,
covered by ``test_value_type_inlining.py``) were unaffected; the bug was
specific to the intersection of multivalued + inlined + identified range.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mosaic
from mosaic.core.storage.ddl_generator import DDLGenerator
from mosaic.linkml_bridge import SchemaRegistry

# Minimal reproduction from issue #121: Param has its own identifier (pid)
# but is embedded inline as a list on Widget.params.
_SCHEMA = """\
id: https://example.org/vo
name: vo
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string
classes:
  Param:
    attributes:
      pid:
        identifier: true
      pname:
        range: string
      pvalue:
        range: string
  Widget:
    is_a: Entity
    attributes:
      params:
        range: Param
        multivalued: true
        inlined_as_list: true
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(_SCHEMA)


@pytest.fixture
def client(tmp_path: Path):
    schema = tmp_path / "schema.yaml"
    schema.write_text(_SCHEMA)
    return mosaic.client_for_schema(schema, database_url=str(tmp_path / "h.db"))


class TestClassification:
    def test_inlined_slot_excluded_from_multivalued_references(self, registry):
        refs = registry.multivalued_reference_slots("Widget")
        assert ("params", "Param") not in refs


class TestDDL:
    def test_owner_slot_is_text_not_linktable(self, registry):
        ddl = DDLGenerator().generate(registry)
        conn = sqlite3.connect(":memory:")
        for stmt in ddl:
            conn.execute(stmt)
        cols = {
            row[1]: (row[2] or "").upper()
            for row in conn.execute("PRAGMA table_info('Widget')")
        }
        assert cols.get("params") == "TEXT"


class TestRoundTrip:
    def test_inline_objects_round_trip_as_structured_dicts(self, client):
        client.put(
            "Widget",
            {"params": [{"pid": "P1", "pname": "a", "pvalue": "x"}]},
            entity_id="W1",
        )
        fetched = client.get("Widget", "W1")
        params = fetched["data"]["params"]
        assert params == [{"pid": "P1", "pname": "a", "pvalue": "x"}]
        assert isinstance(params[0], dict)
