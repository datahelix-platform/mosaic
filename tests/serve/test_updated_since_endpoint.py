"""Tests for the ``updated_since`` polling parameter on the entity list
endpoint (sec4 §4.5)."""

import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry
from mosaic.serve.routers import entity, health, ingest


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_updated_since_endpoint.db")


@pytest.fixture
def hippo_client(db_path):
    storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
    return MosaicClient(storage=storage, bypass_validation=True)


@pytest.fixture
def client(hippo_client):
    app = create_app(
        routers=[health.router, entity.router, ingest.router],
        mosaic_client=hippo_client,
    )
    return TestClient(app, raise_server_exceptions=False)


AUTH = {"Authorization": "Bearer test-token"}


def _create_sample(hippo_client, name):
    result = hippo_client.put("Sample", {"name": name})
    time.sleep(0.005)
    return result["id"]


def test_updated_since_filters_by_watermark(client, hippo_client):
    first_id = _create_sample(hippo_client, "first")
    watermark = hippo_client.get("Sample", first_id)["updated_at"]
    second_id = _create_sample(hippo_client, "second")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample", "updated_since": watermark},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [second_id]
    assert body["total"] == 1


def test_updated_since_orders_by_updated_at_ascending(client, hippo_client):
    ids = [_create_sample(hippo_client, f"e{i}") for i in range(3)]

    response = client.get(
        "/entities",
        params={
            "entity_type": "Sample",
            "updated_since": "2000-01-01T00:00:00Z",
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ids


def test_invalid_updated_since_returns_400(client):
    response = client.get(
        "/entities",
        params={"entity_type": "Sample", "updated_since": "yesterday"},
        headers=AUTH,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Temporal Query Error"
    assert "yesterday" in body["detail"]


def test_omitting_updated_since_preserves_default_listing(client, hippo_client):
    _create_sample(hippo_client, "plain")

    response = client.get(
        "/entities", params={"entity_type": "Sample"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
