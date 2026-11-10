"""Tests for compile_query_spec (ADR-0009 / issue #183's execute half).

Uses an inline schema carrying both a to-one and a to-many reference
(Sample.donor_id, Study.sample_ids) so both relationship-predicate
compile paths (nested edge vs. quantifier) are exercised — the shared
``sample_schema.yaml`` fixture used elsewhere in ``tests/core`` has only
a to-one reference.
"""

from __future__ import annotations

import pytest

from mosaic.core.query_spec import parse_query_spec, validate_query_spec
from mosaic.core.query_spec_compiler import compile_query_spec
from mosaic.core.schema_typing import build_capability_manifest
from mosaic.linkml_bridge import SchemaRegistry
from tests.support import traversal_fixture

_SCHEMA = """
id: https://example.org/hippo/test_compiler
name: test_compiler
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
      samples:                  # virtual reverse edge over Sample.donor_id (ADR-0011)
        range: Sample
        multivalued: true
        inverse: donor_id

  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      donor_id:
        range: Donor
      volume_ml:
        range: float
      status:
        range: SampleStatus

  Study:
    is_a: Entity
    attributes:
      title:
        required: true
      sample_ids:
        range: Sample
        multivalued: true

enums:
  SampleStatus:
    permissible_values:
      active: {}
      archived: {}
"""


def _manifest():
    registry = SchemaRegistry.from_yaml(_SCHEMA)
    return build_capability_manifest(registry)


def _compile(raw: dict):
    manifest = _manifest()
    spec = parse_query_spec(raw)
    result = validate_query_spec(spec, manifest)
    assert result.valid, result.errors
    return compile_query_spec(spec, manifest)


def test_no_criteria_compiles_to_no_where():
    compiled = _compile({"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []})
    assert compiled.entity_type == "Sample"
    assert compiled.where is None


def test_single_field_condition_compiles_to_a_bare_leaf():
    compiled = _compile(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
        }
    )
    assert compiled.where == {"field": "volume_ml", "op": "gt", "value": 1.0}


def test_multiple_conditions_compile_to_the_spec_mode_combinator():
    compiled = _compile(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "OR",
            "criteria": [
                {"kind": "field", "slot": "status", "op": "eq", "value": "active"},
                {"kind": "field", "slot": "status", "op": "eq", "value": "archived"},
            ],
        }
    )
    assert compiled.where == {
        "or": [
            {"field": "status", "op": "eq", "value": "active"},
            {"field": "status", "op": "eq", "value": "archived"},
        ]
    }


def test_nested_group_compiles_to_its_own_combinator():
    compiled = _compile(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [
                {
                    "kind": "group",
                    "mode": "OR",
                    "criteria": [
                        {"kind": "field", "slot": "status", "op": "eq", "value": "active"},
                        {"kind": "field", "slot": "status", "op": "eq", "value": "archived"},
                    ],
                }
            ],
        }
    )
    assert compiled.where == {
        "or": [
            {"field": "status", "op": "eq", "value": "active"},
            {"field": "status", "op": "eq", "value": "archived"},
        ]
    }


class TestToOneRelatedCondition:
    def test_some_compiles_to_the_bare_edge_node(self):
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Sample",
                "mode": "AND",
                "criteria": [
                    {
                        "kind": "related",
                        "edge": "donor_id",
                        "quantifier": "some",
                        "criteria": [{"kind": "field", "slot": "name", "op": "eq", "value": "D1"}],
                    }
                ],
            }
        )
        assert compiled.where == {
            "edge": "donor_id",
            "where": {"field": "name", "op": "eq", "value": "D1"},
        }

    def test_none_compiles_to_a_negated_edge_node(self):
        # normalize_where has no quantifier for a to-one edge — bare
        # {"edge","where"} already means "some" (target exists and
        # matches), so "none" is exactly its negation.
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Sample",
                "mode": "AND",
                "criteria": [
                    {
                        "kind": "related",
                        "edge": "donor_id",
                        "quantifier": "none",
                        "criteria": [{"kind": "field", "slot": "name", "op": "eq", "value": "D1"}],
                    }
                ],
            }
        )
        assert compiled.where == {
            "not": {"edge": "donor_id", "where": {"field": "name", "op": "eq", "value": "D1"}}
        }

    def test_empty_criteria_fills_with_an_identifier_existence_check(self):
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Sample",
                "mode": "AND",
                "criteria": [
                    {"kind": "related", "edge": "donor_id", "quantifier": "some", "criteria": []}
                ],
            }
        )
        assert compiled.where == {
            "edge": "donor_id",
            "where": {"field": "id", "op": "is_null", "value": False},
        }


