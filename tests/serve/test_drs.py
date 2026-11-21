"""Tests for the GA4GH DRS v1 router."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.serve.routers import drs


def _make_raw_entity(
    entity_id="abc-123",
    entity_type="Sample",
    data=None,
    is_available=True,
):
    raw = MagicMock()
    raw.id = entity_id
    raw.entity_type = entity_type
    raw.data = data if data is not None else {}
    raw.is_available = is_available
    return raw


def _make_full_entity(
    entity_id="abc-123",
    entity_type="Sample",
    data=None,
    created_at="2024-01-01T00:00:00+00:00",
    updated_at="2024-01-02T00:00:00+00:00",
):
    return {
        "id": entity_id,
        "entity_type": entity_type,
        "data": data or {},
        "version": 1,
        "created_at": created_at,
        "updated_at": updated_at,
        "superseded_by": None,
    }


def _make_client(raw_entity=None, full_entity=None):
    """Build a mock MosaicClient configured for DRS tests."""
    mock_client = MagicMock()
    mock_client.storage.read.return_value = raw_entity
    if full_entity is not None:
        mock_client.get.return_value = full_entity
    return mock_client


@pytest.fixture
def app_no_client():
    """App with no hippo_client attached (simulates un-initialised server)."""
    return create_app(routers=[drs.router])


class TestDrsMountedInDefaultApp:
    """The DRS router must be part of the default ``mosaic serve`` app."""

    def test_default_app_exposes_drs_route(self):
        from mosaic.serve import create_default_app

        app = create_default_app()
        # Assert via the OpenAPI path map — robust across FastAPI versions and
        # the router-include mechanism (raw `app.routes` may expose opaque
        # include wrappers without a `.path`).
        assert "/ga4gh/drs/v1/objects/{object_id}" in app.openapi()["paths"]

    def test_default_app_resolves_drs_object(self):
        from mosaic.serve import create_default_app

        uri = "s3://bucket/file.bam"
        raw = _make_raw_entity(data={"uri": uri})
        full = _make_full_entity(data={"uri": uri})
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_default_app(mosaic_client=mock_client)
        client = TestClient(app)

        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 200
        assert response.json()["access_methods"][0]["access_url"]["url"] == uri


class TestDrsObjectNotFound:
    def test_no_storage_returns_404(self, app_no_client):
        client = TestClient(app_no_client)
        response = client.get("/ga4gh/drs/v1/objects/missing-id")
        assert response.status_code == 404

    def test_entity_not_in_storage_returns_404(self):
        mock_client = _make_client(raw_entity=None)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)
        response = client.get("/ga4gh/drs/v1/objects/nonexistent-id")
        assert response.status_code == 404

    def test_unavailable_entity_returns_404(self):
        raw = _make_raw_entity(is_available=False, data={"uri": "s3://bucket/file"})
        mock_client = _make_client(raw_entity=raw)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)
        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 404


class TestDrsObjectNoUri:
    def test_entity_with_no_uri_field_returns_404(self):
        raw = _make_raw_entity(data={})
        full = _make_full_entity()
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)
        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 404

    def test_entity_with_none_uri_returns_404(self):
        raw = _make_raw_entity(data={"uri": None})
        full = _make_full_entity()
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)
        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 404


class TestDrsObjectSuccess:
    def test_s3_uri_returns_correct_scheme(self):
        uri = "s3://my-bucket/path/to/file.bam"
        raw = _make_raw_entity(data={"uri": uri})
        full = _make_full_entity(data={"uri": uri})
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)

        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 200
        body = response.json()
        assert body["access_methods"][0]["type"] == "s3"
        assert body["access_methods"][0]["access_url"]["url"] == uri

    def test_file_uri_returns_correct_scheme(self):
        uri = "file:///data/samples/file.fastq"
        raw = _make_raw_entity(data={"uri": uri})
        full = _make_full_entity(data={"uri": uri})
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)

        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 200
        assert response.json()["access_methods"][0]["type"] == "file"

    def test_https_uri_returns_correct_scheme(self):
        uri = "https://example.com/data/file.vcf"
        raw = _make_raw_entity(data={"uri": uri})
        full = _make_full_entity(data={"uri": uri})
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)

        response = client.get("/ga4gh/drs/v1/objects/abc-123")
        assert response.status_code == 200
        assert response.json()["access_methods"][0]["type"] == "https"

    def test_response_structure(self):
        entity_id = "abc-123"
        entity_type = "Sample"
        uri = "s3://bucket/file.bam"
        created = "2024-01-01T00:00:00+00:00"
        updated = "2024-01-02T00:00:00+00:00"

        raw = _make_raw_entity(entity_id=entity_id, entity_type=entity_type, data={"uri": uri})
        full = _make_full_entity(
            entity_id=entity_id,
            entity_type=entity_type,
            data={"uri": uri},
            created_at=created,
            updated_at=updated,
        )
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)

        response = client.get(f"/ga4gh/drs/v1/objects/{entity_id}")
        assert response.status_code == 200
        body = response.json()

        assert body["id"] == entity_id
        assert body["name"] == f"{entity_type}/{entity_id}"
        assert body["self_uri"] == f"drs://localhost/{entity_id}"
        assert body["size"] is None
        assert body["created_time"] == created
        assert body["updated_time"] == updated
        assert body["checksums"] == []
        assert len(body["access_methods"]) == 1

    def test_get_called_with_entity_type_from_storage(self):
        """Verify client.get() is called with entity_type resolved from storage.read()."""
        raw = _make_raw_entity(entity_id="xyz", entity_type="Donor", data={"uri": "s3://b/f"})
        full = _make_full_entity(entity_id="xyz", entity_type="Donor", data={"uri": "s3://b/f"})
        mock_client = _make_client(raw_entity=raw, full_entity=full)
        app = create_app(routers=[drs.router], mosaic_client=mock_client)
        client = TestClient(app)

        response = client.get("/ga4gh/drs/v1/objects/xyz")
        assert response.status_code == 200
        mock_client.get.assert_called_once_with(entity_type="Donor", entity_id="xyz")
