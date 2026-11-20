"""PostgreSQL DDL generator using LinkML-backed SchemaRegistry.

Mirrors the SQLite DDL generator with PostgreSQL types and FTS handling based
on tsvector/GIN indexes rather than FTS5 virtual tables.
"""

from __future__ import annotations

from typing import Any, Optional

from mosaic.core.storage.ddl_generator import (
    ColumnDefinition,
    DDLGenerator,
    ForeignKeyDefinition,
    IndexDefinition,
    TableDefinition,
)
from mosaic.linkml_bridge import (
    MOSAIC_INDEX,
    MOSAIC_INDEX_PARTIAL,
    MOSAIC_UNIQUE,
    SchemaRegistry,
    annotation_value,
    slot_default,
)


class PostgresDDLGenerator:
    TYPE_MAPPING = {
        "string": "TEXT",
        "integer": "INTEGER",
        "float": "DOUBLE PRECISION",
        "double": "DOUBLE PRECISION",
        "decimal": "NUMERIC",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "datetime": "TIMESTAMPTZ",
        "time": "TIME",
        "uri": "TEXT",
        "uriorcurie": "TEXT",
        "curie": "TEXT",
        "ncname": "TEXT",
    }

    IS_AVAILABLE_PREDICATE = "is_available = TRUE"

    def __init__(self) -> None:
        self._tables: dict[str, TableDefinition] = {}

    def generate(self, registry: SchemaRegistry) -> list[str]:
        self._tables = {}
        sv = registry.schema_view
        for class_name in registry.class_names():
            cls = sv.get_class(class_name)
            if cls is None or cls.abstract:
                continue
            self._build_table(registry, class_name)
        ordered = self._topological_sort()
        statements: list[str] = []
        for name in ordered:
            table = self._tables[name]
            statements.append(self._render_create_table(table))
            for index in table.indexes:
                statements.append(self._render_create_index(name, index))
        return statements

    def _build_table(
        self, registry: SchemaRegistry, class_name: str
    ) -> TableDefinition:
        if class_name in self._tables:
            return self._tables[class_name]
        sv = registry.schema_view
        cls = sv.get_class(class_name)
        table = TableDefinition(name=class_name)

        id_slot = registry.identifier_slot(class_name)
        id_name = id_slot.name if id_slot is not None else "id"
        table.columns.append(
            ColumnDefinition(
                name=id_name, column_type="TEXT", not_null=True, primary_key=True
            )
        )

        known_classes = set(registry.class_names())
        # Multivalued reference slots (range is an entity class) get no
        # column and no FK — they persist as relationships keyed by the
        # slot name instead (issue #79/#81 / ADR-0002), mirroring
        # ``DDLGenerator._rewrite_multivalued_scalar_slots``'s SQLite
        # treatment of the equivalent case. Virtual ``inverse`` slots
        # (ADR-0011) likewise own no column: they resolve through the
        # forward FK on the *target* class's table.
        mv_ref_slot_names = registry.non_column_slot_names(class_name)
        for slot in registry.induced_slots(class_name):
            if slot.name == id_name:
                continue
            if slot.name in mv_ref_slot_names:
                continue
            rng = slot.range
            # Multivalued *non-reference* slots (scalars/enums) collapse to
            # a single inline TEXT column carrying JSON-encoded values,
            # rather than the per-value column ``col_type`` below would
            # otherwise imply (issue #79/#81 / ADR-0002). A multivalued slot
            # explicitly marked ``inlined``/``inlined_as_list`` gets the same
            # JSON TEXT treatment even when its range is a known class: it
            # was excluded from ``mv_ref_slot_names`` above precisely because
            # its objects embed inline rather than persisting as
            # relationships (issue #121).
            is_inlined = bool(slot.inlined or slot.inlined_as_list)
            is_mv_scalar = bool(slot.multivalued) and (
                rng not in known_classes or is_inlined
            )
            col_type = (
                "TEXT"
                if is_mv_scalar or rng in known_classes or rng is None
                else self.TYPE_MAPPING.get(rng, "TEXT")
            )
            default = slot_default(slot)
            table.columns.append(
                ColumnDefinition(
                    name=slot.name,
                    column_type=col_type,
                    not_null=bool(slot.required),
                    default=default,
                )
            )

            if annotation_value(slot, MOSAIC_UNIQUE):
                table.unique_constraints.append([slot.name])

            if annotation_value(slot, MOSAIC_INDEX):
                partial = bool(annotation_value(slot, MOSAIC_INDEX_PARTIAL))
                table.indexes.append(
                    IndexDefinition(
                        name=f"idx_{class_name}_{slot.name}",
                        columns=[slot.name],
                        partial_where=self.IS_AVAILABLE_PREDICATE if partial else None,
                    )
                )

            # Skip FKs whose target is a polymorphic base (abstract, or a
            # concrete base with concrete subclasses): subtype instances live
            # in their own per-subclass tables so the base table can never
            # satisfy the FK, and an abstract base has no table at all. The
            # reference is stored as the plain TEXT id column above (issue #93).
            # Also skip inlined multivalued slots (issue #121): the column
            # holds JSON-encoded objects, not an id.
            if (
                rng in known_classes
                and not is_inlined
                and not registry.is_polymorphic_base(rng)
            ):
                table.foreign_keys.append(
                    ForeignKeyDefinition(
                        columns=[slot.name],
                        references_table=rng,
                        references_columns=["id"],
                    )
                )

        # is_available: preferred path is declared on Entity in mosaic_core
        # (flows in via induced_slots above). Fallback hardcoded here for
        # schemas that don't `is_a: Entity`. superseded_by stays hardcoded
        # for now; redesigned in Wave 2 provenance-as-linkml-class.
        existing_column_names = {col.name for col in table.columns}
        if "is_available" not in existing_column_names:
            table.columns.append(
                ColumnDefinition(
                    name="is_available",
                    column_type="BOOLEAN",
                    not_null=True,
                    default=True,
                )
            )
        table.columns.append(
            ColumnDefinition(
                name="superseded_by", column_type="TEXT", not_null=False, default=None
            )
        )

        for uk in (cls.unique_keys or {}).values():
            slots = list(uk.unique_key_slots or [])
            if slots:
                table.unique_constraints.append(slots)

        if cls.is_a:
            parent_cls = sv.get_class(cls.is_a)
            if parent_cls is not None and not parent_cls.abstract:
                self._build_table(registry, cls.is_a)
                table.foreign_keys.append(
                    ForeignKeyDefinition(
                        columns=[id_name],
                        references_table=cls.is_a,
                        references_columns=[id_name],
                        on_delete="CASCADE",
                    )
                )

        self._tables[class_name] = table
        return table

    def _topological_sort(self) -> list[str]:
        visited: set[str] = set()
        ordered: list[str] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            table = self._tables.get(name)
            if table is not None:
                for fk in table.foreign_keys:
                    if fk.references_table in self._tables:
                        visit(fk.references_table)
            ordered.append(name)

        for name in self._tables:
            visit(name)
        return ordered

    def _render_create_table(self, table: TableDefinition) -> str:
        pieces: list[str] = []
        pk_cols = [c.name for c in table.columns if c.primary_key]

        for col in table.columns:
            sql = f'"{col.name}" {col.column_type}'
            if col.primary_key and len(pk_cols) == 1:
                sql += " PRIMARY KEY"
            if col.not_null and not col.primary_key:
                sql += " NOT NULL"
            if col.default is not None:
                sql += f" DEFAULT {self._format_default(col.default)}"
            pieces.append(sql)

        for fk in table.foreign_keys:
            cols = ", ".join(f'"{c}"' for c in fk.columns)
            refs = ", ".join(f'"{c}"' for c in fk.references_columns)
            fk_sql = f'FOREIGN KEY ({cols}) REFERENCES "{fk.references_table}"({refs})'
            if fk.on_delete:
                fk_sql += f" ON DELETE {fk.on_delete}"
            pieces.append(fk_sql)

        if len(pk_cols) > 1:
            pk_sql = ", ".join(f'"{c}"' for c in pk_cols)
            pieces.append(f"PRIMARY KEY ({pk_sql})")

        for unique_cols in table.unique_constraints:
            cols = ", ".join(f'"{c}"' for c in unique_cols)
            pieces.append(f"UNIQUE ({cols})")

        return (
            f'CREATE TABLE IF NOT EXISTS "{table.name}" (\n    '
            + ",\n    ".join(pieces)
            + "\n);"
        )

    def _render_create_index(self, table_name: str, index: IndexDefinition) -> str:
        cols = ", ".join(f'"{c}"' for c in index.columns)
        if index.partial_where:
            return (
                f'CREATE INDEX IF NOT EXISTS "{index.name}" ON "{table_name}" '
                f"({cols}) WHERE {index.partial_where};"
            )
        unique = "UNIQUE " if index.unique else ""
        return (
            f'CREATE {unique}INDEX IF NOT EXISTS "{index.name}" '
            f'ON "{table_name}" ({cols});'
        )

    @staticmethod
    def _format_default(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        return f"'{value}'"


class PostgresFTSMigrationPlanner:
    """Plans PostgreSQL FTS tables (tsvector + GIN) from ``mosaic_search`` annotations."""

    def __init__(self) -> None:
        self._fts_tables: dict[str, list[dict[str, str]]] = {}

    def add_class(self, registry: SchemaRegistry, class_name: str) -> None:
        entries: list[dict[str, str]] = []
        for slot, _mode in registry.searchable_slots(class_name):
            entries.append(
                {
                    "table_name": f"fts_{class_name.lower()}_{slot.name.lower()}",
                    "entity_type": class_name,
                    "field_name": slot.name,
                }
            )
        if entries:
            self._fts_tables[class_name] = entries

    def add_registry(self, registry: SchemaRegistry) -> None:
        sv = registry.schema_view
        for class_name in registry.class_names():
            cls = sv.get_class(class_name)
            if cls is None or cls.abstract:
                continue
            self.add_class(registry, class_name)

    def get_fts_tables_for_entity_type(
        self, entity_type: str
    ) -> list[dict[str, str]]:
        return self._fts_tables.get(entity_type, [])

    def get_all_fts_tables(self) -> dict[str, list[dict[str, str]]]:
        return self._fts_tables

    def generate_fts_ddl(self) -> list[str]:
        statements: list[str] = []
        for entries in self._fts_tables.values():
            for meta in entries:
                table_name = meta["table_name"]
                statements.append(
                    f"""CREATE TABLE IF NOT EXISTS "{table_name}" (
    "entity_id" TEXT NOT NULL PRIMARY KEY,
    "content" TEXT NOT NULL DEFAULT '',
    "content_tsvector" TSVECTOR
        GENERATED ALWAYS AS (to_tsvector('english', "content")) STORED
);"""
                )
                statements.append(
                    f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_tsvector" '
                    f'ON "{table_name}" USING GIN ("content_tsvector");'
                )
                statements.append(
                    f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_trigram" '
                    f'ON "{table_name}" USING GIN ("content" gin_trgm_ops);'
                )
        return statements
