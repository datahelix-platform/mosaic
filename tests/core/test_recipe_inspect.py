"""Tests for ``RecipeService.inspect`` (sec10 §10.2.3).

PR 5 of PTS-291 — read-only ``inspect`` against a ``FileResolver``-backed
recipe directory. Bootstrap install is PR 7 and lives in its own test
module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import RecipeManifestError
from mosaic.core.recipe import RecipeReport
from mosaic.core.recipe_service import RecipeService


MINIMAL_MANIFEST = """\
id: org.example.minimal
name: minimal
version: 0.1.0
created_at: 2026-05-27T00:00:00+00:00
mosaic_version: '>=0.3'
"""

MINIMAL_SCHEMA = """\
id: https://example.org/minimal
name: minimal
default_prefix: minimal
prefixes:
  minimal: https://example.org/minimal/
default_range: string
classes:
  Cell:
    attributes:
      cell_type:
        range: string
  Sample:
    attributes:
      collected_at:
        range: datetime
"""


@pytest.fixture
def recipe_dir(tmp_path: Path) -> Path:
    d = tmp_path / "minimal"
    d.mkdir()
    (d / "recipe.yaml").write_text(MINIMAL_MANIFEST)
    (d / "schema.yaml").write_text(MINIMAL_SCHEMA)
    return d


class TestInspectHappyPath:
    def test_inspect_returns_recipe_report(self, recipe_dir: Path) -> None:
        report = RecipeService().inspect(str(recipe_dir))
        assert isinstance(report, RecipeReport)

    def test_manifest_fields_round_trip(self, recipe_dir: Path) -> None:
        report = RecipeService().inspect(str(recipe_dir))
        m = report.manifest
        assert m.id == "org.example.minimal"
        assert m.name == "minimal"
        assert m.version == "0.1.0"
        assert m.mosaic_version == ">=0.3"

    def test_digest_is_present_and_hex(self, recipe_dir: Path) -> None:
        report = RecipeService().inspect(str(recipe_dir))
        assert isinstance(report.digest, str)
        assert len(report.digest) == 64
        int(report.digest, 16)

    def test_classes_and_slots_extracted(self, recipe_dir: Path) -> None:
        report = RecipeService().inspect(str(recipe_dir))
        assert set(report.classes) == {"Cell", "Sample"}
        # No top-level ``slots:`` in this fixture.
        assert report.slots == ()


class TestInspectStateChanges:
    def test_inspect_makes_no_state_change(self, recipe_dir: Path) -> None:
        before = sorted(p.name for p in recipe_dir.iterdir())
        RecipeService().inspect(str(recipe_dir))
        after = sorted(p.name for p in recipe_dir.iterdir())
        assert before == after


class TestInspectViaClientDelegator:
    def test_client_recipe_inspect_delegates(self, recipe_dir: Path) -> None:
        client = MosaicClient()
        report = client.recipe_inspect(str(recipe_dir))
        assert report.manifest.id == "org.example.minimal"


class TestInspectValidationFailures:
    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "bad"
        d.mkdir()
        # Missing ``mosaic_version``.
        (d / "recipe.yaml").write_text(
            "id: org.example.bad\n"
            "name: bad\n"
            "version: 0.0.1\n"
            "created_at: 2026-05-27T00:00:00+00:00\n"
        )
        with pytest.raises(RecipeManifestError) as exc:
            RecipeService().inspect(str(d))
        assert exc.value.source == str(d)
        assert exc.value.errors  # populated with at least one message

    def test_unknown_top_level_key_raises(self, recipe_dir: Path) -> None:
        (recipe_dir / "recipe.yaml").write_text(
            MINIMAL_MANIFEST + "unknown_key: hello\n"
        )
        with pytest.raises(RecipeManifestError):
            RecipeService().inspect(str(recipe_dir))

    def test_retired_hippo_version_key_names_its_replacement(
        self, recipe_dir: Path
    ) -> None:
        # Platform ADR-0007: ``hippo_version`` is now ``mosaic_version``.
        (recipe_dir / "recipe.yaml").write_text(
            MINIMAL_MANIFEST.replace("mosaic_version:", "hippo_version:")
        )
        with pytest.raises(RecipeManifestError, match="renamed to `mosaic_version`"):
            RecipeService().inspect(str(recipe_dir))

    def test_recipe_yaml_missing_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(RecipeManifestError, match="missing recipe.yaml"):
            RecipeService().inspect(str(d))

    def test_recipe_yaml_malformed_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "broken"
        d.mkdir()
        (d / "recipe.yaml").write_text("::: this is not yaml")
        with pytest.raises(RecipeManifestError):
            RecipeService().inspect(str(d))


class TestInspectOptionalSchema:
    def test_no_schema_yaml_yields_empty_elements(self, tmp_path: Path) -> None:
        d = tmp_path / "manifest-only"
        d.mkdir()
        (d / "recipe.yaml").write_text(MINIMAL_MANIFEST)
        report = RecipeService().inspect(str(d))
        assert report.classes == ()
        assert report.slots == ()
