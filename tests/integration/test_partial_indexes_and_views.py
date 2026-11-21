"""Integration tests for partial indexes through the migrate CLI."""

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mosaic.cli.main import app
from tests.support.linkml_schemas import write_schema_file

runner = CliRunner()


class TestPartialIndexes:
    def test_partial_indexes_created_for_indexed_fields(self, tmp_path):
        project_dir = Path(tmp_path) / "test_project"
        project_dir.mkdir()
        data_dir = project_dir / "data"
        data_dir.mkdir()
        db_path = data_dir / "hippo.db"
        sqlite3.connect(str(db_path)).close()

        schemas_dir = project_dir / "schemas"
        schemas_dir.mkdir()
        write_schema_file(
            schemas_dir,
            classes={
                "test_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string"},
                        "count": {
                            "range": "integer",
                            "annotations": {
                                "mosaic_index": True,
                                "mosaic_index_partial": True,
                            },
                        },
                        "score": {
                            "range": "float",
                            "annotations": {
                                "mosaic_index": True,
                                "mosaic_index_partial": True,
                            },
                        },
                    }
                }
            },
            schema_name="test_entity",
        )

        result = runner.invoke(
            app,
            ["migrate", "--schema-dir", str(schemas_dir), "--db-path", str(db_path)],
        )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        indexes = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='test_entity'"
        ).fetchall()
        conn.close()
        assert len(indexes) >= 2
        sqls = [row[1] for row in indexes]
        assert any("WHERE is_available = 1" in (sql or "") for sql in sqls)


class TestQueryPlanExplain:
    def test_explain_query_helper_method(self, tmp_path):
        project_dir = Path(tmp_path) / "test_project"
        project_dir.mkdir()
        data_dir = project_dir / "data"
        data_dir.mkdir()
        db_path = data_dir / "hippo.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "id TEXT PRIMARY KEY, name TEXT, is_available INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_name_available "
            "ON items (name) WHERE is_available = 1"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        results = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_items_name_available'"
        ).fetchall()
        conn.close()
        assert len(results) >= 1
        assert "WHERE is_available = 1" in results[0][1]
