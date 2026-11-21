"""Integration tests for the ``mosaic validate`` CLI command.

The CLI runs real LinkML validation (sec9 PR 3.2):
- ``--schema PATH``: loads via ``SchemaRegistry.from_path`` — fails on
  non-LinkML YAML, even when it parses as a dict.
- ``--schema PATH --data BUNDLE``: validates a tree-root instance bundle
  against the schema, surfacing field-level LinkML errors.

The key acceptance regression for GitHub Issue #1: the "garbage" schema
``{this_is_not_linkml: true, random_key: 42, noise: [a, b, c]}`` must
exit non-zero with a real LinkML error.
"""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mosaic.cli.main import app

runner = CliRunner()


# Minimal LinkML schema used across happy-path tests. Imports mosaic_core so
# concrete classes (Project, Sample) extend Entity and the tree-root bundle
# carries the matching multivalued slots.
VALID_SCHEMA_YAML = """\
id: https://example.org/mosaic/test/validate_cli
name: validate_cli_schema
description: Minimal LinkML schema for validate CLI tests.

prefixes:
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types
  - mosaic_core

default_range: string

classes:
  Project:
    is_a: Entity
    attributes:
      name:
        required: true

  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      project_id:
        range: Project
"""


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def valid_schema(tmp_dir: Path) -> Path:
    path = tmp_dir / "schema.yaml"
    path.write_text(VALID_SCHEMA_YAML)
    return path


class TestValidateCLISchema:
    def test_valid_linkml_schema_passes(self, valid_schema: Path):
        result = runner.invoke(app, ["validate", "--schema", str(valid_schema)])
        assert result.exit_code == 0, result.output
        assert "Schema is valid LinkML" in result.output
        assert "classes" in result.output

    def test_garbage_schema_fails(self, tmp_dir: Path):
        """Issue #1 Test 2 — the killer demo.

        A YAML file with no LinkML structure must exit non-zero with a
        real error message. Before PR 3.2 this returned "all checks
        passed" — the regression that motivated the rewrite.
        """
        path = tmp_dir / "garbage.yaml"
        path.write_text(
            "this_is_not_linkml: true\n"
            "random_key: 42\n"
            "noise: [a, b, c]\n"
        )
        result = runner.invoke(app, ["validate", "--schema", str(path)])
        assert result.exit_code == 1
        assert "Invalid LinkML schema" in result.output

    def test_missing_schema_file(self, tmp_dir: Path):
        result = runner.invoke(
            app, ["validate", "--schema", str(tmp_dir / "missing.yaml")]
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_malformed_yaml_schema_fails(self, tmp_dir: Path):
        path = tmp_dir / "broken.yaml"
        path.write_text("classes:\n  Foo: { unclosed: \n")
        result = runner.invoke(app, ["validate", "--schema", str(path)])
        assert result.exit_code == 1

    def test_schema_directory(self, tmp_dir: Path):
        schema_dir = tmp_dir / "schemas"
        schema_dir.mkdir()
        (schema_dir / "schema.yaml").write_text(VALID_SCHEMA_YAML)
        result = runner.invoke(app, ["validate", "--schema", str(schema_dir)])
        assert result.exit_code == 0, result.output
        assert "Schema is valid LinkML" in result.output


class TestValidateCLIData:
    def test_valid_bundle_passes(self, valid_schema: Path, tmp_dir: Path):
        bundle = tmp_dir / "bundle.yaml"
        bundle.write_text(
            "projects:\n"
            "  - id: p1\n"
            "    name: Project One\n"
            "    is_available: true\n"
            "samples:\n"
            "  - id: s1\n"
            "    name: Tissue A\n"
            "    project_id: p1\n"
            "    is_available: true\n"
        )
        result = runner.invoke(
            app,
            ["validate", "--schema", str(valid_schema), "--data", str(bundle)],
        )
        assert result.exit_code == 0, result.output
        assert "Validation complete" in result.output

    def test_missing_required_field_fails(
        self, valid_schema: Path, tmp_dir: Path
    ):
        """Issue #1 Test 5 — entity missing a required field must fail
        with a field-named error.
        """
        bundle = tmp_dir / "broken.yaml"
        bundle.write_text(
            "samples:\n"
            "  - id: s1\n"
            "    project_id: p1\n"
            "    is_available: true\n"
        )
        result = runner.invoke(
            app,
            ["validate", "--schema", str(valid_schema), "--data", str(bundle)],
        )
        assert result.exit_code == 1
        assert "name" in result.output
        assert "required" in result.output

    def test_unknown_top_level_slot_fails(
        self, valid_schema: Path, tmp_dir: Path
    ):
        bundle = tmp_dir / "rogue.yaml"
        bundle.write_text(
            "not_a_class:\n"
            "  - id: x\n"
        )
        result = runner.invoke(
            app,
            ["validate", "--schema", str(valid_schema), "--data", str(bundle)],
        )
        assert result.exit_code == 1

    def test_data_without_schema_errors(self, tmp_dir: Path):
        bundle = tmp_dir / "bundle.yaml"
        bundle.write_text("projects: []\n")
        result = runner.invoke(app, ["validate", "--data", str(bundle)])
        assert result.exit_code == 1
        assert "--data requires --schema" in result.output

    def test_data_file_missing(self, valid_schema: Path, tmp_dir: Path):
        result = runner.invoke(
            app,
            [
                "validate",
                "--schema",
                str(valid_schema),
                "--data",
                str(tmp_dir / "missing.yaml"),
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_data_not_a_mapping(self, valid_schema: Path, tmp_dir: Path):
        bundle = tmp_dir / "list.yaml"
        bundle.write_text("- item1\n- item2\n")
        result = runner.invoke(
            app,
            ["validate", "--schema", str(valid_schema), "--data", str(bundle)],
        )
        assert result.exit_code == 1
        assert "mapping" in result.output


class TestValidateCLIDefault:
    def test_no_args_validates_default_config(self):
        result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0
        assert "Default configuration is valid" in result.output


class TestValidateCLIConfig:
    """The legacy ``--config`` flag is preserved (out of scope for PR 3.2)."""

    def test_valid_config(self, tmp_dir: Path):
        path = tmp_dir / "config.yaml"
        path.write_text("storage:\n  backend: sqlite\n")
        result = runner.invoke(app, ["validate", "--config", str(path)])
        assert result.exit_code == 0
        assert "Validation complete" in result.output

    def test_missing_config(self, tmp_dir: Path):
        result = runner.invoke(
            app, ["validate", "--config", str(tmp_dir / "missing.yaml")]
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_config_not_dict(self, tmp_dir: Path):
        path = tmp_dir / "bad_config.yaml"
        path.write_text("just a string\n")
        result = runner.invoke(app, ["validate", "--config", str(path)])
        assert result.exit_code == 1
        assert "Invalid config format" in result.output
