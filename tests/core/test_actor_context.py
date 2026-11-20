from tests.conftest import _build_minimal_schema_registry
"""Tests for actor context propagation (Decision 9.6.G)."""

import sqlite3
from typing import Any

import pytest

from mosaic.core.context import current_actor, get_current_actor, with_actor


class TestContextVar:
    """Tests for the ContextVar API itself."""

    def test_default_is_none(self):
        assert get_current_actor() is None

    def test_with_actor_sets_and_restores(self):
        assert get_current_actor() is None
        with with_actor("agent-uuid-1"):
            assert get_current_actor() == "agent-uuid-1"
        assert get_current_actor() is None

    def test_with_actor_nesting(self):
        with with_actor("outer"):
            assert get_current_actor() == "outer"
            with with_actor("inner"):
                assert get_current_actor() == "inner"
            assert get_current_actor() == "outer"
        assert get_current_actor() is None

    def test_with_actor_restores_on_exception(self):
        with pytest.raises(RuntimeError):
            with with_actor("agent-uuid-2"):
                raise RuntimeError("boom")
        assert get_current_actor() is None


class TestProvenanceStorePicksUpContextVar:
    """Verify ProvenanceStore.record() reads current_actor when actor_id is None."""

    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "actor_ctx_test.db")

    def _get_actor_ids(self, db_path: str, entity_id: str) -> list[str]:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT actor_id FROM "ProvenanceRecord" WHERE entity_id = ? ORDER BY timestamp',
            (entity_id,),
        )
        rows = [r[0] for r in cursor.fetchall()]
        conn.close()
        return rows

    def test_actor_from_context_var_written_to_provenance(self, db_path: str):
        from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter, SQLiteEntity

        adapter = SQLiteAdapter(db_path, wal_mode=True, schema_registry=_build_minimal_schema_registry())
        now = "2026-01-01T00:00:00"

        entity = SQLiteEntity(
            id="ctx-entity-1",
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "ctx-sample"},
        )

        with with_actor("real-agent-uuid-abc123"):
            adapter.create(entity)

        actors = self._get_actor_ids(db_path, "ctx-entity-1")
        assert actors == ["real-agent-uuid-abc123"]
        adapter.close()

    def test_fallback_to_unknown_when_no_context(self, db_path: str):
        from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter, SQLiteEntity

        adapter = SQLiteAdapter(db_path, wal_mode=True, schema_registry=_build_minimal_schema_registry())
        now = "2026-01-01T00:00:00"

        entity = SQLiteEntity(
            id="ctx-entity-2",
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "no-ctx-sample"},
        )
        adapter.create(entity)

        actors = self._get_actor_ids(db_path, "ctx-entity-2")
        assert actors == ["unknown"]
        adapter.close()

    def test_explicit_kwarg_overrides_context_var(self, db_path: str):
        from mosaic.core.storage.adapters.sqlite_adapter import (
            SQLiteAdapter,
            SQLiteEntity,
        )

        adapter = SQLiteAdapter(db_path, wal_mode=True, schema_registry=_build_minimal_schema_registry())

        adapter.create(
            SQLiteEntity(
                id="ctx-entity-3",
                entity_type="Sample",
                is_available=True,
                version=1,
                data={"name": "explicit"},
            )
        )

        with adapter._transaction() as conn:
            provenance = adapter._get_provenance_store(conn)
            with with_actor("context-actor"):
                provenance.record(
                    entity_id="ctx-entity-3",
                    entity_type="Sample",
                    operation="update",
                    actor_id="explicit-actor",  # explicit kwarg wins
                )

        # Pre-PR-2.3 this test inserted into the legacy ``entities``
        # table via raw SQL so the only provenance row came from the
        # explicit ``provenance.record`` call. Post-PR-2.3 ``adapter.create``
        # is the only persistent write path and it emits its own
        # ``create`` provenance record (under ``with_actor("context-actor")``
        # the create writes "unknown" since the create runs *outside*
        # that block, then the explicit call writes "explicit-actor").
        # Assert the explicit-actor row is present rather than asserting
        # the exact list — the property under test ("explicit kwarg wins
        # over context var") still holds.
        actors = self._get_actor_ids(db_path, "ctx-entity-3")
        assert "explicit-actor" in actors
        adapter.close()


class TestMiddlewareSetsContextVar:
    """Verify PassThroughAuthMiddleware sets current_actor per request."""

    @pytest.fixture
    def app(self):
        from fastapi import FastAPI, Request
        from mosaic.core.middleware import PassThroughAuthMiddleware

        api = FastAPI()
        api.add_middleware(PassThroughAuthMiddleware)

        @api.get("/actor")
        async def read_actor(request: Request):
            ctx = getattr(request.state, "hippo_context", None)
            return {"actor_id": ctx.actor_id if ctx else None}

        @api.get("/context-var")
        async def read_context_var():
            return {"actor_id": get_current_actor()}

        return api

    def test_middleware_sets_context_var_on_request(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/context-var", headers={"x-datahelix-actor": "actor:agent-abc"})
        assert resp.status_code == 200
        assert resp.json()["actor_id"] == "agent-abc"

    def test_middleware_no_header_leaves_context_var_none(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/context-var")
        assert resp.status_code == 200
        assert resp.json()["actor_id"] is None
