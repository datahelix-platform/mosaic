"""PostgreSQL parity for multivalued slots (issue #79 / #81 / ADR-0002).

Requires a running PostgreSQL instance. Set MOSAIC_DATABASE_URL to connect:

    MOSAIC_DATABASE_URL=postgresql://hippo_test:hippo_test@localhost:5433/hippo_test \\
        pytest tests/integration/test_postgres_multivalued_refs.py

Port of ``tests/core/test_multivalued_refs.py`` (the SQLite fix from #79)
onto ``PostgresAdapter`` directly — mirroring ``test_postgres_adapter.py``'s
fixture style rather than going through ``mosaic.client_for_schema``, so the
test exercises the adapter's ``create``/``update_data``/``read``/``find``
paths exactly like the rest of the Postgres integration suite.

Covers the two ADR-0002 storage rules, now on Postgres:

- multivalued **reference** slots (range is an entity class) persist as
  relationships keyed by the slot name, hydrated back on read/query;
- multivalued **non-reference** slots (scalars/enums) persist inline in the
  entity's JSONB document, unaffected by the relationship materialization.
"""

from __future__ import annotations

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


def _mv_schema_registry():
    """SchemaRegistry with a multivalued reference slot and a multivalued
    scalar slot — the same shape as ``tests/core/test_multivalued_refs.py``'s
    inline schema, built the way ``tests/conftest.py`` builds its minimal
    registry (overlay + bundled mosaic_core import)."""
    import yaml
    from linkml_runtime.utils.schemaview import SchemaView

    from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap

    overlay = {
        "id": "https://example.org/mosaic/test_mv_postgres",
        "name": "test_mv_postgres",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "mosaic": "https://w3id.org/datahelix/mosaic/",
        },
        "imports": ["linkml:types", "mosaic_core"],
        "default_range": "string",
        "classes": {
            "Sample": {
                "is_a": "Entity",
                "attributes": {
                    "sample_id": {"range": "string"},
                },
            },
            "Assay": {
                "is_a": "Entity",
                "attributes": {
                    "platform": {"range": "string"},
                    "inputs": {  # multivalued reference -> relationships
                        "range": "Sample",
                        "multivalued": True,
                    },
                    "tags": {  # multivalued scalar -> inline JSON
                        "range": "string",
                        "multivalued": True,
                    },
                },
            },
        },
    }
    return SchemaRegistry(
        SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
    )


@pytest.fixture
def registry():
    return _mv_schema_registry()


@pytest.fixture
def adapter(registry):
    """Fresh PostgresAdapter with clean tables, scoped to the mv-ref schema."""
    from mosaic.core.storage.adapters.postgres_adapter import PostgresAdapter

    adapter = PostgresAdapter(
        database_url=POSTGRES_URL,
        schema_registry=registry,
        min_pool_size=1,
        max_pool_size=5,
    )

    yield adapter

    with adapter._transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM entity_external_ids")
        cur.execute("DELETE FROM relationships")
        cur.execute('ALTER TABLE "ProvenanceRecord" DISABLE TRIGGER ALL')
        cur.execute('DELETE FROM "ProvenanceRecord"')
        cur.execute('ALTER TABLE "ProvenanceRecord" ENABLE TRIGGER ALL')
        cur.execute("DELETE FROM entities")

    adapter.close()


def _make_entity(entity_type: str, data: dict, entity_id: str):
    from mosaic.core.storage.adapters.postgres_adapter import PostgresEntity

    return PostgresEntity(
        id=entity_id,
        entity_type=entity_type,
        is_available=True,
        version=1,
        data=data,
    )


def _put(adapter, entity_type: str, data: dict, entity_id: str):
    """Upsert helper mirroring IngestionService's create-or-update dispatch."""
    existing = adapter.read(entity_id)
    if existing is None:
        adapter.create(_make_entity(entity_type, data, entity_id))
    else:
        adapter.update_data(
            entity_id=entity_id,
            entity_type=entity_type,
            data=data,
            new_version=existing.version + 1,
        )


