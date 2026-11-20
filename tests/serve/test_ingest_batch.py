"""Tests for the batch unit-of-work REST endpoints (issue #84 increment 3).

POST /ingest/validate  — whole-set dry-run validation (no writes).
POST /ingest/batch     — atomic multi-entity write.
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.validation import ValidationResult, WriteOperation, WriteValidator
from mosaic.serve import create_default_app
from tests.conftest import _build_minimal_schema_registry

AUTH = {"Authorization": "Bearer test-token"}


class _NameRequiredValidator(WriteValidator):
    def validate(self, operation: WriteOperation) -> ValidationResult:
        if not operation.data.get("name"):
            return ValidationResult(
                is_valid=False, errors=["name is required"],
                entity_id=operation.data.get("id"),
            )
        return ValidationResult(is_valid=True, errors=[])


@pytest.fixture
def hippo_client():
    with tempfile.TemporaryDirectory() as tmpdir:
        registry = _build_minimal_schema_registry()
        storage = SQLiteAdapter(os.path.join(tmpdir, "batch.db"), schema_registry=registry)
        c = MosaicClient(storage=storage, registry=registry)
        c.add_validator(_NameRequiredValidator())
        yield c


@pytest.fixture
def client(hippo_client):
    app = create_default_app(mosaic_client=hippo_client, graphql=False)
    with TestClient(app) as tc:
        yield tc


class TestValidateEndpoint:
    def test_without_auth_header(self, client):
        """Mosaic holds zero authn/authz (#54 Part A) — no header required.

        An empty ``entities`` list is itself invalid (non-empty required),
        so this exercises real request processing, not an auth gate.
        """
        r = client.post("/ingest/validate", json={"entities": []})
        assert r.status_code == 422

    def test_missing_entities_is_422(self, client):
        r = client.post("/ingest/validate", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_reports_per_entity_without_writing(self, client, hippo_client):
        r = client.post(
            "/ingest/validate",
            json={"entities": [
                {"entity_type": "Sample", "data": {"id": "v1", "name": "ok"}},
                {"entity_type": "Sample", "data": {"id": "v2"}},  # no name
            ]},
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is False
        assert len(body["results"]) == 2
        # Nothing written.
        assert hippo_client._storage.read("v1") is None
        assert hippo_client._storage.read("v2") is None


class TestBatchEndpoint:
    def test_without_auth_header(self, client):
        """Mosaic holds zero authn/authz (#54 Part A) — no header required.

        An empty ``entities`` list is itself invalid (non-empty required),
        so this exercises real request processing, not an auth gate.
        """
        r = client.post("/ingest/batch", json={"entities": []})
        assert r.status_code == 422

    def test_missing_entities_is_422(self, client):
        r = client.post("/ingest/batch", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_commits_valid_set(self, client, hippo_client):
        r = client.post(
            "/ingest/batch",
            json={"entities": [
                {"entity_type": "Sample", "data": {"id": "s1", "name": "a"}},
                {"entity_type": "Sample", "data": {"id": "s2", "name": "b"}},
            ]},
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["committed"] is True
        assert len(body["entities"]) == 2
        assert hippo_client._storage.read("s1") is not None
        assert hippo_client._storage.read("s2") is not None

    def test_dry_run_writes_nothing(self, client, hippo_client):
        r = client.post(
            "/ingest/batch",
            json={
                "dry_run": True,
                "entities": [{"entity_type": "Sample", "data": {"id": "d1", "name": "a"}}],
            },
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["committed"] is False
        assert body["dry_run"] is True
        assert hippo_client._storage.read("d1") is None

    def test_invalid_set_is_422_and_writes_nothing(self, client, hippo_client):
        r = client.post(
            "/ingest/batch",
            json={"entities": [
                {"entity_type": "Sample", "data": {"id": "ok", "name": "a"}},
                {"entity_type": "Sample", "data": {"id": "bad"}},  # no name
            ]},
            headers=AUTH,
        )
        assert r.status_code == 422
        # All-or-nothing: the valid sibling is not written either.
        assert hippo_client._storage.read("ok") is None
        assert hippo_client._storage.read("bad") is None

    def test_intra_batch_relationship(self, client, hippo_client):
        r = client.post(
            "/ingest/batch",
            json={
                "entities": [
                    {"entity_type": "Donor", "data": {"id": "donor-1", "name": "D"}},
                    {"entity_type": "Sample", "data": {"id": "sample-1", "name": "S"}},
                ],
                "relationships": [
                    {"source_id": "donor-1", "target_id": "sample-1", "relationship_type": "donated"}
                ],
            },
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["committed"] is True
        assert len(body["relationships"]) == 1
        assert hippo_client._storage.read("donor-1") is not None
        assert hippo_client._storage.read("sample-1") is not None
