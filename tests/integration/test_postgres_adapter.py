"""Integration tests for PostgreSQL storage adapter.

Requires a running PostgreSQL instance. Set MOSAIC_DATABASE_URL to connect:

    MOSAIC_DATABASE_URL=postgresql://hippo_test:hippo_test@localhost:5433/hippo_test pytest tests/integration/test_postgres_adapter.py

Use docker-compose.test.yml to start a test PostgreSQL instance:

    docker compose -f docker-compose.test.yml up -d
"""

import json
import os
import uuid

import pytest

# Skip all tests if psycopg is not installed or no database URL is set
psycopg = pytest.importorskip("psycopg")

POSTGRES_URL = os.environ.get("MOSAIC_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="MOSAIC_DATABASE_URL not set — skipping PostgreSQL tests",
)


@pytest.fixture
def adapter(minimal_schema_registry):
    """Create a fresh PostgresAdapter with clean tables for each test."""
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

    db_url = POSTGRES_URL
    # Use a unique schema prefix to avoid cross-test contamination
    adapter = PostgresAdapter(database_url=db_url, schema_registry=minimal_schema_registry, min_pool_size=1, max_pool_size=5)

    yield adapter

    # Cleanup: drop all test data
    with adapter._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM entity_external_ids")
        cur.execute("DELETE FROM relationships")
        # Disable provenance triggers temporarily for cleanup. The table
        # was renamed from ``provenance`` to ``ProvenanceRecord`` per
        # sec9 §9.6 / Decision 9.6.D.
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")
        # Drop any FTS tables
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'fts_%'"
        )
        for row in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{row["tablename"]}" CASCADE')

    adapter.close()


@pytest.fixture
def sample_entity():
    """Create a sample entity for testing."""
    from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

    return PostgresEntity(
        id=str(uuid.uuid4()),
        entity_type="Sample",
        is_available=True,
        version=1,
        data={"name": "Test Sample", "category": "tissue", "status": "active"},
    )


class TestPostgresAdapterCRUD:
    """Test basic CRUD operations."""

    def test_create_entity(self, adapter, sample_entity):
        result = adapter.create(sample_entity)
        assert result is not None
        assert result.id == sample_entity.id

    def test_read_entity(self, adapter, sample_entity):
        adapter.create(sample_entity)
        result = adapter.read(sample_entity.id)
        assert result is not None
        assert result.id == sample_entity.id
        assert result.entity_type == "Sample"
        assert result.data["name"] == "Test Sample"

    def test_read_nonexistent_entity(self, adapter):
        result = adapter.read("nonexistent-id")
        assert result is None

    def test_update_entity(self, adapter, sample_entity):
        adapter.create(sample_entity)
        result = adapter.update(sample_entity)
        assert result is not None
        assert result.id == sample_entity.id

    def test_delete_entity(self, adapter, sample_entity):
        adapter.create(sample_entity)
        result = adapter.delete(sample_entity.id)
        assert result is True
        # Should not be readable after soft delete
        assert adapter.read(sample_entity.id) is None

    def test_delete_nonexistent_entity(self, adapter):
        result = adapter.delete("nonexistent-id")
        assert result is False

    def test_read_any_returns_deleted(self, adapter, sample_entity):
        adapter.create(sample_entity)
        adapter.delete(sample_entity.id)
        result = adapter.read_any(sample_entity.id)
        assert result is not None
        assert result.is_available is False


class TestPostgresAdapterQuery:
    """Test query operations."""

    def test_find_all(self, adapter, sample_entity):
        adapter.create(sample_entity)
        results = list(adapter.findAll())
        assert len(results) >= 1
        ids = [r.id for r in results]
        assert sample_entity.id in ids

    def test_find_by_entity_type(self, adapter, sample_entity):
        from mosaic.core.storage import Query

        adapter.create(sample_entity)
        query = Query(entity_type="Sample")
        results = list(adapter.find(query))
        assert len(results) >= 1
        assert all(r.entity_type == "Sample" for r in results)

    def test_find_by_field_filter(self, adapter, sample_entity):
        adapter.create(sample_entity)
        results = list(adapter.findBy(category="tissue"))
        assert len(results) >= 1

    def test_find_with_or_filter(self, adapter):
        from mosaic.core.storage import Query
        from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

        e1 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Alpha", "category": "blood"},
        )
        e2 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Beta", "category": "tissue"},
        )
        adapter.create(e1)
        adapter.create(e2)

        query = Query(
            entity_type="Sample",
            filters=[{"category": "blood"}, {"category": "tissue"}],
            filter_mode="or",
        )
        results = list(adapter.find(query))
        assert len(results) >= 2

    def test_find_with_in_filter(self, adapter):
        """IN / set-membership filter (issue #102) — JSONB path."""
        from mosaic.core.storage import Query
        from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

        e1 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Alpha", "category": "blood"},
        )
        e2 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Beta", "category": "tissue"},
        )
        e3 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Gamma", "category": "bone"},
        )
        adapter.create(e1)
        adapter.create(e2)
        adapter.create(e3)

        query = Query(
            entity_type="Sample",
            filters=[
                {"field": "category", "op": "in", "value": ["blood", "tissue"]}
            ],
        )
        results = list(adapter.find(query))
        ids = {r.id for r in results}
        assert e1.id in ids
        assert e2.id in ids
        assert e3.id not in ids

    def test_find_with_empty_in_filter_matches_nothing(self, adapter, sample_entity):
        """Empty IN-list short-circuits to no matches (issue #102)."""
        from mosaic.core.storage import Query

        adapter.create(sample_entity)
        query = Query(
            entity_type="Sample",
            filters=[{"field": "category", "op": "in", "value": []}],
        )
        results = list(adapter.find(query))
        assert results == []

    def test_find_with_limit_offset(self, adapter):
        from mosaic.core.storage import Query
        from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

        for i in range(5):
            e = PostgresEntity(
                id=str(uuid.uuid4()),
                entity_type="Sample",
                is_available=True,
                version=1,
                data={"name": f"Sample-{i}"},
            )
            adapter.create(e)

        query = Query(entity_type="Sample", limit=2, offset=1)
        results = list(adapter.find(query))
        assert len(results) == 2


