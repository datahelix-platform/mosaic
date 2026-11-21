"""Tests for ``mosaic entity`` inspection verbs and ``mosaic status``."""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mosaic.cli.main import app


SCHEMA_YAML = """\
id: https://example.org/mosaic/test/entity_cli
name: entity_cli_schema
description: Minimal LinkML schema for entity CLI tests.

prefixes:
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types
  - mosaic_core

default_range: string

classes:
  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      tissue:
"""


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "schema.yaml"
    p.write_text(SCHEMA_YAML)
    return p


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "mosaic.db"


@pytest.fixture()
def client(db_path: Path, schema_file: Path):
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
    from mosaic.linkml_bridge import SchemaRegistry

    db_path.parent.mkdir(parents=True, exist_ok=True)
    registry = SchemaRegistry.from_path(schema_file)
    return MosaicClient(
        storage=SQLiteAdapter(str(db_path), schema_registry=registry),
        registry=registry,
    )


def _cli(runner, *args, db_path=None, schema_file=None, extra=()):
    argv = list(args)
    if db_path is not None:
        argv += ["--db-path", str(db_path)]
    if schema_file is not None:
        argv += ["--schema", str(schema_file)]
    argv += list(extra)
    return runner.invoke(app, argv)


class TestEntityGet:
    def test_get_outputs_entity_yaml(self, runner, client, db_path, schema_file):
        created = client.put("Sample", {"name": "s1", "tissue": "brain"})

        result = _cli(
            runner, "entity", "get", "Sample", created["id"],
            db_path=db_path, schema_file=schema_file,
        )

        assert result.exit_code == 0
        payload = yaml.safe_load(result.output)
        assert payload["id"] == created["id"]
        assert payload["data"]["tissue"] == "brain"

    def test_get_json_output(self, runner, client, db_path, schema_file):
        created = client.put("Sample", {"name": "s1"})

        result = _cli(
            runner, "entity", "get", "Sample", created["id"],
            db_path=db_path, schema_file=schema_file, extra=["--json"],
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["id"] == created["id"]

    def test_get_missing_entity_exits_1(self, runner, client, db_path, schema_file):
        result = _cli(
            runner, "entity", "get", "Sample", "no-such-id",
            db_path=db_path, schema_file=schema_file,
        )

        assert result.exit_code == 1
        assert "Error" in result.output

    def test_get_missing_database_exits_1(self, runner, tmp_path):
        result = _cli(
            runner, "entity", "get", "Sample", "x",
            db_path=tmp_path / "absent.db",
        )

        assert result.exit_code == 1
        assert "database not found" in result.output
        assert not (tmp_path / "absent.db").exists()


class TestEntityQuery:
    def test_query_lists_entities(self, runner, client, db_path, schema_file):
        client.put("Sample", {"name": "a"})
        client.put("Sample", {"name": "b"})

        result = _cli(
            runner, "entity", "query", "Sample",
            db_path=db_path, schema_file=schema_file, extra=["--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["total"] == 2
        assert len(payload["items"]) == 2

    def test_query_with_field_filter(self, runner, client, db_path, schema_file):
        client.put("Sample", {"name": "keep", "tissue": "brain"})
        client.put("Sample", {"name": "drop", "tissue": "liver"})

        result = _cli(
            runner, "entity", "query", "Sample",
            db_path=db_path, schema_file=schema_file,
            extra=["--filter", "tissue=brain", "--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert [i["data"]["name"] for i in payload["items"]] == ["keep"]

    def test_query_invalid_filter_exits_1(self, runner, client, db_path, schema_file):
        result = _cli(
            runner, "entity", "query", "Sample",
            db_path=db_path, schema_file=schema_file,
            extra=["--filter", "no-equals-sign"],
        )

        assert result.exit_code == 1
        assert "invalid --filter" in result.output


class TestEntityHistory:
    def test_history_lists_provenance(self, runner, client, db_path, schema_file):
        created = client.put("Sample", {"name": "h1"})
        client.replace("Sample", created["id"], {"name": "h2"})

        result = _cli(
            runner, "entity", "history", created["id"],
            db_path=db_path, schema_file=schema_file, extra=["--json"],
        )

        assert result.exit_code == 0
        records = json.loads(result.output)
        assert len(records) >= 2

    def test_history_unknown_entity_exits_1(
        self, runner, client, db_path, schema_file
    ):
        result = _cli(
            runner, "entity", "history", "no-such-id",
            db_path=db_path, schema_file=schema_file,
        )

        assert result.exit_code == 1
        assert "Error: Entity not found" in result.output


class TestStatusCommand:
    def test_status_reports_summary(self, runner, client, db_path, schema_file):
        client.put("Sample", {"name": "s1"})

        result = _cli(
            runner, "status",
            db_path=db_path, schema_file=schema_file, extra=["--json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["service"] == "mosaic"
        assert payload["adapter"] == "SQLiteAdapter"
        assert payload["entity_counts"] == {"Sample": 1}

    def test_status_missing_database_exits_1(self, runner, tmp_path):
        result = _cli(runner, "status", db_path=tmp_path / "absent.db")

        assert result.exit_code == 1
        assert "database not found" in result.output
