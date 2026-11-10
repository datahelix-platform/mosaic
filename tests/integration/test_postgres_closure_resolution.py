"""Closure-aware reference resolution on Postgres (#241, ADR-0014 Decision 1).

Postgres parity for ``tests/core/test_closure_resolution.py`` (SDK) and
``tests/graphql/test_closure_resolution.py`` (GraphQL). Requires a running
PostgreSQL instance (``MOSAIC_DATABASE_URL``), like the rest of
``tests/integration/test_postgres_*``. The GraphQL cases also need the
``graphql`` extra and skip without it (the CI Postgres job installs only
``.[dev,postgres]``).
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.support.closure_fixture import (
    ClosureGraphQLCases,
    ClosureSDKCases,
    build_closure_registry,
    run_graphql,
    seed,
)

POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL") or os.environ.get(
    "HIPPO_DATABASE_URL"
)

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)


@pytest.fixture
def client():
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

    registry = build_closure_registry()
    adapter = PostgresAdapter(
        database_url=POSTGRES_URL,
        schema_registry=registry,
        min_pool_size=1,
        max_pool_size=5,
    )
    c = MosaicClient(storage=adapter, registry=registry)
    seed(c)
    yield c
    with adapter._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM relationships")
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")
    adapter.close()


@pytest.fixture
def gql(client):
    pytest.importorskip("strawberry", reason="graphql extra not installed")
    from mosaic.graphql.resolvers import build_graphql_schema
    from mosaic.graphql.schema_builder import GraphQLTypeBuilder

    registry = client.registry
    builder = GraphQLTypeBuilder(registry).build()
    schema = build_graphql_schema(registry, builder=builder)
    return lambda query: run_graphql(schema, builder, client, query)


class TestPostgresClosureResolution(ClosureSDKCases):
    pass


class TestPostgresGraphQLClosureResolution(ClosureGraphQLCases):
    pass
