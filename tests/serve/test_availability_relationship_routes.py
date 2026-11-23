"""REST routes addressed by id alone resolve the id's own class (#289 review).

``GET``/``POST /entities/{id}/availability`` and the
``/entities/{id}/relationships`` routes used to look the id up as the
placeholder type ``"entity"``, which no row has, so they always answered
404. They now resolve the stored class first, as ``DELETE /entities/{id}``
does.
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.serve.routers import availability, health, ingest, relationship
from tests.conftest import _build_minimal_schema_registry


@pytest.fixture
def mosaic_client():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "routes.db"),
            schema_registry=_build_minimal_schema_registry(),
        )
        yield MosaicClient(storage=storage, bypass_validation=True)


@pytest.fixture
def api(mosaic_client):
    app = create_app(
        routers=[health.router, availability.router, ingest.router, relationship.router],
        mosaic_client=mosaic_client,
    )
    client = TestClient(app)
    for eid in ("s1", "s2"):
        resp = client.post(
            "/ingest", json={"entity_type": "Sample", "data": {"id": eid, "name": eid}}
        )
        assert resp.status_code == 200, resp.text
    return client


def _body(resp):
    payload = resp.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def test_get_availability_of_real_entity(api):
    resp = api.get("/entities/s1/availability")
    assert resp.status_code == 200, resp.text
    assert _body(resp) == {"entity_id": "s1", "entity_type": "Sample", "is_available": True}


def test_post_availability_both_directions(api, mosaic_client):
    resp = api.post("/entities/s1/availability",
                    json={"is_available": False, "reason": "depleted"})
    assert resp.status_code == 200, resp.text
    assert _body(resp) == {"entity_id": "s1", "entity_type": "Sample", "is_available": False}
    assert _body(api.get("/entities/s1/availability"))["is_available"] is False
    row = mosaic_client.get("Sample", "s1", include_unavailable=True)
    assert row["is_available"] is False
    history = mosaic_client.history("s1")
    change = [h for h in history if h["operation_type"] == "availability_change"]
    assert change, history

    resp = api.post("/entities/s1/availability", json={"is_available": True})
    assert resp.status_code == 200, resp.text
    assert _body(api.get("/entities/s1/availability"))["is_available"] is True


@pytest.mark.parametrize("method", ["get", "post"])
def test_availability_unknown_id_is_404(api, method):
    kwargs = {"json": {"is_available": False}} if method == "post" else {}
    resp = getattr(api, method)("/entities/no-such-id/availability", **kwargs)
    assert resp.status_code == 404


def test_relationship_routes_on_real_entities(api):
    resp = api.get("/entities/s1/relationships")
    assert resp.status_code == 200, resp.text
    assert _body(resp) == []
    resp = api.post(
        "/entities/s1/relationships",
        json={"target_entity_id": "s2", "relationship_type": "related_to"},
    )
    assert resp.status_code == 200, resp.text
    assert _body(resp)["target_entity_id"] == "s2"


def test_relationship_routes_unknown_ids_are_404(api):
    assert api.get("/entities/no-such-id/relationships").status_code == 404
    resp = api.post(
        "/entities/s1/relationships",
        json={"target_entity_id": "no-such-id", "relationship_type": "related_to"},
    )
    assert resp.status_code == 404
    resp = api.post(
        "/entities/no-such-id/relationships",
        json={"target_entity_id": "s2", "relationship_type": "related_to"},
    )
    assert resp.status_code == 404
