"""Tests for the ``recipe_manifest`` LinkML schema (sec10 §10.3.2 / §10.3.3).

PR 1 of PTS-290 — Phase 2 schema-level groundwork. Validates that a
well-formed ``RecipeManifest`` example loads via ``SchemaView`` and that
the LinkML-native validator (with ``closed=True``) rejects each missing
required field.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin
from linkml_runtime.utils.schemaview import SchemaView


SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "src"
    / "mosaic"
    / "schemas"
    / "recipe_manifest.yaml"
)


@pytest.fixture(scope="module")
def schema_view() -> SchemaView:
    return SchemaView(str(SCHEMA_PATH))


@pytest.fixture(scope="module")
def validator(schema_view: SchemaView) -> Validator:
    return Validator(
        schema=str(SCHEMA_PATH),
        validation_plugins=[JsonschemaValidationPlugin(closed=True)],
    )


@pytest.fixture
def valid_manifest() -> dict:
    """Hand-written manifest exercising every documented field."""
    return {
        "id": "org.broad.scrnaseq",
        "name": "scrnaseq",
        "version": "1.2.0",
        "description": "Single-cell RNA-seq classes for the Broad Institute.",
        "author": {
            "name": "Jane Doe",
            "email": "jane@broadinstitute.org",
            "organization": "Broad Institute",
        },
        "license": "MIT",
        "created_at": "2026-05-27T14:30:00+00:00",
        "mosaic_version": ">=0.3,<0.5",
        "source": "https://zenodo.org/record/12345",
        "parent": {
            "id": "org.broad.bioinformatics-base",
            "version": "0.4.0",
            "source": "https://zenodo.org/record/9999/files/base-0.4.0.tar.gz",
            "digest": "sha256:" + "a" * 64,
        },
        "requires": {
            "recipes": [
                {
                    "id": "org.broad.cell-ontology",
                    "version": "2026-01-01",
                    "source": "file:./vendored/cell-ontology",
                }
            ],
            "reference_loaders": ["mosaic-reference-ensembl==115"],
        },
    }


class TestRecipeManifestSchema:
    """Schema loads cleanly via ``SchemaView`` and declares both top-level classes."""

    def test_schema_loads(self, schema_view: SchemaView) -> None:
        assert schema_view.schema.name == "recipe_manifest"

    def test_declares_recipe_manifest_class(self, schema_view: SchemaView) -> None:
        cls = schema_view.get_class("RecipeManifest")
        assert cls is not None
        assert cls.tree_root is True

    def test_declares_recipe_ref_class(self, schema_view: SchemaView) -> None:
        cls = schema_view.get_class("RecipeRef")
        assert cls is not None

    def test_recipe_manifest_required_slots(self, schema_view: SchemaView) -> None:
        induced = {
            slot.name: slot
            for slot in schema_view.class_induced_slots("RecipeManifest")
        }
        required_names = {name for name, slot in induced.items() if slot.required}
        assert required_names == {"id", "name", "version", "created_at", "mosaic_version"}


class TestValidManifest:
    """A complete manifest validates without errors."""

    def test_valid_manifest_passes(
        self, validator: Validator, valid_manifest: dict
    ) -> None:
        report = validator.validate(valid_manifest, "RecipeManifest")
        assert report.results == [], [r.message for r in report.results]

    def test_minimal_required_only(self, validator: Validator) -> None:
        minimal = {
            "id": "org.example.minimal",
            "name": "minimal",
            "version": "0.1.0",
            "created_at": "2026-05-27T00:00:00+00:00",
            "mosaic_version": ">=0.3",
        }
        report = validator.validate(minimal, "RecipeManifest")
        assert report.results == [], [r.message for r in report.results]


class TestRequiredFieldsRejected:
    """Each required field, when missing, yields a validation error."""

    @pytest.mark.parametrize(
        "missing_field", ["id", "name", "version", "created_at", "mosaic_version"]
    )
    def test_missing_required_field_fails(
        self, validator: Validator, valid_manifest: dict, missing_field: str
    ) -> None:
        bad = copy.deepcopy(valid_manifest)
        bad.pop(missing_field)
        report = validator.validate(bad, "RecipeManifest")
        assert report.results, (
            f"Expected validation error when {missing_field!r} missing, got none"
        )
        joined = " ".join(r.message for r in report.results).lower()
        assert missing_field.lower() in joined or "required" in joined


class TestRecipeRefShape:
    """``RecipeRef`` requires ``id``/``version``/``source``; ``digest`` is optional."""

    def test_recipe_ref_required_slots(self, schema_view: SchemaView) -> None:
        induced = {
            slot.name: slot
            for slot in schema_view.class_induced_slots("RecipeRef")
        }
        required_names = {name for name, slot in induced.items() if slot.required}
        assert required_names == {"id", "version", "source"}
        assert "digest" in induced and not induced["digest"].required

    def test_parent_recipe_ref_validates(
        self, validator: Validator, valid_manifest: dict
    ) -> None:
        report = validator.validate(valid_manifest, "RecipeManifest")
        assert report.results == [], [r.message for r in report.results]

    def test_parent_missing_required_field_fails(
        self, validator: Validator, valid_manifest: dict
    ) -> None:
        bad = copy.deepcopy(valid_manifest)
        bad["parent"].pop("source")
        report = validator.validate(bad, "RecipeManifest")
        assert report.results, "Expected error when parent.source is missing"


class TestClosedSchemaRejectsUnknownKeys:
    """Closed-schema validation rejects undeclared keys at every nesting level."""

    def test_unknown_top_level_key_rejected(
        self, validator: Validator, valid_manifest: dict
    ) -> None:
        bad = copy.deepcopy(valid_manifest)
        bad["unknown_top_level"] = "nope"
        report = validator.validate(bad, "RecipeManifest")
        assert report.results, "Expected closed-schema rejection of unknown_top_level"

    def test_unknown_author_key_rejected(
        self, validator: Validator, valid_manifest: dict
    ) -> None:
        bad = copy.deepcopy(valid_manifest)
        bad["author"]["pager"] = "555-1212"
        report = validator.validate(bad, "RecipeManifest")
        assert report.results, "Expected closed-schema rejection of author.pager"
