"""REST GET /search — paginated envelope + field-filter composition
(issue #157). Mirrors the entity list endpoint's conventions: the same
``{items, total, limit, offset}`` envelope and the same arbitrary
entity-field query params (sec4 §4.3)."""

import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.serve.routers import health, search

from tests.core.test_search_composition import SCHEMA

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def hippo_client(registry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_search_endpoint.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        c = MosaicClient(storage=storage, registry=registry)
        conn = sqlite3.connect(db_path)
        for tables in c._fts_table_metadata.values():
            for meta in tables:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {meta.table_name} "
                    "USING fts5(entity_id, content)"
                )
        conn.commit()
        conn.close()
        c.put("Note", {"id": "n1", "name": "One", "tissue": "brain",
                       "priority": 1, "body": "cortex cortex cortex alpha"})
        c.put("Note", {"id": "n2", "name": "Two", "tissue": "brain",
                       "priority": 2, "body": "cortex cortex beta filler"})
        c.put("Note", {"id": "n3", "name": "Three", "tissue": "liver",
                       "priority": 3, "body": "cortex gamma delta filler"})
        yield c


@pytest.fixture
def client(hippo_client):
    app = create_app(
        routers=[health.router, search.router], mosaic_client=hippo_client
    )
    return TestClient(app)


def test_envelope_in_rank_order(client):
    resp = client.get(
        "/search", params={"entity_type": "Note", "q": "cortex"}, headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [i["id"] for i in body["items"]] == ["n1", "n2", "n3"]
    assert body["total"] == 3
    assert body["limit"] == 100 and body["offset"] == 0


def test_offset_ge_limit_regression(client):
    # The old endpoint sliced results[offset : offset + limit] over a
    # limit-bounded fetch, so offset >= limit always returned [].
    resp = client.get(
        "/search",
        params={"entity_type": "Note", "q": "cortex", "limit": 1, "offset": 2},
        headers=AUTH,
    )
    body = resp.json()
    assert [i["id"] for i in body["items"]] == ["n3"]
    assert body["total"] == 3


def test_field_filters_compose(client):
    resp = client.get(
        "/search",
        params={"entity_type": "Note", "q": "cortex", "tissue": "brain"},
        headers=AUTH,
    )
    body = resp.json()
    assert [i["id"] for i in body["items"]] == ["n1", "n2"]
    assert body["total"] == 2


def test_order_by_overrides_rank(client):
    resp = client.get(
        "/search",
        params={
            "entity_type": "Note", "q": "cortex",
            "order_by": "priority", "order_dir": "desc",
        },
        headers=AUTH,
    )
    body = resp.json()
    assert [i["id"] for i in body["items"]] == ["n3", "n2", "n1"]
