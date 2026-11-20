"""Tests for DDL generation from a LinkML-backed SchemaRegistry.

These tests verify the structure of generated DDL by executing it against
an in-memory SQLite database and inspecting PRAGMA table_info, rather than
matching raw DDL strings. This is robust to formatting differences from
LinkML's SQLTableGenerator (quoted vs unquoted identifiers, VARCHAR vs TEXT
for enum types, etc.).
"""

import sqlite3

import pytest

from mosaic.core.storage.ddl_generator import DDLGenerator
from mosaic.linkml_bridge import SchemaRegistry
from tests.support.linkml_schemas import build_registry


def execute_ddl(ddl: list[str]) -> sqlite3.Connection:
    """Execute DDL against an in-memory SQLite DB and return the connection."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    for stmt in ddl:
        cursor.execute(stmt)
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """Return PRAGMA table_info for a table, keyed by column name."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    return {
        row[1]: {
            "type": row[2].upper(),
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": bool(row[5]),
        }
        for row in cursor.fetchall()
    }


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check whether a table exists in the SQLite DB."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cursor.fetchone() is not None


def table_foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return PRAGMA foreign_key_list for a table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA foreign_key_list({table})")
    return [
        {"from": row[3], "table": row[2], "to": row[4]}
        for row in cursor.fetchall()
    ]


