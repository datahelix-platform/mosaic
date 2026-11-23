"""REST routes addressed by id alone (#289 review).

``GET``/``POST /entities/{id}/availability`` used to look the id up as the
placeholder type ``"entity"``, which no row has, so they always answered
404. They now resolve the stored class first, as ``DELETE /entities/{id}``
does. The ``/entities/{id}/relationships`` stubs answer 501 (#291).
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


@pytest.mark.parametrize("entity_id", ["s1", "no-such-id"])
def test_relationship_routes_are_not_implemented(api, entity_id):
    """The stub routes stored and read nothing; they answer 501, pointing at
    the supported path, rather than a silent 200 (#290 review, #291)."""
    responses = [
        api.get(f"/entities/{entity_id}/relationships"),
        api.post(
            f"/entities/{entity_id}/relationships",
            json={"target_entity_id": "s2", "relationship_type": "related_to"},
        ),
        api.delete(f"/entities/{entity_id}/relationships/s1->s2:related_to"),
    ]
    for resp in responses:
        assert resp.status_code == 501, (resp.request.method, resp.text)
        assert "not implemented" in resp.text
        assert "MosaicClient.relationships" in resp.text
        assert "#291" in resp.text