class TestPostgresAdapterFTS:
    """Test full-text search functionality."""

    def test_create_fts_table(self, adapter):
        adapter.create_fts_table("fts_sample_name", ["entity_id", "content"])
        tables = adapter.get_fts_tables_for_entity_type("sample")
        assert "fts_sample_name" in tables

    def test_search_entities(self, adapter, sample_entity):
        from mosaic.core.storage.adapters.postgres_adapter import PostgresFTSStore

        adapter.create(sample_entity)
        adapter.create_fts_table("fts_sample_name", ["entity_id", "content"])

        with adapter._transaction() as conn:
            fts = PostgresFTSStore(conn)
            fts.sync_entity_to_fts(
                "fts_sample_name", sample_entity.id, "Test Sample tissue"
            )

        results = adapter.search(
            query="tissue",
            entity_type="Sample",
            field_name="name",
        )
        assert len(results) >= 1
        assert results[0].entity_id == sample_entity.id
        assert results[0].score > 0

    def test_search_nonexistent_fts_table(self, adapter):
        from mosaic.core.exceptions import SearchCapabilityError

        with pytest.raises(SearchCapabilityError):
            adapter.search(
                query="test",
                entity_type="Nonexistent",
                field_name="name",
            )

    def test_search_capabilities(self, adapter):
        caps = adapter.search_capabilities()
        assert "fts" in caps
        assert "trigram" in caps

    def test_entity_counts(self, adapter, sample_entity):
        assert adapter.entity_counts() == {}

        adapter.create(sample_entity)
        assert adapter.entity_counts() == {"Sample": 1}

    def test_entity_counts_include_unavailable(self, adapter, sample_entity):
        """No hard deletes — soft-deleted entities still count."""
        adapter.create(sample_entity)
        adapter.delete(sample_entity.id)

        assert adapter.entity_counts() == {"Sample": 1}


class TestPostgresAdapterProvenance:
    """Test provenance tracking."""

    def test_create_records_provenance(self, adapter, sample_entity):
        adapter.create(sample_entity)
        history = adapter.history(sample_entity.id)
        assert len(history) >= 1
        # sec9 §9.6 Operation enum values (lowercase)
        assert history[0]["operation_type"] == "create"

    def test_delete_records_provenance(self, adapter, sample_entity):
        adapter.create(sample_entity)
        adapter.delete(sample_entity.id)
        history = adapter.history(sample_entity.id)
        assert len(history) >= 2
        ops = [h["operation_type"] for h in history]
        # Decision 9.6.B: SOFT_DELETE → availability_change
        assert "create" in ops
        assert "availability_change" in ops

    def test_track_creation(self, adapter, sample_entity):
        record = adapter.track_creation(sample_entity, {"test": "metadata"})
        assert record.operation == "create"

    def test_track_update(self, adapter, sample_entity):
        record = adapter.track_update(sample_entity, {"test": "metadata"})
        assert record.operation == "update"

    def test_track_deletion(self, adapter, sample_entity):
        record = adapter.track_deletion(sample_entity.id, {"test": "metadata"})
        # Legacy "delete" → availability_change per Decision 9.6.B
        assert record.operation == "availability_change"

    def test_track_methods_carry_the_adapter_schema_version(
        self, minimal_schema_registry, sample_entity
    ):
        """Regression: sec9 §9.6/§9.7 Fallback 2 — these hardcoded schema_version="".

        `track_creation`/`track_update`/`track_deletion` build in-memory
        ``ProvenanceRecord``s (no DB write) and must carry the adapter's
        configured ``_schema_version``, not a hardcoded empty string, so a
        caller that threaded a real version through construction sees it
        reflected here too.
        """
        from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

        versioned_adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=minimal_schema_registry,
            min_pool_size=1,
            max_pool_size=2,
            schema_version="v-test-9.7",
        )
        try:
            assert (
                versioned_adapter.track_creation(sample_entity, {}).schema_version
                == "v-test-9.7"
            )
            assert (
                versioned_adapter.track_update(sample_entity, {}).schema_version
                == "v-test-9.7"
            )
            assert (
                versioned_adapter.track_deletion(sample_entity.id, {}).schema_version
                == "v-test-9.7"
            )
        finally:
            versioned_adapter.close()


class TestPostgresAdapterAtomicUpsert:
    """Test atomic upsert behavior for multi-instance safety."""

    def test_create_same_entity_twice_upserts(self, adapter, sample_entity):
        adapter.create(sample_entity)
        # Second create should upsert, not fail
        sample_entity.data["name"] = "Updated Name"
        adapter.create(sample_entity)

        result = adapter.read(sample_entity.id)
        assert result is not None


class TestPostgresAdapterRelationships:
    """Test relationship operations."""

    def test_create_and_find_relationship(self, adapter):
        from mosaic.core.storage.adapters.postgres_adapter import (
            PostgresEntity,
            PostgresRelationshipStore,
        )

        e1 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Parent"},
        )
        e2 = PostgresEntity(
            id=str(uuid.uuid4()),
            entity_type="Sample",
            is_available=True,
            version=1,
            data={"name": "Child"},
        )
        adapter.create(e1)
        adapter.create(e2)

        with adapter._transaction() as conn:
            rel_store = PostgresRelationshipStore(conn)
            rel = rel_store.create(
                source_id=e1.id,
                target_id=e2.id,
                relationship_type="parent_of",
            )
            assert rel.source_id == e1.id

            results = list(rel_store.find_by_source(e1.id))
            assert len(results) >= 1


class TestPostgresAdapterExternalIds:
    """Test external ID operations."""

    def test_create_and_lookup_external_id(self, adapter, sample_entity):
        from mosaic.core.storage.adapters.postgres_adapter import PostgresExternalIdStore

        adapter.create(sample_entity)

        with adapter._transaction() as conn:
            eid_store = PostgresExternalIdStore(conn)
            record = eid_store.create_external_id(sample_entity.id, "EXT-001")
            assert record.external_id == "EXT-001"

            found = eid_store.get_entity_by_external_id("EXT-001")
            assert found is not None
            assert found.entity_id == sample_entity.id


