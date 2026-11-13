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
