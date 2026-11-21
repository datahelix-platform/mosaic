"""End-to-end integration tests for the full Mosaic stack.

These tests exercise the complete path from config → storage → SDK,
using a real SQLite database in a temporary directory.  No mocking.

Key observations about the actual MosaicClient API (discovered during writing):
- create/get/update return a dict with keys: id, entity_type, data, version,
  created_at, updated_at.  User fields live under result["data"].
- FTS search requires MosaicClient to be initialised with _fts_table_metadata
  (a dict[str, list[FTSTableMetadata]]) so it knows which fields are indexed.
- create_app() only mounts explicitly passed routers; built-in routers
  (health, entity, etc.) must be passed via the routers= kwarg.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app
from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import EntityNotFoundError, ValidationFailure
from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.storage.fts import FTSTableMetadata, FTSFieldMetadata
from mosaic.core.validators.write_validator import CELWriteValidator
from mosaic.serve.routers.health import router as health_router


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

_SCHEMA_CLASSES = {
    "Sample": {
        "attributes": {
            "id": {"identifier": True, "required": True},
            "name": {"range": "string", "required": True},
            "tissue": {"range": "string", "required": True},
            "notes": {
                "range": "string",
                "annotations": {"mosaic_search": "fts5"},
            },
        }
    }
}

# CEL rule: name must match ^S\d{3}$
_VALIDATORS_YAML = {
    "validators": [
        {
            "name": "sample_name_format",
            "entity_type": "Sample",
            "operations": ["create", "update"],
            "condition": 'entity.name.matches("^S[0-9]{3}$")',
            "message": "Sample name must match S followed by 3 digits (e.g. S001)",
        }
    ]
}


@pytest.fixture()
def tmp_hippo(tmp_path: Path) -> Path:
    """Create a minimal Mosaic project in a temp directory."""
    from tests.support.linkml_schemas import write_schema_file

    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    write_schema_file(schemas_dir, _SCHEMA_CLASSES, schema_name="sample")
    (tmp_path / "validators.yaml").write_text(yaml.dump(_VALIDATORS_YAML))
    return tmp_path


# ---------------------------------------------------------------------------
# Client factory helpers
# ---------------------------------------------------------------------------


def _fts_metadata_for_sample() -> dict[str, list[FTSTableMetadata]]:
    """Build the FTS metadata dict that MosaicClient needs for FTS search."""
    field_meta = FTSFieldMetadata(
        field_name="notes",
        field_type="string",
        search_type="fts5",
        source_entity_type="Sample",
    )
    table_meta = FTSTableMetadata(
        table_name="fts_sample_notes",
        source_entity_type="Sample",
        fts_version="fts5",
        content_table="entities",
        content_rowid="rowid",
        fields=[field_meta],
    )
    return {"Sample": [table_meta]}


def _make_client(
    tmp_hippo: Path,
    *,
    validation: bool = False,
    fts: bool = False,
) -> MosaicClient:
    """Instantiate a MosaicClient backed by a real SQLite DB."""
    from mosaic.linkml_bridge import SchemaRegistry

    db_path = tmp_hippo / "hippo.db"
    registry = SchemaRegistry.from_path(tmp_hippo / "schemas")
    storage = SQLiteAdapter(str(db_path), schema_registry=registry)

    pipeline: ValidationPipeline | None = None
    if validation:
        pipeline = ValidationPipeline()
        cel = CELWriteValidator(validators_path=str(tmp_hippo / "validators.yaml"))
        pipeline.add_validator(cel)

    client = MosaicClient(storage=storage, pipeline=pipeline, registry=registry)

    if fts:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        for tables in client._fts_table_metadata.values():
            for meta in tables:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {meta.table_name} "
                    "USING fts5(entity_id, content)"
                )
        conn.commit()
        conn.close()

    return client


def _data(entity: dict[str, Any]) -> dict[str, Any]:
    """Extract the user-data payload from an entity response dict."""
    return entity["data"]


# ---------------------------------------------------------------------------
# 1. Round-trip: create then get
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_create_and_get_entity(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        entity_id = result["id"]

        fetched = client.get("Sample", entity_id)
        assert _data(fetched)["name"] == "S001"
        assert _data(fetched)["tissue"] == "DLPFC"

    def test_create_returns_id(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S002", "tissue": "hippocampus"})
        assert result["id"]  # non-empty string

    def test_create_returns_entity_type(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        assert result["entity_type"] == "Sample"

    def test_multiple_entities_isolated(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        r1 = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        r2 = client.create("Sample", {"name": "S002", "tissue": "hippocampus"})

        assert r1["id"] != r2["id"]
        assert _data(client.get("Sample", r1["id"]))["name"] == "S001"
        assert _data(client.get("Sample", r2["id"]))["name"] == "S002"

    def test_get_nonexistent_raises(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        with pytest.raises(EntityNotFoundError):
            client.get("Sample", "does-not-exist")


# ---------------------------------------------------------------------------
# 2. Validation blocks bad writes
# ---------------------------------------------------------------------------


class TestValidationBlocks:
    def test_invalid_name_raises(self, tmp_hippo):
        client = _make_client(tmp_hippo, validation=True)
        with pytest.raises(Exception):
            client.create("Sample", {"name": "bad-name", "tissue": "DLPFC"})

    def test_valid_name_s001_passes(self, tmp_hippo):
        client = _make_client(tmp_hippo, validation=True)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        assert _data(result)["name"] == "S001"

    def test_valid_name_s000_passes(self, tmp_hippo):
        client = _make_client(tmp_hippo, validation=True)
        result = client.create("Sample", {"name": "S000", "tissue": "DLPFC"})
        assert _data(result)["name"] == "S000"

    def test_valid_name_s999_passes(self, tmp_hippo):
        client = _make_client(tmp_hippo, validation=True)
        result = client.create("Sample", {"name": "S999", "tissue": "DLPFC"})
        assert _data(result)["name"] == "S999"

    def test_name_four_digits_rejected(self, tmp_hippo):
        client = _make_client(tmp_hippo, validation=True)
        with pytest.raises(Exception):
            client.create("Sample", {"name": "S0001", "tissue": "DLPFC"})

    def test_lowercase_s_rejected(self, tmp_hippo):
        client = _make_client(tmp_hippo, validation=True)
        with pytest.raises(Exception):
            client.create("Sample", {"name": "s001", "tissue": "DLPFC"})


# ---------------------------------------------------------------------------
# 3. Additive schema: new optional field doesn't break old data
# ---------------------------------------------------------------------------


def _add_batch_field_to_schema(tmp_hippo: Path) -> None:
    """Evolve the LinkML schema by adding an optional ``batch`` attribute."""
    schema_file = tmp_hippo / "schemas" / "sample.yaml"
    doc = yaml.safe_load(schema_file.read_text())
    doc["classes"]["Sample"]["attributes"]["batch"] = {"range": "string"}
    schema_file.write_text(yaml.safe_dump(doc, sort_keys=False))


class TestAdditiveFieldAddition:
    def test_existing_entity_survives_new_optional_field(self, tmp_hippo):
        client1 = _make_client(tmp_hippo)
        result = client1.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        entity_id = result["id"]

        _add_batch_field_to_schema(tmp_hippo)

        # Re-open the same DB — simulates service restart after schema update
        client2 = _make_client(tmp_hippo)
        fetched = client2.get("Sample", entity_id)

        assert _data(fetched)["name"] == "S001"
        assert _data(fetched)["tissue"] == "DLPFC"
        # batch absent or None on old entity — both acceptable
        assert _data(fetched).get("batch") is None

    def test_new_entity_can_use_new_field(self, tmp_hippo):
        _add_batch_field_to_schema(tmp_hippo)

        client = _make_client(tmp_hippo)
        result = client.create(
            "Sample", {"name": "S001", "tissue": "DLPFC", "batch": "B01"}
        )
        assert _data(client.get("Sample", result["id"]))["batch"] == "B01"


# ---------------------------------------------------------------------------
# 4. REST API via FastAPI TestClient
# ---------------------------------------------------------------------------


class TestRESTAPI:
    def _app(self, tmp_hippo):
        hippo_client = _make_client(tmp_hippo)
        return create_app(mosaic_client=hippo_client, routers=[health_router])

    def test_health_endpoint_returns_200(self, tmp_hippo):
        tc = TestClient(self._app(tmp_hippo))
        r = tc.get("/health")
        assert r.status_code == 200

    def test_health_endpoint_returns_healthy(self, tmp_hippo):
        tc = TestClient(self._app(tmp_hippo))
        r = tc.get("/health")
        assert r.json()["status"] == "healthy"

    def test_openapi_schema_available(self, tmp_hippo):
        tc = TestClient(self._app(tmp_hippo))
        r = tc.get("/openapi.json")
        assert r.status_code == 200
        assert "openapi" in r.json()

    def test_root_endpoint(self, tmp_hippo):
        tc = TestClient(self._app(tmp_hippo))
        r = tc.get("/")
        assert r.status_code == 200
        assert "service" in r.json()


# ---------------------------------------------------------------------------
# 5. FTS5 full-text search end-to-end
# ---------------------------------------------------------------------------


class TestFTSSearch:
    def test_fts_search_returns_matching_entity(self, tmp_hippo):
        client = _make_client(tmp_hippo, fts=True)
        client.create(
            "Sample",
            {
                "name": "S001",
                "tissue": "DLPFC",
                "notes": "hippocampus lesion observed",
            },
        )
        client.create(
            "Sample",
            {
                "name": "S002",
                "tissue": "ACC",
                "notes": "prefrontal cortex damage",
            },
        )

        results = client.search("Sample", "hippocampus")
        names = [_data(r)["name"] for r in results.items]
        assert "S001" in names
        assert "S002" not in names

    def test_fts_search_no_results(self, tmp_hippo):
        client = _make_client(tmp_hippo, fts=True)
        client.create(
            "Sample",
            {
                "name": "S001",
                "tissue": "DLPFC",
                "notes": "hippocampus lesion",
            },
        )
        results = client.search("Sample", "nonexistentterm12345")
        assert results.items == [] and results.total == 0

    def test_fts_search_multiple_results(self, tmp_hippo):
        client = _make_client(tmp_hippo, fts=True)
        client.create(
            "Sample",
            {
                "name": "S001",
                "tissue": "DLPFC",
                "notes": "cortex damage",
            },
        )
        client.create(
            "Sample",
            {
                "name": "S002",
                "tissue": "ACC",
                "notes": "cortex lesion",
            },
        )
        client.create(
            "Sample",
            {
                "name": "S003",
                "tissue": "HC",
                "notes": "hippocampus only",
            },
        )

        results = client.search("Sample", "cortex")
        names = [_data(r)["name"] for r in results.items]
        assert "S001" in names
        assert "S002" in names
        assert "S003" not in names


# ---------------------------------------------------------------------------
# 6. Provenance / entity immutability
# ---------------------------------------------------------------------------


class TestProvenanceImmutability:
    def test_entity_has_id_after_create(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        assert result["id"]

    def test_update_data_fields_works(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        updated = client.update(
            "Sample", result["id"], {"name": "S001", "tissue": "ACC"}
        )
        assert _data(updated)["tissue"] == "ACC"

    def test_entity_type_cannot_be_spoofed_on_get(self, tmp_hippo):
        """Retrieving an entity under a different type raises EntityNotFoundError."""
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        with pytest.raises(EntityNotFoundError):
            client.get("OtherType", result["id"])

    def test_id_unchanged_after_update(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        original_id = result["id"]
        updated = client.update(
            "Sample", original_id, {"name": "S001", "tissue": "ACC"}
        )
        assert updated["id"] == original_id

    def test_version_increments_on_update(self, tmp_hippo):
        client = _make_client(tmp_hippo)
        result = client.create("Sample", {"name": "S001", "tissue": "DLPFC"})
        assert result["version"] == 1
        updated = client.update(
            "Sample", result["id"], {"name": "S001", "tissue": "ACC"}
        )
        assert updated["version"] == 2