class TestPostgresBatchPut:
    """Atomic multi-entity write over Postgres (issue #84 increment 2).

    Confirms ``MosaicClient.batch_put`` is backend-agnostic: the Postgres
    adapter's ``staged_transaction`` drives the same all-or-nothing commit
    and intra-batch forward-reference resolution proven for SQLite.
    """

    @pytest.fixture
    def client(self, adapter):
        from mosaic.core.client import MosaicClient

        return MosaicClient(storage=adapter)

    def test_commits_valid_set_atomically(self, client):
        from mosaic.core.validation import WriteOperation

        ops = [
            WriteOperation(operation="insert", entity_type="Sample", data={"id": "pg-s1", "name": "a"}),
            WriteOperation(operation="insert", entity_type="Sample", data={"id": "pg-s2", "name": "b"}),
        ]
        result = client.batch_put(ops)
        assert result.committed is True
        assert client._storage.read("pg-s1") is not None
        assert client._storage.read("pg-s2") is not None

    def test_rollback_on_mid_batch_failure(self, client, monkeypatch):
        from mosaic.core.validation import WriteOperation

        ops = [
            WriteOperation(operation="insert", entity_type="Sample", data={"id": "pg-r1", "name": "a"}),
            WriteOperation(operation="insert", entity_type="Sample", data={"id": "pg-r2", "name": "b"}),
        ]
        orig = client._put_internal
        calls = {"n": 0}

        def failing(entity_type, data, entity_id=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom on second write")
            return orig(entity_type, data, entity_id)

        monkeypatch.setattr(client, "_put_internal", failing)
        with pytest.raises(RuntimeError, match="boom on second write"):
            client.batch_put(ops)

        assert client._storage.read("pg-r1") is None
        assert client._storage.read("pg-r2") is None

    def test_intra_batch_relationship_forward_reference(self, client):
        from mosaic.core.validation import WriteOperation

        ops = [
            WriteOperation(operation="insert", entity_type="Donor", data={"id": "pg-donor", "name": "D"}),
            WriteOperation(operation="insert", entity_type="Sample", data={"id": "pg-sample", "name": "S"}),
        ]
        rels = [
            {"source_id": "pg-donor", "target_id": "pg-sample", "relationship_type": "donated"}
        ]
        result = client.batch_put(ops, relationships=rels)
        assert result.committed is True
        assert len(result.relationships) == 1
        assert client._storage.read("pg-donor") is not None
        assert client._storage.read("pg-sample") is not None


class TestPostgresClientFTSWrites:
    """Regression: client writes on postgres with mosaic_search schemas.

    The first real DataHelix certification boot (datahelix#45) failed on
    every write: ``IngestionService._sync_entity_to_fts`` checked FTS-table
    existence with the SQLite helper (``sqlite_master`` + ``?`` placeholder),
    which psycopg rejects as "the query has 0 placeholders but 1 parameters
    were passed". The check must go through the adapter's own FTS store.
    """

    @pytest.fixture
    def fts_client(self):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "notes": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                }
            }
        )
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )
        client = MosaicClient(storage=adapter, registry=registry)
        yield client
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'fts_%'"
            )
            for row in cur.fetchall():
                cur.execute(f'DROP TABLE IF EXISTS "{row["tablename"]}" CASCADE')
        adapter.close()

    def test_put_succeeds_with_fts_schema(self, fts_client):
        created = fts_client.put(
            "Sample",
            {"id": str(uuid.uuid4()), "name": "n1", "notes": "searchable text"},
        )
        assert fts_client._storage.read(created["id"]) is not None

    def test_search_finds_seeded_content_on_fresh_deployment(self, fts_client):
        """No manual FTS-table creation: _init_database creates the shadow
        tables from the schema (parity with SQLite's typed-table DDL), the
        ingestion service syncs content on write, and search works out of
        the box — the certification boot's failure mode (datahelix#45,
        'relation "fts_sample_notes" does not exist')."""
        created = fts_client.put(
            "Sample",
            {"id": str(uuid.uuid4()), "name": "n2", "notes": "korokke recipe"},
        )
        results = fts_client.search("Sample", "korokke")
        assert [r["id"] for r in results.items] == [created["id"]]
        assert results.total == 1

    def test_put_syncs_content_when_fts_table_exists(self, fts_client):
        meta = fts_client._fts_table_metadata["Sample"][0]
        adapter = fts_client._storage
        with adapter._transaction() as conn:
            adapter._get_fts_store(conn).create_fts_table(
                meta.table_name, meta.get_fts_columns()
            )
        created = fts_client.put(
            "Sample",
            {"id": str(uuid.uuid4()), "name": "n2", "notes": "korokke recipe"},
        )
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT content FROM {meta.table_name} WHERE entity_id = %s",
                (created["id"],),
            )
            row = cur.fetchone()
        assert row is not None and "korokke" in row["content"]


class TestPostgresWriteParity:
    """Postgres mirrors of SQLite's mutation surface (datahelix#45).

    The ingestion service's update path calls ``storage.update_data`` and the
    availability service calls ``storage.set_availability`` on ANY adapter —
    Postgres previously had neither, so every SDK/transport update or
    availability change crashed with AttributeError. Boolean filters also
    matched nothing: ``data->>field`` yields ``true``/``false`` while the
    filter compared against ``str(False) == "False"``.
    """

    @pytest.fixture
    def parity_client(self, adapter):
        from mosaic.core.client import MosaicClient

        return MosaicClient(storage=adapter, bypass_validation=True)

    def test_update_existing_entity(self, parity_client):
        created = parity_client.put(
            "Sample", {"id": str(uuid.uuid4()), "name": "before"}
        )
        updated = parity_client.put(
            "Sample", {"name": "after"}, created["id"]
        )
        assert updated["version"] == 2
        got = parity_client.get("Sample", created["id"])
        assert got["data"]["name"] == "after"

    def test_set_availability_bulk(self, parity_client):
        created = parity_client.put(
            "Sample", {"id": str(uuid.uuid4()), "name": "to-retire"}
        )
        result = parity_client.set_availability_bulk(
            "Sample", [created["id"]], False, reason="parity test"
        )
        assert result["succeeded"] == 1 and result["failed"] == 0
        from mosaic.core.exceptions import EntityNotFoundError

        with pytest.raises(EntityNotFoundError):
            parity_client.get("Sample", created["id"])

    def test_boolean_filter_matches_jsonb_literals(self, parity_client):
        from mosaic.core.storage import Query

        flagged = parity_client.put(
            "Sample", {"id": str(uuid.uuid4()), "name": "flagged", "in_print": False}
        )
        parity_client.put(
            "Sample", {"id": str(uuid.uuid4()), "name": "unflagged", "in_print": True}
        )
        q = Query()
        q.entity_type = "Sample"
        q.filters = [{"field": "in_print", "value": False}]
        ids = [e.id for e in parity_client._storage.find(q)]
        assert ids == [flagged["id"]]


