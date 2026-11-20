"""Tests for GET /xref/{system}/{value} (issue #48) and the deprecated
/external-ids endpoints' OpenAPI deprecation markers."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.serve import create_default_app

XREF_SERVE_SCHEMA = """
id: https://example.org/mosaic/test_xref_serve
name: test_xref_serve
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      starlims_ref:
        range: ExternalReference
        inlined: true
        annotations:
          mosaic_external_xref: true
"""

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(XREF_SERVE_SCHEMA)


@pytest.fixture
def hippo_client(registry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "xref_serve.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        yield MosaicClient(storage=storage, registry=registry)


@pytest.fixture
def api(hippo_client):
    app = create_default_app(mosaic_client=hippo_client)
    with TestClient(app) as test_client:
        yield test_client


def _create_sample(api, name="s1", system="STARLIMS", value="BC-1"):
    resp = api.post(
        "/ingest",
        json={
            "entity_type": "Sample",
            "data": {"name": name, "starlims_ref": {"system": system, "value": value}},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestXrefEndpoint:
    def test_lookup_round_trip(self, api):
        eid = _create_sample(api)
        resp = api.get("/xref/STARLIMS/BC-1", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == eid
        assert body["entity_type"] == "Sample"
        assert body["data"]["starlims_ref"] == {
            "system": "STARLIMS",
            "value": "BC-1",
        }

    def test_unknown_pair_404(self, api):
        resp = api.get("/xref/STARLIMS/nope", headers=AUTH)
        assert resp.status_code == 404
        assert "STARLIMS" in resp.json()["detail"]

    def test_without_auth_header(self, api):
        """Mosaic holds zero authn/authz (#54 Part A) — no header required."""
        assert api.get("/xref/STARLIMS/BC-1").status_code == 404

    def test_lookup_after_update(self, api):
        eid = _create_sample(api)
        resp = api.put(
            f"/entities/Sample/{eid}",
            json={
                "name": "s1",
                "starlims_ref": {"system": "STARLIMS", "value": "BC-2"},
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert api.get("/xref/STARLIMS/BC-1", headers=AUTH).status_code == 404
        resp = api.get("/xref/STARLIMS/BC-2", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["id"] == eid

    def test_uniqueness_violation_is_422(self, api):
        _create_sample(api)
        resp = api.post(
            "/ingest",
            json={
                "entity_type": "Sample",
                "data": {
                    "name": "s2",
                    "starlims_ref": {"system": "STARLIMS", "value": "BC-1"},
                },
            },
            headers=AUTH,
        )
        assert resp.status_code == 422
        assert "BC-1" in resp.json()["detail"]


class TestOpenAPIDeprecation:
    def test_external_id_routes_marked_deprecated(self, api):
        spec = api.get("/openapi.json").json()
        paths = spec["paths"]
        assert paths["/external-ids/{id_type}/{external_id}"]["get"]["deprecated"]
        assert paths["/entities/{entity_id}/external-ids"]["get"]["deprecated"]
        assert paths["/entities/{entity_id}/external-ids"]["post"]["deprecated"]

    def test_xref_route_not_deprecated(self, api):
        spec = api.get("/openapi.json").json()
        op = spec["paths"]["/xref/{system}/{value}"]["get"]
        assert not op.get("deprecated")
