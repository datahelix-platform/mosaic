"""Tests for the QuerySpec parser/validator (ADR-0009 / issue #183,
validator-only increment).

Mirrors Aperture's ``querySpec.test.ts`` intentions (shape guard + total,
introspection-driven validation) on the Mosaic/server side, against the
capability manifest built in issue #181.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mosaic.core.query_spec import (
    MAX_CRITERIA_DEPTH,
    QuerySpecShapeError,
    parse_query_spec,
    validate_query_spec,
)
from mosaic.core.schema_typing import build_capability_manifest
from mosaic.linkml_bridge import SchemaRegistry

_FIXTURE_SCHEMA = (
    Path(__file__).parents[1] / "fixtures" / "schemas" / "sample_schema.yaml"
)


def _manifest():
    registry = SchemaRegistry.from_path(_FIXTURE_SCHEMA)
    return build_capability_manifest(registry)


# -- parse_query_spec: structural, schema-independent ------------------------


def test_parses_minimal_valid_spec():
    spec = parse_query_spec({"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []})
    assert spec.anchor == "Sample"
    assert spec.mode == "AND"
    assert spec.criteria == ()
    assert spec.as_of is None
    assert spec.sort == ()


def test_parses_field_condition():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
        }
    )
    assert len(spec.criteria) == 1
    cond = spec.criteria[0]
    assert cond.slot == "volume_ml"
    assert cond.op == "gt"
    assert cond.value == 1.0


def test_parses_related_condition():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [
                {
                    "kind": "related",
                    "edge": "project_id",
                    "quantifier": "some",
                    "criteria": [{"kind": "field", "slot": "name", "op": "eq", "value": "P1"}],
                }
            ],
        }
    )
    related = spec.criteria[0]
    assert related.edge == "project_id"
    assert related.quantifier == "some"
    assert related.criteria[0].slot == "name"


def test_parses_nested_group_within_depth_cap():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [
                {
                    "kind": "group",
                    "mode": "OR",
                    "criteria": [
                        {"kind": "field", "slot": "name", "op": "eq", "value": "a"},
                        {"kind": "field", "slot": "name", "op": "eq", "value": "b"},
                    ],
                }
            ],
        }
    )
    group = spec.criteria[0]
    assert group.mode == "OR"
    assert len(group.criteria) == 2


def test_rejects_missing_v():
    with pytest.raises(QuerySpecShapeError) as exc:
        parse_query_spec({"anchor": "Sample", "mode": "AND", "criteria": []})
    assert exc.value.code == "INVALID_QUERYSPEC_SHAPE"


def test_rejects_bad_mode():
    with pytest.raises(QuerySpecShapeError):
        parse_query_spec({"v": 1, "anchor": "Sample", "mode": "XOR", "criteria": []})


_BASE = {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []}


def test_parses_anchor_columns():
    spec = parse_query_spec({**_BASE, "columns": [{"path": ["name"]}, {"path": ["volume_ml"]}]})
    assert [c.slot for c in spec.columns] == ["name", "volume_ml"]


def test_absent_or_null_columns_means_no_selection():
    assert parse_query_spec(_BASE).columns == ()
    assert parse_query_spec({**_BASE, "columns": None}).columns == ()
    assert parse_query_spec({**_BASE, "columns": []}).columns == ()


def test_multi_hop_column_is_still_rejected_naming_the_hop():
    with pytest.raises(QuerySpecShapeError) as exc:
        parse_query_spec({**_BASE, "columns": [{"path": ["donor", "name"]}]})
    assert exc.value.code == "COLUMNS_NOT_SUPPORTED"
    assert "donor" in str(exc.value) and "name" in str(exc.value)
    assert exc.value.path == "$.columns[0].path"


@pytest.mark.parametrize("key", ["aggregate", "explode", "mode", "many"])
def test_grain_choice_is_still_rejected(key):
    with pytest.raises(QuerySpecShapeError) as exc:
        parse_query_spec({**_BASE, "columns": [{"path": ["name"], key: True}]})
    assert exc.value.code == "COLUMNS_NOT_SUPPORTED"


@pytest.mark.parametrize(
    "columns",
    ["name", [{"slot": "name"}], [{"path": []}], [{"path": "name"}], [{"path": ["name"], "label": "x"}], ["name"]],
)
def test_malformed_columns_are_shape_errors(columns):
    with pytest.raises(QuerySpecShapeError) as exc:
        parse_query_spec({**_BASE, "columns": columns})
    assert exc.value.code == "INVALID_QUERYSPEC_SHAPE"


def test_validates_columns_against_the_manifest():
    ok = parse_query_spec({**_BASE, "columns": [{"path": ["name"]}]})
    assert validate_query_spec(ok, _manifest()).valid
    bad = parse_query_spec({**_BASE, "columns": [{"path": ["name"]}, {"path": ["nope"]}]})
    errors = validate_query_spec(bad, _manifest()).errors
    assert [(e.code, e.path) for e in errors] == [("UNKNOWN_SLOT", "$.columns[1].path")]
    assert "nope" in errors[0].message


def test_rejects_depth_beyond_cap():
    # Build a chain of nested groups one deeper than MAX_CRITERIA_DEPTH.
    innermost = {"kind": "field", "slot": "name", "op": "eq", "value": "x"}
    node = innermost
    for _ in range(MAX_CRITERIA_DEPTH + 1):
        node = {"kind": "group", "mode": "AND", "criteria": [node]}
    with pytest.raises(QuerySpecShapeError) as exc:
        parse_query_spec({"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [node]})
    assert exc.value.code == "DEPTH_EXCEEDED"


def test_accepts_as_of_and_sort():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [],
            "asOf": "2026-01-01T00:00:00Z",
            "sort": [{"slot": "volume_ml", "direction": "desc"}],
        }
    )
    assert spec.as_of == "2026-01-01T00:00:00Z"
    assert spec.sort[0].slot == "volume_ml"
    assert spec.sort[0].direction == "desc"


# -- validate_query_spec: capability-manifest-driven -------------------------


def test_valid_spec_has_no_errors():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert result.valid is True
    assert result.errors == ()


def test_unknown_anchor_is_reported():
    spec = parse_query_spec({"v": 1, "anchor": "Nonexistent", "mode": "AND", "criteria": []})
    result = validate_query_spec(spec, _manifest())
    assert result.valid is False
    assert result.errors[0].code == "UNKNOWN_ANCHOR"


def test_unknown_slot_is_reported():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [{"kind": "field", "slot": "nope", "op": "eq", "value": 1}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert result.errors[0].code == "UNKNOWN_SLOT"


def test_reference_field_with_filter_op_is_rejected_in_favor_of_related_condition():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [{"kind": "field", "slot": "project_id", "op": "eq", "value": "x"}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    err = result.errors[0]
    assert err.code == "UNSUPPORTED_OP"
    assert "RelatedCondition" in err.message


def test_related_condition_on_exposed_target_is_valid():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [
                {
                    "kind": "related",
                    "edge": "project_id",
                    "quantifier": "some",
                    "criteria": [{"kind": "field", "slot": "name", "op": "eq", "value": "P1"}],
                }
            ],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert result.valid is True


def test_unknown_edge_is_reported():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [{"kind": "related", "edge": "nope", "quantifier": "some", "criteria": []}],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert result.errors[0].code == "UNKNOWN_EDGE"


def test_field_condition_used_as_edge_is_reported_as_unknown_edge():
    # `name` is a real field but not a REFERENCE — cannot be used as an edge.
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [{"kind": "related", "edge": "name", "quantifier": "some", "criteria": []}],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert result.errors[0].code == "UNKNOWN_EDGE"


def test_invalid_enum_value_is_reported_with_valid_set():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [{"kind": "field", "slot": "status", "op": "eq", "value": "not-a-status"}],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    err = result.errors[0]
    assert err.code == "INVALID_ENUM_VALUE"
    assert "active" in err.message


def test_valid_enum_value_passes():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [{"kind": "field", "slot": "status", "op": "eq", "value": "active"}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert result.valid is True


def test_in_op_requires_list_value():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [{"kind": "field", "slot": "status", "op": "in", "value": "active"}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert result.errors[0].code == "INVALID_VALUE_TYPE"


def test_in_op_with_bad_value_on_enum_field_reports_only_value_type_error():
    # Regression: iterating a non-list `in` value character-by-character
    # used to also emit a spurious INVALID_ENUM_VALUE alongside the real
    # INVALID_VALUE_TYPE error.
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [{"kind": "field", "slot": "status", "op": "in", "value": "active"}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert [e.code for e in result.errors] == ["INVALID_VALUE_TYPE"]


def test_multi_column_sort_is_rejected():
    # Regression: Mosaic's query surface takes one order_by/order_dir pair
    # (mirrors GraphQL's single-valued orderBy) -- a second sort field used
    # to validate clean and then silently lose the second column at
    # execute time (issue #129's "loud over wrong" rule).
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "criteria": [],
            "sort": [{"slot": "volume_ml"}, {"slot": "name"}],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert result.errors[0].code == "MULTI_COLUMN_SORT_UNSUPPORTED"


def test_as_of_with_related_condition_anywhere_is_rejected():
    spec = parse_query_spec(
        {
            "v": 1,
            "anchor": "Sample",
            "mode": "AND",
            "asOf": "2026-01-01T00:00:00Z",
            "criteria": [
                {
                    "kind": "group",
                    "mode": "AND",
                    "criteria": [
                        {
                            "kind": "related",
                            "edge": "project_id",
                            "quantifier": "some",
                            "criteria": [],
                        }
                    ],
                }
            ],
        }
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert any(e.code == "ASOF_RELATIONSHIP_FILTER_UNSUPPORTED" for e in result.errors)


def test_as_of_without_related_condition_is_fine():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "asOf": "2026-01-01T00:00:00Z", "criteria": []}
    )
    result = validate_query_spec(spec, _manifest())
    assert result.valid is True


def test_sort_on_orderable_field_is_valid():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [], "sort": [{"slot": "volume_ml"}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert result.valid is True


def test_sort_on_reference_field_is_rejected():
    spec = parse_query_spec(
        {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": [], "sort": [{"slot": "project_id"}]}
    )
    result = validate_query_spec(spec, _manifest())
    assert not result.valid
    assert result.errors[0].code == "UNORDERABLE_SORT_FIELD"