class TestPostgresComparisonFilters:
    """Parity for the ADR-0006 increment-1 operators (issue #155).

    The JSONB predicates — including the per-range casts driven by
    ``SlotModel.range`` (``::numeric`` / ``::date``), the wrong answer
    ADR-0006 names as the main Postgres risk — must produce the same id
    sets as the SQLite column predicates and as the shared as-of mirror
    (``matches_operator``). Expectations here intentionally match
    ``tests/core/test_comparison_filters.py``.
    """

    FUTURE = "2999-01-01T00:00:00+00:00"

    SCHEMA = """
id: https://example.org/mosaic/test_pg_comparison_filters
name: test_pg_comparison_filters
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

types:
  AgeInYears:
    typeof: integer
    base: int
    uri: linkml:Integer

classes:
  Specimen:
    is_a: Entity
    attributes:
      name:
        required: true
      age:
        range: AgeInYears
      score:
        range: float
      collected_on:
        range: date
      is_tumor:
        range: boolean
      notes: {}
"""

    @pytest.fixture
    def cmp_adapter(self):
        from mosaic.core.storage.adapters.postgres_adapter import (
            PostgresAdapter,
            PostgresEntity,
        )
        from mosaic.linkml_bridge import SchemaRegistry

        registry = SchemaRegistry.from_yaml(self.SCHEMA)
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )

        def seed(entity_id, **data):
            adapter.create(
                PostgresEntity(
                    id=entity_id,
                    entity_type="Specimen",
                    is_available=True,
                    version=1,
                    data={"id": entity_id, **data},
                )
            )

        seed(
            "s1",
            name="Alpha",
            age=45,
            score=1.5,
            collected_on="2024-01-10",
            is_tumor=False,
            notes="First batch",
        )
        seed(
            "s2",
            name="Beta",
            age=60,
            score=2.5,
            collected_on="2025-03-05",
            is_tumor=True,
        )
        seed(
            "s3",
            name="Gamma",
            age=75,
            score=3.5,
            collected_on="2026-06-20",
            is_tumor=False,
            notes="follow-up 50%_done",
        )

        yield adapter

        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")

    def _both_paths(self, adapter, filters, filter_mode="and") -> set:
        from mosaic.core.storage import Query

        def run(as_of=None):
            q = Query(
                entity_type="Specimen",
                filters=filters,
                filter_mode=filter_mode,
            )
            return {e.id for e in adapter.find(q, as_of=as_of)}

        live = run()
        asof = run(as_of=self.FUTURE)
        assert live == asof, (
            f"live/as-of divergence for {filters!r}: {live} != {asof}"
        )
        return live

    def test_gt_integer_casts_numerically(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter, [{"field": "age", "op": "gt", "value": 60}]
        ) == {"s3"}

    def test_gte_lte_float(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter, [{"field": "score", "op": "gte", "value": 2.5}]
        ) == {"s2", "s3"}
        assert self._both_paths(
            cmp_adapter, [{"field": "score", "op": "lte", "value": 2.5}]
        ) == {"s1", "s2"}

    def test_numeric_not_lexicographic(self, cmp_adapter):
        # '100' < '9' as text — the ::numeric cast must prevent that.
        assert self._both_paths(
            cmp_adapter, [{"field": "age", "op": "lt", "value": 100}]
        ) == {"s1", "s2", "s3"}

    def test_gt_date_casts(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter,
            [{"field": "collected_on", "op": "gt", "value": "2024-12-31"}],
        ) == {"s2", "s3"}

    def test_neq_excludes_absent(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter,
            [{"field": "notes", "op": "neq", "value": "First batch"}],
        ) == {"s3"}

    def test_contains_case_insensitive_and_literal_wildcards(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter, [{"field": "notes", "op": "contains", "value": "FIRST"}]
        ) == {"s1"}
        assert self._both_paths(
            cmp_adapter, [{"field": "notes", "op": "contains", "value": "50%"}]
        ) == {"s3"}
        assert self._both_paths(
            cmp_adapter, [{"field": "notes", "op": "contains", "value": "0_x"}]
        ) == set()

    def test_is_null_both_ways(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter, [{"field": "notes", "op": "is_null", "value": True}]
        ) == {"s2"}
        assert self._both_paths(
            cmp_adapter, [{"field": "notes", "op": "is_null", "value": False}]
        ) == {"s1", "s3"}

    def test_eq_none_matches_nothing(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter, [{"field": "notes", "op": "eq", "value": None}]
        ) == set()

    def test_or_composition(self, cmp_adapter):
        assert self._both_paths(
            cmp_adapter,
            [
                {"field": "age", "op": "gt", "value": 70},
                {"field": "notes", "op": "contains", "value": "first"},
            ],
            filter_mode="or",
        ) == {"s1", "s3"}

    def test_comparison_without_entity_type_raises(self, cmp_adapter):
        from mosaic.core.exceptions import ValidationError
        from mosaic.core.storage import Query

        q = Query(filters=[{"field": "age", "op": "gt", "value": 60}])
        with pytest.raises(ValidationError, match="entity_type"):
            list(cmp_adapter.find(q))


class TestPostgresWhereTree:
    """Parity for `where` boolean filter trees (ADR-0006 increment 2).

    The JSONB tree compiler — including the COALESCE two-valued `not`
    semantics — must produce the same id sets as the SQLite column tree
    compiler and the shared Python mirror (`matches_tree`). Expectations
    intentionally match `tests/core/test_where_tree.py`.
    """

    FUTURE = "2999-01-01T00:00:00+00:00"

    @pytest.fixture
    def tree_adapter(self):
        from mosaic.core.storage.adapters.postgres_adapter import (
            PostgresAdapter,
            PostgresEntity,
        )
        from mosaic.linkml_bridge import SchemaRegistry

        registry = SchemaRegistry.from_yaml(TestPostgresComparisonFilters.SCHEMA)
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )

        def seed(entity_id, **data):
            adapter.create(
                PostgresEntity(
                    id=entity_id,
                    entity_type="Specimen",
                    is_available=True,
                    version=1,
                    data={"id": entity_id, **data},
                )
            )

        seed("s1", name="Alpha", age=45, is_tumor=False, notes="First batch")
        seed("s2", name="Beta", age=60, is_tumor=True)
        seed("s3", name="Gamma", age=75, is_tumor=False, notes="follow-up")

        yield adapter

        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")

    def _both_paths(self, adapter, where, filters=None) -> set:
        from mosaic.core.storage import Query

        def run(as_of=None):
            q = Query(
                entity_type="Specimen", filters=filters or [], where=where
            )
            return {e.id for e in adapter.find(q, as_of=as_of)}

        live = run()
        asof = run(as_of=self.FUTURE)
        assert live == asof, (
            f"live/as-of divergence for {where!r}: {live} != {asof}"
        )
        return live

    def test_leaf_with_cast(self, tree_adapter):
        assert self._both_paths(
            tree_adapter, {"field": "age", "op": "gt", "value": 50}
        ) == {"s2", "s3"}

    def test_and_or_nesting(self, tree_adapter):
        assert self._both_paths(
            tree_adapter,
            {"and": [
                {"not": {"field": "is_tumor", "value": True}},
                {"or": [
                    {"field": "age", "op": "lte", "value": 45},
                    {"field": "age", "op": "gte", "value": 75},
                ]},
            ]},
        ) == {"s1", "s3"}

    def test_not_is_two_valued_on_jsonb(self, tree_adapter):
        # s2 has no notes: NOT(contains) must include it — the COALESCE
        # wrap prevents SQL NULL from silently excluding the row.
        assert self._both_paths(
            tree_adapter,
            {"not": {"field": "notes", "op": "contains", "value": "First"}},
        ) == {"s2", "s3"}

    def test_composes_with_flat_filters(self, tree_adapter):
        assert self._both_paths(
            tree_adapter,
            {"field": "age", "op": "gt", "value": 50},
            filters=[{"field": "is_tumor", "value": False}],
        ) == {"s3"}

    def test_is_null_leaf(self, tree_adapter):
        assert self._both_paths(
            tree_adapter, {"field": "notes", "op": "is_null", "value": True}
        ) == {"s2"}


