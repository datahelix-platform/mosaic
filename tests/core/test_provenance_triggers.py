from tests.conftest import _build_minimal_schema_registry
"""Tests for ProvenanceRecord immutability triggers (sec9 §9.6 / Decision 9.6.C).

The SQLite adapter installs two triggers on the ``ProvenanceRecord``
table: ``prevent_provenance_update`` (BEFORE UPDATE) and
``prevent_provenance_delete`` (BEFORE DELETE). Either trigger firing
rejects the statement with an ``sqlite3.IntegrityError``.
"""

import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

import pytest

from mosaic.core.storage.adapters import SQLiteAdapter


class TestProvenanceImmutability:
    """Append-only enforcement at the SQLite trigger level."""

    @pytest.fixture
    def db_path(self) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield os.path.join(tmpdir, "test.db")

    @pytest.fixture
    def adapter(self, db_path: str) -> SQLiteAdapter:
        adapter = SQLiteAdapter(db_path, wal_mode=True, schema_registry=_build_minimal_schema_registry())
        yield adapter
        adapter.close()

    @pytest.fixture
    def conn(self, adapter: SQLiteAdapter, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()

    @staticmethod
    def _insert(conn: sqlite3.Connection, entity_id: str) -> str:
        """Insert a valid ProvenanceRecord row and return its id."""
        rec_id = str(uuid.uuid4())
        conn.execute(
            'INSERT INTO "ProvenanceRecord" '
            '(id, entity_id, entity_type, operation, actor_id, timestamp, '
            ' schema_version) '
            "VALUES (?, ?, 'TestEntity', 'create', 'test', ?, '')",
            (rec_id, entity_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return rec_id

    def test_update_any_column_rejected(self, conn: sqlite3.Connection) -> None:
        """BEFORE UPDATE trigger rejects any column change on ProvenanceRecord."""
        self._insert(conn, "test-1")

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                'UPDATE "ProvenanceRecord" SET entity_id = ? WHERE entity_id = ?',
                ("new-id", "test-1"),
            )
            conn.commit()

        assert "mosaic_append_only" in str(exc_info.value)

    def test_update_timestamp_rejected(self, conn: sqlite3.Connection) -> None:
        self._insert(conn, "test-2")

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                'UPDATE "ProvenanceRecord" SET timestamp = ? WHERE entity_id = ?',
                ("2030-01-01", "test-2"),
            )
            conn.commit()

        assert "mosaic_append_only" in str(exc_info.value)

    def test_update_actor_rejected(self, conn: sqlite3.Connection) -> None:
        self._insert(conn, "test-3")

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                'UPDATE "ProvenanceRecord" SET actor_id = ? WHERE entity_id = ?',
                ("someone_else", "test-3"),
            )
            conn.commit()

        assert "mosaic_append_only" in str(exc_info.value)

    def test_update_patch_rejected(self, conn: sqlite3.Connection) -> None:
        self._insert(conn, "test-4")

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                'UPDATE "ProvenanceRecord" SET patch = ? WHERE entity_id = ?',
                ('{"x": 1}', "test-4"),
            )
            conn.commit()

        assert "mosaic_append_only" in str(exc_info.value)

    def test_delete_rejected(self, conn: sqlite3.Connection) -> None:
        """BEFORE DELETE trigger rejects row deletion."""
        self._insert(conn, "test-5")

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                'DELETE FROM "ProvenanceRecord" WHERE entity_id = ?',
                ("test-5",),
            )
            conn.commit()

        assert "mosaic_append_only" in str(exc_info.value)

    def test_transaction_rollback_preserves_row(
        self, conn: sqlite3.Connection
    ) -> None:
        """An UPDATE that's rejected rolls back; the row persists unchanged."""
        self._insert(conn, "test-6")

        try:
            conn.execute(
                'UPDATE "ProvenanceRecord" SET entity_id = ? WHERE entity_id = ?',
                ("new-id", "test-6"),
            )
            conn.commit()
            pytest.fail("Update should have been blocked")
        except sqlite3.IntegrityError:
            conn.rollback()

        cursor = conn.execute(
            'SELECT entity_id FROM "ProvenanceRecord" WHERE entity_id = ?',
            ("test-6",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["entity_id"] == "test-6"
