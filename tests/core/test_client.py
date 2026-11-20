"""Unit tests for MosaicClient put, get, query operations."""

import json
import os
import sqlite3
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import EntityNotFoundError, ValidationFailure
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry


class TestPutOperation:
    """Tests for MosaicClient.put() operation."""

    @pytest.fixture
    def db_path(self) -> str:
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield os.path.join(tmpdir, "test_put.db")

    @pytest.fixture
    def client(self, db_path: str) -> MosaicClient:
        """Create a MosaicClient with SQLite storage."""
        storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
        return MosaicClient(storage=storage, bypass_validation=True)

    def test_put_creates_entity_with_generated_id(self, client: MosaicClient) -> None:
        """Test that put creates an entity with a generated UUID when no ID provided."""
        result = client.put("Sample", {"name": "test", "value": 123})

        assert "id" in result
        assert result["entity_type"] == "Sample"
        assert result["data"] == {"name": "test", "value": 123}
        assert result["version"] == 1

    def test_put_creates_entity_with_provided_id(self, client: MosaicClient) -> None:
        """Test that put uses provided ID when specified."""
        result = client.put("Sample", {"id": "my-id", "name": "test"})

        assert result["id"] == "my-id"

    def test_put_uses_id_from_data(self, client: MosaicClient) -> None:
        """Test that put uses ID from data dict when entity_id not provided."""
        result = client.put("Sample", {"id": "data-id", "name": "test"})

        assert result["id"] == "data-id"

    def test_put_increments_version_on_update(self, client: MosaicClient) -> None:
        """Test that version is incremented when updating an existing entity."""
        result1 = client.put("Sample", {"id": "v-test", "name": "v1"})
        assert result1["version"] == 1

        result2 = client.put("Sample", {"name": "v2"}, "v-test")
        assert result2["version"] == 2

        result3 = client.put("Sample", {"name": "v3"}, "v-test")
        assert result3["version"] == 3

    def test_put_rejects_null_data(self, client: MosaicClient) -> None:
        """Test that put rejects null data with ValidationFailure."""
        with pytest.raises(ValidationFailure) as exc_info:
            client.put("Sample", None)

        assert "null or empty" in str(exc_info.value).lower()

    def test_put_rejects_empty_data(self, client: MosaicClient) -> None:
        """Test that put rejects empty data with ValidationFailure."""
        with pytest.raises(ValidationFailure) as exc_info:
            client.put("Sample", {})

        assert "null or empty" in str(exc_info.value).lower()


class TestGetOperation:
    """Tests for MosaicClient.get() operation."""

    @pytest.fixture
    def db_path(self) -> str:
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield os.path.join(tmpdir, "test_get.db")

    @pytest.fixture
    def client(self, db_path: str) -> MosaicClient:
        """Create a MosaicClient with SQLite storage."""
        storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
        return MosaicClient(storage=storage, bypass_validation=True)

    def test_get_returns_entity_with_metadata(self, client: MosaicClient) -> None:
        """Test that get returns entity with all metadata fields."""
        created = client.put("Sample", {"id": "get-test", "name": "test"})
        result = client.get("Sample", "get-test")

        assert result["id"] == "get-test"
        assert result["entity_type"] == "Sample"
        assert result["data"]["name"] == "test"
        assert result["version"] == 1
        assert "created_at" in result
        assert "updated_at" in result

    def test_get_raises_error_for_nonexistent_entity(self, client: MosaicClient) -> None:
        """Test that get raises EntityNotFoundError for non-existent entity."""
        with pytest.raises(EntityNotFoundError) as exc_info:
            client.get("Sample", "non-existent")

        assert "non-existent" in str(exc_info.value)

    def test_get_raises_error_for_wrong_entity_type(self, client: MosaicClient) -> None:
        """Test that get raises error when entity exists but wrong type."""
        client.put("Sample", {"id": "type-test", "name": "test"})

        with pytest.raises(EntityNotFoundError):
            client.get("OtherType", "type-test")