class TestPostgresAggregationAndOrdering:
    """Parity for the ADR-0007 aggregation & ordering surface (issue #156).

    ORDER BY pushdown over JSONB (per-range casts so numeric/temporal slots
    order by value, NULLS LAST + id tiebreak matching SQLite), COUNT(*)
    under the identical predicate, facet buckets with native jsonb value
    decode, and min/max with driver-type normalization (Decimal → int/
    float, date/datetime → ISO strings). Expectations intentionally match
    ``tests/core/test_aggregation_and_ordering.py``.
    """

    FUTURE = "2999-01-01T00:00:00+00:00"

    @pytest.fixture
    def agg_adapter(self):
        from mosaic.core.storage.adapters.postgres_adapter import (
            PostgresAdapter,
            PostgresEntity,
        )
        from mosaic.linkml_bridge import SchemaRegistry

        registry = SchemaRegistry.from_yaml(TestPostgresComparisonFilters.SCHEMA)
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )

        def seed(entity_id, **data):
            adapter.create(
                PostgresEntity(
                    id=entity_id,
                    entity_type="Specimen",
                    is_available=True,
                    version=1,
                    data={"id": entity_id, **data},
                )
            )

        seed("s1", name="Alpha", age=45, score=1.5,
             collected_on="2024-01-10", is_tumor=False)
        seed("s2", name="Beta", age=60, score=2.5,
             collected_on="2025-03-05", is_tumor=True)
        seed("s3", name="Gamma", age=75, score=3.5,
             collected_on="2026-06-20", is_tumor=False)
        # No age/score/collected_on — the NULLs-last target.
        seed("s4", name="Delta", is_tumor=True)
        # Made unavailable — must be invisible to every aggregate
        # (ADR-0007's availability-consistency rule).
        seed("s5", name="Ghost", age=99, score=9.9, is_tumor=True)
        adapter.set_availability("s5", "Specimen", False, reason="test")

        yield adapter

        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")

    @staticmethod
    def _ids(adapter, **query_kwargs):
        from mosaic.core.storage import Query

        return [
            e.id
            for e in adapter.find(Query(entity_type="Specimen", **query_kwargs))
        ]

    def test_order_asc_nulls_last(self, agg_adapter):
        assert self._ids(agg_adapter, order_by="age") == ["s1", "s2", "s3", "s4"]

    def test_order_desc_nulls_still_last(self, agg_adapter):
        assert self._ids(agg_adapter, order_by="age", order_dir="desc") == [
            "s3", "s2", "s1", "s4",
        ]

    def test_numeric_order_is_by_value_not_text(self, agg_adapter):
        # Text ordering would put 9.9 < 45 wrong; the ::numeric cast keeps
        # value order. (s5 is unavailable and must stay invisible.)
        assert self._ids(agg_adapter, order_by="score", order_dir="desc") == [
            "s3", "s2", "s1", "s4",
        ]

    def test_date_order_by_value(self, agg_adapter):
        assert self._ids(
            agg_adapter, order_by="collected_on", order_dir="desc"
        ) == ["s3", "s2", "s1", "s4"]

    def test_id_tiebreak_is_stable(self, agg_adapter):
        # is_tumor False×2 (s1,s3), True×2 (s2,s4): ties break by id asc.
        # Boolean false < true in jsonb-text order ("false" < "true").
        assert self._ids(agg_adapter, order_by="is_tumor") == [
            "s1", "s3", "s2", "s4",
        ]

    def test_order_with_limit_offset_pushdown(self, agg_adapter):
        assert self._ids(agg_adapter, order_by="age", limit=2, offset=1) == [
            "s2", "s3",
        ]

    def test_order_by_id_column(self, agg_adapter):
        assert self._ids(agg_adapter, order_by="id", order_dir="desc") == [
            "s4", "s3", "s2", "s1",
        ]

    def test_unknown_order_column_raises(self, agg_adapter):
        from mosaic.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="order_by"):
            self._ids(agg_adapter, order_by="nope")

    def test_order_by_with_as_of_raises(self, agg_adapter):
        from mosaic.core.exceptions import ValidationError
        from mosaic.core.storage import Query

        with pytest.raises(ValidationError, match="as_of|as-of|asOf"):
            list(
                agg_adapter.find(
                    Query(entity_type="Specimen", order_by="age"),
                    as_of=self.FUTURE,
                )
            )

    def test_count_excludes_unavailable(self, agg_adapter):
        from mosaic.core.storage import Query

        assert agg_adapter.count(Query(entity_type="Specimen")) == 4

    def test_count_with_filters_and_where(self, agg_adapter):
        from mosaic.core.storage import Query

        assert (
            agg_adapter.count(
                Query(
                    entity_type="Specimen",
                    filters=[{"field": "age", "op": "gt", "value": 50}],
                )
            )
            == 2
        )
        assert (
            agg_adapter.count(
                Query(
                    entity_type="Specimen",
                    where={
                        "or": [
                            {"field": "is_tumor", "value": True},
                            {"field": "age", "op": "gte", "value": 75},
                        ]
                    },
                )
            )
            == 3
        )

    def test_count_ignores_limit_offset(self, agg_adapter):
        from mosaic.core.storage import Query

        assert agg_adapter.count(
            Query(entity_type="Specimen", limit=1, offset=1)
        ) == 4

    def test_count_as_of(self, agg_adapter):
        from mosaic.core.storage import Query

        assert (
            agg_adapter.count(Query(entity_type="Specimen"), as_of=self.FUTURE)
            == 4
        )

    def test_facets_decode_native_values(self, agg_adapter):
        from mosaic.core.storage import Query

        # Booleans come back as bool (native jsonb decode), unavailable s5
        # is excluded, ties order by value (false < true in jsonb).
        assert agg_adapter.facet_counts(
            Query(entity_type="Specimen"), "is_tumor"
        ) == [(False, 2), (True, 2)]
        # Integers come back as int, s4's absent age is not a bucket.
        assert agg_adapter.facet_counts(
            Query(entity_type="Specimen"), "age"
        ) == [(45, 1), (60, 1), (75, 1)]

    def test_facets_respect_filters(self, agg_adapter):
        from mosaic.core.storage import Query

        assert agg_adapter.facet_counts(
            Query(
                entity_type="Specimen",
                filters=[{"field": "age", "op": "gte", "value": 60}],
            ),
            "is_tumor",
        ) == [(False, 1), (True, 1)]

    def test_facet_unknown_field_raises(self, agg_adapter):
        from mosaic.core.exceptions import ValidationError
        from mosaic.core.storage import Query

        with pytest.raises(ValidationError, match="facet_counts"):
            agg_adapter.facet_counts(Query(entity_type="Specimen"), "nope")

    def test_field_range_normalizes_driver_types(self, agg_adapter):
        from mosaic.core.storage import Query

        q = Query(entity_type="Specimen")
        assert agg_adapter.field_range(q, "age") == (45, 75)  # int, not Decimal
        assert agg_adapter.field_range(q, "score") == (1.5, 3.5)
        assert agg_adapter.field_range(q, "collected_on") == (
            "2024-01-10", "2026-06-20",  # ISO strings, not date objects
        )

    def test_field_range_empty_is_none_none(self, agg_adapter):
        from mosaic.core.storage import Query

        assert agg_adapter.field_range(
            Query(
                entity_type="Specimen",
                filters=[{"field": "name", "value": "nobody"}],
            ),
            "age",
        ) == (None, None)


