"""Postgres parity for traversal ``columns`` (issue #236).

Runs the exact ``CASES`` the SQLite suite runs
(``tests/core/test_query_spec_compiler.py::TestTraversalColumns``) against
the JSONB adapter: to-one chains, ``count``/``joinIds``/``explode`` on a
to-many hop, a reverse hop (``Author.books``), both two-hop shapes, and a
reverse hop whose members live in a subclass's rows. Requires
``MOSAIC_DATABASE_URL``, like the rest of ``tests/integration/test_postgres_*``.
"""

from __future__ import annotations

import os

import pytest

from tests.support import traversal_fixture

psycopg = pytest.importorskip("psycopg")

POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL") or os.environ.get(
    "HIPPO_DATABASE_URL"
)

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)


@pytest.fixture(scope="module")
def client():
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

    registry = traversal_fixture.registry()
    adapter = PostgresAdapter(
        database_url=POSTGRES_URL,
        schema_registry=registry,
        min_pool_size=1,
        max_pool_size=5,
    )
    c = MosaicClient(storage=adapter, registry=registry)
    traversal_fixture.seed(c)
    yield c
    with adapter._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM relationships")
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")
    adapter.close()


@pytest.mark.parametrize(
    "name, anchor, columns, expected, grain",
    traversal_fixture.CASES,
    ids=[case[0] for case in traversal_fixture.CASES],
)
def test_postgres_traversal_matches_sqlite(client, name, anchor, columns, expected, grain):
    got, got_grain, _rows = traversal_fixture.run_case(client, anchor, columns)
    assert got == expected
    assert got_grain == grain