class TestToManyRelatedCondition:
    def test_some_carries_the_quantifier_explicitly(self):
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Study",
                "mode": "AND",
                "criteria": [
                    {
                        "kind": "related",
                        "edge": "sample_ids",
                        "quantifier": "some",
                        "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
                    }
                ],
            }
        )
        assert compiled.where == {
            "edge": "sample_ids",
            "quantifier": "some",
            "where": {"field": "volume_ml", "op": "gt", "value": 1.0},
        }

    def test_none_carries_the_quantifier_explicitly_not_negated(self):
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Study",
                "mode": "AND",
                "criteria": [
                    {
                        "kind": "related",
                        "edge": "sample_ids",
                        "quantifier": "none",
                        "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
                    }
                ],
            }
        )
        assert compiled.where == {
            "edge": "sample_ids",
            "quantifier": "none",
            "where": {"field": "volume_ml", "op": "gt", "value": 1.0},
        }


def test_as_of_and_order_by_pass_through():
    compiled = _compile(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [],
            "asOf": "2026-01-01T00:00:00Z",
            "sort": [{"slot": "volume_ml", "direction": "desc"}],
        }
    )
    assert compiled.as_of == "2026-01-01T00:00:00Z"
    assert compiled.order_by == "volume_ml"
    assert compiled.order_dir == "desc"


def test_no_sort_defaults_to_none_asc():
    compiled = _compile({"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []})
    assert compiled.order_by is None
    assert compiled.order_dir == "asc"


def test_compiled_where_is_actually_accepted_by_the_real_storage_layer():
    """The one test that proves this against ground truth rather than
    self-consistency with the compiler's own assumptions: feed a compiled
    tree covering every shape (nested group, to-one some/none, to-many
    some/none) into the real `normalize_where` and confirm it round-trips
    with no ValidationError."""
    from mosaic.core.storage import normalize_where

    compiled = _compile(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [
                {
                    "kind": "group",
                    "mode": "OR",
                    "criteria": [
                        {"kind": "field", "slot": "status", "op": "eq", "value": "active"},
                        {"kind": "field", "slot": "status", "op": "eq", "value": "archived"},
                    ],
                },
                {
                    "kind": "related",
                    "edge": "donor_id",
                    "quantifier": "none",
                    "criteria": [{"kind": "field", "slot": "name", "op": "eq", "value": "D1"}],
                },
            ],
        }
    )
    assert normalize_where(compiled.where) == compiled.where

    study_compiled = _compile(
        {
            "v": 1,
            "anchor": "Study",
            "mode": "AND",
            "criteria": [
                {
                    "kind": "related",
                    "edge": "sample_ids",
                    "quantifier": "some",
                    "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
                }
            ],
        }
    )
    assert normalize_where(study_compiled.where) == study_compiled.where


class TestInverseEdges:
    """ADR-0011: a reverse edge declared with LinkML ``inverse`` compiles
    exactly like any other to-many edge — the validator and compiler are
    manifest-driven and need no reverse-specific path (issue #204)."""

    def test_reverse_edge_validates_and_compiles_to_the_quantified_node(self):
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Donor",
                "mode": "AND",
                "criteria": [
                    {
                        "kind": "related",
                        "edge": "samples",
                        "quantifier": "some",
                        "criteria": [{"kind": "field", "slot": "status", "op": "eq", "value": "active"}],
                    }
                ],
            }
        )
        assert compiled.where == {
            "edge": "samples",
            "quantifier": "some",
            "where": {"field": "status", "op": "eq", "value": "active"},
        }

    def test_reverse_edge_executes_against_real_storage(self, tmp_path):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

        registry = SchemaRegistry.from_yaml(_SCHEMA)
        client = MosaicClient(
            storage=SQLiteAdapter(str(tmp_path / "c.db"), schema_registry=registry),
            registry=registry,
        )
        client.put("Donor", {"id": "d1", "name": "D1"})
        client.put("Donor", {"id": "d2", "name": "D2"})
        client.put("Sample", {"id": "s1", "name": "S1", "donor_id": "d1", "status": "active"})
        client.put("Sample", {"id": "s2", "name": "S2", "donor_id": "d2", "status": "archived"})
        compiled = _compile(
            {
                "v": 1,
                "anchor": "Donor",
                "mode": "AND",
                "criteria": [
                    {
                        "kind": "related",
                        "edge": "samples",
                        "quantifier": "some",
                        "criteria": [{"kind": "field", "slot": "status", "op": "eq", "value": "active"}],
                    }
                ],
            }
        )
        page = client.query(entity_type=compiled.entity_type, where=compiled.where)
        assert [i["id"] for i in page.items] == ["d1"]


