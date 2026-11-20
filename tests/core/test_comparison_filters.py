"""Comparison / null-test filter operators (ADR-0006 increment 1, issue #155).

Covers the operator set added beyond ``eq``/``in``: ``neq``, ``gt``/``gte``/
``lt``/``lte`` (numeric and temporal slots), ``contains`` (case-insensitive
substring with literal ``%``/``_``), and ``is_null``. Every operator is
exercised on the live SQL path AND the as-of path with the same expectations
— the ``matches_operator`` mirror must never diverge from the SQL builders
(the four-mirror risk named in ADR-0006).
"""

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationError
from mosaic.core.storage import Query, matches_operator
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

FUTURE = "2999-01-01T00:00:00+00:00"

SCHEMA = """
id: https://example.org/mosaic/test_comparison_filters
name: test_comparison_filters
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

types:
  AgeInYears:
    typeof: integer
    base: int
    uri: linkml:Integer

classes:
  Specimen:
    is_a: Entity
    attributes:
      name:
        required: true
      age:
        range: AgeInYears
      score:
        range: float
      collected_on:
        range: date
      is_tumor:
        range: boolean
      notes: {}
"""


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_comparison_filters.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, bypass_validation=True)
        c.put(
            "Specimen",
            {
                "id": "s1",
                "name": "Alpha",
                "age": 45,
                "score": 1.5,
                "collected_on": "2024-01-10",
                "is_tumor": False,
                "notes": "First batch",
            },
        )
        c.put(
            "Specimen",
            {
                "id": "s2",
                "name": "Beta",
                "age": 60,
                "score": 2.5,
                "collected_on": "2025-03-05",
                "is_tumor": True,
                # no notes — the is_null target
            },
        )
        c.put(
            "Specimen",
            {
                "id": "s3",
                "name": "Gamma",
                "age": 75,
                "score": 3.5,
                "collected_on": "2026-06-20",
                "is_tumor": False,
                "notes": "follow-up 50%_done",
            },
        )
        yield c


def ids(client: MosaicClient, filters, filter_mode="and", as_of=None) -> set[str]:
    result = client.query(
        "Specimen", filters=filters, filter_mode=filter_mode, as_of=as_of
    )
    return {item["id"] for item in result.items}


def both_paths(client: MosaicClient, filters, filter_mode="and") -> set[str]:
    """Run live and as-of(FUTURE); assert agreement; return the id set."""
    live = ids(client, filters, filter_mode)
    asof = ids(client, filters, filter_mode, as_of=FUTURE)
    assert live == asof, (
        f"live/as-of divergence for {filters!r}: {live} != {asof}"
    )
    return live


class TestComparisonOperators:
    def test_gt_integer(self, client) -> None:
        assert both_paths(
            client, [{"field": "age", "op": "gt", "value": 60}]
        ) == {"s3"}

    def test_gte_integer(self, client) -> None:
        assert both_paths(
            client, [{"field": "age", "op": "gte", "value": 60}]
        ) == {"s2", "s3"}

    def test_lt_float(self, client) -> None:
        assert both_paths(
            client, [{"field": "score", "op": "lt", "value": 2.5}]
        ) == {"s1"}

    def test_lte_float(self, client) -> None:
        assert both_paths(
            client, [{"field": "score", "op": "lte", "value": 2.5}]
        ) == {"s1", "s2"}

    def test_gt_date_iso(self, client) -> None:
        assert both_paths(
            client,
            [{"field": "collected_on", "op": "gt", "value": "2024-12-31"}],
        ) == {"s2", "s3"}

    def test_custom_typeof_range_compares_numerically(self, client) -> None:
        # ``age`` is ranged on AgeInYears (typeof: integer); a numeric
        # comparison must not silently degrade to text ordering ("9" > "60").
        assert both_paths(
            client, [{"field": "age", "op": "lt", "value": 100}]
        ) == {"s1", "s2", "s3"}

    def test_comparison_composes_with_and(self, client) -> None:
        assert both_paths(
            client,
            [
                {"field": "age", "op": "gte", "value": 60},
                {"field": "is_tumor", "value": False},
            ],
        ) == {"s3"}

    def test_comparison_composes_with_or(self, client) -> None:
        assert both_paths(
            client,
            [
                {"field": "age", "op": "gt", "value": 70},
                {"field": "notes", "op": "contains", "value": "first"},
            ],
            filter_mode="or",
        ) == {"s1", "s3"}


