"""REST `?as_of=` on the entity list endpoint — sec6 §6.8 / ADR-0001 increment 4.

Uses timestamp-robust past/future boundaries: as-of far in the past yields the
empty set (nothing created yet); as-of far in the future yields the current set.
This proves the parameter is threaded end-to-end (REST → client.query → as-of
reconstruction) without depending on fragile sub-second timing.
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry
from mosaic.serve.routers import entity, health, ingest

AUTH = {"Authorization": "Bearer test-token"}
PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_asof_endpoint.db")


@pytest.fixture
def client(db_path):
    storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
    hippo_client = MosaicClient(storage=storage, bypass_validation=True)
    app = create_app(
        routers=[health.router, entity.router, ingest.router],
        mosaic_client=hippo_client,
    )
    return TestClient(app)


def _create(client, entity_id, name):
    resp = client.post(
        "/ingest",
        json={"entity_type": "Sample", "data": {"id": entity_id, "name": name}},
        headers=AUTH,
    )
    assert resp.status_code == 200


def test_list_entities_as_of_past_is_empty(client):
    _create(client, "s1", "Alpha")
    resp = client.get(
        "/entities",
        params={"entity_type": "Sample", "as_of": PAST},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_list_entities_as_of_future_includes_current(client):
    _create(client, "s2", "Beta")
    resp = client.get(
        "/entities",
        params={"entity_type": "Sample", "as_of": FUTURE},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert "Beta" in {item["data"]["name"] for item in body["items"]}