# -- traversal columns (issue #236) -------------------------------------------


class TestTraversalColumns:
    """Compilation of traversal paths and their server-side hydration on
    SQLite. ``tests/integration/test_postgres_query_spec_traversal.py``
    runs the identical ``CASES`` against Postgres."""

    @pytest.fixture
    def tclient(self, tmp_path):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

        registry = traversal_fixture.registry()
        client = MosaicClient(
            storage=SQLiteAdapter(str(tmp_path / "t.db"), schema_registry=registry), registry=registry
        )
        traversal_fixture.seed(client)
        return client

    def test_compiles_hops_with_their_cardinality(self):
        from mosaic.core.query_spec_compiler import Hop

        manifest = build_capability_manifest(traversal_fixture.registry())
        spec = parse_query_spec(
            {
                "v": 1,
                "anchor": "Sample",
                "mode": "AND",
                "criteria": [],
                "columns": [
                    {"path": ["name"]},
                    {"path": ["donor", "age_at_death"]},
                    {"path": ["aliquots", "container", "kind"], "mode": "explode"},
                ],
            }
        )
        assert validate_query_spec(spec, manifest).valid
        compiled = compile_query_spec(spec, manifest)
        # Hop slots must be read before trimming, so no `fields` pushdown.
        assert compiled.fields is None
        t = compiled.traversal
        assert t.anchor_fields == ("name",)
        assert [(c.key, c.hops, c.leaf, c.mode) for c in t.columns] == [
            ("name", (), "name", None),
            ("donor.age_at_death", (Hop("donor", False),), "age_at_death", None),
            ("aliquots.container.kind", (Hop("aliquots", True), Hop("container", False)), "kind", "explode"),
        ]
        assert t.explode.link == ("aliquots",)
        assert t.columns[1].link is None

    def test_anchor_only_columns_compile_without_a_traversal(self):
        compiled = _compile(
            {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [], "columns": [{"path": ["name"]}]}
        )
        assert compiled.fields == ("name",)
        assert compiled.traversal is None

    @pytest.mark.parametrize(
        "name, anchor, columns, expected, grain",
        traversal_fixture.CASES,
        ids=[case[0] for case in traversal_fixture.CASES],
    )
    def test_hydrates(self, tclient, name, anchor, columns, expected, grain):
        got, got_grain, rows = traversal_fixture.run_case(tclient, anchor, columns)
        assert got == expected
        assert got_grain == grain
        anchor_slots = {c["path"][0] for c in columns if len(c["path"]) == 1}
        for row in rows:
            # `data` is trimmed to anchor-owned columns; the envelope stays.
            assert set(row["data"]) == anchor_slots
            assert row["entity_type"] == anchor

    def test_hydration_is_batched_per_hop_not_per_anchor(self, tclient, monkeypatch):
        calls: list[str] = []
        real_query, real_resolve = tclient.query, tclient.resolve_types

        def query(*a, **kw):
            calls.append(f"query:{kw.get('entity_type')}")
            return real_query(*a, **kw)

        def resolve(ids):
            calls.append("resolve")
            return real_resolve(ids)

        monkeypatch.setattr(tclient, "query", query)
        monkeypatch.setattr(tclient, "resolve_types", resolve)
        traversal_fixture.run_case(
            tclient,
            "Sample",
            [
                {"path": ["donor", "age_at_death"]},
                {"path": ["donor", "samples", "name"], "mode": "count"},
                {"path": ["aliquots", "container", "kind"], "mode": "joinIds"},
            ],
        )
        # 1 anchor page; hop 1 = one resolve + one query per concrete type
        # (Donor, Aliquot); hop 2 = one resolve + (Sample, SolidSample,
        # Container). Independent of how many anchors the page holds.
        assert calls[0] == "query:Sample"
        assert calls.count("resolve") == 2
        assert sorted(calls[1:]) == sorted(
            ["resolve", "query:Donor", "query:Aliquot", "resolve", "query:Sample", "query:SolidSample", "query:Container"]
        )