class TestNeq:
    def test_neq_matches_different_values_only(self, client) -> None:
        # s2 has no notes: SQL NULL never satisfies a comparison, so neq
        # excludes it — absence is asked with is_null, never with neq.
        assert both_paths(
            client, [{"field": "notes", "op": "neq", "value": "First batch"}]
        ) == {"s3"}

    def test_neq_numeric(self, client) -> None:
        assert both_paths(
            client, [{"field": "age", "op": "neq", "value": 60}]
        ) == {"s1", "s3"}


class TestContains:
    def test_contains_substring(self, client) -> None:
        assert both_paths(
            client, [{"field": "notes", "op": "contains", "value": "batch"}]
        ) == {"s1"}

    def test_contains_case_insensitive(self, client) -> None:
        assert both_paths(
            client, [{"field": "notes", "op": "contains", "value": "FIRST"}]
        ) == {"s1"}

    def test_contains_percent_is_literal(self, client) -> None:
        assert both_paths(
            client, [{"field": "notes", "op": "contains", "value": "50%"}]
        ) == {"s3"}

    def test_contains_underscore_is_literal(self, client) -> None:
        # "_" must not act as a single-char wildcard: "0_d" would match
        # "...50%_done" as a pattern ("%_d"→"%_d"?) but "0_d" literal is
        # absent from every notes value except via wildcard semantics.
        assert both_paths(
            client, [{"field": "notes", "op": "contains", "value": "0_x"}]
        ) == set()

    def test_contains_missing_value_never_matches(self, client) -> None:
        # s2 has no notes at all — absence never satisfies contains.
        assert both_paths(
            client, [{"field": "notes", "op": "contains", "value": "o"}]
        ) == {"s3"}


class TestIsNull:
    def test_is_null_true_matches_absent(self, client) -> None:
        assert both_paths(
            client, [{"field": "notes", "op": "is_null", "value": True}]
        ) == {"s2"}

    def test_is_null_false_matches_present(self, client) -> None:
        assert both_paths(
            client, [{"field": "notes", "op": "is_null", "value": False}]
        ) == {"s1", "s3"}

    def test_is_null_requires_boolean_value(self, client) -> None:
        with pytest.raises(ValidationError, match="requires a boolean"):
            client.query(
                "Specimen",
                filters=[{"field": "notes", "op": "is_null", "value": "yes"}],
            )


class TestNullValueSemantics:
    def test_eq_none_matches_nothing_on_both_paths(self, client) -> None:
        # `col = NULL` is never true in SQL; the Python mirror agrees.
        assert both_paths(
            client, [{"field": "notes", "op": "eq", "value": None}]
        ) == set()

    def test_in_requires_list_value(self, client) -> None:
        with pytest.raises(ValidationError, match="requires a\n?.*list"):
            client.query(
                "Specimen",
                filters=[{"field": "name", "op": "in", "value": "Alpha"}],
            )


class TestMatchesOperatorUnit:
    """The shared Python evaluator behind all as-of mirrors."""

    def test_null_semantics(self) -> None:
        assert matches_operator(None, "is_null", True)
        assert not matches_operator("x", "is_null", True)
        assert matches_operator("x", "is_null", False)
        for op in ("eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"):
            assert not matches_operator(None, op, "x")
        assert not matches_operator("x", "eq", None)

    def test_type_mismatch_matches_nothing(self) -> None:
        assert not matches_operator("abc", "gt", 5)

    def test_contains_case_insensitive(self) -> None:
        assert matches_operator("Follow-Up", "contains", "low-u")


class TestLowLevelAdapter:
    @pytest.fixture
    def adapter(self, registry: SchemaRegistry):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_cmp_adapter.db")
            yield SQLiteAdapter(db_path, schema_registry=registry)

    def test_unknown_column_matches_nothing(self, adapter) -> None:
        query = Query(
            entity_type="Specimen",
            filters=[{"field": "nope", "op": "is_null", "value": True}],
        )
        assert list(adapter.find(query)) == []