class TestPostgresSearchComposition:
    """Parity for search composition (issue #157): ranked ts_rank ids feed
    one composed find(); envelope, filter intersection, rank-vs-order_by
    precedence, and availability consistency match the SQLite path
    (``tests/core/test_search_composition.py``)."""

    @pytest.fixture
    def search_client(self):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Note": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "tissue": {"range": "string"},
                        "priority": {"range": "integer"},
                        "body": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                }
            }
        )
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )
        client = MosaicClient(storage=adapter, registry=registry)
        client.put("Note", {"id": "n1", "name": "One", "tissue": "brain",
                            "priority": 1, "body": "cortex cortex cortex alpha"})
        client.put("Note", {"id": "n2", "name": "Two", "tissue": "brain",
                            "priority": 2, "body": "cortex cortex beta filler"})
        client.put("Note", {"id": "n3", "name": "Three", "tissue": "liver",
                            "priority": 3, "body": "cortex gamma delta filler"})
        client.put("Note", {"id": "n4", "name": "Four", "tissue": "brain",
                            "priority": 4, "body": "hippocampus only here now"})
        yield client
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'fts_%'"
            )
            for row in cur.fetchall():
                cur.execute(f'DROP TABLE IF EXISTS "{row["tablename"]}" CASCADE')
        adapter.close()

    @staticmethod
    def _ids(result):
        return [item["id"] for item in result.items]

    def test_envelope_in_rank_order(self, search_client):
        result = search_client.search("Note", "cortex")
        assert self._ids(result) == ["n1", "n2", "n3"]
        assert result.total == 3

    def test_offset_ge_limit_returns_correct_page(self, search_client):
        result = search_client.search("Note", "cortex", limit=1, offset=2)
        assert self._ids(result) == ["n3"]
        assert result.total == 3

    def test_filters_intersect_ranked_hits(self, search_client):
        result = search_client.search(
            "Note", "cortex", filters=[{"field": "tissue", "value": "brain"}]
        )
        assert self._ids(result) == ["n1", "n2"]
        assert result.total == 2

    def test_where_tree_composes(self, search_client):
        result = search_client.search(
            "Note", "cortex",
            where={"field": "priority", "op": "gte", "value": 2},
        )
        assert self._ids(result) == ["n2", "n3"]

    def test_order_by_overrides_rank(self, search_client):
        result = search_client.search(
            "Note", "cortex", order_by="priority", order_dir="desc"
        )
        assert self._ids(result) == ["n3", "n2", "n1"]
        assert result.total == 3

    def test_unavailable_entities_never_surface(self, search_client):
        search_client.put("Note", {"id": "n5", "name": "Ghost", "tissue": "x",
                                   "priority": 5, "body": "cortex ghost gone"})
        search_client.set_availability_bulk(
            entity_type="Note", entity_ids=["n5"],
            is_available=False, reason="test",
        )
        result = search_client.search("Note", "cortex")
        assert "n5" not in self._ids(result)
        assert result.total == 3