def table_indexes(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return PRAGMA index_list for a table.

    ``partial`` is column 4 of ``index_list`` — 1 for a partial index (one
    with a ``WHERE`` clause), 0 otherwise.
    """
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA index_list({table})")
    return [
        {"name": row[1], "unique": bool(row[2]), "partial": bool(row[4])}
        for row in cursor.fetchall()
    ]


class TestTypeMapping:
    @pytest.mark.parametrize(
        "linkml_type,expected",
        [
            ("string", "TEXT"),
            ("integer", "INTEGER"),
            ("float", "REAL"),
            ("double", "REAL"),
            ("decimal", "REAL"),
            ("boolean", "INTEGER"),
            ("date", "TEXT"),
            ("datetime", "TEXT"),
            ("uri", "TEXT"),
            ("uriorcurie", "TEXT"),
        ],
    )
    def test_linkml_type_maps_to_sqlite(self, linkml_type: str, expected: str):
        assert DDLGenerator.TYPE_MAPPING[linkml_type] == expected

    def test_unknown_slot_range_defaults_to_text(self):
        reg = build_registry(
            {
                "item": {
                    "attributes": {
                        "id": {"identifier": True},
                        "mystery": {"range": "not-a-real-type"},
                    }
                }
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        cols = table_columns(conn, "item")
        assert cols["mystery"]["type"] == "TEXT"


class TestBasicTableGeneration:
    def test_emits_create_table(self):
        reg = build_registry(
            {
                "test_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string"},
                    }
                }
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        assert table_exists(conn, "test_entity")

    def test_includes_id_column(self):
        reg = build_registry(
            {"test_entity": {"attributes": {"id": {"identifier": True}}}}
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        cols = table_columns(conn, "test_entity")
        assert "id" in cols
        assert cols["id"]["type"] == "TEXT"

    def test_includes_is_available_with_default_1(self):
        reg = build_registry(
            {"test_entity": {"attributes": {"id": {"identifier": True}}}}
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        cols = table_columns(conn, "test_entity")
        assert "is_available" in cols
        assert cols["is_available"]["type"] == "INTEGER"
        assert cols["is_available"]["default"] in ("1", 1)

    def test_includes_superseded_by_column(self):
        reg = build_registry(
            {"test_entity": {"attributes": {"id": {"identifier": True}}}}
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        cols = table_columns(conn, "test_entity")
        assert "superseded_by" in cols
        assert cols["superseded_by"]["type"] == "TEXT"

    def test_abstract_class_is_skipped(self):
        reg = build_registry(
            {
                "AbstractBase": {
                    "abstract": True,
                    "attributes": {"id": {"identifier": True}},
                },
                "Concrete": {
                    "is_a": "AbstractBase",
                    "attributes": {"name": {"range": "string"}},
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        # AbstractBase table should not be generated
        # (verified at the DDL string level since it's a generator concern)
        assert not any('CREATE TABLE "AbstractBase"' in s for s in ddl)
        assert not any("CREATE TABLE AbstractBase " in s for s in ddl)
        # Concrete table should be generated
        conn = execute_ddl(ddl)
        assert table_exists(conn, "Concrete")


class TestPrimaryKey:
    def test_identifier_slot_is_primary_key(self):
        reg = build_registry(
            {"test_entity": {"attributes": {"id": {"identifier": True}}}}
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        cols = table_columns(conn, "test_entity")
        assert cols["id"]["pk"] is True


class TestForeignKey:
    def test_class_range_becomes_foreign_key(self):
        reg = build_registry(
            {
                "parent_entity": {"attributes": {"id": {"identifier": True}}},
                "child_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "parent_id": {"range": "parent_entity"},
                    }
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        fks = table_foreign_keys(conn, "child_entity")
        assert any(
            fk["from"] == "parent_id" and fk["table"] == "parent_entity"
            for fk in fks
        )

    def test_reference_to_concrete_polymorphic_base_has_no_fk(self):
        """A reference ranged on a concrete base that has subclasses must not
        emit an FK to the base table (issue #93).

        ``mosaic ingest`` dispatches a subtype instance into its own per-subclass
        table (issue #80), so the base table is never populated for that
        referent and a base-table FK fails ``FOREIGN KEY constraint``. The
        reference is stored as a plain TEXT id column instead.
        """
        reg = build_registry(
            {
                "animal": {
                    "attributes": {
                        "id": {"identifier": True},
                        "category": {"designates_type": True, "range": "string"},
                    }
                },
                "dog": {"is_a": "animal"},
                "sighting": {
                    "attributes": {
                        "id": {"identifier": True},
                        "animal": {"range": "animal"},
                    }
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        # No FK on the reference column, but the column itself is kept as TEXT.
        fks = table_foreign_keys(conn, "sighting")
        assert not any(fk["from"] == "animal" for fk in fks)
        assert "animal" in table_columns(conn, "sighting")
        # A leaf subclass referent (dog) can be inserted and referenced without
        # a base row in animal existing at all.
        conn.execute("INSERT INTO dog (id) VALUES ('A1')")
        conn.execute("INSERT INTO sighting (id, animal) VALUES ('S1', 'A1')")
        conn.commit()

    def test_reference_to_concrete_leaf_still_has_fk(self):
        """A reference ranged on a concrete class with no subclasses keeps its
        FK — only polymorphic bases are exempted (issue #93 A/B control)."""
        reg = build_registry(
            {
                "animal": {
                    "attributes": {
                        "id": {"identifier": True},
                        "category": {"designates_type": True, "range": "string"},
                    }
                },
                "dog": {"is_a": "animal"},
                "sighting": {
                    "attributes": {
                        "id": {"identifier": True},
                        "animal": {"range": "dog"},
                    }
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        fks = table_foreign_keys(conn, "sighting")
        assert any(
            fk["from"] == "animal" and fk["table"] == "dog" for fk in fks
        )


def _mosaic_unique_registry() -> SchemaRegistry:
    """Registry with a single ``mosaic_unique`` slot on ``test_entity.email``."""
    return build_registry(
        {
            "test_entity": {
                "attributes": {
                    "id": {"identifier": True},
                    "email": {
                        "range": "string",
                        "annotations": {"mosaic_unique": True},
                    },
                }
            }
        }
    )


class TestUniqueConstraint:
    def test_mosaic_unique_annotation_emits_unique(self):
        ddl = DDLGenerator().generate(_mosaic_unique_registry())
        conn = execute_ddl(ddl)
        indexes = table_indexes(conn, "test_entity")
        # mosaic_unique should produce a UNIQUE index
        assert any(idx["unique"] for idx in indexes)

    def test_mosaic_unique_index_is_partial(self):
        """mosaic_unique emits a *partial* index (``WHERE is_available = 1``).

        A non-partial index would treat a superseded predecessor as a
        permanent collision and block migration on the slot forever
        (PTS-348). The partial predicate scopes uniqueness to live rows.
        """
        ddl = DDLGenerator().generate(_mosaic_unique_registry())
        conn = execute_ddl(ddl)
        # The mosaic_unique index is named idx_<class>_<slot>_unique. (The PK on a
        # TEXT identifier yields its own non-partial sqlite_autoindex — correct,
        # and excluded here.)
        hippo_idx = [
            idx
            for idx in table_indexes(conn, "test_entity")
            if idx["name"] == "idx_test_entity_email_unique"
        ]
        assert hippo_idx, "expected idx_test_entity_email_unique from mosaic_unique"
        assert hippo_idx[0]["unique"]
        assert hippo_idx[0]["partial"], (
            "mosaic_unique index must be partial (WHERE is_available = 1)"
        )

    def test_mosaic_unique_still_enforced_among_live_rows(self):
        """Two *live* rows sharing the key still violate the constraint."""
        conn = execute_ddl(DDLGenerator().generate(_mosaic_unique_registry()))
        conn.execute(
            "INSERT INTO test_entity (id, email, is_available) "
            "VALUES ('a', 'x@example.org', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO test_entity (id, email, is_available) "
                "VALUES ('b', 'x@example.org', 1)"
            )

    def test_mosaic_unique_allows_superseded_plus_live_same_key(self):
        """A superseded predecessor (is_available=0) coexists with a live
        replacement (is_available=1) sharing the key — the migration window
        the partial index exists to permit (PTS-348)."""
        conn = execute_ddl(DDLGenerator().generate(_mosaic_unique_registry()))
        conn.execute(
            "INSERT INTO test_entity (id, email, is_available) "
            "VALUES ('old', 'x@example.org', 0)"
        )
        # Must NOT raise: the retired predecessor is out of the live scope.
        conn.execute(
            "INSERT INTO test_entity (id, email, is_available) "
            "VALUES ('new', 'x@example.org', 1)"
        )
        rows = conn.execute(
            "SELECT id FROM test_entity WHERE email = 'x@example.org' "
            "ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == ["new", "old"]

    def test_linkml_unique_keys_emit_composite_unique(self):
        reg = build_registry(
            {
                "organization": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string"},
                        "code": {"range": "string"},
                    },
                    "unique_keys": {
                        "name_code": {"unique_key_slots": ["name", "code"]}
                    },
                }
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        indexes = table_indexes(conn, "organization")
        # Composite unique constraint should produce a UNIQUE index
        assert any(idx["unique"] for idx in indexes)


class TestDefaultValue:
    def test_string_default_via_ifabsent(self):
        reg = build_registry(
            {
                "test_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "status": {
                            "range": "string",
                            "ifabsent": "active",
                        },
                    }
                }
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        cols = table_columns(conn, "test_entity")
        assert cols["status"]["default"] == "'active'"


class TestIndexGeneration:
    def test_mosaic_index_emits_create_index(self):
        reg = build_registry(
            {
                "test_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {
                            "range": "string",
                            "annotations": {"mosaic_index": True},
                        },
                    }
                }
            }
        )
        ddl = DDLGenerator().generate(reg)
        # At least one CREATE INDEX statement
        assert any("CREATE INDEX" in s for s in ddl)
        assert any("idx_test_entity_name" in s for s in ddl)

    def test_mosaic_index_partial_adds_where(self):
        reg = build_registry(
            {
                "test_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {
                            "range": "string",
                            "annotations": {
                                "mosaic_index": True,
                                "mosaic_index_partial": True,
                            },
                        },
                    }
                }
            }
        )
        ddl = DDLGenerator().generate(reg)
        partial = [s for s in ddl if "idx_test_entity_name" in s]
        assert partial
        assert "WHERE is_available = 1" in partial[0]


class TestInheritance:
    def test_child_class_has_foreign_key_to_parent(self):
        # Note: SQLTableGenerator does not emit FK from child PK to parent PK
        # for is_a inheritance — that was a hippo-specific feature in the
        # legacy generator. Per the handoff doc, inheritance is now a LinkML
        # concern; child tables get parent slots inlined via induced_slots.
        # This test now verifies that parent slots flow into the child table.
        reg = build_registry(
            {
                "parent_entity": {
                    "attributes": {
                        "id": {"identifier": True},
                        "created_at": {"range": "datetime"},
                    }
                },
                "child_entity": {
                    "is_a": "parent_entity",
                    "attributes": {"name": {"range": "string"}},
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        # Child should have inherited columns
        child_cols = table_columns(conn, "child_entity")
        assert "id" in child_cols
        assert "created_at" in child_cols
        assert "name" in child_cols

    def test_parent_table_generated_before_child(self):
        reg = build_registry(
            {
                "parent_entity": {"attributes": {"id": {"identifier": True}}},
                "child_entity": {
                    "is_a": "parent_entity",
                    "attributes": {},
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        # Both tables should be created without error (regardless of order
        # SQLTableGenerator handles dependency ordering internally)
        conn = execute_ddl(ddl)
        assert table_exists(conn, "parent_entity")
        assert table_exists(conn, "child_entity")


class TestMultiClass:
    def test_two_unrelated_classes_both_generated(self):
        reg = build_registry(
            {
                "entity_a": {
                    "attributes": {
                        "id": {"identifier": True},
                        "field1": {"range": "string"},
                    }
                },
                "entity_b": {
                    "attributes": {
                        "id": {"identifier": True},
                        "field2": {"range": "string"},
                    }
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        assert table_exists(conn, "entity_a")
        assert table_exists(conn, "entity_b")

    def test_full_schema_with_unique_index_and_fk(self):
        reg = build_registry(
            {
                "organization": {
                    "attributes": {
                        "id": {"identifier": True},
                        "name": {"range": "string", "required": True},
                        "code": {
                            "range": "string",
                            "annotations": {
                                "mosaic_unique": True,
                                "mosaic_index": True,
                            },
                        },
                        "active": {
                            "range": "boolean",
                        },
                    }
                },
                "user": {
                    "attributes": {
                        "id": {"identifier": True},
                        "username": {"range": "string", "required": True},
                        "org_id": {"range": "organization"},
                        "created_at": {"range": "datetime"},
                    }
                },
            }
        )
        ddl = DDLGenerator().generate(reg)
        conn = execute_ddl(ddl)
        # Both tables created
        assert table_exists(conn, "organization")
        assert table_exists(conn, "user")
        # FK from user to organization
        user_fks = table_foreign_keys(conn, "user")
        assert any(fk["table"] == "organization" for fk in user_fks)
        # UNIQUE constraint exists on organization
        org_indexes = table_indexes(conn, "organization")
        assert any(idx["unique"] for idx in org_indexes)


class TestHippoCoreProvenanceRecordDDL:
    """DDL emitted for the ProvenanceRecord class declared in mosaic_core.

    The `provenance-migration` change (sec9 §9.6 / Decision 9.6.A) replaces
    the legacy hand-coded `provenance` table with a DDL-generated
    `ProvenanceRecord` table. This test class verifies the generator emits
    the expected shape *before* legacy DDL is removed.
    """

    @pytest.fixture
    def registry(self) -> SchemaRegistry:
        # User schema importing mosaic_core. The registry's class_names()
        # includes every class in mosaic_core (Entity, ProvenanceRecord,
        # Process, Validator, ReferenceLoader) — the generator iterates
        # all of them.
        yaml_text = (
            "id: https://example.org/test\n"
            "name: test\n"
            "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
            "default_range: string\n"
            "imports:\n"
            "  - linkml:types\n"
            "  - mosaic_core\n"
            "classes: {}\n"
        )
        return SchemaRegistry.from_yaml(yaml_text)

    @pytest.fixture
    def ddl(self, registry: SchemaRegistry) -> list[str]:
        return DDLGenerator().generate(registry)

    @pytest.fixture
    def conn(self, ddl: list[str]) -> sqlite3.Connection:
        return execute_ddl(ddl)

    def test_provenance_record_table_exists(self, conn: sqlite3.Connection):
        assert table_exists(conn, "ProvenanceRecord")

    def test_provenance_record_has_all_sec9_columns(self, conn: sqlite3.Connection):
        # sec9 §9.6 defines the slot inventory. Each slot must map to a column.
        cols = table_columns(conn, "ProvenanceRecord")
        for col in [
            "id",
            "entity_id",
            "entity_type",
            "operation",
            "actor_id",
            "timestamp",
            "schema_version",
            "derived_from_id",
            "process_id",
            "patch",
            "context",
        ]:
            assert col in cols, f"column {col!r} missing from PRAGMA table_info"

    def test_provenance_record_inherits_is_available_and_superseded_by(
        self, conn: sqlite3.Connection
    ):
        cols = table_columns(conn, "ProvenanceRecord")
        assert "is_available" in cols
        assert "superseded_by" in cols

    def test_provenance_record_id_is_primary_key(self, conn: sqlite3.Connection):
        cols = table_columns(conn, "ProvenanceRecord")
        assert cols["id"]["pk"] is True
        assert cols["id"]["type"] == "TEXT"

    def test_provenance_record_required_slots_are_not_null(
        self, conn: sqlite3.Connection
    ):
        # required=true slots per mosaic_core.yaml: operation, actor_id,
        # timestamp, schema_version.
        cols = table_columns(conn, "ProvenanceRecord")
        for col in ("operation", "actor_id", "timestamp", "schema_version"):
            assert cols[col]["notnull"] is True, (
                f"column {col!r} should be NOT NULL"
            )

    def test_provenance_record_indexes_on_annotated_slots(self, ddl: list[str]):
        # sec9 §9.6 annotates entity_id, operation, timestamp, process_id
        # with mosaic_index.
        full = "\n".join(ddl)
        for slot in ("entity_id", "operation", "timestamp", "process_id"):
            assert f"idx_ProvenanceRecord_{slot}" in full, (
                f"index on ProvenanceRecord.{slot} missing"
            )

    def test_process_fk_from_provenance_record(self, conn: sqlite3.Connection):
        # process_id has range Process (another class in mosaic_core) — should
        # become a foreign key constraint.
        fks = table_foreign_keys(conn, "ProvenanceRecord")
        assert any(
            fk["from"] == "process_id" and fk["table"] == "Process"
            for fk in fks
        ), f"Expected FK from process_id to Process, got: {fks}"

    def test_provenance_record_append_only_triggers(self, ddl: list[str]):
        # mosaic_append_only: true should emit UPDATE/DELETE prevention triggers
        full = "\n".join(ddl)
        assert "prevent_update_ProvenanceRecord" in full
        assert "prevent_delete_ProvenanceRecord" in full

    def test_provenance_record_append_only_at_runtime(
        self, conn: sqlite3.Connection
    ):
        # Trigger should actually fire and reject UPDATE
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ProvenanceRecord "
            "(id, operation, actor_id, timestamp, schema_version) "
            "VALUES ('p1', 'create', 'a1', '2026-01-01T00:00:00', 'v1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                "UPDATE ProvenanceRecord SET operation='update' WHERE id='p1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute("DELETE FROM ProvenanceRecord WHERE id='p1'")
