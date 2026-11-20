"""Tests for ``RecipeService.export`` (sec10 §10.5).

PR 8 of PTS-291. Verifies selectivity, ``requires.recipes``
auto-population, and the integration acceptance test from the issue:
"instance with one imported recipe + local additions exports only the
local additions."

``export`` is read-only — no DB writes, no merge, no provenance — so
all assertions check the returned :class:`RecipeExport` shape.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.recipe import RecipeExport
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry


def _make_upstream_recipe(
    root: Path, *, recipe_id: str, name: str
) -> Path:
    """Build a minimal recipe directory the export test can `import_`."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "recipe.yaml").write_text(
        dedent(
            f"""\
            id: {recipe_id}
            name: {name}
            version: 0.1.0
            created_at: '2026-05-27T00:00:00+00:00'
            mosaic_version: '>=0.0'
            """
        )
    )
    cls_pascal = name.capitalize()
    (d / "schema.yaml").write_text(
        dedent(
            f"""\
            id: https://example.org/{name}
            name: {name}
            default_prefix: {name}
            prefixes:
              {name}: https://example.org/{name}/
            default_range: string
            classes:
              {cls_pascal}:
                attributes:
                  label:
                    range: string
            """
        )
    )
    return d


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "export.db")


@pytest.fixture
def storage(db_path):
    return SQLiteAdapter(
        db_path, schema_registry=_build_minimal_schema_registry()
    )


@pytest.fixture
def client(storage):
    return MosaicClient(
        storage=storage,
        registry=_build_minimal_schema_registry(),
        bypass_validation=True,
    )


class TestExportSelectivity:
    def test_framework_classes_excluded(self, client: MosaicClient) -> None:
        """Mosaic core's ``Entity``/``ProvenanceRecord`` MUST NOT appear."""
        result = client.recipe_export()
        classes = result.schema_fragment.get("classes") or {}
        assert "Entity" not in classes
        assert "ProvenanceRecord" not in classes
        assert "Process" not in classes

    def test_local_user_classes_included(self, client: MosaicClient) -> None:
        """The minimal-registry user classes (Sample, Donor, …) ARE exported."""
        result = client.recipe_export()
        classes = result.schema_fragment.get("classes") or {}
        assert "Sample" in classes
        assert "Donor" in classes

    def test_imported_recipe_classes_excluded(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        """Acceptance test (issue): import a recipe, export, recipe's
        classes do NOT re-appear in the export."""
        upstream = _make_upstream_recipe(
            tmp_path, recipe_id="org.example.upstream", name="upstream"
        )
        client.recipe_import(str(upstream))

        result = client.recipe_export()
        classes = result.schema_fragment.get("classes") or {}
        # `Upstream` was provided by the imported recipe → must be excluded.
        assert "Upstream" not in classes
        # Local user classes still pass.
        assert "Sample" in classes


class TestExportProvidedByStripped:
    def test_no_provided_by_annotation_in_export(self, client: MosaicClient) -> None:
        result = client.recipe_export()
        for body in (result.schema_fragment.get("classes") or {}).values():
            anns = body.get("annotations") or {}
            if isinstance(anns, dict):
                assert "provided_by" not in anns


class TestExportManifestStub:
    def test_manifest_carries_required_fields(self, client: MosaicClient) -> None:
        result = client.recipe_export()
        m = result.manifest
        assert "id" in m
        assert "name" in m
        assert "version" in m
        assert "created_at" in m
        assert "mosaic_version" in m

    def test_manifest_stubs_marked_TODO(self, client: MosaicClient) -> None:
        result = client.recipe_export()
        m = result.manifest
        assert "TODO" in m["id"]
        assert "TODO" in m["name"]

    def test_manifest_omits_parent_when_not_requested(
        self, client: MosaicClient
    ) -> None:
        result = client.recipe_export()
        assert "parent" not in result.manifest


class TestExportParent:
    def test_explicit_parent_populated_from_installed(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        upstream = _make_upstream_recipe(
            tmp_path, recipe_id="org.example.parent", name="parent"
        )
        client.recipe_import(str(upstream))

        result = client.recipe_export(parent="org.example.parent")
        assert result.manifest.get("parent") is not None
        assert result.manifest["parent"]["id"] == "org.example.parent"
        assert result.manifest["parent"]["version"] == "0.1.0"

    def test_unknown_parent_raises(self, client: MosaicClient) -> None:
        with pytest.raises(ValueError, match="not found in installed_recipes"):
            client.recipe_export(parent="org.example.nope")


class TestExportRequiresAutoPopulation:
    def test_no_requires_when_no_upstream_referenced(
        self, client: MosaicClient
    ) -> None:
        """A clean instance exports with empty requires.recipes."""
        result = client.recipe_export()
        assert result.auto_resolved_requires == ()
        assert "requires" not in result.manifest


class TestExportReturnShape:
    def test_returns_recipe_export(self, client: MosaicClient) -> None:
        result = client.recipe_export()
        assert isinstance(result, RecipeExport)
        assert isinstance(result.manifest, dict)
        assert isinstance(result.schema_fragment, dict)
        assert isinstance(result.auto_resolved_requires, tuple)


class TestExportScopeRestriction:
    def test_unknown_scope_rejected(self, client: MosaicClient) -> None:
        with pytest.raises(ValueError, match="scope='schema'"):
            client.recipe_export(scope="data")
