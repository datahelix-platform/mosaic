"""Tests for ``RecipeService.import_`` — the bootstrap install path (sec10 §10.4).

PR 7 of PTS-291. Verifies:

- Happy-path file: install creates the ``installed_recipes`` meta
  entry AND emits the ``recipe_imported`` provenance event in one
  transaction.
- Dry-run leaves no state change.
- Bottom-up dependency resolution + cycle detection.
- ``mosaic_version`` incompatibility rejection.
- The no-in-place-override merge guard fires through ``import_``.
- A failure mid-merge rolls back both the meta write and the provenance
  event (atomicity, invariant 3).
- The ``recipe_imported`` operation is a recognised Operation enum value.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

import mosaic
from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import (
    RecipeLineageCycleError,
    RecipeSchemaError,
    RecipeVersionIncompatibleError,
)
from mosaic.core.meta import get_meta, set_meta
from mosaic.core.recipe_service import (
    META_KEY_INSTALLED_RECIPES,
    RecipeService,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry


def _make_recipe(
    root: Path,
    *,
    recipe_id: str,
    name: str,
    version: str = "0.1.0",
    parent: dict | None = None,
    requires: dict | None = None,
    mosaic_version: str = ">=0.0",
    extra_classes: dict | None = None,
) -> Path:
    """Build a minimal recipe directory under ``root``."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)

    manifest_lines = [
        f"id: {recipe_id}",
        f"name: {name}",
        f"version: {version}",
        "created_at: '2026-05-27T00:00:00+00:00'",
        f"mosaic_version: '{mosaic_version}'",
    ]
    if parent is not None:
        manifest_lines.append("parent:")
        for k, v in parent.items():
            if v is None:
                continue
            manifest_lines.append(f"  {k}: {v}")
    if requires is not None and requires.get("recipes"):
        manifest_lines.append("requires:")
        manifest_lines.append("  recipes:")
        for ref in requires["recipes"]:
            manifest_lines.append(f"    - id: {ref['id']}")
            manifest_lines.append(f"      version: {ref['version']}")
            manifest_lines.append(f"      source: {ref['source']}")
            if ref.get("digest"):
                manifest_lines.append(f"      digest: {ref['digest']}")
    (d / "recipe.yaml").write_text("\n".join(manifest_lines) + "\n")

    classes = {name.capitalize(): {"attributes": {"label": {"range": "string"}}}}
    if extra_classes:
        classes.update(extra_classes)
    cls_yaml = "\n".join(
        [
            "classes:",
            *[
                f"  {cls_name}:\n    attributes:\n      "
                + "\n      ".join(
                    f"{a}:\n        range: {body['range']}"
                    for a, body in (cls_body.get("attributes") or {}).items()
                )
                for cls_name, cls_body in classes.items()
            ],
        ]
    )
    schema = dedent(
        f"""
        id: https://example.org/{name}
        name: {name}
        default_prefix: {name}
        prefixes:
          {name}: https://example.org/{name}/
        default_range: string
        """
    ).strip() + "\n" + cls_yaml + "\n"
    (d / "schema.yaml").write_text(schema)
    return d


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "import.db")


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


class TestImportHappyPath:
    def test_install_creates_meta_entry(
        self,
        client: MosaicClient,
        storage: SQLiteAdapter,
        tmp_path: Path,
    ) -> None:
        recipe_dir = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        result = client.recipe_import(str(recipe_dir))

        assert result.dry_run is False
        assert len(result.installed) == 1
        assert result.installed[0].id == "org.example.solo"

        with storage._transaction() as conn:
            installed = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}
        assert "org.example.solo" in installed
        assert installed["org.example.solo"]["version"] == "0.1.0"
        assert len(installed["org.example.solo"]["digest"]) == 64

    def test_install_emits_recipe_imported_provenance(
        self,
        client: MosaicClient,
        storage: SQLiteAdapter,
        tmp_path: Path,
    ) -> None:
        recipe_dir = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe_dir))

        with storage._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT operation, patch FROM "ProvenanceRecord" '
                "WHERE operation = ?",
                ("recipe_imported",),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        import json

        patch = json.loads(rows[0][1])
        assert patch["recipe_id"] == "org.example.solo"
        assert patch["recipe_version"] == "0.1.0"
        assert "Solo" in patch["classes_added"]

    def test_installed_recipes_returned_after_install(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        recipe_dir = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe_dir))
        installed = client.recipe_list()
        assert len(installed) == 1
        assert installed[0].id == "org.example.solo"


class TestDryRun:
    def test_dry_run_makes_no_state_change(
        self,
        client: MosaicClient,
        storage: SQLiteAdapter,
        tmp_path: Path,
    ) -> None:
        recipe_dir = _make_recipe(
            tmp_path, recipe_id="org.example.dry", name="dry"
        )
        result = client.recipe_import(str(recipe_dir), dry_run=True)
        assert result.dry_run is True
        assert len(result.installed) == 1

        with storage._transaction() as conn:
            installed = get_meta(conn, META_KEY_INSTALLED_RECIPES)
            cur = conn.cursor()
            cur.execute(
                'SELECT COUNT(*) FROM "ProvenanceRecord" '
                "WHERE operation = ?",
                ("recipe_imported",),
            )
            count = cur.fetchone()[0]

        assert installed is None or "org.example.dry" not in installed
        assert count == 0