class TestPostgresRelationshipPredicates:
    """Parity for to-one relationship predicates (ADR-0006 M5a, #155).

    The JSONB compiler's correlated EXISTS — `relN.id = data->>'edge'`
    with `entity_type` and availability guards, aliases threaded so nested
    and self-referential edges never collide, and per-range casts resolved
    against the TARGET class — must produce the same id sets as the SQLite
    per-class-table compiler. Expectations intentionally match
    ``tests/core/test_relationship_predicates.py``.
    """

    FUTURE = "2999-01-01T00:00:00+00:00"

    SCHEMA_CLASSES = {
        "Facility": {
            "attributes": {
                "id": {"identifier": True},
                "name": {"range": "string", "required": True},
                "city": {"range": "string"},
            }
        },
        "Donor": {
            "attributes": {
                "id": {"identifier": True},
                "name": {"range": "string", "required": True},
                "age": {"range": "integer"},
                "facility_id": {"range": "Facility"},
            }
        },
        "Sample": {
            "attributes": {
                "id": {"identifier": True},
                "name": {"range": "string", "required": True},
                "tissue": {"range": "string"},
                "donor_id": {"range": "Donor"},
                "parent": {"range": "Sample"},
            }
        },
    }

    @pytest.fixture
    def rel_adapter(self):
        from mosaic.core.storage.adapters.postgres_adapter import (
            PostgresAdapter,
            PostgresEntity,
        )
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(self.SCHEMA_CLASSES)
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )

        def seed(entity_id, entity_type, **data):
            adapter.create(
                PostgresEntity(
                    id=entity_id,
                    entity_type=entity_type,
                    is_available=True,
                    version=1,
                    data={"id": entity_id, **data},
                )
            )

        seed("f1", "Facility", name="North", city="Boston")
        seed("f2", "Facility", name="South", city="NYC")
        seed("d1", "Donor", name="Ada", age=76, facility_id="f1")
        seed("d2", "Donor", name="Alan", age=41, facility_id="f2")
        seed("d3", "Donor", name="Grace", age=85)
        seed("s1", "Sample", name="S1", tissue="brain", donor_id="d1")
        seed("s2", "Sample", name="S2", tissue="brain", donor_id="d2")
        seed("s3", "Sample", name="S3", tissue="liver", donor_id="d1")
        seed("s4", "Sample", name="S4", tissue="brain")
        seed("s5", "Sample", name="S5", tissue="liver", parent="s1")

        yield adapter

        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")
        adapter.close()

    @staticmethod
    def _ids(adapter, where, entity_type="Sample") -> set:
        from mosaic.core.storage import Query

        return {
            e.id
            for e in adapter.find(Query(entity_type=entity_type, where=where))
        }

    def test_basic_edge_predicate_with_numeric_cast(self, rel_adapter):
        # age compares through ::numeric on the TARGET class — a text
        # comparison would order "41" > "60" wrong.
        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        assert self._ids(rel_adapter, where) == {"s1", "s3"}

    def test_edge_composes_with_scalar_by_and(self, rel_adapter):
        where = {
            "and": [
                {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}},
                {"field": "tissue", "value": "brain"},
            ]
        }
        assert self._ids(rel_adapter, where) == {"s1"}

    def test_not_edge_includes_refless_entities(self, rel_adapter):
        where = {
            "not": {
                "edge": "donor_id",
                "where": {"field": "age", "op": "gt", "value": 60},
            }
        }
        assert self._ids(rel_adapter, where) == {"s2", "s4", "s5"}

    def test_nested_edge_inside_edge(self, rel_adapter):
        where = {
            "edge": "donor_id",
            "where": {
                "edge": "facility_id",
                "where": {"field": "city", "value": "Boston"},
            },
        }
        assert self._ids(rel_adapter, where) == {"s1", "s3"}

    def test_self_referential_edge(self, rel_adapter):
        where = {"edge": "parent", "where": {"field": "tissue", "value": "brain"}}
        assert self._ids(rel_adapter, where) == {"s5"}

    def test_unavailable_target_never_matches(self, rel_adapter):
        rel_adapter.set_availability("d2", "Donor", False, reason="test")
        where = {"edge": "donor_id", "where": {"field": "age", "op": "lt", "value": 50}}
        assert self._ids(rel_adapter, where) == set()

    def test_count_with_edge_predicate(self, rel_adapter):
        from mosaic.core.storage import Query

        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        assert rel_adapter.count(Query(entity_type="Sample", where=where)) == 2

    def test_unknown_edge_raises(self, rel_adapter):
        from mosaic.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="reference slots"):
            self._ids(
                rel_adapter,
                {"edge": "nope", "where": {"field": "age", "value": 1}},
            )

    def test_as_of_with_edge_predicate_raises(self, rel_adapter):
        from mosaic.core.exceptions import ValidationError
        from mosaic.core.storage import Query

        where = {"edge": "donor_id", "where": {"field": "age", "op": "gt", "value": 60}}
        with pytest.raises(ValidationError, match="as_of"):
            list(
                rel_adapter.find(
                    Query(entity_type="Sample", where=where), as_of=self.FUTURE
                )
            )


class TestPostgresHeterogeneousRoots:
    """Parity for the heterogeneous roots (issue #158): searchAll's
    cross-class rank merge over ts_rank and neighbors' two-edge-store
    union (link table + JSONB column references). Expectations
    intentionally match ``tests/core/test_heterogeneous_roots.py``."""

    @pytest.fixture
    def het_client(self):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Donor": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "bio": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                },
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "donor_id": {"range": "Donor"},
                        "notes": {
                            "range": "string",
                            "annotations": {"mosaic_search": "fts5"},
                        },
                    }
                },
                "Study": {
                    "attributes": {
                        "id": {"identifier": True},
                        "title": {"range": "string", "required": True},
                    }
                },
            }
        )
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )
        client = MosaicClient(storage=adapter, registry=registry)
        client.put("Donor", {"id": "d1", "name": "Ada",
                             "bio": "cortex cortex cortex researcher"})
        client.put("Sample", {"id": "s1", "name": "S1", "donor_id": "d1",
                              "notes": "cortex cortex lesion sample"})
        client.put("Sample", {"id": "s2", "name": "S2",
                              "notes": "cortex intact tissue sample"})
        client.put("Study", {"id": "st1", "title": "Cortex Study"})
        client.relationships.relate("st1", "s1", "includes_sample")
        yield client
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'fts_%'"
            )
            for row in cur.fetchall():
                cur.execute(f'DROP TABLE IF EXISTS "{row["tablename"]}" CASCADE')
        adapter.close()

    def test_search_all_rank_merged(self, het_client):
        hits = het_client.search_all("cortex")
        assert [(h["entity_type"], h["id"]) for h in hits] == [
            ("Donor", "d1"), ("Sample", "s1"), ("Sample", "s2"),
        ]
        assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]

    def test_search_all_availability_parity(self, het_client):
        het_client.set_availability_bulk(
            entity_type="Sample", entity_ids=["s1"],
            is_available=False, reason="test",
        )
        hits = het_client.search_all("cortex")
        assert [(h["entity_type"], h["id"]) for h in hits] == [
            ("Donor", "d1"), ("Sample", "s2"),
        ]

    def test_neighbors_gap_both_edge_stores(self, het_client):
        graph = het_client.neighbors("s1")
        edges = {(e["source"], e["target"], e["edge_source"])
                 for e in graph["edges"]}
        assert ("s1", "d1", "COLUMN") in edges
        assert ("st1", "s1", "LINK_TABLE") in edges
        assert {n["entity_id"] for n in graph["nodes"]} == {"s1", "d1", "st1"}

    def test_neighbors_reverse_column_edges(self, het_client):
        graph = het_client.neighbors("d1")
        edges = {(e["source"], e["target"], e["type"]) for e in graph["edges"]}
        assert ("s1", "d1", "donor_id") in edges

    def test_neighbors_as_of_disclosure(self, het_client):
        graph = het_client.neighbors(
            "s1", depth=2, as_of="2999-01-01T00:00:00+00:00"
        )
        assert graph["edge_sources"] == ["LINK_TABLE"]
        assert any("hippo#71" in n for n in graph["notices"])
        assert {(e["source"], e["target"]) for e in graph["edges"]} == {
            ("st1", "s1")
        }


