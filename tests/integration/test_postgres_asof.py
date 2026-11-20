"""As-of reconstruction parity on the PostgreSQL adapter — sec6 §6.8 / ADR-0001
increment 5.

Gated on a live PostgreSQL (``MOSAIC_DATABASE_URL``); skipped otherwise and in
``.[dev]``-only CI, exactly like ``test_postgres_adapter.py``. These mirror the
SQLite as-of tests (tests/core/test_asof_reconstruction.py) and exist so the
Postgres parity is covered whenever a real PostgreSQL is available.

    MOSAIC_DATABASE_URL=postgresql://hippo_test:hippo_test@localhost:5433/hippo_test \
        pytest tests/integration/test_postgres_asof.py
"""

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def adapter(minimal_schema_registry):
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

    a = PostgresAdapter(
        database_url=POSTGRES_URL,
        schema_registry=minimal_schema_registry,
        min_pool_size=1,
        max_pool_size=5,
    )
    yield a
    with a._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM entity_external_ids")
        cur.execute("DELETE FROM relationships")
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")
    a.close()


@pytest.fixture
def client(adapter):
    from mosaic.core.client import MosaicClient

    return MosaicClient(storage=adapter, bypass_validation=True)


def _entity(adapter, name):
    from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

    eid = str(uuid.uuid4())
    adapter.create(
        PostgresEntity(
            id=eid, entity_type="Sample", is_available=True, version=1,
            data={"name": name},
        )
    )
    return eid


def test_query_as_of_past_empty_future_present(client, adapter):
    _entity(adapter, "alpha")
    assert client.query(entity_type="Sample", as_of=PAST).total == 0
    fut = client.query(entity_type="Sample", as_of=FUTURE)
    assert fut.total >= 1
    assert "alpha" in {item["data"]["name"] for item in fut.items}


def test_query_as_of_applies_filters(client, adapter):
    _entity(adapter, "keep")
    _entity(adapter, "drop")
    res = client.query(
        entity_type="Sample",
        filters=[{"field": "name", "value": "keep"}],
        as_of=FUTURE,
    )
    assert {item["data"]["name"] for item in res.items} == {"keep"}


def test_query_as_of_applies_in_filter(client, adapter):
    """IN / set-membership filter (issue #102) on the as-of (Python-side)
    matcher — Postgres parity with the SQLite as-of path."""
    _entity(adapter, "keep1")
    _entity(adapter, "keep2")
    _entity(adapter, "drop")
    res = client.query(
        entity_type="Sample",
        filters=[{"field": "name", "op": "in", "value": ["keep1", "keep2"]}],
        as_of=FUTURE,
    )
    assert {item["data"]["name"] for item in res.items} == {"keep1", "keep2"}


def test_query_as_of_empty_in_filter_matches_nothing(client, adapter):
    _entity(adapter, "alpha")
    res = client.query(
        entity_type="Sample",
        filters=[{"field": "name", "op": "in", "value": []}],
        as_of=FUTURE,
    )
    assert res.total == 0


def test_traverse_as_of(client, adapter):
    a = _entity(adapter, "a")
    b = _entity(adapter, "b")
    client.relationships.relate(a, b, "contains")

    at_future = client.relationships.traverse(a, as_of=FUTURE)
    assert any(e["target_id"] == b for e in at_future)
    # nothing live in the far past (edge added later)
    assert client.relationships.traverse(a, as_of=PAST) == []
