"""Postgres adapter parity tests for ``reference_write_log`` DDL and
the one-time backfill from ``mosaic_meta.reference_entity_ids``
(sec2 §2.14.9, Decision 2.14.J).

Mirrors ``tests/core/test_reference_write_log.py``. Requires a running
PostgreSQL instance — set ``MOSAIC_DATABASE_URL`` (see
``docker-compose.test.yml``).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")

from mosaic.core.meta import set_meta as sqlite_set_meta  # noqa: E402

POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)


def _set_meta_pg(conn, key: str, value: dict) -> None:
    """Postgres-flavoured upsert into ``mosaic_meta``.

    Mirrors ``mosaic.core.meta.set_meta`` but with ``%s`` placeholders so
    it works against psycopg cursors.
    """
    from datetime import datetime, timezone

    payload = json.dumps(value, sort_keys=True)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO mosaic_meta (key, value, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, payload, now),
    )


def _cleanup_db(database_url: str) -> None:
    """Drop test-affected rows so each test starts from a clean slate."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT to_regclass('public.reference_write_log') IS NOT NULL"
        )
        if cur.fetchone()[0]:
            cur.execute("DELETE FROM reference_write_log")
        for table in (
            "mosaic_meta",
            "entity_external_ids",
            "relationships",
            '"ProvenanceRecord"',
            "entities",
        ):
            cur.execute(
                f"SELECT to_regclass('public.{table.strip(chr(34))}') IS NOT NULL"
            )
            if cur.fetchone()[0]:
                if table == '"ProvenanceRecord"':
                    cur.execute(f"DROP TRIGGER IF EXISTS prevent_provenance_delete ON {table}")
                cur.execute(f"DELETE FROM {table}")


@pytest.fixture
def database_url() -> str:
    return POSTGRES_URL


@pytest.fixture
def fresh_db(database_url) -> Iterator[str]:
    _cleanup_db(database_url)
    yield database_url
    _cleanup_db(database_url)


@pytest.fixture
def adapter_factory(minimal_schema_registry, fresh_db):
    """Construct ``PostgresAdapter`` instances against the shared test DB."""
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

    created = []

    def _make() -> "PostgresAdapter":
        adapter = PostgresAdapter(
            database_url=fresh_db,
            schema_registry=minimal_schema_registry,
            min_pool_size=1,
            max_pool_size=2,
        )
        created.append(adapter)
        return adapter

    yield _make

    for adapter in created:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            pass


def _select_log(database_url: str) -> list[dict]:
    with psycopg.connect(database_url) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT loader_name, version, entity_id, entity_type "
            "FROM reference_write_log "
            "ORDER BY loader_name, version, entity_id"
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _meta_value(database_url: str, key: str) -> str | None:
    with psycopg.connect(database_url) as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM mosaic_meta WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


_MIGRATION_MARKER = "_reference_write_log_v1_migrated"


def _simulate_v1_state(database_url: str) -> None:
    """Strip the v2 migration marker so the next adapter init treats this
    DB as if it had just been upgraded from v1.
    """
    with psycopg.connect(database_url, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM mosaic_meta WHERE key = %s", (_MIGRATION_MARKER,)
        )


def _put_entity(adapter, entity_type: str, name: str) -> str:
    from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

    entity_id = str(uuid.uuid4())
    adapter.create(
        PostgresEntity(
            id=entity_id,
            entity_type=entity_type,
            is_available=True,
            version=1,
            data={"name": name},
        )
    )
    return entity_id


class TestReferenceWriteLogDDL:
    def test_table_columns_match_spec(self, adapter_factory, database_url):
        adapter_factory()
        with psycopg.connect(database_url) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'reference_write_log'"
            )
            cols = {row[0] for row in cur.fetchall()}
        assert cols == {
            "loader_name",
            "version",
            "entity_id",
            "entity_type",
            "written_at",
        }

    def test_lookup_index_created(self, adapter_factory, database_url):
        adapter_factory()
        with psycopg.connect(database_url) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'reference_write_log'"
            )
            indexes = {row[0] for row in cur.fetchall()}
        assert "idx_reference_write_log_lookup" in indexes

    def test_primary_key_collapses_duplicate_writes(
        self, adapter_factory, database_url
    ):
        adapter = adapter_factory()
        with adapter._transaction() as conn:
            cur = conn.cursor()
            for _ in range(2):
                cur.execute(
                    "INSERT INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (loader_name, version, entity_id) DO NOTHING",
                    ("l", "1", "e1", "Sample"),
                )
        assert len(_select_log(database_url)) == 1

    def test_reinit_is_idempotent(self, adapter_factory):
        adapter_factory().close()
        # Second init against the same DB must not raise.
        adapter_factory().close()