class TestPostgresQuantifiedPredicates:
    """Parity for to-many quantified predicates (ADR-0006 M5b, #155).

    EXISTS/NOT EXISTS over the relationships link table joined to the
    target's entities row (entity_type + availability guards on both edge
    and target) must match the SQLite per-class-table compiler.
    Expectations intentionally match
    ``tests/core/test_quantified_predicates.py``.
    """

    FUTURE = "2999-01-01T00:00:00+00:00"

    @pytest.fixture
    def quant_client(self):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "tissue": {"range": "string"},
                        "volume": {"range": "integer"},
                        "parent": {"range": "Sample"},
                    }
                },
                "Study": {
                    "attributes": {
                        "id": {"identifier": True},
                        "title": {"range": "string", "required": True},
                        "sample_ids": {"range": "Sample", "multivalued": True},
                    }
                },
            }
        )
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )
        client = MosaicClient(storage=adapter, registry=registry)
        client.put("Sample", {"id": "s1", "name": "S1", "tissue": "brain", "volume": 10})
        client.put("Sample", {"id": "s2", "name": "S2", "tissue": "liver", "volume": 20})
        client.put("Sample", {"id": "s3", "name": "S3", "tissue": "brain", "volume": 30})
        client.put("Study", {"id": "st1", "title": "Brain+Liver",
                             "sample_ids": ["s1", "s2"]})
        client.put("Study", {"id": "st2", "title": "LiverOnly",
                             "sample_ids": ["s2"]})
        client.put("Study", {"id": "st3", "title": "Empty"})
        client.put("Study", {"id": "st4", "title": "BrainOnly",
                             "sample_ids": ["s3"]})
        yield client
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")
        adapter.close()

    @staticmethod
    def _ids(client, where, entity_type="Study") -> set:
        return {i["id"] for i in client.query(entity_type, where=where).items}

    @staticmethod
    def _some(sub):
        return {"edge": "sample_ids", "quantifier": "some", "where": sub}

    @staticmethod
    def _none(sub):
        return {"edge": "sample_ids", "quantifier": "none", "where": sub}

    def test_some(self, quant_client):
        assert self._ids(
            quant_client, self._some({"field": "tissue", "value": "brain"})
        ) == {"st1", "st4"}

    def test_none_includes_edgeless(self, quant_client):
        assert self._ids(
            quant_client, self._none({"field": "tissue", "value": "brain"})
        ) == {"st2", "st3"}

    def test_comparison_on_target_casts_numerically(self, quant_client):
        assert self._ids(
            quant_client,
            self._some({"field": "volume", "op": "gte", "value": 25}),
        ) == {"st4"}

    def test_composes_with_combinators(self, quant_client):
        where = {
            "and": [
                self._some({"field": "tissue", "value": "brain"}),
                {"not": self._some({"field": "tissue", "value": "liver"})},
            ]
        }
        assert self._ids(quant_client, where) == {"st4"}

    def test_unavailable_target_never_satisfies_some(self, quant_client):
        quant_client.set_availability_bulk(
            entity_type="Sample", entity_ids=["s3"],
            is_available=False, reason="test",
        )
        assert self._ids(
            quant_client, self._some({"field": "tissue", "value": "brain"})
        ) == {"st1"}
        assert self._ids(
            quant_client, self._none({"field": "tissue", "value": "brain"})
        ) == {"st2", "st3", "st4"}

    def test_quantifier_cardinality_mismatches_raise(self, quant_client):
        from mosaic.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="some"):
            self._ids(quant_client, {"edge": "sample_ids",
                                     "where": {"field": "tissue", "value": "x"}})
        with pytest.raises(ValidationError, match="to-one"):
            self._ids(
                quant_client,
                {"edge": "parent", "quantifier": "some",
                 "where": {"field": "tissue", "value": "x"}},
                entity_type="Sample",
            )

    def test_as_of_with_quantifier_raises(self, quant_client):
        from mosaic.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="as_of"):
            quant_client.query(
                "Study",
                where=self._some({"field": "tissue", "value": "brain"}),
                as_of=self.FUTURE,
            )


class TestPostgresRelationshipCount:
    """Parity for the cheap relationship-cardinality primitive (issue
    #132, deferred from ADR-0005). Single ``COUNT(*)`` over the
    relationships table joined to ``entities`` — expectations mirror
    ``tests/core/test_relationship_count.py``.
    """

    @pytest.fixture
    def count_client(self):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter
        from tests.support.linkml_schemas import build_registry

        registry = build_registry(
            {
                "Sample": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "parent": {"range": "Sample"},
                    }
                },
                "Study": {
                    "attributes": {
                        "id": {"identifier": True},
                        "title": {"range": "string", "required": True},
                        "sample_ids": {"range": "Sample", "multivalued": True},
                    }
                },
            }
        )
        adapter = PostgresAdapter(
            database_url=POSTGRES_URL,
            schema_registry=registry,
            min_pool_size=1,
            max_pool_size=5,
        )
        client = MosaicClient(storage=adapter, registry=registry)
        client.put("Sample", {"id": "s1", "name": "S1"})
        client.put("Sample", {"id": "s2", "name": "S2"})
        client.put("Study", {"id": "st1", "title": "Mixed",
                             "sample_ids": ["s1", "s2"]})
        client.put("Study", {"id": "st2", "title": "Empty"})
        yield client
        with adapter._transaction() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM relationships")
            cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
            cur.execute('DELETE FROM "ProvenanceRecord"')
            cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
            cur.execute("DELETE FROM entities")
        adapter.close()

    def test_counts_without_resolving(self, count_client):
        assert count_client.count_relationship("Study", "st1", "sample_ids") == 2

    def test_edgeless_entity_counts_zero(self, count_client):
        assert count_client.count_relationship("Study", "st2", "sample_ids") == 0

    def test_unavailable_target_not_counted(self, count_client):
        count_client.set_availability_bulk(
            entity_type="Sample", entity_ids=["s2"],
            is_available=False, reason="test",
        )
        assert count_client.count_relationship("Study", "st1", "sample_ids") == 1

    def test_quantifier_cardinality_mismatches_raise(self, count_client):
        from mosaic.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="reference slots"):
            count_client.count_relationship("Study", "st1", "nope")
        with pytest.raises(ValidationError, match="to-one"):
            count_client.count_relationship("Sample", "s1", "parent")
