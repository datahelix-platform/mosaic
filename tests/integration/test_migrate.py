"""Integration tests for the ``mosaic migrate`` CLI command."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mosaic.cli.main import app
from tests.support.linkml_schemas import write_schema_file

runner = CliRunner()


@pytest.fixture
def temp_project_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir) / "test_project"
        project_dir.mkdir()

        data_dir = project_dir / "data"
        data_dir.mkdir()

        db_path = data_dir / "hippo.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                is_available INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                data TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_entity (
                id TEXT PRIMARY KEY,
                name TEXT,
                is_available INTEGER NOT NULL DEFAULT 1,
                superseded_by TEXT
            )
        """)
        conn.commit()
        conn.close()

        yield project_dir


@pytest.fixture
def schema_dir(temp_project_dir):
    schemas_dir = temp_project_dir / "schemas"
    schemas_dir.mkdir()
    return schemas_dir


class TestMigrateCommand:
    def test_migrate_no_schemas_dir(self, tmp_path):
        result = runner.invoke(app, ["migrate", "--db-path", str(tmp_path / "test.db")])
        assert result.exit_code == 1
        assert "Schema directory not found" in result.output

    def test_migrate_no_schema_files(self, schema_dir):
        result = runner.invoke(
            app,
            ["migrate", "--schema-dir", str(schema_dir), "--db-path", "nonexistent.db"],
        )
        assert "No schema files found" in result.output
        assert "No migrations needed" in result.output

    def test_migrate_no_changes_detected(self, schema_dir, temp_project_dir):
        db_path = temp_project_dir / "data" / "hippo.db"
        write_schema_file(
            schema_dir,
            classes={
                "test_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string"},
                    }
                }
            },
            schema_name="test",
        )
        result = runner.invoke(
            app, ["migrate", "--schema-dir", str(schema_dir), "--db-path", str(db_path)]
        )
        assert "No schema changes detected" in result.output

    def test_migrate_preview_new_table(self, schema_dir, temp_project_dir):
        db_path = temp_project_dir / "data" / "hippo.db"
        write_schema_file(
            schema_dir,
            classes={
                "new_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                    }
                }
            },
            schema_name="new_entity",
        )
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(schema_dir),
                "--db-path",
                str(db_path),
                "--preview",
            ],
        )
        assert "New tables to create" in result.output
        assert "new_entity" in result.output
        assert "CREATE TABLE" in result.output


class TestPreviewModeOutput:
    def test_preview_mode_shows_ddl(self, schema_dir, temp_project_dir):
        db_path = temp_project_dir / "data" / "hippo.db"
        write_schema_file(
            schema_dir,
            classes={
                "preview_test": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string"},
                        "count": {
                            "range": "integer",
                            "annotations": {"mosaic_index": True},
                        },
                    }
                }
            },
            schema_name="preview_test",
        )
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(schema_dir),
                "--db-path",
                str(db_path),
                "--preview",
            ],
        )
        assert "DDL Statements (Preview)" in result.output
        assert "CREATE TABLE" in result.output
        assert "CREATE INDEX" in result.output

    def test_preview_does_not_modify_db(self, schema_dir, temp_project_dir):
        db_path = temp_project_dir / "data" / "hippo.db"
        write_schema_file(
            schema_dir,
            classes={
                "preview_no_change": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string"},
                    }
                }
            },
            schema_name="preview_no_change",
        )
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(schema_dir),
                "--db-path",
                str(db_path),
                "--preview",
            ],
        )
        assert "Preview complete. No changes applied" in result.output

        conn = sqlite3.connect(str(db_path))
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        conn.close()
        assert "preview_no_change" not in tables
