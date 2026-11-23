"""Closure-aware reference resolution, GraphQL on SQLite (#241, ADR-0014 Decision 1).

The cases and fixture live in ``tests/support/closure_fixture.py``.
Postgres parity: ``tests/integration/test_postgres_closure_resolution.py``.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.graphql.resolvers import build_graphql_schema
from mosaic.graphql.schema_builder import GraphQLTypeBuilder
from tests.support.closure_fixture import (
    ClosureGraphQLCases,
    build_closure_registry,
    run_graphql,
    seed,
)
from tests.support.kind_check_cases import KindCheckGraphQLCases


@pytest.fixture
def client():
    registry = build_closure_registry()
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(os.path.join(tmpdir, "closure241.db"), schema_registry=registry)
        c = MosaicClient(storage=storage, registry=registry, bypass_validation=True)
        seed(c)
        yield c


@pytest.fixture
def gql(client):
    registry = client.registry
    builder = GraphQLTypeBuilder(registry).build()
    schema = build_graphql_schema(registry, builder=builder)
    return lambda query: run_graphql(schema, builder, client, query)


class TestSQLiteGraphQLClosureResolution(ClosureGraphQLCases):
    pass


class TestSQLiteGraphQLKindCheckedWrites(KindCheckGraphQLCases):
    """#289: per-type lifecycle mutations reject another kind's id."""


class TestUnexposedSubclassConsistency:
    """ADR-0014 Decision 1, #276 WP3: one closure for storage and GraphQL.

    A concrete subclass without its own GraphQL type (today: none can be,
    since only mosaic_core infrastructure and value types are unexposed;
    Decision 5's ``mosaic_anchor: false`` will add real ones) still has its
    rows listed through the parent. The parent's SDK totals, GraphQL list
    and ``entityType`` must agree on that, and ``entityType`` must exist.
    """

    @pytest.fixture
    def gql_hidden(self, client, monkeypatch):
        import mosaic.core.schema_typing as schema_typing

        real = schema_typing.exposed_class_names
        monkeypatch.setattr(
            schema_typing,
            "exposed_class_names",
            lambda registry: [c for c in real(registry) if c != "BankBrain"],
        )
        registry = client.registry
        builder = GraphQLTypeBuilder(registry).build()
        schema = build_graphql_schema(registry, builder=builder)
        assert "BankBrain" not in builder.entities
        return lambda query: run_graphql(schema, builder, client, query)

    def test_parent_list_total_and_entity_type_agree_with_sdk(self, client, gql_hidden):
        sdk = client.query("Process")
        data = gql_hidden("{ processes { total items { id entityType } } processesCount }")
        page = data["processes"]
        assert page["total"] == sdk.total == client.count("Process") == 2
        assert data["processesCount"] == 2
        assert {i["id"]: i["entityType"] for i in page["items"]} == {
            item["id"]: item["entity_type"] for item in sdk.items
        } == {"pr1": "Process", "bb1": "BankBrain"}

    def test_entity_type_field_present_with_only_unexposed_subclasses(self, gql_hidden):
        data = gql_hidden(
            '{ __type(name: "Process") { fields { name type { kind ofType { name } } } } }'
        )
        fields = {f["name"]: f["type"] for f in data["__type"]["fields"]}
        assert fields["entityType"] == {"kind": "NON_NULL", "ofType": {"name": "String"}}


class TestEntityTypeCollisionGuard:
    """#276 WP5: a concrete parent's slot whose GraphQL name would be
    ``entityType`` collides with the system field, however it is spelled."""

    _SCHEMA = """\
id: https://example.org/collide
name: collide
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string
classes:
  Process:
    is_a: Entity
    attributes:
      name:
      {slot}:
  BankBrain:
    is_a: Process
"""

    @pytest.mark.parametrize("slot", ["entity_type", "entityType"])
    def test_colliding_slot_on_concrete_parent_is_rejected(self, slot):
        from mosaic.linkml_bridge import SchemaRegistry

        registry = SchemaRegistry.from_yaml(self._SCHEMA.format(slot=slot))
        with pytest.raises(
            ValueError,
            match=rf"Process\.{slot}: the slot collides with the entityType "
                  r"system field a concrete parent carries",
        ):
            GraphQLTypeBuilder(registry).build()

    def test_same_slot_on_a_leaf_is_fine(self):
        from mosaic.linkml_bridge import SchemaRegistry

        schema = self._SCHEMA.format(slot="entityType").replace(
            "  BankBrain:\n    is_a: Process\n", ""
        )
        builder = GraphQLTypeBuilder(SchemaRegistry.from_yaml(schema)).build()
        assert "Process" in builder.entities
