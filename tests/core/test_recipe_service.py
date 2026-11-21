"""Tests for the RecipeService shell + installed_recipes accessor.

PR 3 of PTS-290 — covers only the Phase 2 surface: ``__init__`` and
``list_installed``. The remaining verbs (``inspect``, ``import_``, etc.)
land in Phase 3+.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.meta import set_meta
from mosaic.core.recipe import InstalledRecipe
from mosaic.core.recipe_service import (
    META_KEY_INSTALLED_RECIPES,
    RecipeService,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "recipe_service.db")


@pytest.fixture
def storage(db_path):
    return SQLiteAdapter(db_path, schema_registry=_build_minimal_schema_registry())


@pytest.fixture
def client(storage):
    return MosaicClient(storage=storage, bypass_validation=True)


class TestServiceConstruction:
    def test_service_attached_to_client(self, client: MosaicClient) -> None:
        assert isinstance(client._recipe_service, RecipeService)

    def test_no_storage_returns_empty(self) -> None:
        svc = RecipeService(storage=None)
        assert svc.list_installed() == []


class TestListInstalledCleanInstance:
    def test_returns_empty_on_clean_instance(self, client: MosaicClient) -> None:
        assert client.recipe_list() == []

    def test_service_returns_empty_on_clean_instance(self, client: MosaicClient) -> None:
        assert client._recipe_service.list_installed() == []


class TestListInstalledHydration:
    """``list_installed`` rehydrates ``InstalledRecipe`` entries from ``mosaic_meta``."""

    def _seed(self, storage: SQLiteAdapter, payload: dict) -> None:
        with storage._transaction() as conn:
            set_meta(conn, META_KEY_INSTALLED_RECIPES, payload)

    def test_single_entry_without_parent(
        self, storage: SQLiteAdapter, client: MosaicClient
    ) -> None:
        self._seed(
            storage,
            {
                "org.example.foo": {
                    "id": "org.example.foo",
                    "version": "1.0.0",
                    "source": "https://example.org/foo-1.0.0.tar.gz",
                    "digest": "sha256:" + "a" * 64,
                    "installed_at": "2026-05-27T00:00:00+00:00",
                    "parent": None,
                },
            },
        )
        installed = client.recipe_list()
        assert len(installed) == 1
        rec = installed[0]
        assert isinstance(rec, InstalledRecipe)
        assert rec.id == "org.example.foo"
        assert rec.version == "1.0.0"
        assert rec.digest == "sha256:" + "a" * 64
        assert rec.parent is None

    def test_entry_with_parent_recipe_ref(
        self, storage: SQLiteAdapter, client: MosaicClient
    ) -> None:
        self._seed(
            storage,
            {
                "org.example.child": {
                    "id": "org.example.child",
                    "version": "0.2.0",
                    "source": "file:./child",
                    "digest": "sha256:" + "b" * 64,
                    "installed_at": "2026-05-27T01:00:00+00:00",
                    "parent": {
                        "id": "org.example.base",
                        "version": "0.1.0",
                        "source": "https://example.org/base-0.1.0.tar.gz",
                        "digest": "sha256:" + "c" * 64,
                    },
                },
            },
        )
        installed = client.recipe_list()
        assert len(installed) == 1
        assert installed[0].parent is not None
        assert installed[0].parent.id == "org.example.base"
        assert installed[0].parent.version == "0.1.0"
