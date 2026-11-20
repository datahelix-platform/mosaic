"""Tests for entity-field filtering (eq + IN via multi-value params) on the
entity list endpoint (sec4 §4.3 "Query Filtering — OR Composition"; issue
#102).
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


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_field_filter_endpoint.db")


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


def test_single_value_field_param_is_eq_filter(client):
    _create_entity(client, "s1", "Alpha", "brain")
    _create_entity(client, "s2", "Beta", "liver")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample", "tissue": "brain"},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["data"]["tissue"] == "brain"


def test_repeated_field_param_is_in_filter(client):
    _create_entity(client, "s3", "Gamma", "brain")
    _create_entity(client, "s4", "Delta", "liver")
    _create_entity(client, "s5", "Epsilon", "heart")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample", "tissue": ["brain", "liver"]},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    tissues = {item["data"]["tissue"] for item in body["items"]}
    assert body["total"] == 2
    assert tissues == {"brain", "liver"}


def test_field_filters_and_across_fields(client):
    _create_entity(client, "s6", "Zeta", "brain")
    _create_entity(client, "s7", "Zeta", "liver")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample", "name": "Zeta", "tissue": "brain"},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["data"]["tissue"] == "brain"


def test_field_filters_or_across_fields(client):
    _create_entity(client, "s8", "Eta", "brain")
    _create_entity(client, "s9", "Theta", "liver")
    _create_entity(client, "s10", "Iota", "heart")

    response = client.get(
        "/entities",
        params={
            "entity_type": "Sample",
            "filter_mode": "or",
            "name": "Eta",
            "tissue": "liver",
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    names = {item["data"]["name"] for item in body["items"]}
    assert names == {"Eta", "Theta"}


def test_no_field_params_returns_all(client):
    _create_entity(client, "s11", "Kappa", "brain")

    response = client.get(
        "/entities",
        params={"entity_type": "Sample"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["total"] >= 1
