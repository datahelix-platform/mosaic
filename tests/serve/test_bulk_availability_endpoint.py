"""Tests for POST /entities/{entity_type}/bulk-availability endpoint."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry
from mosaic.serve.routers import availability, health, ingest


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_bulk_avail_endpoint.db")


@pytest.fixture
def hippo_client(db_path):
    storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
    return MosaicClient(storage=storage, bypass_validation=True)


@pytest.fixture
def client(hippo_client):
    app = create_app(
        routers=[health.router, availability.router, ingest.router],
        mosaic_client=hippo_client,
    )
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-token"}


def _create_entity(client, entity_id, name="test"):
    """Helper to create an entity via the API."""
    resp = client.post(
        "/ingest",
        json={"entity_type": "Sample", "data": {"id": entity_id, "name": name}},
        headers=AUTH,
    )
    assert resp.status_code == 200
    return resp.json()


def test_bulk_set_unavailable(client):
    """Bulk-mark multiple entities as unavailable via API."""
    _create_entity(client, "b1", "one")
    _create_entity(client, "b2", "two")

    response = client.post(
        "/entities/Sample/bulk-availability",
        json={"entity_ids": ["b1", "b2"], "is_available": False},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["succeeded"] == 2
    assert body["failed"] == 0


def test_bulk_partial_failure(client):
    """Returns 207 on partial failure."""
    _create_entity(client, "b3", "exists")

    response = client.post(
        "/entities/Sample/bulk-availability",
        json={"entity_ids": ["b3", "nonexistent"], "is_available": False},
        headers=AUTH,
    )

    assert response.status_code == 207
    body = response.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1


def test_bulk_without_auth_header(client):
    """Mosaic holds zero authn/authz (#54 Part A) — no header required."""
    _create_entity(client, "b4", "no-header")

    response = client.post(
        "/entities/Sample/bulk-availability",
        json={"entity_ids": ["b4"], "is_available": False},
    )
    assert response.status_code == 200
