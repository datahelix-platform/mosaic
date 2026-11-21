"""Tests for ``RecipeService.extend`` (sec10 §10.7.3).

PR 9 of PTS-292. Verifies:

- ``extend(installed_id, out_dir)`` writes ``recipe.yaml`` + ``schema.yaml``.
- ``recipe.yaml.parent`` is populated from the matching ``installed_recipes``
  entry (id, version, source, digest).
- The scaffolded manifest passes closed-schema validation against
  ``recipe_manifest.yaml`` once the author replaces the TODO stubs.
- The extended directory can itself be inspected by
  :meth:`RecipeService.inspect` so the lineage is real.
- Unknown ``installed_id`` raises ``ValueError``.
- Refuses to overwrite an existing ``recipe.yaml`` or ``schema.yaml``.
- Invariant 5: ``extend`` is the only operation that creates a ``parent``
  lineage pointer.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from mosaic.core.client import MosaicClient
from mosaic.core.recipe_service import RecipeService
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry


def _make_recipe(root: Path, *, recipe_id: str, name: str) -> Path:
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
        yield os.path.join(tmpdir, "extend.db")


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


@pytest.fixture
def parent_recipe(client: MosaicClient, tmp_path: Path) -> Path:
    """Import an upstream recipe so it appears in installed_recipes."""
    recipe_dir = _make_recipe(
        tmp_path, recipe_id="org.example.parent", name="parent"
    )
    client.recipe_import(str(recipe_dir))
    return recipe_dir


class TestExtendHappyPath:
    def test_writes_both_files(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        result = client.recipe_extend("org.example.parent", out_dir)
        assert result == out_dir
        assert (out_dir / "recipe.yaml").is_file()
        assert (out_dir / "schema.yaml").is_file()

    def test_creates_missing_out_dir(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "nested" / "child"
        assert not out_dir.exists()
        client.recipe_extend("org.example.parent", out_dir)
        assert out_dir.is_dir()

    def test_parent_field_populated_from_installed(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", out_dir)
        manifest = yaml.safe_load((out_dir / "recipe.yaml").read_text())
        assert manifest["parent"]["id"] == "org.example.parent"
        assert manifest["parent"]["version"] == "0.1.0"
        assert manifest["parent"]["source"] == str(parent_recipe)
        # Digest carries the sha256: prefix for portability.
        assert manifest["parent"]["digest"].startswith("sha256:")
        assert len(manifest["parent"]["digest"]) == len("sha256:") + 64

    def test_manifest_has_author_fillable_stubs(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", out_dir)
        manifest = yaml.safe_load((out_dir / "recipe.yaml").read_text())
        # Stubs the author MUST replace before sharing.
        assert "TODO" in manifest["id"]
        assert "TODO" in manifest["name"]
        # But version and mosaic_version are real strings so closed-schema
        # validation passes once id/name are set.
        assert manifest["version"]
        assert manifest["mosaic_version"]
        assert manifest["created_at"]

    def test_schema_fragment_is_empty_but_valid_linkml(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", out_dir)
        fragment = yaml.safe_load((out_dir / "schema.yaml").read_text())
        # Empty of user content but structurally a LinkML schema.
        assert "id" in fragment
        assert "name" in fragment
        assert "default_prefix" in fragment
        # No classes / slots — author starts from scratch.
        assert "classes" not in fragment or not fragment["classes"]
        assert "slots" not in fragment or not fragment["slots"]

    def test_extended_recipe_round_trips_through_inspect(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        """Author replaces the TODO stubs → recipe is inspectable.

        Demonstrates the extend → publish workflow: the scaffold is
        valid once authors set their identity. This also exercises the
        parent-lineage round-trip through ``inspect``.
        """
        out_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", out_dir)
        manifest = yaml.safe_load((out_dir / "recipe.yaml").read_text())
        manifest["id"] = "org.example.child"
        manifest["name"] = "child"
        manifest["version"] = "0.1.0"
        (out_dir / "recipe.yaml").write_text(yaml.safe_dump(manifest))

        service = RecipeService()
        report = service.inspect(out_dir)
        assert report.manifest.id == "org.example.child"
        assert report.manifest.parent is not None
        assert report.manifest.parent.id == "org.example.parent"


class TestExtendErrors:
    def test_unknown_installed_id_raises(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="not found in installed_recipes"):
            client.recipe_extend("org.example.nope", tmp_path / "child")

    def test_refuses_to_overwrite_recipe_yaml(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        out_dir.mkdir()
        (out_dir / "recipe.yaml").write_text("preexisting: true\n")
        with pytest.raises(ValueError, match="already exists"):
            client.recipe_extend("org.example.parent", out_dir)

    def test_refuses_to_overwrite_schema_yaml(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        out_dir.mkdir()
        (out_dir / "schema.yaml").write_text("preexisting: true\n")
        with pytest.raises(ValueError, match="already exists"):
            client.recipe_extend("org.example.parent", out_dir)

    def test_rejects_non_directory_out_dir(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "child"
        out_path.write_text("I am a file, not a dir")
        with pytest.raises(ValueError, match="not a directory"):
            client.recipe_extend("org.example.parent", out_path)


class TestExtendInvariant5:
    """Invariant 5: ``extend`` is the ONLY operation that creates a
    ``parent`` lineage pointer.

    The complementary half of this invariant — that ``import`` and
    ``export`` do NOT create implicit lineage — lives in their own test
    modules. Here we just verify that ``extend`` always writes a non-null
    ``parent`` block.
    """

    def test_parent_is_always_set(
        self, client: MosaicClient, parent_recipe: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", out_dir)
        manifest = yaml.safe_load((out_dir / "recipe.yaml").read_text())
        assert "parent" in manifest
        assert manifest["parent"] is not None
        assert manifest["parent"]["id"]
        assert manifest["parent"]["version"]
        assert manifest["parent"]["source"]