def _relationships_from(adapter, source_id: str):
    with adapter._transaction() as conn:
        rel_store = adapter._get_relationship_store(conn)
        return list(rel_store.find_by_source(source_id))


class TestPostgresMultivaluedReferenceSlots:
    def test_get_round_trips_multivalued_reference(self, adapter):
        _put(adapter, "Sample", {"sample_id": "S1"}, "S1")
        _put(adapter, "Sample", {"sample_id": "S2"}, "S2")
        _put(adapter, "Assay", {"platform": "Illumina", "inputs": ["S1", "S2"]}, "A1")

        got = adapter.read("A1")
        assert got.data["platform"] == "Illumina"
        assert got.data["inputs"] == ["S1", "S2"]  # order preserved

    def test_query_round_trips_multivalued_reference(self, adapter):
        from mosaic.core.storage import Query

        _put(adapter, "Sample", {"sample_id": "S1"}, "S1")
        _put(adapter, "Assay", {"inputs": ["S1"]}, "A1")

        results = list(adapter.find(Query(entity_type="Assay", limit=1)))
        assert results[0].data["inputs"] == ["S1"]

    def test_reference_materializes_as_relationships(self, adapter):
        _put(adapter, "Sample", {"sample_id": "S1"}, "S1")
        _put(adapter, "Assay", {"inputs": ["S1"]}, "A1")

        rels = _relationships_from(adapter, "A1")
        assert [(r.relationship_type, r.target_id) for r in rels] == [
            ("inputs", "S1")
        ]

    def test_forward_reference_is_stored_without_target(self, adapter):
        # Target does not exist yet (bulk-ingest ordering); the edge must
        # still persist, matching single-valued-ref behavior.
        _put(adapter, "Assay", {"inputs": ["S_FUTURE"]}, "A1")

        rels = _relationships_from(adapter, "A1")
        assert [r.target_id for r in rels] == ["S_FUTURE"]

    def test_update_reconciles_edges(self, adapter):
        _put(adapter, "Sample", {"sample_id": "S1"}, "S1")
        _put(adapter, "Sample", {"sample_id": "S2"}, "S2")
        _put(adapter, "Assay", {"inputs": ["S1", "S2"]}, "A1")

        # Drop S2 on update — its edge must go away, S1 stays.
        _put(adapter, "Assay", {"inputs": ["S1"]}, "A1")

        got = adapter.read("A1")
        assert got.data["inputs"] == ["S1"]
        rels = _relationships_from(adapter, "A1")
        assert [r.target_id for r in rels] == ["S1"]

    def test_update_omitting_slot_clears_edges(self, adapter):
        _put(adapter, "Sample", {"sample_id": "S1"}, "S1")
        _put(adapter, "Assay", {"inputs": ["S1"]}, "A1")

        # Omitting the slot entirely clears its edges (matching column-NULL
        # semantics on the SQLite side).
        _put(adapter, "Assay", {"platform": "X"}, "A1")

        got = adapter.read("A1")
        assert "inputs" not in got.data
        assert _relationships_from(adapter, "A1") == []

    def test_scalar_value_is_unaffected(self, adapter):
        # The contrast case from the issue: scalar slots always worked.
        _put(adapter, "Sample", {"sample_id": "S1"}, "S1")
        _put(adapter, "Assay", {"platform": "Illumina"}, "A1")
        assert adapter.read("A1").data["platform"] == "Illumina"


class TestPostgresMultivaluedScalarSlots:
    def test_get_round_trips_multivalued_scalar(self, adapter):
        _put(adapter, "Assay", {"tags": ["x", "y"]}, "A1")
        assert adapter.read("A1").data["tags"] == ["x", "y"]

    def test_scalar_slot_is_not_a_relationship(self, adapter):
        _put(adapter, "Assay", {"tags": ["x", "y"]}, "A1")
        # A non-reference multivalued slot stores inline, NOT as edges.
        assert _relationships_from(adapter, "A1") == []