class TestQueryOperation:
    """Tests for MosaicClient.query() operation."""

    @pytest.fixture
    def db_path(self) -> str:
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield os.path.join(tmpdir, "test_query.db")

    @pytest.fixture
    def client(self, db_path: str) -> MosaicClient:
        """Create a MosaicClient with SQLite storage."""
        storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
        return MosaicClient(storage=storage, bypass_validation=True)

    def test_query_returns_matching_entities(self, client: MosaicClient) -> None:
        """Test that query returns entities matching the entity type."""
        client.put("Sample", {"id": "q1", "name": "first"})
        client.put("Sample", {"id": "q2", "name": "second"})
        client.put("Other", {"id": "q3", "name": "third"})

        results = client.query("Sample")

        assert len(results.items) == 2
        ids = {r["id"] for r in results.items}
        assert ids == {"q1", "q2"}

    def test_query_returns_empty_list_for_no_matches(self, client: MosaicClient) -> None:
        """Test that query returns PaginatedResult with empty items when no entities match."""
        client.put("Sample", {"id": "q1", "name": "test"})

        results = client.query("NonExistent")

        assert results.items == []
        assert results.total == 0

    def test_query_sorts_by_created_at_ascending(self, client: MosaicClient) -> None:
        """Test that query returns results sorted by created_at ascending."""
        client.put("Sample", {"id": "last", "name": "last"})
        client.put("Sample", {"id": "first", "name": "first"})
        client.put("Sample", {"id": "middle", "name": "middle"})

        results = client.query("Sample")

        assert len(results.items) == 3
        created_at = [r["created_at"] for r in results.items]
        assert created_at == sorted(created_at)

    def test_query_filters_by_date_from(self, client: MosaicClient) -> None:
        """Test that query filters entities by date_from."""
        client.put("Sample", {"id": "old", "name": "old"})
        client.put("Sample", {"id": "new", "name": "new"})

        results = client.query("Sample", date_from="2030-01-01")

        assert len(results.items) == 0

    def test_query_filters_by_date_to(self, client: MosaicClient) -> None:
        """Test that query filters entities by date_to."""
        client.put("Sample", {"id": "old", "name": "old"})
        client.put("Sample", {"id": "new", "name": "new"})

        results = client.query("Sample", date_to="2020-01-01")

        assert len(results.items) == 0

    def test_query_respects_limit(self, client: MosaicClient) -> None:
        """Test that query respects the limit parameter."""
        for i in range(5):
            client.put("Sample", {"id": f"q{i}", "name": f"name{i}"})

        results = client.query("Sample", limit=3)

        assert len(results.items) == 3

    def test_query_respects_offset(self, client: MosaicClient) -> None:
        """Test that query respects the offset parameter."""
        for i in range(5):
            client.put("Sample", {"id": f"q{i}", "name": f"name{i}"})

        results = client.query("Sample", offset=3)

        assert len(results.items) == 2


class TestErrorCases:
    """Tests for error handling in MosaicClient."""

    @pytest.fixture
    def db_path(self) -> str:
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield os.path.join(tmpdir, "test_errors.db")

    @pytest.fixture
    def client(self, db_path: str) -> MosaicClient:
        """Create a MosaicClient with SQLite storage."""
        storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
        return MosaicClient(storage=storage, bypass_validation=True)

    def test_get_nonexistent_entity_raises_error(self, client: MosaicClient) -> None:
        """Test that getting a non-existent entity raises EntityNotFoundError."""
        with pytest.raises(EntityNotFoundError):
            client.get("Sample", "does-not-exist")

    def test_get_with_wrong_type_raises_error(self, client: MosaicClient) -> None:
        """Test that getting an entity with wrong type raises EntityNotFoundError."""
        client.put("Sample", {"id": "test", "name": "test"})

        with pytest.raises(EntityNotFoundError):
            client.get("WrongType", "test")

    def test_put_null_data_raises_validation_error(self, client: MosaicClient) -> None:
        """Test that putting null data raises ValidationFailure."""
        with pytest.raises(ValidationFailure):
            client.put("Sample", None)

    def test_put_empty_data_raises_validation_error(self, client: MosaicClient) -> None:
        """Test that putting empty data raises ValidationFailure."""
        with pytest.raises(ValidationFailure):
            client.put("Sample", {})