class TestReferenceEntityIdsMigration:
    def test_fresh_v2_db_is_a_noop(self, adapter_factory, database_url):
        adapter_factory()
        assert _select_log(database_url) == []
        assert _meta_value(database_url, "reference_entity_ids") is None

    def test_empty_payload_clears_meta_key(
        self, adapter_factory, database_url
    ):
        adapter = adapter_factory()
        with adapter._transaction() as conn:
            _set_meta_pg(conn, "reference_entity_ids", {})
        adapter.close()
        _simulate_v1_state(database_url)

        adapter_factory()

        assert _select_log(database_url) == []
        assert _meta_value(database_url, "reference_entity_ids") is None

    def test_populated_payload_writes_rows_and_clears_key(
        self, adapter_factory, database_url
    ):
        adapter = adapter_factory()
        s1 = _put_entity(adapter, "Sample", "S1")
        s2 = _put_entity(adapter, "Sample", "S2")
        d1 = _put_entity(adapter, "Document", "D1")
        with adapter._transaction() as conn:
            _set_meta_pg(
                conn,
                "reference_entity_ids",
                {
                    "loader_a": {"1.0.0": [s1, s2]},
                    "loader_b": {"2.0.0": [d1]},
                },
            )
        adapter.close()
        _simulate_v1_state(database_url)

        adapter_factory()

        rows = _select_log(database_url)
        triples = {
            (r["loader_name"], r["version"], r["entity_id"], r["entity_type"])
            for r in rows
        }
        assert triples == {
            ("loader_a", "1.0.0", s1, "Sample"),
            ("loader_a", "1.0.0", s2, "Sample"),
            ("loader_b", "2.0.0", d1, "Document"),
        }
        assert _meta_value(database_url, "reference_entity_ids") is None

    def test_marker_prevents_re_migration_when_v1_path_rewrites_key(
        self, adapter_factory, database_url
    ):
        """v1 ``_write_versions`` is still in tree (removed in PTS-256).
        After the first v2 migration, the marker must guard against
        clobbering anything the v1 path writes on subsequent runs.
        """
        adapter = adapter_factory()
        s1 = _put_entity(adapter, "Sample", "S1")
        with adapter._transaction() as conn:
            _set_meta_pg(
                conn,
                "reference_entity_ids",
                {"loader_a": {"1.0.0": [s1]}},
            )
        adapter.close()
        _simulate_v1_state(database_url)

        # First re-open performs the migration.
        adapter_factory().close()
        rows_after_migration = _select_log(database_url)
        assert len(rows_after_migration) == 1
        assert _meta_value(database_url, "reference_entity_ids") is None

        # Now mimic the v1 path rewriting the meta key after migration.
        with psycopg.connect(database_url, autocommit=True) as conn:
            _set_meta_pg(
                conn,
                "reference_entity_ids",
                {"loader_a": {"2.0.0": [s1]}},
            )

        # Second re-open must NOT clobber the v1 write, NOT duplicate log
        # rows, and NOT re-migrate the v1 entries.
        adapter_factory()
        assert _select_log(database_url) == rows_after_migration
        assert _meta_value(database_url, "reference_entity_ids") is not None

    def test_resume_after_partial_migration_completes_cleanly(
        self, adapter_factory, database_url
    ):
        adapter = adapter_factory()
        s1 = _put_entity(adapter, "Sample", "S1")
        s2 = _put_entity(adapter, "Sample", "S2")

        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO reference_write_log "
                "(loader_name, version, entity_id, entity_type) "
                "VALUES (%s, %s, %s, %s)",
                ("loader_a", "1.0.0", s1, "Sample"),
            )
            _set_meta_pg(
                conn,
                "reference_entity_ids",
                {"loader_a": {"1.0.0": [s1, s2]}},
            )
        adapter.close()
        _simulate_v1_state(database_url)

        adapter_factory()

        rows = _select_log(database_url)
        assert {r["entity_id"] for r in rows} == {s1, s2}
        assert len(rows) == 2
        assert _meta_value(database_url, "reference_entity_ids") is None

    def test_unresolvable_entity_type_is_skipped(
        self, adapter_factory, database_url
    ):
        adapter = adapter_factory()
        known = _put_entity(adapter, "Sample", "S1")
        with adapter._transaction() as conn:
            _set_meta_pg(
                conn,
                "reference_entity_ids",
                {
                    "loader_a": {
                        "1.0.0": [known, "id-that-was-never-created"]
                    }
                },
            )
        adapter.close()
        _simulate_v1_state(database_url)

        adapter_factory()

        rows = _select_log(database_url)
        ids = {r["entity_id"] for r in rows}
        assert ids == {known}
        assert _meta_value(database_url, "reference_entity_ids") is None
