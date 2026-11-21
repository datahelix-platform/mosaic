"""Tests for PUT /entities/{entity_type}/{entity_id} endpoint."""

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
        yield os.path.join(tmpdir, "test_replace_endpoint.db")


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


def test_put_replaces_existing_entity(client):
    """PUT replaces an existing entity and returns updated data."""
    # Create entity through the API to avoid cross-thread SQLite issues
    create_resp = client.post(
        "/ingest",
        json={"entity_type": "Sample", "data": {"id": "e1", "name": "original", "extra": "data"}},
        headers=AUTH,
    )
    assert create_resp.status_code == 200

    response = client.put(
        "/entities/Sample/e1",
        json={"name": "replaced"},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == {"name": "replaced"}
    assert body["version"] == 2


def test_put_returns_404_for_missing(client):
    """PUT returns 404 when entity does not exist."""
    response = client.put(
        "/entities/Sample/nonexistent",
        json={"name": "test"},
        headers=AUTH,
    )
    assert response.status_code == 404


def test_put_without_auth_header(client):
    """Mosaic holds zero authn/authz (#54 Part A) — no header required."""
    response = client.put("/entities/Sample/e1", json={"name": "test"})
    assert response.status_code == 404