class TestFTSIntegration:
    """Tests for FTS metadata derivation from schema."""

    def test_client_with_no_schemas_has_empty_fts_metadata(self) -> None:
        """Test that client with no schemas has empty _fts_table_metadata."""
        client = MosaicClient()
        assert hasattr(client, "_fts_table_metadata")
        assert client._fts_table_metadata == {}

    def test_client_with_schema_containing_fts_field_has_fts_metadata(self) -> None:
        from mosaic.core.storage.fts import FTSTableMetadata
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "notes": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                }
            }
        )
        client = MosaicClient(registry=registry)
        assert len(client._fts_table_metadata) == 1
        assert "Sample" in client._fts_table_metadata
        meta = client._fts_table_metadata["Sample"][0]
        assert isinstance(meta, FTSTableMetadata)
        assert meta.fields[0].field_name == "notes"
        assert meta.source_entity_type == "Sample"

    def test_client_with_schema_containing_no_fts_fields_has_empty_fts_metadata(
        self,
    ) -> None:
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "description": {"range": "string"},
                    }
                }
            }
        )
        client = MosaicClient(registry=registry)
        assert "Sample" not in client._fts_table_metadata

    def test_client_with_multiple_entity_types_each_have_fts_fields(self) -> None:
        from mosaic.core.storage.fts import FTSTableMetadata
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "notes": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                },
                "Project": {
                    "attributes": {
                        "id": {"identifier": True},
                        "title": {"range": "string", "required": True},
                        "description": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                },
            }
        )
        client = MosaicClient(registry=registry)
        assert len(client._fts_table_metadata) == 2
        assert "Sample" in client._fts_table_metadata
        assert "Project" in client._fts_table_metadata
        sample_meta = client._fts_table_metadata["Sample"][0]
        project_meta = client._fts_table_metadata["Project"][0]
        assert isinstance(sample_meta, FTSTableMetadata)
        assert isinstance(project_meta, FTSTableMetadata)
        assert sample_meta.fields[0].field_name == "notes"
        assert project_meta.fields[0].field_name == "description"


class TestTypeResolution:
    """Tests for MosaicClient.resolve_type / resolve_types per sec9 §9.5."""

    @pytest.fixture
    def db_path(self) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield os.path.join(tmpdir, "test_resolve.db")

    @pytest.fixture
    def client(self, db_path: str) -> MosaicClient:
        storage = SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())
        return MosaicClient(storage=storage, bypass_validation=True)

    def test_resolve_type_returns_entity_type_for_known_uuid(
        self, client: MosaicClient
    ) -> None:
        result = client.put("Sample", {"name": "s1"})
        uuid = result["id"]

        assert client.resolve_type(uuid) == "Sample"

    def test_resolve_type_returns_none_for_unknown_uuid(
        self, client: MosaicClient
    ) -> None:
        assert client.resolve_type("nonexistent-uuid") is None

    def test_resolve_types_batch(self, client: MosaicClient) -> None:
        r1 = client.put("Sample", {"name": "s1"})
        r2 = client.put("Project", {"name": "p1"})
        r3 = client.put("Sample", {"name": "s2"})

        ids = [r1["id"], r2["id"], r3["id"], "unknown-uuid"]
        resolved = client.resolve_types(ids)

        assert resolved[r1["id"]] == "Sample"
        assert resolved[r2["id"]] == "Project"
        assert resolved[r3["id"]] == "Sample"
        # Unknown uuid is absent, not raised
        assert "unknown-uuid" not in resolved

    def test_resolve_types_empty_input_returns_empty(
        self, client: MosaicClient
    ) -> None:
        assert client.resolve_types([]) == {}

    def test_resolve_type_after_delete_still_works(self, client: MosaicClient) -> None:
        # Per sec9 §9.5, type resolution must work regardless of availability
        # (archived / deleted entities still have a knowable type).
        result = client.put("Sample", {"name": "s1"})
        uuid = result["id"]
        client.delete("Sample", uuid)

        assert client.resolve_type(uuid) == "Sample"
