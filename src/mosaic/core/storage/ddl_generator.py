"""DDL generation from a LinkML-backed SchemaRegistry.

Delegates base DDL generation to LinkML's ``SQLTableGenerator``, then
post-processes for Mosaic-specific extras: ``superseded_by`` column,
partial indexes (``mosaic_index_partial``), FTS5 virtual tables
(``mosaic_search: fts5``), and append-only triggers (``mosaic_append_only``).
"""

from __future__ import annotations

import re
import tempfile
import yaml
from pathlib import Path
from typing import Any

from linkml.generators.sqltablegen import SQLTableGenerator

from mosaic.core.storage.fts import FTSFieldMetadata, FTSTableMetadata
from mosaic.core.storage.xref import XREF_TABLE_DDL
from mosaic.linkml_bridge import (
    MOSAIC_APPEND_ONLY,
    MOSAIC_INDEX,
    MOSAIC_INDEX_PARTIAL,
    MOSAIC_UNIQUE,
    SchemaRegistry,
    annotation_value,
    slot_default,
    _flatten_for_validator,
)


class DDLGenerator:
    """Generate SQLite DDL from a ``SchemaRegistry``."""

    TYPE_MAPPING = {
        "string": "TEXT",
        "integer": "INTEGER",
        "float": "REAL",
        "double": "REAL",
        "decimal": "REAL",
        "boolean": "INTEGER",
        "date": "TEXT",
        "datetime": "TEXT",
        "time": "TEXT",
        "uri": "TEXT",
        "uriorcurie": "TEXT",
        "curie": "TEXT",
        "ncname": "TEXT",
        "jsonpointer": "TEXT",
        "jsonpath": "TEXT",
        "sparqlpath": "TEXT",
    }

    IS_AVAILABLE_PREDICATE = "is_available = 1"

    def __init__(self) -> None:
        pass

    def generate(self, registry: SchemaRegistry) -> list[str]:
        """Generate SQLite DDL via LinkML SQLTableGenerator + Mosaic post-processing."""
        sv = registry.schema_view

        # Step 1: Flatten schema to self-contained dict (resolves imports inline)
        flat_schema = _flatten_for_validator(sv)

        # Step 1b: Value types (ExternalReference and any identifier-less
        # value object — issue #90) store INLINE as JSON TEXT on the
        # declaring entity's table — they get no table of their own and no
        # FK column. Rewrite the flat schema so SQLTableGenerator emits a
        # plain TEXT column for every slot ranged against a value type
        # (single- AND multivalued; without this, single-valued slots become
        # `<slot>_id` FK columns and multivalued slots become dropped
        # linktables — the data is then silently lost on ingest).
        value_types = registry.value_type_classes()
        # A value type that a *retained* (non-value-type) class inherits from
        # must stay in the temp schema so SQLTableGenerator can resolve its
        # ``is_a`` (its abstract table is filtered out later by
        # ``concrete_classes``). Only value types with no retained descendant
        # are safe to drop. In the common case (ExternalReference, or a
        # Quantity→Mass value-object hierarchy that is value types all the way
        # down) every value type is poppable.
        poppable = self._poppable_value_types(registry, value_types)
        self._rewrite_value_type_slots(flat_schema, value_types, poppable)

        # Step 1c: Multivalued slots whose range is NOT an entity class
        # (scalars, enums, unresolved ranges) store INLINE as a single JSON
        # TEXT column rather than a dropped linktable (issue #79 / ADR-0002).
        # Multivalued *reference* slots (range is an entity class) are left
        # multivalued here so SQLTableGenerator still emits their linktable,
        # which is filtered below; their values persist as relationships.
        self._rewrite_multivalued_scalar_slots(flat_schema)

        # Step 2: Write to temp file for SQLTableGenerator
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tmp:
            yaml.safe_dump(flat_schema, tmp, sort_keys=False)
            tmp_path = tmp.name

        try:
            # Step 3: Generate base DDL with LinkML SQLTableGenerator
            gen = SQLTableGenerator(
                schema=tmp_path,
                generate_abstract_class_ddl=True,
                autogenerate_index=False,
            )
            raw_ddl = gen.generate_ddl()
        finally:
            Path(tmp_path).unlink()

        # Step 4: Parse DDL string into statements
        base_statements = self._parse_ddl_string(raw_ddl)

        # Step 5: Filter and post-process CREATE TABLE statements.
        # Value types are excluded: they are concrete LinkML classes but
        # have no entity table (stored inline as JSON TEXT).
        concrete_classes = {
            name for name in registry.class_names()
            if name not in value_types
            and (not (cls := sv.get_class(name)) or not cls.abstract)
        }

        table_statements = []
        index_statements = []

        for stmt in base_statements:
            if "CREATE TABLE" in stmt:
                table_name = self._extract_table_name(stmt)
                # Filter: keep only concrete classes (drop linktables, abstract)
                if table_name not in concrete_classes:
                    continue
                # Post-process this CREATE TABLE
                stmt = self._post_process_create_table(stmt, registry, table_name)
                table_statements.append(stmt)
            elif "CREATE INDEX" in stmt or "CREATE UNIQUE INDEX" in stmt:
                # Indexes from SQLTableGenerator (we disabled autogenerate_index)
                index_statements.append(stmt)

        # Step 6: Add Mosaic-specific indexes, unique constraints, triggers
        mosaic_extras = self._generate_mosaic_extras(registry, concrete_classes)

        # Step 7: Add FTS5 virtual tables
        fts_planner = FTSMigrationPlanner()
        fts_planner.add_registry(registry)
        fts_statements = fts_planner.generate_fts_ddl()

        # Step 8: mosaic_external_xref side-index table (issue #48) —
        # emitted once when any concrete class carries an annotated slot.
        # ``external_xref_slots`` also validates annotation placement
        # (range must be ExternalReference), failing DDL generation —
        # i.e. adapter startup — on a misdeclared schema.
        xref_statements: list[str] = []
        if any(
            registry.external_xref_slots(name) for name in sorted(concrete_classes)
        ):
            xref_statements = list(XREF_TABLE_DDL)

        return (
            table_statements
            + mosaic_extras
            + index_statements
            + fts_statements
            + xref_statements
        )

    @staticmethod
    def _poppable_value_types(
        registry: SchemaRegistry, value_types: frozenset[str]
    ) -> frozenset[str]:
        """Value types safe to remove from the DDL temp schema.

        A value type kept as an ``is_a`` ancestor of a retained entity class
        cannot be popped — SQLTableGenerator resolves the parent while
        inducing the child's slots and raises if it is missing. Returns the
        value types that no retained (non-value-type) class descends from.
        """
        sv = registry.schema_view
        keep: set[str] = set()
        for name in registry.class_names():
            if name in value_types:
                continue
            for anc in sv.class_ancestors(name):
                if anc in value_types:
                    keep.add(anc)
        return value_types - keep

    @staticmethod
    def _rewrite_value_type_slots(
        flat_schema: dict[str, Any],
        value_types: frozenset[str],
        poppable: frozenset[str],
    ) -> None:
        """Mutate a flattened schema dict so value-type-ranged slots emit
        plain TEXT columns.

        For every slot (class ``attributes``/``slot_usage`` and top-level
        ``slots``) whose range is a value type (``ExternalReference`` or any
        identifier-less value object — issue #90): force ``range: string``
        and ``multivalued: false`` and drop the inlining flags — the storage
        representation is one JSON TEXT column either way
        (``_coerce_for_column`` JSON-encodes dict/list values;
        ``_decode_column_value`` reverses on read). The value-type classes
        themselves are removed so no table is generated for them. This
        rewrite is local to the DDL temp schema; validation and typed
        surfaces keep seeing the real value-type range.

        ``value_types`` is the schema-driven set from
        :meth:`SchemaRegistry.value_type_classes`; ranges against any of them
        are rewritten to TEXT. ``poppable`` (a subset) names the value-type
        classes actually removed from the schema — see
        :meth:`_poppable_value_types` for why a value type used as an entity's
        ``is_a`` ancestor must be retained.
        """

        def _rewrite(slot_spec: Any) -> None:
            if (
                isinstance(slot_spec, dict)
                and slot_spec.get("range") in value_types
            ):
                slot_spec["range"] = "string"
                slot_spec["multivalued"] = False
                slot_spec.pop("inlined", None)
                slot_spec.pop("inlined_as_list", None)

        for cls_spec in (flat_schema.get("classes") or {}).values():
            if not isinstance(cls_spec, dict):
                continue
            for section in ("attributes", "slot_usage"):
                for slot_spec in (cls_spec.get(section) or {}).values():
                    _rewrite(slot_spec)
        for slot_spec in (flat_schema.get("slots") or {}).values():
            _rewrite(slot_spec)
        for name in poppable:
            (flat_schema.get("classes") or {}).pop(name, None)

    @staticmethod
    def _rewrite_multivalued_scalar_slots(flat_schema: dict[str, Any]) -> None:
        """Collapse multivalued non-reference slots to a single JSON TEXT column.

        Runs after :meth:`_rewrite_value_type_slots` (value-type classes are
        already gone). For every multivalued slot whose range is **not** a
        remaining class — i.e. a scalar, an enum, or an unresolved range —
        force ``multivalued: false`` and drop the inlining flags so
        SQLTableGenerator emits one plain column instead of a linktable. The
        list round-trips through that TEXT column via ``_coerce_for_column``
        (JSON-encodes lists) / ``_decode_column_value`` (JSON-decodes on read).

        A slot explicitly marked ``inlined``/``inlined_as_list`` is rewritten
        the same way even when its range *is* a remaining class: the schema
        author has said its objects embed inline, so they belong in a JSON
        TEXT column on the declaring table, not a linktable/relationship —
        otherwise a range class that also declares an identifier (so it isn't
        a value type and keeps its own table) gets no column at all on the
        declaring side and the values are dropped (issue #121).

        Multivalued slots whose range *is* a remaining class and are **not**
        marked inline are left untouched so their linktable is still
        generated (and then filtered in ``generate``); those edges persist
        as relationships (ADR-0002).
        """
        class_names = set((flat_schema.get("classes") or {}).keys())

        def _rewrite(slot_spec: Any) -> None:
            if not isinstance(slot_spec, dict) or not slot_spec.get("multivalued"):
                return
            rng = slot_spec.get("range")
            is_inlined = bool(
                slot_spec.get("inlined") or slot_spec.get("inlined_as_list")
            )
            if rng not in class_names or is_inlined:
                if is_inlined:
                    # rng is still a real class here (the branch above
                    # already handles non-class ranges) — force it to a
                    # scalar range too, or SQLTableGenerator treats the now
                    # single-valued slot as a `<slot>_id` FK column instead
                    # of a plain JSON TEXT column (issue #121).
                    slot_spec["range"] = "string"
                slot_spec["multivalued"] = False
                slot_spec.pop("inlined", None)
                slot_spec.pop("inlined_as_list", None)

        for cls_spec in (flat_schema.get("classes") or {}).values():
            if not isinstance(cls_spec, dict):
                continue
            for section in ("attributes", "slot_usage"):
                for slot_spec in (cls_spec.get(section) or {}).values():
                    _rewrite(slot_spec)
        for slot_spec in (flat_schema.get("slots") or {}).values():
            _rewrite(slot_spec)

    def _parse_ddl_string(self, raw_ddl: str) -> list[str]:
        """Parse semicolon-delimited DDL string into statement list.

        LinkML SQLTableGenerator emits all tables in one string with comments.
        We split on CREATE TABLE markers to isolate each table statement.
        """
        statements = []
        # Split on CREATE TABLE to get individual table blocks
        parts = raw_ddl.split("\nCREATE TABLE ")
        for i, part in enumerate(parts):
            if i == 0:
                # First part is just comments before any tables
                continue
            # Reconstruct the CREATE TABLE statement
            stmt = "CREATE TABLE " + part
            # Find the end of this statement (");")
            end_idx = stmt.find(");")
            if end_idx != -1:
                stmt = stmt[:end_idx + 2]  # Include the ");
            statements.append(stmt)
        return statements

    def _extract_table_name(self, create_table_stmt: str) -> str:
        """Extract table name from 'CREATE TABLE name (...);' or 'CREATE TABLE "name" (...);'."""
        match = re.search(r'CREATE TABLE (?:"([^"]+)"|(\w+))', create_table_stmt, re.IGNORECASE)
        if not match:
            return ""
        return match.group(1) or match.group(2)

    def _post_process_create_table(
        self, stmt: str, registry: SchemaRegistry, table_name: str
    ) -> str:
        """Post-process CREATE TABLE: fix BOOLEAN→INTEGER, inject DEFAULTs, add superseded_by."""
        sv = registry.schema_view

        # Strip FKs whose target is a *polymorphic base* — a class whose
        # instances do not all live in the one table the FK points at:
        #   * an abstract base has no SQL table (filtered out earlier in
        #     ``generate``), so the FK references a non-existent table; and
        #   * a concrete base with concrete subclasses (issue #93) has its
        #     subtype instances dispatched into their own per-subclass tables
        #     (``mosaic ingest`` routes them via ``designates_type`` — issue
        #     #80), so the base table is never populated for those referents
        #     and the FK fails ``FOREIGN KEY constraint`` for any subtype.
        # In both cases the reference persists as a plain TEXT id column and is
        # resolved across the subtype tables at read time. PR 2.4 will
        # reintroduce these as FKs against the ``_entity_registry`` shadow table.
        for slot in registry.induced_slots(table_name):
            if not slot.range:
                continue
            target_cls = sv.get_class(slot.range)
            if target_cls is None or not registry.is_polymorphic_base(slot.range):
                continue
            stmt = re.sub(
                rf',\s*\n?\s*FOREIGN\s+KEY\s*\(\s*"?{re.escape(slot.name)}"?\s*\)'
                rf'\s+REFERENCES\s+"?{re.escape(slot.range)}"?\s*\([^)]*\)',
                "",
                stmt,
                flags=re.IGNORECASE,
            )

        # Fix BOOLEAN → INTEGER for is_available (handles both quoted and unquoted)
        stmt = re.sub(
            r'\bis_available\s+BOOLEAN\b',
            'is_available INTEGER',
            stmt,
            flags=re.IGNORECASE,
        )

        # Inject DEFAULT 1 for is_available if not present (handles both quoted and unquoted)
        if 'is_available' in stmt:
            is_avail_match = re.search(
                r'\bis_available\s+INTEGER(?:\s+NOT\s+NULL)?',
                stmt,
                re.IGNORECASE
            )
            if is_avail_match and "DEFAULT" not in is_avail_match.group():
                stmt = re.sub(
                    r'(\bis_available\s+INTEGER(?:\s+NOT\s+NULL)?)',
                    r"\1 DEFAULT 1",
                    stmt,
                    flags=re.IGNORECASE,
                )

        # Inject ifabsent DEFAULTs for other slots (SQLTableGenerator doesn't emit them)
        # Handle both quoted and unquoted column names
        for slot in registry.induced_slots(table_name):
            default = slot_default(slot)
            if default is not None and slot.name != "is_available":
                # Match "name TYPE" or "\tname TYPE" - look for the column line
                # before next comma or newline
                pattern = rf'(\b{re.escape(slot.name)}\s+\w+(?:\([^)]+\))?(?:\s+NOT\s+NULL)?)'
                match = re.search(pattern, stmt, re.IGNORECASE)
                if match and "DEFAULT" not in match.group():
                    col_def = match.group()
                    new_col_def = col_def + f" DEFAULT {self._format_default(default)}"
                    stmt = stmt.replace(col_def, new_col_def, 1)

        # Fallback: add is_available if missing (for schemas without is_a: Entity)
        if not re.search(r'\bis_available\b', stmt):
            constraint_pattern = r',\n\t((?:PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK))'
            if re.search(constraint_pattern, stmt):
                stmt = re.sub(
                    constraint_pattern,
                    r',\n\t"is_available" INTEGER NOT NULL DEFAULT 1,\n\t\1',
                    stmt,
                    count=1,
                )

        # Inject superseded_by column before table constraints (PRIMARY KEY, FOREIGN KEY, etc.)
        # In SQLite, columns must come before constraints
        if 'superseded_by' not in stmt:
            # Find first constraint line (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK)
            # and inject superseded_by before it
            constraint_pattern = r',\n\t((?:PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK))'
            if re.search(constraint_pattern, stmt):
                stmt = re.sub(
                    constraint_pattern,
                    r',\n\t"superseded_by" TEXT,\n\t\1',
                    stmt,
                    count=1,
                )
            else:
                # No constraints, inject before closing paren
                stmt = re.sub(
                    r'\n\);$',
                    r',\n\t"superseded_by" TEXT\n);',
                    stmt,
                    flags=re.MULTILINE,
                )

        return stmt

    def _generate_mosaic_extras(
        self, registry: SchemaRegistry, concrete_classes: set[str]
    ) -> list[str]:
        """Generate Mosaic-specific indexes, unique constraints, triggers."""
        sv = registry.schema_view
        statements = []

        for class_name in concrete_classes:
            cls = sv.get_class(class_name)
            if cls is None:
                continue

            # mosaic_index / mosaic_index_partial annotations
            for slot in registry.induced_slots(class_name):
                if annotation_value(slot, MOSAIC_INDEX):
                    partial = bool(annotation_value(slot, MOSAIC_INDEX_PARTIAL))
                    idx_name = f"idx_{class_name}_{slot.name}"
                    idx_sql = f'CREATE INDEX "{idx_name}" ON "{class_name}" ("{slot.name}")'
                    if partial:
                        idx_sql += f" WHERE {self.IS_AVAILABLE_PREDICATE}"
                    idx_sql += ";"
                    statements.append(idx_sql)

                # mosaic_unique: emit a *partial* CREATE UNIQUE INDEX scoped to
                # live rows (``WHERE is_available = 1``). mosaic_unique means
                # "unique among live records", not across every historical
                # revision: a superseded predecessor (is_available = 0) keeps
                # its business key, and a migration replacement re-uses that key
                # on a fresh row. A non-partial index would treat the retired
                # predecessor as a permanent collision and block migration on
                # any mosaic_unique slot forever (PTS-348). The live-only
                # predicate preserves the constraint among available rows while
                # leaving superseded revisions out of scope.
                if annotation_value(slot, MOSAIC_UNIQUE):
                    idx_name = f"idx_{class_name}_{slot.name}_unique"
                    statements.append(
                        f'CREATE UNIQUE INDEX "{idx_name}" ON "{class_name}" '
                        f'("{slot.name}") WHERE {self.IS_AVAILABLE_PREDICATE};'
                    )

            # mosaic_append_only: emit trigger rejecting UPDATE/DELETE
            if annotation_value(cls, MOSAIC_APPEND_ONLY):
                statements.extend(self._generate_append_only_triggers(class_name))

        return statements

    def _generate_append_only_triggers(self, table_name: str) -> list[str]:
        """Generate triggers that reject UPDATE and DELETE on append-only tables."""
        return [
            f"""CREATE TRIGGER "prevent_update_{table_name}"
BEFORE UPDATE ON "{table_name}"
BEGIN
    SELECT RAISE(ABORT, 'UPDATE not allowed on append-only table {table_name}');
END;""",
            f"""CREATE TRIGGER "prevent_delete_{table_name}"
BEFORE DELETE ON "{table_name}"
BEGIN
    SELECT RAISE(ABORT, 'DELETE not allowed on append-only table {table_name}');
END;""",
        ]

    @staticmethod
    def _format_default(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        return f"'{value}'"


class FTSMigrationPlanner:
    """Plans FTS5 virtual tables from slot ``mosaic_search`` annotations."""

    def __init__(self) -> None:
        self._fts_tables: dict[str, list[FTSTableMetadata]] = {}

    def add_class(self, registry: SchemaRegistry, class_name: str) -> None:
        slots = registry.searchable_slots(class_name)
        if not slots:
            return
        tables = []
        for slot, mode in slots:
            tables.append(
                FTSTableMetadata(
                    table_name=FTSTableMetadata.generate_table_name(
                        class_name, slot.name
                    ),
                    source_entity_type=class_name,
                    fts_version=mode,
                    # Post-PR-2.3 the legacy ``entities`` table is gone;
                    # FTS5 contentless tables manage their own storage,
                    # so emit them without a ``content=`` clause. The
                    # adapter maintains FTS rows directly on entity
                    # writes (see ``IngestionService._sync_entity_to_fts``).
                    content_table="",
                    content_rowid="rowid",
                    fields=[
                        FTSFieldMetadata(
                            field_name=slot.name,
                            field_type=slot.range or "string",
                            search_type=mode,
                            source_entity_type=class_name,
                        )
                    ],
                )
            )
        self._fts_tables[class_name] = tables

    def add_registry(self, registry: SchemaRegistry) -> None:
        sv = registry.schema_view
        for class_name in registry.class_names():
            cls = sv.get_class(class_name)
            if cls is None or cls.abstract:
                continue
            self.add_class(registry, class_name)

    def get_fts_tables_for_entity_type(
        self, entity_type: str
    ) -> list[FTSTableMetadata]:
        return self._fts_tables.get(entity_type, [])

    def get_all_fts_tables(self) -> dict[str, list[FTSTableMetadata]]:
        return self._fts_tables

    def generate_fts_ddl(self) -> list[str]:
        from mosaic.core.storage.fts import generate_fts_create_sql

        statements: list[str] = []
        for tables in self._fts_tables.values():
            for meta in tables:
                # Post-PR-2.3 every FTS table is a regular FTS5 table
                # (no external ``content=`` clause) with the standard
                # ``entity_id, content`` column shape — the same shape
                # ``IngestionService._sync_entity_to_fts`` writes into.
                # The field-specific table name (e.g. ``fts_sample_notes``)
                # already encodes which slot the index covers; per-field
                # columns inside the table are redundant.
                statements.append(
                    generate_fts_create_sql(
                        table_name=meta.table_name,
                        columns=["entity_id", "content"],
                        content_table=None,
                        content_rowid=meta.content_rowid,
                    )
                )
        return statements
