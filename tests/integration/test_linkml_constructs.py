"""Integration tests for LinkML schema constructs in the Mosaic migrate pipeline."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mosaic.cli.main import app
from tests.support.linkml_schemas import write_schema_file

runner = CliRunner()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def project_dir(tmp_dir):
    schemas = tmp_dir / "schemas"
    schemas.mkdir()
    data = tmp_dir / "data"
    data.mkdir()
    db_path = data / "hippo.db"
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
    conn.commit()
    conn.close()
    return tmp_dir


class TestMigrateWithLinkMLConstructs:
    def test_migrate_entity_with_all_types(self, project_dir):
        write_schema_file(
            project_dir / "schemas",
            classes={
                "all_types_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "f_str": {"range": "string"},
                        "f_int": {"range": "integer"},
                        "f_float": {"range": "float"},
                        "f_bool": {"range": "boolean"},
                        "f_date": {"range": "date"},
                        "f_dt": {"range": "datetime"},
                        "f_uri": {"range": "uri"},
                    }
                }
            },
            schema_name="all_types",
        )
        db_path = project_dir / "data" / "hippo.db"
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(project_dir / "schemas"),
                "--db-path",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(db_path))
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        conn.close()
        assert "all_types_entity" in tables

    def test_migrate_with_references(self, project_dir):
        write_schema_file(
            project_dir / "schemas",
            classes={
                "project": {
                    "attributes": {
                        "id": {"identifier": True},
                        "title": {"range": "string", "required": True},
                    }
                },
                "sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "project_id": {"range": "project"},
                    }
                },
            },
            schema_name="refs",
        )
        db_path = project_dir / "data" / "hippo.db"
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(project_dir / "schemas"),
                "--db-path",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(db_path))
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        conn.close()
        assert "project" in tables
        assert "sample" in tables

    def test_migrate_preview_with_indexes(self, project_dir):
        write_schema_file(
            project_dir / "schemas",
            classes={
                "indexed_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {
                            "range": "string",
                            "annotations": {"mosaic_index": True},
                        },
                        "barcode": {
                            "range": "string",
                            "annotations": {"mosaic_index": True},
                        },
                        "notes": {"range": "string"},
                    }
                }
            },
            schema_name="indexed",
        )
        db_path = project_dir / "data" / "hippo.db"
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(project_dir / "schemas"),
                "--db-path",
                str(db_path),
                "--preview",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "CREATE INDEX" in result.output
        assert "indexed_entity" in result.output

    def test_migrate_enum_field(self, project_dir):
        write_schema_file(
            project_dir / "schemas",
            classes={
                "status_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "status": {"range": "StatusEnum"},
                    }
                }
            },
            enums={
                "StatusEnum": {
                    "permissible_values": {"active": {}, "archived": {}}
                }
            },
            schema_name="status",
        )
        db_path = project_dir / "data" / "hippo.db"
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(project_dir / "schemas"),
                "--db-path",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(db_path))
        cols = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(status_entity)")}
        conn.close()
        assert "status" in cols

    def test_migrate_required_fields(self, project_dir):
        write_schema_file(
            project_dir / "schemas",
            classes={
                "req_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "notes": {"range": "string"},
                    }
                }
            },
            schema_name="req",
        )
        db_path = project_dir / "data" / "hippo.db"
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(project_dir / "schemas"),
                "--db-path",
                str(db_path),
                "--preview",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "CREATE TABLE" in result.output

    def test_migrate_multiple_entities(self, project_dir):
        write_schema_file(
            project_dir / "schemas",
            classes={
                name: {
                    "attributes": {
                        "id": {"identifier": True},
                        "label": {"range": "string"},
                    }
                }
                for name in ["alpha", "beta", "gamma"]
            },
            schema_name="entities",
        )
        db_path = project_dir / "data" / "hippo.db"
        result = runner.invoke(
            app,
            [
                "migrate",
                "--schema-dir",
                str(project_dir / "schemas"),
                "--db-path",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(db_path))
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        conn.close()
        for name in ["alpha", "beta", "gamma"]:
            assert name in tables
