"""Pytest configuration and fixtures for schema validation integration tests."""

import sqlite3
from contextlib import contextmanager
from typing import Any

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.validation.schema_validator import (
    SchemaValidationConfig,
    SchemaValidator,
)
from mosaic.linkml_bridge import SchemaRegistry
from tests.support.linkml_schemas import build_registry


class InMemoryStorage:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._entities: dict[str, dict[str, Any]] = {}

    def close(self):
        self._conn.close()

    @contextmanager
    def get_connection(self):
        try:
            yield self._conn
        finally:
            pass

    def insert(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        key = f"{entity_type}:{data.get('id')}"
        self._entities[key] = data.copy()
        return data

    def update(self, entity_type: str, entity_id: str, data: dict[str, Any]):
        key = f"{entity_type}:{entity_id}"
        if key in self._entities:
            self._entities[key].update(data)
            return self._entities[key]
        return data

    def get(self, entity_type: str, entity_id: str):
        return self._entities.get(f"{entity_type}:{entity_id}")

    def exists(self, entity_type: str, entity_id: str) -> bool:
        return f"{entity_type}:{entity_id}" in self._entities

    def list_all(self, entity_type: str):
        prefix = f"{entity_type}:"
        return [v for k, v in self._entities.items() if k.startswith(prefix)]


@pytest.fixture
def in_memory_storage():
    storage = InMemoryStorage()
    yield storage
    storage.close()


@pytest.fixture
def sample_registry() -> SchemaRegistry:
    """LinkML registry exposing test entity classes."""
    return build_registry(
        classes={
            "sample": {
                "attributes": {
                    "id": {"identifier": True, "range": "string", "required": True},
                    "name": {"range": "string", "required": True},
                    "description": {"range": "string"},
                    "quantity": {"range": "integer", "required": True},
                    "price": {"range": "float"},
                    "is_active": {"range": "boolean"},
                    "tags": {"range": "string", "multivalued": True},
                    "metadata": {"range": "string"},
                }
            },
            "project": {
                "attributes": {
                    "id": {"identifier": True, "range": "string", "required": True},
                    "name": {
                        "range": "string",
                        "required": True,
                        "annotations": {"mosaic_unique": True},
                    },
                    "status": {"range": "string", "required": True},
                }
            },
            "sample_with_reference": {
                "attributes": {
                    "id": {"identifier": True, "range": "string", "required": True},
                    "project_id": {"range": "project", "required": True},
                    "name": {"range": "string", "required": True},
                }
            },
            "sample_with_constraints": {
                "attributes": {
                    "id": {"identifier": True, "range": "string", "required": True},
                    "short_code": {"range": "string", "required": True},
                    "tags": {"range": "string", "multivalued": True},
                }
            },
        }
    )


@pytest.fixture
def hippo_client_with_validation(sample_registry, in_memory_storage):
    def entity_exists_fn(entity_type: str, entity_id: str) -> bool:
        return in_memory_storage.exists(entity_type, entity_id)

    validator = SchemaValidator(
        config=SchemaValidationConfig(
            registry=sample_registry, entity_exists_fn=entity_exists_fn
        )
    )
    pipeline = ValidationPipeline()
    pipeline.add_validator(validator)

    class ValidatedMosaicClient(MosaicClient):
        def __init__(self, storage, pipeline):
            super().__init__(pipeline=pipeline)
            self._storage = storage
            self._pipeline = pipeline

        def _create_internal(self, entity_type: str, data: dict[str, Any]):
            return self._storage.insert(entity_type, data)

        def _update_internal(
            self, entity_type: str, entity_id: str, data: dict[str, Any]
        ):
            return self._storage.update(entity_type, entity_id, data)

        def _delete_internal(self, entity_type: str, entity_id: str) -> bool:
            key = f"{entity_type}:{entity_id}"
            if key in self._storage._entities:
                del self._storage._entities[key]
                return True
            return False

    return ValidatedMosaicClient(in_memory_storage, pipeline)


@pytest.fixture
def hippo_client_without_validation():
    return MosaicClient(bypass_validation=True)


@pytest.fixture
def valid_sample_data():
    return {
        "id": "sample-001",
        "name": "Test Sample",
        "description": "A test sample",
        "quantity": 42,
        "price": 19.99,
        "is_active": True,
        "tags": ["test", "sample"],
        "metadata": "{}",
    }


@pytest.fixture
def valid_project_data():
    return {
        "id": "project-001",
        "name": "Test Project",
        "status": "active",
    }


@pytest.fixture
def sample_with_reference_data(valid_project_data):
    return {
        "id": "sample-ref-001",
        "project_id": valid_project_data["id"],
        "name": "Sample with Project Reference",
    }
