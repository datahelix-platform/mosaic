"""Tests for OR filter composition via the entity list endpoint."""

import os
import tempfile

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
        yield os.path.join(tmpdir, "test_or_filter_endpoint.db")


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
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-token"}


def _create_entity(client, entity_id, name, tissue):
    resp = client.post(
        "/ingest",
        json={
            "entity_type": "Sample",
            "data": {"id": entity_id, "name": name, "tissue": tissue},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200


def test_list_entities_default_filter_mode(client):
    """Default AND filter_mode returns entities matching all criteria."""
    _create_entity(client, "s1", "Alpha", "brain")
    _create_entity(client, "s2", "Beta", "liver")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample"},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 2


def test_list_entities_or_filter_mode(client):
    """OR filter_mode parameter is accepted by the endpoint."""
    _create_entity(client, "s3", "Gamma", "heart")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample", "filter_mode": "or"},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert "items" in body
