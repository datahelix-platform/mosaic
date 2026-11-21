"""Adapter tests for the ``reference_write_log`` table and the one-time
backfill from ``mosaic_meta.reference_entity_ids`` (sec2 §2.14.9,
Decision 2.14.J).

Covers the SQLite adapter unconditionally. Postgres parity is covered by
``tests/integration/test_reference_write_log_postgres.py`` (gated on
``MOSAIC_DATABASE_URL``).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Iterator

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.meta import set_meta
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry
from tests.support.linkml_schemas import build_registry


def _registry() -> SchemaRegistry:
    return build_registry(
        {
            "Sample": {
                "attributes": {
                    "id": {"identifier": True, "required": True},
                    "name": {"range": "string", "required": True},
                }
            },
            "Document": {
                "attributes": {
                    "id": {"identifier": True, "required": True},
                    "title": {"range": "string", "required": True},
                }
            },
        }
    )


@pytest.fixture
def db_path() -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "ref_write_log.db")


def _table_columns(db_path: str, table: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    finally:
        conn.close()
    return {row[1]: row[2] for row in rows}


def _index_names(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    finally:
        conn.close()
    return {row[1] for row in rows}


def _select_log(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT loader_name, version, entity_id, entity_type "
            "FROM reference_write_log "
            "ORDER BY loader_name, version, entity_id"
        ).fetchall()
    finally:
        conn.close()


def _meta_value(db_path: str, key: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM mosaic_meta WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


_MIGRATION_MARKER = "_reference_write_log_v1_migrated"


def _simulate_v1_state(db_path: str) -> None:
    """Strip the v2 migration marker so the next adapter init treats this
    DB as if it had just been upgraded from v1.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM mosaic_meta WHERE key = ?", (_MIGRATION_MARKER,)
        )
        conn.commit()
    finally:
        conn.close()


class TestReferenceWriteLogDDL:
    def test_table_columns_match_spec(self, db_path: str):
        SQLiteAdapter(db_path, schema_registry=_registry()).close()
        cols = _table_columns(db_path, "reference_write_log")
        assert set(cols) == {
            "loader_name",
            "version",
            "entity_id",
            "entity_type",
            "written_at",
        }

    def test_lookup_index_created(self, db_path: str):
        SQLiteAdapter(db_path, schema_registry=_registry()).close()
        assert "idx_reference_write_log_lookup" in _index_names(
            db_path, "reference_write_log"
        )

    def test_primary_key_collapses_duplicate_writes(self, db_path: str):
        adapter = SQLiteAdapter(db_path, schema_registry=_registry())
        try:
            with adapter._transaction() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT OR IGNORE INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (?, ?, ?, ?)",
                    ("l", "1", "e1", "Sample"),
                )
                cur.execute(
                    "INSERT OR IGNORE INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (?, ?, ?, ?)",
                    ("l", "1", "e1", "Sample"),
                )
        finally:
            adapter.close()
        assert len(_select_log(db_path)) == 1

    def test_reinit_is_idempotent(self, db_path: str):
        SQLiteAdapter(db_path, schema_registry=_registry()).close()
        # Second init must not raise on the already-existing table/index.
        SQLiteAdapter(db_path, schema_registry=_registry()).close()