class TestDependencyResolution:
    def test_parent_installed_before_child(
        self,
        client: MosaicClient,
        storage: SQLiteAdapter,
        tmp_path: Path,
    ) -> None:
        parent_dir = _make_recipe(
            tmp_path, recipe_id="org.example.parent", name="parent"
        )
        child_dir = _make_recipe(
            tmp_path,
            recipe_id="org.example.child",
            name="child",
            parent={
                "id": "org.example.parent",
                "version": "0.1.0",
                "source": "../parent",
            },
        )
        result = client.recipe_import(str(child_dir))

        ids_in_order = [r.id for r in result.installed]
        assert ids_in_order == ["org.example.parent", "org.example.child"]

        with storage._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT patch FROM "ProvenanceRecord" '
                "WHERE operation = ? ORDER BY timestamp",
                ("recipe_imported",),
            )
            rows = cur.fetchall()
        import json

        recipe_ids = [json.loads(r[0])["recipe_id"] for r in rows]
        assert recipe_ids == ["org.example.parent", "org.example.child"]

    def test_lineage_cycle_detected(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        # A requires B, B requires A — cycle.
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        _make_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            requires={"recipes": [{"id": "org.example.b", "version": "0.1.0", "source": "../b"}]},
        )
        _make_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            requires={"recipes": [{"id": "org.example.a", "version": "0.1.0", "source": "../a"}]},
        )

        with pytest.raises(RecipeLineageCycleError) as exc:
            client.recipe_import(str(a_dir))
        assert "org.example.a" in exc.value.cycle


class TestVersionCompatibility:
    def test_incompatible_mosaic_version_rejected(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        recipe_dir = _make_recipe(
            tmp_path,
            recipe_id="org.example.future",
            name="future",
            mosaic_version=">=99.99",
        )
        with pytest.raises(RecipeVersionIncompatibleError):
            client.recipe_import(str(recipe_dir))


class TestOverrideGuardThroughImport:
    def test_in_place_override_rejected(
        self,
        client: MosaicClient,
        tmp_path: Path,
    ) -> None:
        first = _make_recipe(
            tmp_path, recipe_id="org.example.first", name="first"
        )
        client.recipe_import(str(first))

        # Build a recipe whose default_prefix is `first` AND whose
        # schema redefines the `First` class — this should be rejected
        # by check_no_inplace_override.
        offender = tmp_path / "offender"
        offender.mkdir()
        (offender / "recipe.yaml").write_text(
            dedent(
                """\
                id: org.example.offender
                name: offender
                version: 0.1.0
                created_at: '2026-05-27T00:00:00+00:00'
                mosaic_version: '>=0.0'
                """
            )
        )
        (offender / "schema.yaml").write_text(
            dedent(
                """\
                id: https://example.org/offender
                name: offender
                default_prefix: offender
                prefixes:
                  offender: https://example.org/offender/
                default_range: string
                classes:
                  First:
                    attributes:
                      hijacked:
                        range: string
                """
            )
        )

        with pytest.raises(RecipeSchemaError):
            client.recipe_import(str(offender))


class TestAtomicity:
    def test_failed_merge_rolls_back_meta_and_provenance(
        self,
        client: MosaicClient,
        storage: SQLiteAdapter,
        tmp_path: Path,
    ) -> None:
        # Install one recipe successfully so there is an "upstream"
        # provided_by annotation for the failing import to clash with.
        first = _make_recipe(
            tmp_path, recipe_id="org.example.first", name="first"
        )
        client.recipe_import(str(first))

        with storage._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT COUNT(*) FROM "ProvenanceRecord" '
                "WHERE operation = ?",
                ("recipe_imported",),
            )
            count_before = cur.fetchone()[0]

        # Offender redefines First.
        offender = tmp_path / "offender2"
        offender.mkdir()
        (offender / "recipe.yaml").write_text(
            dedent(
                """\
                id: org.example.offender2
                name: offender2
                version: 0.1.0
                created_at: '2026-05-27T00:00:00+00:00'
                mosaic_version: '>=0.0'
                """
            )
        )
        (offender / "schema.yaml").write_text(
            dedent(
                """\
                id: https://example.org/offender2
                name: offender2
                default_prefix: offender2
                prefixes:
                  offender2: https://example.org/offender2/
                default_range: string
                classes:
                  First:
                    attributes:
                      x:
                        range: string
                """
            )
        )

        with pytest.raises(RecipeSchemaError):
            client.recipe_import(str(offender))

        # Atomicity: no new meta entry, no new provenance.
        with storage._transaction() as conn:
            installed = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}
            cur = conn.cursor()
            cur.execute(
                'SELECT COUNT(*) FROM "ProvenanceRecord" '
                "WHERE operation = ?",
                ("recipe_imported",),
            )
            count_after = cur.fetchone()[0]

        assert "org.example.offender2" not in installed
        assert count_after == count_before


class TestRecipeImportedOperationEnum:
    def test_operation_value_recognized(self) -> None:
        from mosaic.core.types import Operation

        assert Operation.recipe_imported.value == "recipe_imported"
