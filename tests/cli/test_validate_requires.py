"""Integration tests for ``mosaic validate --schema`` `requires:` directive.

Exercises PTS-227 acceptance criteria end-to-end through the Typer CLI.
The Python-distribution lookup is patched so tests stay hermetic — no
real ``mosaic-reference-*`` package needs to be on the test environment.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mosaic.cli.main import app


SCHEMA_TEMPLATE = """\
id: https://example.org/mosaic/test/requires_cli
name: requires_cli_schema

prefixes:
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types
  - mosaic_core

default_range: string

{requires_block}
classes:
  Project:
    is_a: Entity
    attributes:
      name:
        required: true
"""


@pytest.fixture()
def runner():
    return CliRunner()


def _write_schema(tmp_path: Path, requires_lines: list[str] | None) -> Path:
    if requires_lines is None:
        block = ""
    else:
        block = "requires:\n" + "".join(f"  - {ln}\n" for ln in requires_lines)
    schema = tmp_path / "schema.yaml"
    schema.write_text(SCHEMA_TEMPLATE.format(requires_block=block))
    return schema


class TestValidateRequiresCLI:
    def test_exact_match_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("mosaic.requires._dist_version", lambda name: "3.3")
        schema = _write_schema(tmp_path, ["mosaic-reference-fma==3.3"])
        result = runner.invoke(app, ["validate", "--schema", str(schema)])
        assert result.exit_code == 0, result.output
        assert "requires" in result.output.lower()
        assert "satisfied" in result.output

    def test_no_requires_block_passes_silently(
        self, runner: CliRunner, tmp_path: Path
    ):
        schema = _write_schema(tmp_path, None)
        result = runner.invoke(app, ["validate", "--schema", str(schema)])
        assert result.exit_code == 0, result.output
        assert "satisfied" not in result.output

    def test_range_comparator_rejected(
        self, runner: CliRunner, tmp_path: Path
    ):
        schema = _write_schema(tmp_path, ["mosaic-reference-fma>=3.3"])
        result = runner.invoke(app, ["validate", "--schema", str(schema)])
        assert result.exit_code == 1
        assert "exact-match" in result.output

    def test_missing_loader_fails_with_install_hint(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def _missing(name: str) -> str:
            raise PackageNotFoundError(name)

        monkeypatch.setattr("mosaic.requires._dist_version", _missing)
        schema = _write_schema(tmp_path, ["mosaic-reference-fma==3.3"])
        result = runner.invoke(app, ["validate", "--schema", str(schema)])
        assert result.exit_code == 1
        assert "mosaic-reference-fma" in result.output
        assert "not installed" in result.output
        assert "mosaic reference install fma --version 3.3" in result.output

    def test_version_mismatch_fails_with_install_hint(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("mosaic.requires._dist_version", lambda name: "3.2")
        schema = _write_schema(tmp_path, ["mosaic-reference-fma==3.3"])
        result = runner.invoke(app, ["validate", "--schema", str(schema)])
        assert result.exit_code == 1
        assert "mosaic-reference-fma==3.3" in result.output
        assert "version 3.2 is installed" in result.output
        assert "mosaic reference install fma --version 3.3" in result.output