class TestReferenceEntityIdsMigration:
    def test_fresh_v2_db_is_a_noop(self, db_path: str):
        """No ``reference_entity_ids`` key present → nothing written, no error."""
        SQLiteAdapter(db_path, schema_registry=_registry()).close()
        assert _select_log(db_path) == []
        assert _meta_value(db_path, "reference_entity_ids") is None

    def test_empty_payload_clears_meta_key(self, db_path: str):
        """A ``{}`` payload writes no rows but still deletes the meta key
        so the re-run no-op invariant holds.
        """
        adapter = SQLiteAdapter(db_path, schema_registry=_registry())
        conn = adapter._get_connection()
        set_meta(conn, "reference_entity_ids", {})
        conn.commit()
        adapter.close()
        _simulate_v1_state(db_path)

        SQLiteAdapter(db_path, schema_registry=_registry()).close()

        assert _select_log(db_path) == []
        assert _meta_value(db_path, "reference_entity_ids") is None

    def test_populated_payload_writes_rows_and_clears_key(self, db_path: str):
        adapter = SQLiteAdapter(db_path, schema_registry=_registry())
        client = MosaicClient(
            storage=adapter,
            registry=adapter.schema_registry,
            bypass_validation=True,
        )
        s1 = client.put("Sample", {"name": "S1"})
        s2 = client.put("Sample", {"name": "S2"})
        d1 = client.put("Document", {"title": "D1"})

        conn = adapter._get_connection()
        set_meta(
            conn,
            "reference_entity_ids",
            {
                "loader_a": {"1.0.0": [s1["id"], s2["id"]]},
                "loader_b": {"2.0.0": [d1["id"]]},
            },
        )
        conn.commit()
        adapter.close()
        _simulate_v1_state(db_path)

        SQLiteAdapter(db_path, schema_registry=_registry()).close()

        rows = _select_log(db_path)
        triples = {
            (r["loader_name"], r["version"], r["entity_id"], r["entity_type"])
            for r in rows
        }
        assert triples == {
            ("loader_a", "1.0.0", s1["id"], "Sample"),
            ("loader_a", "1.0.0", s2["id"], "Sample"),
            ("loader_b", "2.0.0", d1["id"], "Document"),
        }
        assert _meta_value(db_path, "reference_entity_ids") is None

    def test_marker_prevents_re_migration_when_v1_path_rewrites_key(
        self, db_path: str
    ):
        """v1 ``_write_versions`` is still in tree (removed in PTS-256).
        After the first v2 migration, the marker must guard against
        clobbering anything the v1 path writes on subsequent runs.
        """
        adapter = SQLiteAdapter(db_path, schema_registry=_registry())
        client = MosaicClient(
            storage=adapter,
            registry=adapter.schema_registry,
            bypass_validation=True,
        )
        s1 = client.put("Sample", {"name": "S1"})
        conn = adapter._get_connection()
        set_meta(
            conn,
            "reference_entity_ids",
            {"loader_a": {"1.0.0": [s1["id"]]}},
        )
        conn.commit()
        adapter.close()
        _simulate_v1_state(db_path)

        # First re-open performs the migration.
        SQLiteAdapter(db_path, schema_registry=_registry()).close()
        rows_after_migration = _select_log(db_path)
        assert len(rows_after_migration) == 1
        assert _meta_value(db_path, "reference_entity_ids") is None

        # Now mimic the v1 path rewriting the meta key after migration.
        conn = sqlite3.connect(db_path)
        try:
            set_meta(
                conn,
                "reference_entity_ids",
                {"loader_a": {"2.0.0": [s1["id"]]}},
            )
            conn.commit()
        finally:
            conn.close()

        # Second re-open must NOT clobber the v1 write, NOT duplicate log
        # rows, and NOT re-migrate the v1 entries.
        SQLiteAdapter(db_path, schema_registry=_registry()).close()
        assert _select_log(db_path) == rows_after_migration
        assert _meta_value(db_path, "reference_entity_ids") is not None

    def test_resume_after_partial_migration_completes_cleanly(
        self, db_path: str
    ):
        """If a prior migration inserted some rows but crashed before
        stamping the marker, the next init must complete the work
        without duplicating rows.
        """
        adapter = SQLiteAdapter(db_path, schema_registry=_registry())
        client = MosaicClient(
            storage=adapter,
            registry=adapter.schema_registry,
            bypass_validation=True,
        )
        s1 = client.put("Sample", {"name": "S1"})
        s2 = client.put("Sample", {"name": "S2"})

        # Simulate the prior interrupted run: one row already in the log,
        # meta key still present with the full payload, marker absent.
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO reference_write_log "
                "(loader_name, version, entity_id, entity_type) "
                "VALUES (?, ?, ?, ?)",
                ("loader_a", "1.0.0", s1["id"], "Sample"),
            )
        conn = adapter._get_connection()
        set_meta(
            conn,
            "reference_entity_ids",
            {"loader_a": {"1.0.0": [s1["id"], s2["id"]]}},
        )
        conn.commit()
        adapter.close()
        _simulate_v1_state(db_path)

        SQLiteAdapter(db_path, schema_registry=_registry()).close()

        rows = _select_log(db_path)
        assert {r["entity_id"] for r in rows} == {s1["id"], s2["id"]}
        assert len(rows) == 2  # no duplicate for s1
        assert _meta_value(db_path, "reference_entity_ids") is None

    def test_unresolvable_entity_type_is_skipped(self, db_path: str):
        """Ids the adapter has never seen are skipped rather than crashing
        startup. The acceptance criteria say the migration is best-effort;
        prune is opt-in, so a partial log is preferable to a broken init.
        """
        adapter = SQLiteAdapter(db_path, schema_registry=_registry())
        client = MosaicClient(
            storage=adapter,
            registry=adapter.schema_registry,
            bypass_validation=True,
        )
        known = client.put("Sample", {"name": "S1"})
        conn = adapter._get_connection()
        set_meta(
            conn,
            "reference_entity_ids",
            {
                "loader_a": {
                    "1.0.0": [known["id"], "id-that-was-never-created"]
                }
            },
        )
        conn.commit()
        adapter.close()
        _simulate_v1_state(db_path)

        SQLiteAdapter(db_path, schema_registry=_registry()).close()

        rows = _select_log(db_path)
        ids = {r["entity_id"] for r in rows}
        assert ids == {known["id"]}
        assert _meta_value(db_path, "reference_entity_ids") is None
