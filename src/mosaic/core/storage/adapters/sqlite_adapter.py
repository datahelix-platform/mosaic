"""SQLite storage adapter with WAL mode support.

The SQLite adapter provides:
- WAL mode for improved concurrency
- Automatic trigger creation for ProvenanceRecord immutability
- Thread-safe connection management

## ProvenanceRecord Immutability Triggers

Enforces ``hippo_append_only: true`` on the ``ProvenanceRecord`` table
(sec9 §9.6 / Decision 9.6.C) at the SQL level:

- ``prevent_provenance_update``: rejects any UPDATE on ProvenanceRecord
- ``prevent_provenance_delete``: rejects any DELETE on ProvenanceRecord

Triggers use ``CREATE TRIGGER IF NOT EXISTS`` for idempotent initialization.
"""

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Generator, Iterator, List, Optional

if TYPE_CHECKING:
    from mosaic.linkml_bridge import InverseSlot, SchemaRegistry

from mosaic.core.storage import (
    COMPARISON_SQL_OPS,
    EntityStore,
    Query,
    ScoredMatch,
    escape_like,
    has_relationship_predicate,
    matches_operator,
    matches_tree,
    normalize_filter,
    normalize_where,
)
from mosaic.core.storage.adapters import sqlite_triggers
from mosaic.core.storage.fts import (
    FTSTableMetadata,
    fts_table_exists,
    generate_fts_column_definitions,
    generate_fts_create_sql,
    generate_fts_delete_sql,
    generate_fts_insert_sql,
    generate_fts_phrase_query,
    generate_fts_update_sql,
    get_fts_tables_for_entity_type,
    normalize_bm25_score,
)
from mosaic.core.storage.xref import XREF_TABLE, extract_xref_pairs
from mosaic.core.types import ProvenanceRecord as ProvenanceRecordType, TemporalRecord
from mosaic.core.exceptions import (
    DanglingReferenceError,
    EntityTypeConflictError,
    SearchCapabilityError,
    XrefUniquenessError,
)


class SQLiteEntity:
    """Entity stored in SQLite."""

    def __init__(
        self,
        id: str,
        entity_type: str,
        is_available: bool,
        version: int,
        data: dict[str, Any],
        superseded_by: Optional[str] = None,
    ):
        self.id = id
        self.entity_type = entity_type
        self.is_available = is_available
        self.version = version
        self.data = data
        self.superseded_by = superseded_by


# Legacy operation-string → Operation enum mapping (Decision 9.6.B).
# Resolved per-site via reading the surrounding code at write time;
# preserved here as a compatibility shim so callers not yet migrated to
# pass Operation enum values continue to work during the transition.
_LEGACY_OPERATION_MAP: dict[str, str] = {
    "CREATE": "create",
    "UPDATE": "update",
    "EntityUpdated": "update",
    "REPLACED": "update",
    "EntitySuperseded": "supersede",
    "SUPERSEDE": "supersede",
    "AvailabilityChanged": "availability_change",
    "AVAILABILITY_CHANGE": "availability_change",
    "SOFT_DELETE": "availability_change",
    "RELATE": "relationship_add",
    "UNRELATE": "relationship_remove",
}


def _normalize_operation(op: Any) -> str:
    """Accept an Operation enum value or legacy string; return the enum value.

    Used by ``ProvenanceStore.record()`` and its Postgres sibling to
    absorb legacy operation strings (``"CREATE"``, ``"SOFT_DELETE"``, etc.)
    during the migration to sec9 §9.6's ``Operation`` enum. See
    Decision 9.6.B for the per-site mapping rationale.
    """
    if hasattr(op, "value"):
        return op.value
    s = str(op)
    return _LEGACY_OPERATION_MAP.get(s, s)


class ProvenanceStore:
    """Store for ``ProvenanceRecord`` rows (sec9 §9.6).

    Writes the table generated from the ``hippo_core.ProvenanceRecord``
    LinkML declaration; the DDL is emitted by the shared DDL generator.
    The adapter's SQL triggers enforce append-only semantics at the DB
    level (Decision 9.6.C).
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        schema_version: Optional[str] = None,
    ):
        self._conn = connection
        self._schema_version = schema_version or ""

    def record(
        self,
        entity_id: Optional[str],
        entity_type: Optional[str],
        # Preferred (sec9 §9.6) parameters
        operation: Any = None,
        actor_id: Optional[str] = None,
        patch: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
        derived_from_id: Optional[str] = None,
        process_id: Optional[str] = None,
        schema_version: Optional[str] = None,
        # Legacy parameters — accepted during the transition and mapped
        # to the sec9 shape (Decision 9.6.B).
        operation_type: Any = None,
        user_context: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        # Legacy fields that no longer exist in sec9 — accepted and
        # discarded so old callers don't raise TypeError.
        operation_id: Optional[str] = None,  # noqa: ARG002
        previous_state_hash: Optional[str] = None,  # noqa: ARG002
        state_snapshot: Optional[dict[str, Any]] = None,  # noqa: ARG002
    ) -> ProvenanceRecordType:
        """Record a provenance event. Accepts either sec9 kwargs or legacy kwargs."""
        import uuid
        from mosaic.core.types import ProvenanceRecord as _ProvRec

        op_source = operation if operation is not None else operation_type
        if op_source is None:
            raise ValueError("operation (or legacy operation_type) is required")
        op_value = _normalize_operation(op_source)

        # actor_id resolution order (Decision 9.6.G):
        # 1. explicit kwarg passed by the caller
        # 2. legacy user_context shim (Decision 9.6.B)
        # 3. ContextVar set by middleware / with_actor() (sec9 §9.6.G)
        # 4. "unknown" sentinel — satisfies NOT NULL; signals unmigrated path
        effective_actor = actor_id if actor_id is not None else user_context
        if effective_actor is None:
            from mosaic.core.context import get_current_actor
            effective_actor = get_current_actor()
        if effective_actor is None:
            effective_actor = "unknown"
        effective_patch = patch if patch is not None else payload

        now = datetime.now(timezone.utc)
        record_id = str(uuid.uuid4())
        sv = schema_version if schema_version is not None else self._schema_version

        cursor = self._conn.cursor()
        cursor.execute(
            """INSERT INTO "ProvenanceRecord"
               (id, entity_id, entity_type, operation, actor_id, timestamp,
                schema_version, derived_from_id, process_id, patch, context,
                is_available)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                record_id,
                entity_id,
                entity_type,
                op_value,
                effective_actor,
                now.isoformat(),
                sv,
                derived_from_id,
                process_id,
                json.dumps(effective_patch) if effective_patch is not None else None,
                json.dumps(context) if context is not None else None,
            ),
        )

        return _ProvRec(
            id=record_id,
            entity_id=entity_id,
            entity_type=entity_type,
            operation=op_value,
            actor_id=effective_actor,
            timestamp=now,
            schema_version=sv,
            derived_from_id=derived_from_id,
            process_id=process_id,
            patch=effective_patch,
            context=context,
        )

    def find_by_entity(
        self,
        entity_id: str,
        operation: Any = None,
        operation_type: Any = None,  # legacy alias
    ) -> Iterator[ProvenanceRecordType]:
        """Find provenance records for an entity."""
        from mosaic.core.types import ProvenanceRecord as _ProvRec

        op_filter = operation if operation is not None else operation_type
        cursor = self._conn.cursor()
        sql = 'SELECT * FROM "ProvenanceRecord" WHERE entity_id = ?'
        params: list[Any] = [entity_id]

        if op_filter is not None:
            sql += " AND operation = ?"
            params.append(_normalize_operation(op_filter))

        sql += " ORDER BY timestamp DESC"

        cursor.execute(sql, params)
        for row in cursor.fetchall():
            yield _ProvRec(
                id=row["id"],
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                operation=row["operation"],
                actor_id=row["actor_id"] or "",
                timestamp=datetime.fromisoformat(row["timestamp"]),
                schema_version=row["schema_version"] or "",
                derived_from_id=row["derived_from_id"],
                process_id=row["process_id"],
                patch=json.loads(row["patch"]) if row["patch"] else None,
                context=json.loads(row["context"]) if row["context"] else None,
            )

    def get_history(self, entity_id: str) -> list[dict[str, Any]]:
        """Get the complete change history for an entity (oldest first).

        Returns dicts shaped for legacy callers: legacy keys
        (``operation_type``, ``user_id``, ``state_snapshot``) are preserved
        by mapping onto the corresponding sec9 fields — ``state_snapshot``
        draws from ``patch``, ``user_id`` from ``actor_id``. ``previous_state_hash``
        is no longer tracked (sec9 §9.6) and is returned as ``None``.
        """
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT id, entity_id, entity_type, operation, timestamp,
                      actor_id, patch
               FROM "ProvenanceRecord"
               WHERE entity_id = ?
               ORDER BY timestamp ASC""",
            (entity_id,),
        )

        results = []
        for row in cursor.fetchall():
            patch = json.loads(row["patch"]) if row["patch"] else None
            results.append(
                {
                    "operation_id": row["id"],
                    "entity_id": row["entity_id"],
                    "entity_type": row["entity_type"],
                    "operation_type": row["operation"],
                    "timestamp": row["timestamp"],
                    "user_id": row["actor_id"],
                    "previous_state_hash": None,
                    "state_snapshot": patch,
                }
            )
        return results

    def get_state_at(self, entity_id: str, timestamp: str) -> Optional[dict[str, Any]]:
        """Reconstruct entity state as of ``timestamp`` (transaction-time as-of).

        Per sec6 §6.8.2, an entity's data state at ``T`` is the **full
        post-image** carried by its most recent *state-replacing* record
        (``create`` / ``update``) with ``timestamp <= T`` — verified: the
        ``create`` and ``update_data`` paths both record the full entity data
        as the ``patch``. Non-state-replacing records (``availability_change``,
        ``external_id_add``, ``supersede``) carry *deltas*, not entity state, so
        they never define the returned state. The most recent
        ``availability_change`` with ``timestamp <= T`` decides availability: if
        it marks the entity deleted/unavailable, the entity is absent at ``T``
        and ``None`` is returned.

        Known limitation (flagged for a later increment, sec6 §6.8.2): the
        supersede path records an ``operation='update'`` *annotation* patch on
        the replacement entity (not a full post-image), and source-entity
        availability via ``supersede`` is not yet reflected here.
        """
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT patch, timestamp, operation
               FROM "ProvenanceRecord"
               WHERE entity_id = ? AND timestamp <= ?
               ORDER BY timestamp DESC""",
            (entity_id, timestamp),
        )

        data_state: Optional[dict[str, Any]] = None
        data_ts: Optional[str] = None
        availability_resolved = False
        deleted = False

        for row in cursor.fetchall():  # newest first
            patch_obj = json.loads(row["patch"]) if row["patch"] else None
            op = row["operation"]
            # First (newest) availability_change <= T decides availability.
            if (
                not availability_resolved
                and op == "availability_change"
                and isinstance(patch_obj, dict)
            ):
                availability_resolved = True
                if (
                    patch_obj.get("status") == "deleted"
                    or patch_obj.get("is_available") is False
                ):
                    deleted = True
            # Newest state-replacing record carries the full post-image.
            if data_state is None and op in ("create", "update"):
                data_state = patch_obj
                data_ts = row["timestamp"]
            if availability_resolved and data_state is not None:
                break

        if deleted or data_state is None:
            return None

        return {
            "entity_id": entity_id,
            "state": data_state,
            "timestamp": data_ts,
        }

    def get_entity_creation_time(self, entity_id: str) -> Optional[str]:
        """Get the creation timestamp of an entity."""
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT timestamp FROM "ProvenanceRecord"
               WHERE entity_id = ? AND operation = 'create'
               ORDER BY timestamp ASC
               LIMIT 1""",
            (entity_id,),
        )
        row = cursor.fetchone()
        return row["timestamp"] if row else None

    def entities_created_by(
        self, as_of: str, entity_type: Optional[str] = None
    ) -> list[tuple[str, str]]:
        """Candidate ``(entity_id, entity_type)`` set for an as-of query: entities
        with a ``create`` record at-or-before ``as_of`` (sec6 §6.8.2 entity-set
        selection), optionally scoped to ``entity_type``. Per-entity availability
        at ``as_of`` is decided downstream by ``get_state_at``."""
        cursor = self._conn.cursor()
        if entity_type is not None:
            cursor.execute(
                """SELECT DISTINCT entity_id, entity_type FROM "ProvenanceRecord"
                   WHERE operation = 'create' AND entity_type = ?
                     AND timestamp <= ?""",
                (entity_type, as_of),
            )
        else:
            cursor.execute(
                """SELECT DISTINCT entity_id, entity_type FROM "ProvenanceRecord"
                   WHERE operation = 'create' AND timestamp <= ?""",
                (as_of,),
            )
        return [(row["entity_id"], row["entity_type"]) for row in cursor.fetchall()]

    def state_version_at(self, entity_id: str, as_of: str) -> int:
        """Entity version as of ``T`` — the count of state-replacing
        (``create``/``update``) records at-or-before ``as_of``."""
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) AS n FROM "ProvenanceRecord"
               WHERE entity_id = ? AND operation IN ('create', 'update')
                 AND timestamp <= ?""",
            (entity_id, as_of),
        )
        row = cursor.fetchone()
        return int(row["n"]) if row and row["n"] else 1

    def get_provenance_timestamps(
        self, entity_id: str
    ) -> Optional[dict[str, Optional[str]]]:
        """Legacy single-entity timestamp derivation. Kept for backward compat;
        new callers should use ``get_temporal([entity_id])`` instead.
        """
        out = self.get_temporal([entity_id])
        if entity_id not in out:
            return None
        rec = out[entity_id]
        return {
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
        }

    def get_temporal(
        self, entity_ids: list[str], *, as_of: Optional[str] = None
    ) -> "dict[str, TemporalRecord]":
        """Batch-derive sec9 §9.7 temporal fields for the given entities.

        When ``as_of`` (an ISO-8601 timestamp) is given, the derivation is
        bounded to provenance records with ``timestamp <= as_of`` — the
        transaction-time as-of view (sec6 §6.8): ``created_at`` / ``updated_at``
        / ``schema_version`` reflect the graph as it stood at ``as_of``.


        Returns a dict mapping ``entity_id`` → ``TemporalRecord``. An
        ``entity_id`` that has no ``ProvenanceRecord`` rows is absent from
        the returned dict (caller decides whether to raise
        ``ProvenanceIntegrityError``). Uses a single SQL round-trip via
        aggregation + a correlated subquery for the latest record's
        ``schema_version`` and ``actor_id``.

        SQL strategy:
        - ``MIN(timestamp) WHERE operation='create'`` → created_at.
        - ``MAX(timestamp)`` excluding availability-deletion events →
          updated_at. Availability-deletion patches carry
          ``status='deleted'`` or ``is_available=0``.
        - ``actor_id`` of the earliest ``create`` → created_by.
        - For ``updated_by`` and ``schema_version``, read the single
          latest non-deletion record per entity via a GROUP BY subquery.
        """
        from mosaic.core.types import TemporalRecord

        if not entity_ids:
            return {}

        placeholders = ",".join("?" for _ in entity_ids)
        as_of_clause = " AND timestamp <= ?" if as_of else ""
        params = (*entity_ids, as_of) if as_of else tuple(entity_ids)
        cursor = self._conn.cursor()
        cursor.execute(
            f"""WITH target AS (
                    SELECT entity_id, operation, timestamp, actor_id,
                           schema_version, patch
                    FROM "ProvenanceRecord"
                    WHERE entity_id IN ({placeholders}){as_of_clause}
                ),
                agg AS (
                    SELECT
                        entity_id,
                        MIN(CASE WHEN operation = 'create'
                                 THEN timestamp END) AS created_at,
                        MAX(CASE
                            WHEN operation = 'availability_change'
                                 AND (json_extract(patch, '$.status') = 'deleted'
                                      OR json_extract(patch, '$.is_available') = 0)
                                THEN NULL
                            ELSE timestamp
                        END) AS updated_at
                    FROM target
                    GROUP BY entity_id
                )
                SELECT
                    agg.entity_id,
                    agg.created_at,
                    agg.updated_at,
                    (SELECT actor_id FROM target t
                     WHERE t.entity_id = agg.entity_id
                       AND t.operation = 'create'
                       AND t.timestamp = agg.created_at
                     LIMIT 1) AS created_by,
                    (SELECT actor_id FROM target t
                     WHERE t.entity_id = agg.entity_id
                       AND t.timestamp = agg.updated_at
                     LIMIT 1) AS updated_by,
                    (SELECT schema_version FROM target t
                     WHERE t.entity_id = agg.entity_id
                       AND t.timestamp = agg.updated_at
                     LIMIT 1) AS schema_version
                FROM agg""",
            params,
        )

        result: dict[str, TemporalRecord] = {}
        for row in cursor.fetchall():
            eid = row["entity_id"]
            result[eid] = TemporalRecord(
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                schema_version=row["schema_version"] or None,
                created_by=row["created_by"] or None,
                updated_by=row["updated_by"] or None,
            )
        return result


class RelationshipRecord:
    """Relationship between two entities."""

    def __init__(
        self,
        id: str,
        source_id: str,
        target_id: str,
        relationship_type: str,
        metadata: Optional[dict[str, Any]],
        created_at: str,
        created_by: Optional[str],
        is_available: bool = True,
    ):
        self.id = id
        self.source_id = source_id
        self.target_id = target_id
        self.relationship_type = relationship_type
        self.metadata = metadata
        self.created_at = created_at
        self.created_by = created_by
        self.is_available = is_available


class RelationshipStore:
    """Store for relationship records in SQLite."""

    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def create(
        self,
        source_id: str,
        target_id: str,
        relationship_type: str,
        created_by: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> RelationshipRecord:
        """Create a new relationship."""
        import uuid

        now = datetime.now(timezone.utc).isoformat()
        rel_id = str(uuid.uuid4())

        cursor = self._conn.cursor()
        cursor.execute(
            """INSERT INTO relationships (id, source_id, target_id, relationship_type, metadata, created_at, created_by, is_available)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                rel_id,
                source_id,
                target_id,
                relationship_type,
                json.dumps(metadata) if metadata else None,
                now,
                created_by,
            ),
        )

        return RelationshipRecord(
            id=rel_id,
            source_id=source_id,
            target_id=target_id,
            relationship_type=relationship_type,
            metadata=metadata,
            created_at=now,
            created_by=created_by,
            is_available=True,
        )

    def delete(self, source_id: str, target_id: str, relationship_type: str) -> bool:
        """Soft delete a relationship."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.cursor()
        cursor.execute(
            """UPDATE relationships SET is_available = 0, created_at = ?
               WHERE source_id = ? AND target_id = ? AND relationship_type = ? AND is_available = 1""",
            (now, source_id, target_id, relationship_type),
        )
        return cursor.rowcount > 0

    def find_by_source(
        self, source_id: str, relationship_type: Optional[str] = None
    ) -> Iterator[RelationshipRecord]:
        """Find relationships by source entity."""
        cursor = self._conn.cursor()
        sql = "SELECT * FROM relationships WHERE source_id = ? AND is_available = 1"
        params = [source_id]

        if relationship_type:
            sql += " AND relationship_type = ?"
            params.append(relationship_type)

        cursor.execute(sql, params)
        for row in cursor.fetchall():
            yield self._row_to_relationship(row)

    def find_by_target(
        self, target_id: str, relationship_type: Optional[str] = None
    ) -> Iterator[RelationshipRecord]:
        """Find relationships by target entity."""
        cursor = self._conn.cursor()
        sql = "SELECT * FROM relationships WHERE target_id = ? AND is_available = 1"
        params = [target_id]

        if relationship_type:
            sql += " AND relationship_type = ?"
            params.append(relationship_type)

        cursor.execute(sql, params)
        for row in cursor.fetchall():
            yield self._row_to_relationship(row)

    def find(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        relationship_type: Optional[str] = None,
    ) -> Iterator[RelationshipRecord]:
        """Find relationships matching criteria."""
        cursor = self._conn.cursor()
        conditions = ["is_available = 1"]
        params = []

        if source_id:
            conditions.append("source_id = ?")
            params.append(source_id)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        if relationship_type:
            conditions.append("relationship_type = ?")
            params.append(relationship_type)

        sql = f"SELECT * FROM relationships WHERE {' AND '.join(conditions)}"
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            yield self._row_to_relationship(row)

    def traverse(
        self,
        source_id: str,
        relationship_type: Optional[str] = None,
        max_depth: int = 10,
        *,
        as_of: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Traverse relationships using a recursive CTE over the current graph.

        When ``as_of`` is given, traversal instead walks only edges live at that
        transaction-time, reconstructed from relationship_add/remove provenance
        events (sec6 §6.8.2) — the relationships table's ``is_available`` flag is
        not consulted (it carries no per-edge change timestamp).
        """
        if as_of is not None:
            return self._traverse_as_of(
                source_id, relationship_type, max_depth, as_of
            )
        cursor = self._conn.cursor()

        if relationship_type:
            cte_sql = f"""
                WITH RECURSIVE traversal(id, source_id, target_id, relationship_type, depth) AS (
                    SELECT id, source_id, target_id, relationship_type, 1
                    FROM relationships
                    WHERE source_id = ? AND relationship_type = ? AND is_available = 1
                    UNION ALL
                    SELECT r.id, r.source_id, r.target_id, r.relationship_type, t.depth + 1
                    FROM relationships r
                    INNER JOIN traversal t ON r.source_id = t.target_id
                    WHERE r.is_available = 1 AND t.depth < ?
                )
                SELECT * FROM traversal
            """
            cursor.execute(cte_sql, (source_id, relationship_type, max_depth))
        else:
            cte_sql = f"""
                WITH RECURSIVE traversal(id, source_id, target_id, relationship_type, depth) AS (
                    SELECT id, source_id, target_id, relationship_type, 1
                    FROM relationships
                    WHERE source_id = ? AND is_available = 1
                    UNION ALL
                    SELECT r.id, r.source_id, r.target_id, r.relationship_type, t.depth + 1
                    FROM relationships r
                    INNER JOIN traversal t ON r.source_id = t.target_id
                    WHERE r.is_available = 1 AND t.depth < ?
                )
                SELECT * FROM traversal
            """
            cursor.execute(cte_sql, (source_id, max_depth))

        results = []
        for row in cursor.fetchall():
            results.append(
                {
                    "id": row["id"],
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "relationship_type": row["relationship_type"],
                    "depth": row["depth"],
                }
            )
        return results

    def _live_edges_at(self, as_of: str) -> list[dict[str, Any]]:
        """Edges live at ``as_of`` (sec6 §6.8.2): replay ``relationship_add`` /
        ``relationship_remove`` provenance events with ``timestamp <= as_of`` in
        order; an edge keyed by (source, target, type) is live iff its latest
        such event is an add. The relationships table is NOT consulted."""
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT operation,
                      json_extract(patch, '$.source_id')         AS s,
                      json_extract(patch, '$.target_id')         AS t,
                      json_extract(patch, '$.relationship_type') AS rt,
                      json_extract(patch, '$.relationship_id')   AS rid
               FROM "ProvenanceRecord"
               WHERE operation IN ('relationship_add', 'relationship_remove')
                 AND timestamp <= ?
               ORDER BY timestamp ASC""",
            (as_of,),
        )
        live: dict[tuple[Any, Any, Any], Optional[str]] = {}
        for row in cursor.fetchall():
            key = (row["s"], row["t"], row["rt"])
            if row["operation"] == "relationship_add":
                live[key] = row["rid"]
            else:
                live.pop(key, None)
        return [
            {"id": rid, "source_id": s, "target_id": t, "relationship_type": rt}
            for (s, t, rt), rid in live.items()
        ]

    def _traverse_as_of(
        self,
        source_id: str,
        relationship_type: Optional[str],
        max_depth: int,
        as_of: str,
    ) -> list[dict[str, Any]]:
        """Depth-bounded traversal over the edges live at ``as_of``. Mirrors the
        recursive-CTE semantics: the anchor filters by ``relationship_type`` (if
        given); deeper hops follow live edges of any type. ``id`` is the original
        relationship id when the add event recorded it, else ``None``."""
        from collections import defaultdict, deque

        adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self._live_edges_at(as_of):
            adjacency[edge["source_id"]].append(edge)

        results: list[dict[str, Any]] = []
        queue: deque[tuple[dict[str, Any], int]] = deque()
        for edge in adjacency.get(source_id, []):
            if relationship_type and edge["relationship_type"] != relationship_type:
                continue
            queue.append((edge, 1))
        while queue:
            edge, depth = queue.popleft()
            results.append(
                {
                    "id": edge["id"],
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "relationship_type": edge["relationship_type"],
                    "depth": depth,
                }
            )
            if depth < max_depth:
                for nxt in adjacency.get(edge["target_id"], []):
                    queue.append((nxt, depth + 1))
        return results

    def _row_to_relationship(self, row: sqlite3.Row) -> RelationshipRecord:
        """Convert a database row to a relationship."""
        return RelationshipRecord(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relationship_type=row["relationship_type"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
            created_at=row["created_at"],
            created_by=row["created_by"],
            is_available=bool(row["is_available"]),
        )


class FTSStore:
    """Store for FTS5 virtual tables."""

    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def create_fts_table(
        self,
        table_name: str,
        columns: list[str],
        content_table: str = "",
        content_rowid: str = "rowid",
    ) -> None:
        """Create an FTS5 virtual table.

        Always includes ``entity_id`` and ``content`` columns (the Mosaic
        standard FTS schema) in addition to any caller-supplied columns.
        """
        cursor = self._conn.cursor()
        # Ensure the standard Mosaic FTS columns are always present.
        # Pass include_entity_id=False because we manage entity_id ourselves.
        standard = ["entity_id", "content"]
        extra = [c for c in columns if c not in standard]
        fts_columns = generate_fts_column_definitions(
            standard + extra, include_entity_id=False
        )
        sql = generate_fts_create_sql(
            table_name=table_name,
            columns=fts_columns,
            content_table=content_table if content_table else None,
            content_rowid=content_rowid,
        )
        cursor.execute(sql)

    def sync_entity_to_fts(
        self,
        table_name: str,
        entity_id: str,
        content: str,
    ) -> None:
        """Sync an entity to the FTS table (insert or update)."""
        cursor = self._conn.cursor()
        cursor.execute(
            f"SELECT rowid FROM {table_name} WHERE entity_id = ?",
            (entity_id,),
        )
        existing = cursor.fetchone()

        if existing:
            self.update_fts_entry(table_name, entity_id, content)
        else:
            self.insert_fts_entry(table_name, entity_id, content)

    def remove_entity_from_fts(
        self,
        table_name: str,
        entity_id: str,
    ) -> None:
        """Remove an entity from the FTS table."""
        self.delete_fts_entry(table_name, entity_id)

    def drop_fts_table(self, table_name: str) -> None:
        """Drop an FTS5 virtual table."""
        cursor = self._conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")

    def insert_fts_entry(
        self,
        table_name: str,
        entity_id: str,
        content: str,
    ) -> None:
        """Insert an entry into an FTS table."""
        cursor = self._conn.cursor()
        columns = ["entity_id", "content"]
        sql = generate_fts_insert_sql(table_name, columns)
        cursor.execute(sql, (entity_id, content))

    def update_fts_entry(
        self,
        table_name: str,
        entity_id: str,
        content: str,
    ) -> None:
        """Update an entry in an FTS table."""
        cursor = self._conn.cursor()
        cursor.execute(
            f"UPDATE {table_name} SET content = ? WHERE entity_id = ?",
            (content, entity_id),
        )

    def delete_fts_entry(self, table_name: str, entity_id: str) -> None:
        """Delete an entry from an FTS table."""
        cursor = self._conn.cursor()
        cursor.execute(f"DELETE FROM {table_name} WHERE entity_id = ?", (entity_id,))

    def search_fts(
        self,
        table_name: str,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search an FTS table."""
        cursor = self._conn.cursor()
        cursor.execute(
            f"SELECT entity_id, content FROM {table_name} WHERE {table_name} MATCH ? LIMIT ?",
            (generate_fts_phrase_query(query), limit),
        )
        return [
            {"entity_id": row["entity_id"], "content": row["content"]}
            for row in cursor.fetchall()
        ]

    def get_fts_tables_for_entity_type(self, entity_type: str) -> list[str]:
        """Get all FTS tables for an entity type."""
        return get_fts_tables_for_entity_type(self._conn.cursor(), entity_type)

    def fts_table_exists(self, table_name: str) -> bool:
        """Check if an FTS table exists."""
        return fts_table_exists(self._conn.cursor(), table_name)


class SQLiteAdapter(EntityStore):
    """SQLite storage adapter with WAL mode for improved concurrency."""

    def __init__(
        self,
        database_path: str | Path,
        schema_registry: "SchemaRegistry",
        wal_mode: bool = True,
        schema_version: Optional[str] = None,
    ):
        """Initialize the SQLite adapter.

        Args:
            database_path: Path to the SQLite database file.
            schema_registry: LinkML schema registry for schema introspection
                and validation. Required for LinkML-native storage operations.
            wal_mode: Whether to enable WAL journal mode.
            schema_version: Schema version string captured on each
                ``ProvenanceRecord`` write (sec9 §9.6 / §9.7). Typically
                derived from ``SchemaRegistry.schema_view.schema.version``
                by ``MosaicClient``; callers constructing the adapter
                directly may supply it explicitly. Defaults to the empty
                string (legacy transition; see Decision 9.6.F).
        """
        self.database_path = Path(database_path)
        self.schema_registry = schema_registry
        self.wal_mode = wal_mode
        self._schema_version = schema_version or ""
        self._local = threading.local()
        self._provenance_store: Optional[ProvenanceStore] = None
        self._relationship_store: Optional[RelationshipStore] = None
        self._fts_store: Optional[FTSStore] = None
        # Per-class cache of hippo_external_xref slot names (issue #48).
        self._xref_slots_cache: dict[str, list[str]] = {}
        # Per-class cache of multivalued reference slot names (issue #79).
        self._mv_ref_slots_cache: dict[str, list[str]] = {}
        # Per-class cache of virtual inverse slots (ADR-0011): slot name ->
        # InverseSlot(target_class, forward_slot).
        self._inverse_slots_cache: dict[str, dict[str, "InverseSlot"]] = {}
        # Per-class cache of single-valued polymorphic-base reference slots
        # (slot_name, base_range) — the references with no SQL FK, checked in
        # the application layer at commit (issue #127).
        self._poly_ref_slots_cache: dict[str, list[tuple[str, str]]] = {}
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(
                str(self.database_path),
                timeout=30.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            self._configure_connection(conn)
            self._local.connection = conn
        return self._local.connection

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Configure connection with WAL mode and other settings."""
        cursor = conn.cursor()

        if self.wal_mode:
            cursor.execute("PRAGMA journal_mode=WAL")

        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")

        conn.commit()

    @staticmethod
    def _safe_rollback(conn: sqlite3.Connection) -> None:
        """Roll back without masking the in-flight exception.

        A ``rollback()`` that itself raises (rare — e.g. the connection
        died) must not replace the original error that triggered it; that
        error is the actionable one. We swallow a failed rollback so the
        caller's bare ``raise`` re-raises the original with its traceback
        intact (PTS-346 item 4).
        """
        try:
            conn.rollback()
        except Exception:
            pass

    @contextmanager
    def _transaction(
        self,
    ) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for database transactions.

        When a :meth:`staged_transaction` scope is active on this thread
        (sec11 §11.5.2 — the orchestrator's single commit-or-rollback
        wrapper), inner writes **defer** their commit to that outer scope:
        this yields the shared thread-local connection without committing,
        so a whole multi-package / multi-hop migration chain commits or
        rolls back as one unit. Reads on the same connection still observe
        the staged (uncommitted) writes, so later hops see earlier hops'
        output and the end-to-end gate sees the staged write-set.
        """
        conn = self._get_connection()
        if getattr(self._local, "staging_depth", 0) > 0:
            # Inside an outer staged scope: defer commit/rollback to it. On
            # error, propagate so the staged scope rolls everything back.
            yield conn
            return
        try:
            yield conn
            # Application-level referential-integrity for polymorphic-base
            # references (no SQL FK exists to enforce them) — see issue #127.
            self._verify_pending_ref_checks(conn)
            conn.commit()
        except Exception:
            self._local.pending_ref_checks = []
            self._safe_rollback(conn)
            raise

    @contextmanager
    def staged_transaction(
        self,
    ) -> Generator[sqlite3.Connection, None, None]:
        """Outer commit-or-rollback scope for the lifecycle orchestrator.

        Within this scope every inner :meth:`_transaction` defers its commit
        (sec11 §11.5.2). All writes across the staged chain — entity rows,
        provenance, relationships, FTS (all on the one thread-local
        connection) — commit together on clean exit, or roll back together
        if any exception escapes the scope (e.g. the end-to-end validation
        gate raising :class:`~mosaic.core.exceptions.MigrationGateError`).
        Nesting is reference-counted: only the outermost scope commits.

        Foreign-key checks are **deferred to commit** for the duration of the
        scope (``PRAGMA defer_foreign_keys=ON``, issue #95): with the whole
        write-set landing in one transaction, rows may reference each other in
        any order — self-referential and cyclic references (``A → B → A``, a
        self-loop) that no per-row insertion order can satisfy now insert and
        are validated together at commit. The pragma auto-resets at the next
        commit/rollback, so it only affects this scope; genuinely dangling
        references still fail the commit and roll the whole set back.
        """
        conn = self._get_connection()
        depth = getattr(self._local, "staging_depth", 0)
        if depth > 0:
            # Already staging on this thread — join the existing scope.
            self._local.staging_depth = depth + 1
            try:
                yield conn
            finally:
                self._local.staging_depth -= 1
            return
        self._local.staging_depth = 1
        # Defer FK enforcement to commit for the whole staged write-set so
        # cyclic / self-referential references can be inserted (issue #95).
        #
        # ``defer_foreign_keys`` is a per-transaction flag that SQLite clears
        # at the next COMMIT/ROLLBACK. In sqlite3's legacy autocommit mode each
        # bare SELECT runs (and auto-commits) in its own implicit transaction,
        # so a flag set in autocommit is wiped by the first read the write path
        # does before any INSERT. We therefore open the scope's transaction
        # explicitly *first*, then set the flag inside it: it now survives every
        # interleaved read until the single staged commit. A genuinely dangling
        # reference still fails that commit and rolls the whole set back.
        if not conn.in_transaction:
            conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys=ON")
        try:
            yield conn
            # Deferred app-level check for polymorphic-base references, run at
            # the single staged commit so forward references across the bundle
            # resolve just like the deferred SQL FKs do (issues #127, #95).
            self._verify_pending_ref_checks(conn)
            conn.commit()
        except Exception:
            self._local.pending_ref_checks = []
            self._safe_rollback(conn)
            raise
        finally:
            self._local.staging_depth = 0
            self._local.pending_ref_checks = []

    def _init_database(self) -> None:
        """Initialize database schema."""
        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS hippo_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # ProvenanceRecord — shape stamped inline here to match what the
            # LinkML DDL generator produces from hippo_core.ProvenanceRecord
            # (verified by tests/core/test_ddl_generator.py::
            # TestHippoCoreProvenanceRecordDDL). Per Decision 9.6.D, this
            # table replaces the legacy hand-coded `provenance` table.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS "ProvenanceRecord" (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT,
                    entity_type TEXT,
                    operation TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    derived_from_id TEXT,
                    process_id TEXT,
                    patch TEXT,
                    context TEXT,
                    is_available INTEGER NOT NULL DEFAULT 1,
                    superseded_by TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_entity_id
                ON "ProvenanceRecord"(entity_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_operation
                ON "ProvenanceRecord"(operation)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_timestamp
                ON "ProvenanceRecord"(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_process_id
                ON "ProvenanceRecord"(process_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_entity_timestamp
                ON "ProvenanceRecord"(entity_id, timestamp)
            """)
            # Type-scoped as-of set selection (sec6 §6.8.4): "entities of type X
            # present at T" scans creates by (entity_type, timestamp).
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_type_timestamp
                ON "ProvenanceRecord"(entity_type, timestamp, entity_id)
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    created_by TEXT,
                    is_available INTEGER NOT NULL DEFAULT 1
                )
            """)

            # Shadow table for cross-class UUID → class_name lookup
            # (sec9 §9.5 / PR 2.4). With per-class typed tables,
            # ``read(uuid)`` would otherwise need to scan every class
            # table or join ``ProvenanceRecord``; this O(1) lookup is
            # maintained inline by ``create()``. Idempotent backfill
            # below covers DBs created before this table existed.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS _entity_registry (
                    uuid TEXT PRIMARY KEY,
                    class_name TEXT NOT NULL
                )
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO _entity_registry (uuid, class_name)
                SELECT entity_id, entity_type
                FROM "ProvenanceRecord"
                WHERE operation = 'create'
                  AND entity_id IS NOT NULL
                  AND entity_type IS NOT NULL
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_source
                ON relationships(source_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_target
                ON relationships(target_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_type
                ON relationships(relationship_type)
            """)

            # Reference-loader write log (sec2 §2.14.9 / Decision 2.14.J).
            # Substrate for ``--prune-old``: records every entity written by
            # a reference loader inside ``MosaicClient.load_context()``.
            # Composite PK collapses repeat writes of the same id within a
            # ``(loader_name, version)`` window to a single row, keeping
            # ``upgrade()`` re-writes and migration backfill idempotent.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reference_write_log (
                    loader_name TEXT NOT NULL,
                    version     TEXT NOT NULL,
                    entity_id   TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    written_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (loader_name, version, entity_id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reference_write_log_lookup
                ON reference_write_log (loader_name, version)
            """)

            self._init_triggers(cursor)

            # Create entity_provenance_summary view before entity-level migrations.
            # This view is REQUIRED (not optional) for correct provenance-derived field
            # derivation in client.query(). Columns:
            #   entity_id       - the entity
            #   created_at      - timestamp of earliest record for the entity
            #   updated_at      - timestamp of most recent non-deletion record
            #   schema_version  - schema_version from the latest record (sec9 §9.7)
            # Post-migration (sec9 §9.6 / Decision 9.6.D) this reads from
            # the ProvenanceRecord table with the new column shape. The
            # availability-change exclusion mirrors the earlier SOFT_DELETE
            # exclusion: patch.status == 'deleted' or is_available == 0 is
            # not an update for updated_at purposes.
            cursor.execute("""
                CREATE VIEW IF NOT EXISTS entity_provenance_summary AS
                SELECT
                    entity_id,
                    entity_type,
                    MIN(timestamp) AS created_at,
                    MAX(CASE
                        WHEN operation = 'availability_change'
                             AND (json_extract(patch, '$.status') = 'deleted'
                                  OR json_extract(patch, '$.is_available') = 0)
                            THEN NULL
                        ELSE timestamp
                    END) AS updated_at,
                    (SELECT schema_version FROM "ProvenanceRecord" p2
                     WHERE p2.entity_id = p1.entity_id
                     ORDER BY timestamp DESC LIMIT 1) AS schema_version
                FROM "ProvenanceRecord" p1
                WHERE entity_id IS NOT NULL
                GROUP BY entity_id, entity_type
            """)

            self._run_migrations(cursor)

            self._init_per_class_tables(cursor)

            self._migrate_reference_entity_ids(cursor)

    def _init_per_class_tables(self, cursor: sqlite3.Cursor) -> None:
        """Emit per-class typed tables for every concrete class in the user schema.

        Generated via :class:`DDLGenerator`, which delegates to LinkML's
        ``SQLTableGenerator`` and post-processes for Mosaic extras
        (``is_available`` default, ``superseded_by`` column, partial
        indexes, FTS5 tables, append-only triggers).

        ``ProvenanceRecord`` is filtered out — it is hand-coded above to
        preserve the explicit shape relied on by ``ProvenanceStore`` and
        the ``entity_provenance_summary`` view. Statements that reference
        a name already present in ``sqlite_master`` are skipped so that
        re-initialising an existing database is idempotent.
        """
        from mosaic.core.storage.ddl_generator import DDLGenerator

        ddl = DDLGenerator().generate(self.schema_registry)
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger','view')"
        )
        # SQLite compares identifiers case-insensitively for ASCII (so
        # "DNASample" and "DnaSample" refer to the same table). Track
        # existing names in lowercase to avoid duplicate-table errors
        # when a schema declares colliding class names.
        existing = {row[0].lower() for row in cursor.fetchall()}

        for stmt in ddl:
            target = self._extract_ddl_target(stmt)
            if target == "ProvenanceRecord" or target.startswith(
                ("idx_ProvenanceRecord_", "prevent_update_ProvenanceRecord",
                 "prevent_delete_ProvenanceRecord")
            ):
                continue
            key = target.lower()
            if key in existing:
                continue
            cursor.execute(stmt)
            existing.add(key)

    @staticmethod
    def _extract_ddl_target(stmt: str) -> str:
        """Return the table/index/trigger name a CREATE statement defines."""
        match = re.search(
            r'CREATE\s+(?:VIRTUAL\s+)?(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\s+'
            r'(?:IF\s+NOT\s+EXISTS\s+)?(?:"([^"]+)"|(\w+))',
            stmt,
            re.IGNORECASE,
        )
        if not match:
            return ""
        return match.group(1) or match.group(2) or ""

    def _per_class_table_exists(self, entity_type: str) -> bool:
        """Whether a per-class typed table for ``entity_type`` is in the schema."""
        registry = self.schema_registry
        if registry is None or not entity_type:
            return False
        if not registry.has_class(entity_type):
            return False
        cls = registry.get_class(entity_type)
        if cls is None or getattr(cls, "abstract", False):
            return False
        # Value types (ExternalReference, or any identifier-less value
        # object) are concrete LinkML classes but have no entity table —
        # they are stored inline (JSON TEXT) on the slot that ranges them
        # (issue #48 / #90). Detection is schema-driven via the registry.
        if entity_type in registry.value_type_classes():
            return False
        # ProvenanceRecord is hand-coded with its own write path; the
        # adapter's CRUD never targets it as a per-class typed table.
        return entity_type != "ProvenanceRecord"

    def _per_class_columns(self, entity_type: str) -> list[str]:
        """Return the column names actually present on the per-class table.

        Falls back to ``induced_slots`` when the table doesn't exist
        yet (used during DDL generation and tests that probe the
        registry before init). Multivalued slots and other declarations
        that LinkML emits as separate junction tables do not appear
        here.
        """
        if not self._per_class_table_exists(entity_type):
            return []
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(f'PRAGMA table_info("{entity_type}")')
                cols = [row[1] for row in cursor.fetchall()]
        except sqlite3.Error:
            cols = []
        if cols:
            return cols
        return [slot.name for slot in self.schema_registry.induced_slots(entity_type)]

    def _project_to_columns(
        self, entity_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Project a user-data dict onto the columns of the per-class table.

        Drops keys that do not correspond to a slot in the class. The
        legacy ``entities.data`` blob keeps the full payload, so dropping
        here is non-lossy. System slots managed separately (``id``,
        ``is_available``, ``superseded_by``) are skipped — callers
        provide explicit values for those.
        """
        managed = {"id", "is_available", "superseded_by"}
        valid = set(self._per_class_columns(entity_type)) - managed
        return {k: v for k, v in data.items() if k in valid}

    @staticmethod
    def _coerce_for_column(value: Any) -> Any:
        """Coerce a Python value to a SQLite-storable form.

        Booleans become 0/1 integers; nested containers are JSON-encoded
        so they round-trip through TEXT columns. Scalars pass through.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return value

    # -- multivalued reference slots → relationships (issue #79 / ADR-0002) ---

    def _multivalued_ref_slot_names(self, entity_type: str) -> list[str]:
        """Names of multivalued reference slots for a class (issue #79).

        Cached per class. Empty when the registry is absent or the class
        declares no multivalued slot ranged on an entity type. These slots
        get no per-class column; their values persist as relationships keyed
        by the slot name.
        """
        cached = self._mv_ref_slots_cache.get(entity_type)
        if cached is None:
            registry = self.schema_registry
            if registry is None or not registry.has_class(entity_type):
                cached = []
            else:
                cached = [
                    name
                    for name, _target in registry.multivalued_reference_slots(
                        entity_type
                    )
                ]
            self._mv_ref_slots_cache[entity_type] = cached
        return cached

    @staticmethod
    def _normalize_ref_list(value: Any) -> list[str]:
        """Normalize a multivalued reference slot value to a list of ids.

        Tolerates a bare scalar (treated as a single-element list) and drops
        ``None`` entries. Non-string ids are coerced to ``str`` so they match
        the ``relationships`` table's TEXT columns.
        """
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        return [str(v) for v in items if v is not None]

    def _materialize_multivalued_refs(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        reconcile: bool,
        actor: Optional[str] = None,
    ) -> None:
        """Persist multivalued reference slots as relationships (issue #79).

        One relationship per target id, keyed by the slot name, written in
        the caller's transaction. On ``reconcile`` (update/replace) the
        existing live edges of every such slot for this source are
        soft-deleted first, then rebuilt from ``data`` — matching
        ``_update_per_class``'s replace-every-user-slot semantics (an omitted
        slot clears its edges, just as it nulls its column).

        Target existence is NOT checked: single-valued reference ids persist
        as opaque strings, and this matches that, so forward references during
        bulk ingest are preserved. Each add/remove records a
        ``relationship_add`` / ``relationship_remove`` ProvenanceRecord so
        as-of edge replay (ADR-0001) stays consistent.
        """
        slot_names = self._multivalued_ref_slot_names(entity_type)
        if not slot_names:
            return

        rel_store = self._get_relationship_store(conn)
        prov = self._get_provenance_store(conn)

        for slot in slot_names:
            if reconcile:
                for rel in list(
                    rel_store.find_by_source(entity_id, relationship_type=slot)
                ):
                    rel_store.delete(entity_id, rel.target_id, slot)
                    prov.record(
                        entity_id=entity_id,
                        entity_type="relationship",
                        operation="relationship_remove",
                        actor_id=actor,
                        patch={
                            "source_id": entity_id,
                            "target_id": rel.target_id,
                            "relationship_type": slot,
                        },
                    )

            if slot not in data:
                continue
            for target_id in self._normalize_ref_list(data[slot]):
                rel = rel_store.create(
                    source_id=entity_id,
                    target_id=target_id,
                    relationship_type=slot,
                    created_by=actor,
                )
                prov.record(
                    entity_id=entity_id,
                    entity_type="relationship",
                    operation="relationship_add",
                    actor_id=actor,
                    patch={
                        "relationship_id": rel.id,
                        "source_id": entity_id,
                        "target_id": target_id,
                        "relationship_type": slot,
                    },
                )

    def _hydrate_multivalued_refs_batch(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_ids: list[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Read multivalued reference slot edges for a set of entities.

        Returns ``{entity_id: {slot_name: [target_id, ...]}}`` in a single
        round-trip (no N+1). Target ids preserve insertion order (``rowid``).
        Only currently-live edges are returned.
        """
        slot_names = self._multivalued_ref_slot_names(entity_type)
        if not slot_names or not entity_ids:
            return {}

        cursor = conn.cursor()
        id_ph = ",".join("?" for _ in entity_ids)
        slot_ph = ",".join("?" for _ in slot_names)
        cursor.execute(
            "SELECT source_id, relationship_type, target_id FROM relationships "
            f"WHERE source_id IN ({id_ph}) "
            f"AND relationship_type IN ({slot_ph}) "
            "AND is_available = 1 "
            "ORDER BY rowid",
            (*entity_ids, *slot_names),
        )
        out: dict[str, dict[str, list[str]]] = {}
        for row in cursor.fetchall():
            out.setdefault(row["source_id"], {}).setdefault(
                row["relationship_type"], []
            ).append(row["target_id"])
        return out

    # -- inverse slots → virtual reverse edges (ADR-0011) ---------------------

    def _inverse_slots(self, entity_type: str) -> dict[str, "InverseSlot"]:
        """Virtual inverse slots of a class, keyed by slot name (ADR-0011).

        Cached per class. Empty when the registry is absent or the class
        declares no ``inverse`` multivalued reference. These slots own no
        column, link table, or relationship rows; they resolve through the
        forward FK column on the *target* class's table.
        """
        cached = self._inverse_slots_cache.get(entity_type)
        if cached is None:
            registry = self.schema_registry
            if registry is None or not registry.has_class(entity_type):
                cached = {}
            else:
                cached = {
                    inv.name: inv
                    for inv in registry.inverse_reference_slots(entity_type)
                }
            self._inverse_slots_cache[entity_type] = cached
        return cached

    def _strip_inverse_slots(
        self, entity_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop virtual inverse slot keys from a write payload (ADR-0011).

        The reverse edge is *derived* from the forward FK on the target
        class; a payload carrying it (typically a get-then-put round-trip
        of a hydrated entity) is accepted and the key ignored, so it never
        reaches a column, relationship rows, or the provenance patch (which
        keeps as-of reconstruction free of stale derived lists). Returns
        ``data`` unchanged (no copy) when nothing needs stripping.
        """
        inverse = self._inverse_slots(entity_type)
        if not inverse or not any(name in data for name in inverse):
            return data
        return {k: v for k, v in data.items() if k not in inverse}

    def _inverse_tables(self, inv: "InverseSlot") -> list[str]:
        """Per-class tables a virtual inverse slot resolves against.

        The range's concrete closure (issue #224): an abstract range such as
        ``Sample`` owns no table, so its rows live in ``Brain``, ``CSF``, ...
        Only tables that exist are returned.
        """
        return [t for t in inv.tables if self._per_class_table_exists(t)]

    def _hydrate_inverse_refs_batch(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_ids: list[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Read virtual inverse slots for a set of entities (ADR-0011).

        Returns ``{entity_id: {slot_name: [target_id, ...]}}`` — one query
        per inverse slot over the target's per-class table, keyed by the
        forward FK column (no N+1). Only **available** targets appear,
        matching what list queries and the ``some`` quantifier see. Ids
        are in ``rowid`` order for determinism.
        """
        inverse = self._inverse_slots(entity_type)
        if not inverse or not entity_ids:
            return {}
        out: dict[str, dict[str, list[str]]] = {}
        cursor = conn.cursor()
        id_ph = ",".join("?" for _ in entity_ids)
        for slot_name, inv in inverse.items():
            tables = self._inverse_tables(inv)
            if not tables:
                continue
            # One statement per slot even when the range is abstract
            # (issue #224): UNION ALL across the concrete closure, ordered
            # by table position then rowid so ids stay deterministic.
            selects = [
                f'SELECT "id", "{inv.forward_slot}" AS fk, {i} AS t_ord, rowid AS r_ord '
                f'FROM "{table}" WHERE "{inv.forward_slot}" IN ({id_ph}) '
                "AND is_available = 1"
                for i, table in enumerate(tables)
            ]
            cursor.execute(
                " UNION ALL ".join(selects) + " ORDER BY t_ord, r_ord",
                tuple(entity_ids) * len(tables),
            )
            for row in cursor.fetchall():
                out.setdefault(row["fk"], {}).setdefault(slot_name, []).append(
                    row["id"]
                )
        return out

    def _hydrate_derived_refs_batch(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_ids: list[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Every slot that is *not* a column of the per-class table but does
        belong in ``entity["data"]``: relationships-backed multivalued
        references (ADR-0002) plus virtual inverse slots (ADR-0011)."""
        out = self._hydrate_multivalued_refs_batch(conn, entity_type, entity_ids)
        for eid, slots in self._hydrate_inverse_refs_batch(
            conn, entity_type, entity_ids
        ).items():
            out.setdefault(eid, {}).update(slots)
        return out

    def _init_triggers(self, cursor: sqlite3.Cursor) -> None:
        """Initialize provenance immutability triggers."""
        for trigger_sql in sqlite_triggers.get_trigger_sql_list():
            cursor.execute(trigger_sql)

    def _run_migrations(self, cursor: sqlite3.Cursor) -> None:
        """Run database migrations for schema updates.

        Post-sec9 §9.6 migration, the legacy ``provenance`` table is
        replaced by ``ProvenanceRecord`` (created in ``_init_database``).
        Legacy column-level ADD COLUMN migrations are no longer needed;
        any dev database that still contains a legacy ``provenance``
        table should be dropped and recreated (no production deployments
        per earlier user directive).
        """
        # Drop the legacy provenance table if it exists; data migration
        # from legacy to ProvenanceRecord is not supported (dev-only
        # deployments).
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='provenance'"
        )
        if cursor.fetchone() is not None:
            cursor.execute("DROP TABLE IF EXISTS provenance")

        # Drop the legacy entities and entity_external_ids tables if a
        # pre-PR-2.3 database still has them. Data migration is not
        # supported (dev-only deployments per earlier user directive);
        # all live data is in per-class typed tables and ProvenanceRecord.
        for legacy in ("entity_external_ids", "entities"):
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (legacy,),
            )
            if cursor.fetchone() is not None:
                cursor.execute(f"DROP TABLE IF EXISTS {legacy}")

    _REFERENCE_WRITE_LOG_MIGRATION_KEY = "_reference_write_log_v1_migrated"

    def _migrate_reference_entity_ids(self, cursor: sqlite3.Cursor) -> None:
        """One-time backfill from ``hippo_meta.reference_entity_ids`` JSON
        blob to the ``reference_write_log`` table (sec2 §2.14.9).

        v1 stored ``{loader_name: {version: [entity_id, ...]}}`` under the
        ``reference_entity_ids`` meta key. v2 replaces that with the write
        log. The first ``_init_database`` after this code lands performs
        the backfill, then stamps a marker key so subsequent startups skip
        entirely. The marker is required because the v1 ``_write_versions``
        path is still in tree (removed in PTS-256) and would otherwise be
        clobbered on every adapter init.

        ``entity_type`` is resolved per id by looking it up in
        ``_entity_registry`` first (created entities) and falling back to
        ``ProvenanceRecord`` (survives any future entity deletion). Rows
        whose type cannot be resolved are skipped — preferable to a hard
        failure during init, since the prune flow that consumes the log
        is opt-in.

        ``INSERT OR IGNORE`` plus the composite primary key make the row
        writes idempotent even if a previous migration crashed after
        inserting some rows but before stamping the marker.
        """
        cursor.execute(
            "SELECT 1 FROM hippo_meta WHERE key = ?",
            (self._REFERENCE_WRITE_LOG_MIGRATION_KEY,),
        )
        if cursor.fetchone() is not None:
            return

        cursor.execute(
            "SELECT value FROM hippo_meta WHERE key = 'reference_entity_ids'"
        )
        row = cursor.fetchone()
        if row is not None:
            raw = row["value"] if isinstance(row, sqlite3.Row) else row[0]
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = None

            if isinstance(payload, dict):
                for loader_name, by_version in payload.items():
                    if not isinstance(loader_name, str) or not isinstance(
                        by_version, dict
                    ):
                        continue
                    for version, entity_ids in by_version.items():
                        if not isinstance(version, str) or not isinstance(
                            entity_ids, list
                        ):
                            continue
                        for entity_id in entity_ids:
                            if not isinstance(entity_id, str):
                                continue
                            entity_type = (
                                self._lookup_entity_type_for_migration(
                                    cursor, entity_id
                                )
                            )
                            if entity_type is None:
                                continue
                            cursor.execute(
                                "INSERT OR IGNORE INTO reference_write_log "
                                "(loader_name, version, entity_id, entity_type) "
                                "VALUES (?, ?, ?, ?)",
                                (loader_name, version, entity_id, entity_type),
                            )

            cursor.execute(
                "DELETE FROM hippo_meta WHERE key = 'reference_entity_ids'"
            )

        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO hippo_meta (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "                                updated_at = excluded.updated_at",
            (
                self._REFERENCE_WRITE_LOG_MIGRATION_KEY,
                json.dumps({"completed_at": now}, sort_keys=True),
                now,
            ),
        )

    @staticmethod
    def _lookup_entity_type_for_migration(
        cursor: sqlite3.Cursor, entity_id: str
    ) -> Optional[str]:
        """Best-effort entity_type lookup used by the v1→v2 backfill.

        Checks ``_entity_registry`` first; falls back to ``ProvenanceRecord``
        in case the registry was missed (older databases predating the
        registry, or future entity-row removal).
        """
        cursor.execute(
            "SELECT class_name FROM _entity_registry WHERE uuid = ?",
            (entity_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            return row["class_name"] if isinstance(row, sqlite3.Row) else row[0]
        cursor.execute(
            'SELECT entity_type FROM "ProvenanceRecord" '
            "WHERE entity_id = ? AND entity_type IS NOT NULL LIMIT 1",
            (entity_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            return row["entity_type"] if isinstance(row, sqlite3.Row) else row[0]
        return None

    def _get_provenance_store(self, conn: sqlite3.Connection) -> ProvenanceStore:
        """Get or create a ProvenanceStore for the given connection."""
        if self._provenance_store is None or self._provenance_store._conn is not conn:
            self._provenance_store = ProvenanceStore(
                conn, schema_version=self._schema_version
            )
        return self._provenance_store

    def _get_relationship_store(self, conn: sqlite3.Connection) -> RelationshipStore:
        """Get or create a RelationshipStore for the given connection."""
        if self._relationship_store is None or self._relationship_store._conn is not conn:
            self._relationship_store = RelationshipStore(conn)
        return self._relationship_store

    def _get_fts_store(self, conn: Optional[sqlite3.Connection] = None) -> FTSStore:
        """Get or create an FTSStore for the given connection.

        Re-binds when *conn* differs from the cached store's connection —
        connections are thread-local, so a store cached by one thread must
        not be reused from another. Writing FTS rows through a foreign
        thread's connection leaves that connection in a never-committed
        write transaction, permanently write-locking the database.
        """
        if conn is None:
            conn = self._get_connection()
        if self._fts_store is None or self._fts_store._conn is not conn:
            self._fts_store = FTSStore(conn)
        return self._fts_store

    def create_fts_table(
        self,
        table_name: str,
        columns: list[str],
        content_table: str = "",
    ) -> None:
        """Create an FTS5 virtual table."""
        with self._transaction() as conn:
            fts_store = self._get_fts_store(conn)
            fts_store.create_fts_table(table_name, columns, content_table)

    def search_fts(
        self,
        table_name: str,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search an FTS table."""
        with self._transaction() as conn:
            fts_store = self._get_fts_store(conn)
            return fts_store.search_fts(table_name, query, limit)

    def get_fts_tables_for_entity_type(self, entity_type: str) -> list[str]:
        """Get all FTS tables for an entity type."""
        with self._transaction() as conn:
            fts_store = self._get_fts_store(conn)
            return fts_store.get_fts_tables_for_entity_type(entity_type)

    def search(
        self,
        query: str,
        entity_type: str,
        field_name: str,
        min_score: float = 0.0,
        limit: int = 100,
    ) -> list[ScoredMatch]:
        """Search entities using full-text search with BM25 ranking.

        Args:
            query: The search query string.
            entity_type: The type of entities to search.
            field_name: The FTS-indexed field to search in.
            min_score: Minimum score threshold (0.0-1.0).
            limit: Maximum number of results to return.

        Returns:
            List of ScoredMatch objects ordered by score descending.

        Raises:
            SearchCapabilityError: If the field is not FTS-indexed.
        """
        if limit <= 0:
            limit = 100
        limit = min(limit, 1000)

        fts_table_name = f"fts_{entity_type.lower()}_{field_name.lower()}"

        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (fts_table_name,),
            )
            if cursor.fetchone() is None:
                raise SearchCapabilityError(
                    message=f"Field '{field_name}' on entity type '{entity_type}' is not FTS-indexed",
                    field_name=field_name,
                    entity_type=entity_type,
                )

            # Join the FTS table against the per-class typed table on
            # ``entity_id`` (the standard Mosaic FTS column) so that
            # availability filtering happens on the per-class row. The
            # per-class table only holds rows of ``entity_type``, so an
            # explicit ``entity_type =`` predicate is unnecessary.
            cursor.execute(
                f"""SELECT pc.id, bm25({fts_table_name}) as bm25_score
                    FROM {fts_table_name} fts
                    INNER JOIN "{entity_type}" pc ON fts.entity_id = pc.id
                    WHERE {fts_table_name} MATCH ?
                    AND pc.is_available = 1
                    ORDER BY bm25_score
                    LIMIT ?""",
                (generate_fts_phrase_query(query), limit * 2),
            )

            results = []
            max_bm25 = None
            for row in cursor.fetchall():
                if max_bm25 is None:
                    max_bm25 = abs(row["bm25_score"]) if row["bm25_score"] != 0 else 1.0

                raw_bm25 = abs(row["bm25_score"]) if row["bm25_score"] != 0 else 0.0
                normalized = raw_bm25 / max_bm25 if max_bm25 > 0 else 0.0

                if normalized >= min_score:
                    results.append(
                        ScoredMatch(
                            entity_id=row["id"],
                            score=normalized,
                            highlights=None,
                        )
                    )

            results.sort(key=lambda x: x.score, reverse=True)
            return results[:limit]

    def create(
        self,
        entity: SQLiteEntity,
        user_context: Optional[str] = None,
        loader_context: Optional[tuple[str, str]] = None,
    ) -> SQLiteEntity:
        """Create a new entity in the store.

        Writes a typed row to the per-class table for ``entity.entity_type``,
        registers ``(uuid, class_name)`` in ``_entity_registry`` for fast
        cross-class lookup (sec9 §9.5), and records a ``create``
        ``ProvenanceRecord``.

        When ``loader_context`` is a ``(loader_name, version)`` tuple
        (set by ``MosaicClient.load_context()``), a row is appended to
        ``reference_write_log`` inside the same SQL transaction as the
        entity write (sec2 §2.14.9 / Decision 2.14.J).
        """
        import uuid

        entity_id = (
            entity.id if hasattr(entity, "id") and entity.id else str(uuid.uuid4())
        )
        entity_type = (
            entity.entity_type
            if hasattr(entity, "entity_type")
            else type(entity).__name__
        )

        entity_data = {}
        if hasattr(entity, "data") and isinstance(entity.data, dict):
            entity_data = entity.data
        else:
            for attr in dir(entity):
                if not attr.startswith("_"):
                    value = getattr(entity, attr, None)
                    if not callable(value):
                        entity_data[attr] = value

        is_available = (
            1 if not hasattr(entity, "is_available") or entity.is_available else 0
        )
        # Virtual inverse slots are derived, never written (ADR-0011).
        entity_data = self._strip_inverse_slots(entity_type, entity_data)

        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT class_name FROM _entity_registry WHERE uuid = ?",
                (entity_id,),
            )
            registry_row = cursor.fetchone()
            if registry_row is not None and registry_row["class_name"] != entity_type:
                raise EntityTypeConflictError(
                    message=(
                        f"Cannot create entity {entity_id!r} as {entity_type!r}: "
                        f"id already registered under "
                        f"{registry_row['class_name']!r}."
                    ),
                    entity_id=entity_id,
                    requested_entity_type=entity_type,
                    existing_entity_type=registry_row["class_name"],
                )

            self._insert_per_class(cursor, entity_type, entity_id, entity_data,
                                   is_available=is_available)

            cursor.execute(
                "INSERT OR IGNORE INTO _entity_registry (uuid, class_name) "
                "VALUES (?, ?)",
                (entity_id, entity_type),
            )

            # Queue polymorphic-base references for the deferred integrity
            # check at commit (issue #127) — these have no SQL FK to catch a
            # dangling id.
            self._record_ref_checks(entity_type, entity_id, entity_data)

            provenance = self._get_provenance_store(conn)
            provenance.record(
                entity_id=entity_id,
                entity_type=entity_type,
                operation="create",
                actor_id=user_context,
                patch=entity_data,
            )

            self._materialize_multivalued_refs(
                conn,
                entity_type,
                entity_id,
                entity_data,
                reconcile=False,
                actor=user_context,
            )

            if loader_context is not None:
                loader_name, version = loader_context
                cursor.execute(
                    "INSERT OR IGNORE INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (?, ?, ?, ?)",
                    (loader_name, version, entity_id, entity_type),
                )

        return entity

    def _polymorphic_base_ref_slots(
        self, entity_type: str
    ) -> list[tuple[str, str]]:
        """(slot_name, base_range) for single-valued references on
        ``entity_type`` whose range is a polymorphic base.

        These are exactly the references the DDL stores as a bare TEXT id with
        no SQL foreign key (issue #93), so nothing at the SQL layer catches a
        dangling id — the gap this integrity check fills (issue #127).
        Multivalued references are excluded: they persist as relationship rows
        (issue #79), not scalar columns.
        """
        cached = self._poly_ref_slots_cache.get(entity_type)
        if cached is not None:
            return cached
        registry = self.schema_registry
        result: list[tuple[str, str]] = []
        if registry is not None:
            try:
                # Relationship-backed multivalued refs (ADR-0002) and virtual
                # inverse slots (ADR-0011) are not scalar FK columns.
                multivalued = registry.non_column_slot_names(entity_type)
                for slot_name, target_range in registry.reference_slots(entity_type):
                    if (
                        target_range
                        and slot_name not in multivalued
                        and registry.is_polymorphic_base(target_range)
                    ):
                        result.append((slot_name, target_range))
            except Exception:
                result = []
        self._poly_ref_slots_cache[entity_type] = result
        return result

    def _record_ref_checks(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        """Queue this entity's polymorphic-base references for the deferred
        commit-time existence check (issue #127)."""
        slots = self._polymorphic_base_ref_slots(entity_type)
        if not slots:
            return
        pending = getattr(self._local, "pending_ref_checks", None)
        if pending is None:
            pending = []
            self._local.pending_ref_checks = pending
        for slot_name, target_range in slots:
            value = data.get(slot_name)
            if isinstance(value, str) and value:
                pending.append(
                    (entity_id, entity_type, slot_name, target_range, value)
                )

    def _verify_pending_ref_checks(self, conn: sqlite3.Connection) -> None:
        """Verify every queued polymorphic-base reference resolves, then clear
        the queue.

        Run at commit (see :meth:`_transaction` / :meth:`staged_transaction`)
        against the committing connection, so staged-but-uncommitted
        registrations — forward references within an ingest bundle — are
        visible, exactly as SQLite's deferred foreign keys would see them.

        A reference is valid when its target id is registered in
        ``_entity_registry`` under a class within the reference's declared base
        subtree. A missing id, or an id registered under an unrelated class,
        raises :class:`DanglingReferenceError`, rolling the write back.
        """
        pending = getattr(self._local, "pending_ref_checks", None)
        if not pending:
            return
        self._local.pending_ref_checks = []

        registry = self.schema_registry
        subtree_cache: dict[str, set[str]] = {}

        def subtree(base: str) -> set[str]:
            names = subtree_cache.get(base)
            if names is None:
                names = {base}
                if registry is not None:
                    try:
                        names.update(registry.concrete_subclasses(base))
                    except Exception:
                        pass
                subtree_cache[base] = names
            return names

        for entity_id, entity_type, slot_name, target_range, target_id in pending:
            row = conn.execute(
                "SELECT class_name FROM _entity_registry WHERE uuid = ?",
                (target_id,),
            ).fetchone()
            if row is None:
                raise DanglingReferenceError(
                    message=(
                        f"{entity_type} {entity_id!r} references {target_id!r} "
                        f"via {slot_name!r}, but no entity with that id exists. "
                        f"The reference is ranged on the polymorphic base "
                        f"{target_range!r}, which has no SQL foreign key to "
                        f"enforce this (issue #127)."
                    ),
                    entity_id=entity_id,
                    entity_type=entity_type,
                    slot=slot_name,
                    target_id=target_id,
                    target_range=target_range,
                )
            actual_class = row["class_name"]
            if actual_class not in subtree(target_range):
                raise DanglingReferenceError(
                    message=(
                        f"{entity_type} {entity_id!r} references {target_id!r} "
                        f"via {slot_name!r}, but that id is a {actual_class!r}, "
                        f"which is not {target_range!r} or one of its subclasses."
                    ),
                    entity_id=entity_id,
                    entity_type=entity_type,
                    slot=slot_name,
                    target_id=target_id,
                    target_range=target_range,
                )

    def _insert_per_class(
        self,
        cursor: sqlite3.Cursor,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        is_available: int = 1,
        superseded_by: Optional[str] = None,
    ) -> None:
        """Insert a row into the per-class typed table when the class is known.

        No-op when the class is not in the user schema or is the
        hand-coded ``ProvenanceRecord``. ``id`` and ``is_available``
        are always set; user slot values come from ``data`` with
        unknown keys dropped.
        """
        if not self._per_class_table_exists(entity_type):
            return
        projected = self._project_to_columns(entity_type, data)
        columns: list[str] = ["id", "is_available", "superseded_by"]
        values: list[Any] = [entity_id, is_available, superseded_by]
        for name, value in projected.items():
            columns.append(name)
            values.append(self._coerce_for_column(value))
        column_sql = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        cursor.execute(
            f'INSERT INTO "{entity_type}" ({column_sql}) VALUES ({placeholders})',
            values,
        )
        self._refresh_xref_rows(cursor, entity_type, entity_id)

    def _update_per_class(
        self,
        cursor: sqlite3.Cursor,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
    ) -> None:
        """Update user-slot columns on the per-class typed table.

        Replaces every user-slot column with the new value (including
        ``NULL`` for missing keys), mirroring the legacy semantics where
        ``entities.data`` was overwritten wholesale. System columns
        (``is_available``, ``superseded_by``) are handled by dedicated
        helpers. No-op when the class is not in the user schema.
        """
        if not self._per_class_table_exists(entity_type):
            return
        managed = {"id", "is_available", "superseded_by"}
        cols = [c for c in self._per_class_columns(entity_type) if c not in managed]
        if not cols:
            return
        sets = []
        params: list[Any] = []
        for c in cols:
            sets.append(f'"{c}" = ?')
            params.append(self._coerce_for_column(data.get(c)))
        params.append(entity_id)
        cursor.execute(
            f'UPDATE "{entity_type}" SET {", ".join(sets)} WHERE id = ?',
            params,
        )
        self._refresh_xref_rows(cursor, entity_type, entity_id)

    def _set_per_class_availability(
        self,
        cursor: sqlite3.Cursor,
        entity_type: str,
        entity_id: str,
        is_available: bool,
    ) -> None:
        """Flip ``is_available`` on the per-class table for one entity."""
        if not self._per_class_table_exists(entity_type):
            return
        cursor.execute(
            f'UPDATE "{entity_type}" SET "is_available" = ? WHERE id = ?',
            (1 if is_available else 0, entity_id),
        )
        # Availability transitions drive the xref index lifecycle: rows
        # are removed when the entity goes unavailable and re-derived
        # (re-checking global uniqueness) when it comes back.
        self._refresh_xref_rows(cursor, entity_type, entity_id)

    def _set_per_class_superseded_by(
        self,
        cursor: sqlite3.Cursor,
        entity_type: str,
        entity_id: str,
        replacement_id: Optional[str],
        is_available: Optional[bool] = None,
    ) -> None:
        """Record a supersession pointer on the per-class table."""
        if not self._per_class_table_exists(entity_type):
            return
        if is_available is None:
            cursor.execute(
                f'UPDATE "{entity_type}" SET "superseded_by" = ? WHERE id = ?',
                (replacement_id, entity_id),
            )
        else:
            cursor.execute(
                f'UPDATE "{entity_type}" SET "superseded_by" = ?, "is_available" = ?'
                f" WHERE id = ?",
                (replacement_id, 1 if is_available else 0, entity_id),
            )
            self._refresh_xref_rows(cursor, entity_type, entity_id)

    # -- hippo_external_xref side index (issue #48) ---------------------------
    #
    # Index rows exist ONLY for available entities (the `hippo_unique`
    # "unique among live records" semantics from PTS-348, realised here by
    # deleting/recreating rows on availability transitions instead of a
    # partial-index predicate). Every write-path hook re-derives the
    # entity's rows from the just-written per-class row, inside the SAME
    # transaction as the entity write — a uniqueness violation rolls the
    # whole write back.

    def _xref_slot_names(self, entity_type: str) -> list[str]:
        """Names of ``hippo_external_xref``-annotated slots for a class.

        Cached per class. Empty when the registry is absent, the class is
        unknown/abstract, or no slot carries the annotation.
        """
        cached = self._xref_slots_cache.get(entity_type)
        if cached is None:
            if not self._per_class_table_exists(entity_type):
                cached = []
            else:
                cached = [
                    slot.name
                    for slot in self.schema_registry.external_xref_slots(
                        entity_type
                    )
                ]
            self._xref_slots_cache[entity_type] = cached
        return cached

    def _refresh_xref_rows(
        self, cursor: sqlite3.Cursor, entity_type: str, entity_id: str
    ) -> None:
        """Re-derive an entity's ``hippo_xref_index`` rows from its stored row.

        Idempotent delete-then-insert: existing rows for the entity are
        removed, then re-inserted from the entity's current slot values —
        only when the row exists AND is available. Runs on the caller's
        cursor so it shares the entity write's transaction.

        Raises:
            XrefUniquenessError: When a ``(system, value)`` pair is
                already claimed by another available entity (or twice by
                this one). The caller's transaction rolls back.
        """
        slots = self._xref_slot_names(entity_type)
        if not slots:
            return

        cursor.execute(
            f"DELETE FROM {XREF_TABLE} WHERE entity_id = ?", (entity_id,)
        )

        cols = ", ".join(f'"{s}"' for s in slots)
        cursor.execute(
            f'SELECT {cols}, "is_available" FROM "{entity_type}" '
            f"WHERE id = ?",
            (entity_id,),
        )
        row = cursor.fetchone()
        if row is None or not row["is_available"]:
            return

        for slot in slots:
            for system, value in extract_xref_pairs(row[slot]):
                try:
                    cursor.execute(
                        f"INSERT INTO {XREF_TABLE} "
                        "(entity_id, entity_type, slot, system, value) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (entity_id, entity_type, slot, system, value),
                    )
                except sqlite3.IntegrityError as exc:
                    cursor.execute(
                        f"SELECT entity_id, entity_type FROM {XREF_TABLE} "
                        "WHERE system = ? AND value = ?",
                        (system, value),
                    )
                    conflict = cursor.fetchone()
                    holder_id = conflict["entity_id"] if conflict else None
                    holder_type = conflict["entity_type"] if conflict else None
                    if holder_id == entity_id:
                        detail = (
                            f"duplicated within entity {entity_id} "
                            f"({entity_type})"
                        )
                    else:
                        detail = (
                            f"already registered to entity {holder_id} "
                            f"({holder_type})"
                        )
                    raise XrefUniquenessError(
                        f"External reference (system={system!r}, "
                        f"value={value!r}) on {entity_type}.{slot} is "
                        f"{detail}; hippo_external_xref requires "
                        f"(system, value) to be globally unique among "
                        f"available entities.",
                        system=system,
                        value=value,
                        conflicting_entity_id=holder_id,
                        conflicting_entity_type=holder_type,
                    ) from exc

    def _xref_table_exists(self, cursor: sqlite3.Cursor) -> bool:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (XREF_TABLE,),
        )
        return cursor.fetchone() is not None

    def find_xref(self, system: str, value: str) -> Optional[dict[str, Any]]:
        """Reverse-lookup the available entity holding ``(system, value)``.

        Returns ``{"entity_id", "entity_type", "slot"}`` or ``None``. At
        most one match can exist — the side table enforces global
        uniqueness of ``(system, value)`` among available entities.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            if not self._xref_table_exists(cursor):
                return None
            cursor.execute(
                f"SELECT entity_id, entity_type, slot FROM {XREF_TABLE} "
                "WHERE system = ? AND value = ?",
                (system, value),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "entity_id": row["entity_id"],
                "entity_type": row["entity_type"],
                "slot": row["slot"],
            }

    def list_xrefs(self, entity_id: str) -> list[dict[str, Any]]:
        """All indexed ``hippo_external_xref`` pairs for an entity.

        Returns ``[{"slot", "system", "value"}, ...]`` (empty when the
        entity is unavailable, unknown, or has no annotated slots). The
        full ``ExternalReference`` values — including ``retrieved_at`` /
        ``version`` — live on the entity's slots; this is the index view.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            if not self._xref_table_exists(cursor):
                return []
            cursor.execute(
                f"SELECT slot, system, value FROM {XREF_TABLE} "
                "WHERE entity_id = ? ORDER BY slot, system, value",
                (entity_id,),
            )
            return [
                {
                    "slot": row["slot"],
                    "system": row["system"],
                    "value": row["value"],
                }
                for row in cursor.fetchall()
            ]

    def read(self, entity_id: str) -> Optional[SQLiteEntity]:
        """Read an entity by its ID (available entities only).

        Cross-class UUID lookup is served by ``_entity_registry``: the
        registered ``class_name`` for ``entity_id`` is read first, then
        the typed row comes from the per-class table. Returns ``None``
        when the entity is unknown or not currently available.
        """
        entity_type = self.resolve_type(entity_id)
        if entity_type is None:
            return None
        return self._read_per_class(entity_id, entity_type, only_available=True)

    def resolve_type(self, entity_id: str) -> Optional[str]:
        """Return the entity_type for a given UUID, or None if unknown.

        Looks up the class via the ``_entity_registry`` shadow table
        (sec9 §9.5). Includes entities regardless of availability — the
        type is still meaningful for archived / superseded rows.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT class_name FROM _entity_registry WHERE uuid = ?",
                (entity_id,),
            )
            row = cursor.fetchone()
            return row["class_name"] if row else None

    def resolve_types(self, entity_ids: list[str]) -> dict[str, str]:
        """Batch variant of ``resolve_type``. Returns a dict keyed by id.

        Unknown UUIDs are absent from the returned dict (not raised). One
        SQL round-trip regardless of input size.
        """
        if not entity_ids:
            return {}
        with self._transaction() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in entity_ids)
            cursor.execute(
                f"SELECT uuid, class_name FROM _entity_registry "
                f"WHERE uuid IN ({placeholders})",
                tuple(entity_ids),
            )
            return {row["uuid"]: row["class_name"] for row in cursor.fetchall()}

    def read_any(self, entity_id: str) -> Optional[SQLiteEntity]:
        """Read an entity by its ID, regardless of availability.

        This includes unavailable (soft-deleted, superseded) entities.
        """
        entity_type = self.resolve_type(entity_id)
        if entity_type is None:
            return None
        return self._read_per_class(entity_id, entity_type, only_available=False)

    def _read_per_class(
        self,
        entity_id: str,
        entity_type: str,
        only_available: bool,
    ) -> Optional[SQLiteEntity]:
        """Hydrate a SQLiteEntity from the per-class typed table.

        Returns ``None`` when the row is missing or (when
        ``only_available=True``) when it is unavailable. ``version`` is
        computed from the count of ``create``/``update`` ProvenanceRecord
        rows for the entity — see ``_compute_version``.
        """
        if not self._per_class_table_exists(entity_type):
            return None

        slot_columns = [
            c for c in self._per_class_columns(entity_type)
            if c not in {"id", "is_available", "superseded_by"}
        ]
        select_cols = ['"id"', '"is_available"', '"superseded_by"'] + [
            f'"{c}"' for c in slot_columns
        ]
        sql = (
            f"SELECT {', '.join(select_cols)} FROM \"{entity_type}\" "
            "WHERE id = ?"
        )
        if only_available:
            sql += " AND is_available = 1"

        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (entity_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            version = self._compute_version(cursor, entity_id)
            mv_refs = self._hydrate_derived_refs_batch(
                conn, entity_type, [entity_id]
            ).get(entity_id, {})

        boolean_cols = (
            self.schema_registry.boolean_slot_names(entity_type)
            if self.schema_registry is not None
            else set()
        )
        string_cols = (
            self.schema_registry.string_slot_names(entity_type)
            if self.schema_registry is not None
            else set()
        )
        data = {
            c: self._decode_column_value(
                    row[c],
                    is_boolean=c in boolean_cols,
                    is_string=c in string_cols,
                )
            for c in slot_columns
            if row[c] is not None
        }
        data.update(mv_refs)
        return SQLiteEntity(
            id=row["id"],
            entity_type=entity_type,
            is_available=bool(row["is_available"]),
            version=version,
            data=data,
            superseded_by=row["superseded_by"],
        )

    @staticmethod
    def _compute_version(cursor: sqlite3.Cursor, entity_id: str) -> int:
        """Derive the entity version from ProvenanceRecord.

        Counts the number of ``create``/``update`` records: the create
        gives version 1 and each subsequent update increments by 1. This
        replaces the legacy ``entities.version`` column dropped in PR 2.3.
        """
        cursor.execute(
            'SELECT COUNT(*) AS c FROM "ProvenanceRecord" '
            "WHERE entity_id = ? AND operation IN ('create', 'update')",
            (entity_id,),
        )
        row = cursor.fetchone()
        count = int(row["c"] if row else 0)
        return count if count > 0 else 1

    def update(self, entity: SQLiteEntity) -> SQLiteEntity:
        """Update an existing entity's typed columns.

        Performs a full-replacement update of the user-slot columns on
        the per-class typed table (when the class is known). Temporal
        tracking and version derivation live in ``ProvenanceRecord``;
        callers that want a provenance event must invoke ``update_data``
        or write the record themselves. This method is kept for protocol
        compliance and for callers that already produced their own
        ``ProvenanceRecord``.
        """
        entity_id = getattr(entity, "id", None)
        if not entity_id:
            return entity
        entity_type = getattr(entity, "entity_type", "") or ""
        data = getattr(entity, "data", None) or {}

        with self._transaction() as conn:
            cursor = conn.cursor()
            self._update_per_class(cursor, entity_type, entity_id, data)
            self._record_ref_checks(entity_type, entity_id, data)
        return entity

    def update_data(
        self,
        entity_id: str,
        entity_type: str,
        data: dict[str, Any],
        new_version: int,
        actor: Optional[str] = None,
        operation: str = "update",
        loader_context: Optional[tuple[str, str]] = None,
    ) -> None:
        """Update typed columns and record provenance.

        Used by ``IngestionService._put_with_sqlite``/`_replace_with_sqlite`
        and the supersede paths. Version is no longer stored — it is
        derived on read from ``ProvenanceRecord`` (see
        ``_compute_version``); the ``new_version`` argument is accepted
        for API stability but has no persistent effect.

        When ``loader_context`` is a ``(loader_name, version)`` tuple
        (set by ``MosaicClient.load_context()``), a row is appended to
        ``reference_write_log`` inside the same SQL transaction as the
        entity write (sec2 §2.14.9 / Decision 2.14.J).
        """
        del new_version  # version is derived from ProvenanceRecord on read.
        # Virtual inverse slots are derived, never written (ADR-0011).
        data = self._strip_inverse_slots(entity_type, data)
        with self._transaction() as conn:
            cursor = conn.cursor()
            self._update_per_class(cursor, entity_type, entity_id, data)
            self._record_ref_checks(entity_type, entity_id, data)

            provenance = self._get_provenance_store(conn)
            provenance.record(
                entity_id=entity_id,
                entity_type=entity_type,
                operation=operation,
                actor_id=actor,
                patch=data,
            )

            self._materialize_multivalued_refs(
                conn,
                entity_type,
                entity_id,
                data,
                reconcile=True,
                actor=actor,
            )

            if loader_context is not None:
                loader_name, version = loader_context
                cursor.execute(
                    "INSERT OR IGNORE INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (?, ?, ?, ?)",
                    (loader_name, version, entity_id, entity_type),
                )

    def set_availability(
        self,
        entity_id: str,
        entity_type: str,
        is_available: bool,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Flip ``is_available`` on the per-class typed table.

        Records an ``availability_change`` provenance entry. Caller is
        responsible for ensuring the entity exists; this method does not
        raise on missing rows so that bulk loops can report per-entity
        success/failure.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            self._set_per_class_availability(cursor, entity_type, entity_id, is_available)

            patch: dict[str, Any] = {"is_available": is_available}
            if reason is not None:
                patch["reason"] = reason
            provenance = self._get_provenance_store(conn)
            provenance.record(
                entity_id=entity_id,
                entity_type=entity_type,
                operation="availability_change",
                actor_id=actor,
                patch=patch,
            )

    def mark_superseded(
        self,
        entity_id: str,
        entity_type: str,
        replacement_id: str,
    ) -> None:
        """Mark an entity as superseded on the per-class typed table.

        Sets ``is_available = 0`` and ``superseded_by = replacement_id``
        on the per-class typed row. Provenance is the caller's
        responsibility — supersession currently writes multiple
        ``ProvenanceRecord`` rows around this mutation (sec9 §9.6) which
        would be awkward to inline here.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            self._set_per_class_superseded_by(
                cursor, entity_type, entity_id, replacement_id, is_available=False
            )

    def delete(self, entity_id: str, user_context: Optional[str] = None) -> bool:
        """Delete an entity by its ID (soft delete).

        Looks up the entity's class via ``resolve_type``, flips
        ``is_available = 0`` on the per-class typed table, then records
        an ``availability_change`` provenance entry whose patch carries
        the original payload (for round-trip reconstruction). Returns
        ``False`` if the entity is unknown or already unavailable.
        """
        existing = self.read(entity_id)
        if existing is None:
            return False

        # ``id`` is a system column on the per-class table, not part of
        # ``existing.data``. Re-inject it so the soft-delete provenance
        # patch preserves the full entity payload for round-trip
        # reconstruction by callers that inspect ``patch.data``.
        snapshot = {
            "id": entity_id,
            **self._strip_inverse_slots(existing.entity_type, existing.data),
        }

        with self._transaction() as conn:
            cursor = conn.cursor()
            self._set_per_class_availability(
                cursor, existing.entity_type, entity_id, False
            )

            provenance = self._get_provenance_store(conn)
            provenance.record(
                entity_id=entity_id,
                entity_type=existing.entity_type,
                operation="availability_change",
                actor_id=user_context,
                patch={
                    "status": "deleted",
                    "is_available": False,
                    "data": snapshot,
                },
            )

            return True

    def find(
        self, query: Query, *, as_of: Optional[str] = None
    ) -> Iterator[SQLiteEntity]:
        """Find entities matching a query.

        When ``query.entity_type`` is set the query targets the
        corresponding per-class typed table and filters on real columns;
        if the type is unknown to the schema, the result is empty.
        Without ``entity_type``, the adapter scans every concrete
        per-class table in the schema and merges results (cross-class
        scan).

        When ``as_of`` (ISO-8601) is given, the result is reconstructed from the
        provenance log as the graph stood at that transaction-time — query-spanning
        as-of reconstruction (sec6 §6.8 / ADR-0001). Omitted = current state.
        """
        if as_of is not None:
            yield from self._find_as_of(query, as_of)
            return
        if query.entity_type:
            if not self._per_class_table_exists(query.entity_type):
                return
            yield from self._find_per_class(query.entity_type, query)
            return

        registry = self.schema_registry
        if registry is None:
            return
        for class_name in registry.class_names():
            if not self._per_class_table_exists(class_name):
                continue
            yield from self._find_per_class(class_name, query)

    def _find_as_of(
        self, query: Query, as_of: str
    ) -> Iterator[SQLiteEntity]:
        """Query-spanning as-of reconstruction (sec6 §6.8).

        Candidate entities are those created at-or-before ``as_of`` (optionally
        scoped to ``query.entity_type``); each is reconstructed to its state at
        ``as_of`` via the provenance log (``get_state_at`` — ``None`` if it was
        unavailable/deleted then); filters apply to the reconstructed data via
        the shared operator evaluator; offset/limit are applied last.

        Filters and pagination run in Python over the reconstructed records
        (correctness-first; the §6.8.4 indexed/cached fast path is a later
        optimization). Relationship-existence filters and cross-class temporal
        joins are out of scope for this increment (BU-Neuromics/hippo#71).
        """
        if getattr(query, "order_by", None):
            from mosaic.core.exceptions import ValidationError

            raise ValidationError(
                message=(
                    "order_by is not supported together with as_of: ordering "
                    "pushdown targets current-state typed columns, and the "
                    "reconstructed as-of path keeps its documented Python "
                    "ordering (ADR-0007 gate decision)."
                ),
                field_name="order_by",
            )
        where = getattr(query, "where", None)
        if where is not None and has_relationship_predicate(
            normalize_where(where)
        ):
            from mosaic.core.exceptions import ValidationError

            raise ValidationError(
                message=(
                    "Relationship predicates are not supported together "
                    "with as_of: cross-class temporal joins are out of "
                    "scope for the reconstruction path (hippo#71 / "
                    "ADR-0006 M5a)."
                ),
                field_name="where",
            )
        with self._transaction() as conn:
            prov = self._get_provenance_store(conn)
            candidates = prov.entities_created_by(
                as_of, entity_type=query.entity_type
            )

            matched: list[SQLiteEntity] = []
            for entity_id, entity_type in candidates:
                reconstructed = prov.get_state_at(entity_id, as_of)
                if reconstructed is None:
                    continue  # not created yet, or unavailable/deleted at as_of
                data = reconstructed.get("state")
                if not isinstance(data, dict):
                    continue
                if not self._matches_filters(data, entity_id, query):
                    continue
                matched.append(
                    SQLiteEntity(
                        id=entity_id,
                        entity_type=entity_type,
                        is_available=True,
                        version=prov.state_version_at(entity_id, as_of),
                        data=data,
                    )
                )

        offset = query.offset or 0
        sliced = matched[offset:]
        if query.limit is not None:  # limit=0 → zero rows, not unlimited (#130)
            sliced = sliced[: query.limit]
        yield from sliced

    @staticmethod
    def _matches_filters(
        data: dict[str, Any], entity_id: str, query: Query
    ) -> bool:
        """Match ``query.filters`` against a reconstructed ``data`` dict,
        honoring ``filter_mode`` — the as-of (Python-side) analogue of
        ``_find_per_class``'s column predicates. ``id`` resolves to ``entity_id``.

        Operator semantics live once in
        :func:`mosaic.core.storage.matches_operator`, shared with the
        Postgres mirror so as-of results never diverge from the SQL
        builders (ADR-0006).
        """
        where = getattr(query, "where", None)
        if where is not None and not matches_tree(
            data, entity_id, normalize_where(where)
        ):
            return False

        filters = query.filters or []
        if not filters:
            return True

        checks: list[bool] = []
        for f in filters:
            for field, op, value in normalize_filter(f):
                actual = entity_id if field == "id" else data.get(field)
                checks.append(matches_operator(actual, op, value))
        if not checks:
            return True
        mode = getattr(query, "filter_mode", "and")
        return any(checks) if mode == "or" else all(checks)

    def _leaf_predicate(
        self, field: str, op: str, value: Any, params: list[Any], qual: str = ""
    ) -> str:
        """SQL predicate for one ``(field, op, value)`` leaf on a per-class
        typed table, appending its parameters to ``params``. Shared by the
        flat ``filters`` path and the ``where`` tree compiler so operator
        rendering lives once (ADR-0006). ``qual`` scopes the column to a
        table alias inside relationship EXISTS subqueries (M5a)."""
        col = f'{qual}"{field}"'
        if op == "in":
            if not value:
                # Empty IN-list: short-circuit to "no rows match" rather
                # than emitting invalid `IN ()` SQL.
                return "0"
            placeholders = ", ".join("?" for _ in value)
            params.extend(self._coerce_for_column(v) for v in value)
            return f"{col} IN ({placeholders})"
        if op == "is_null":
            return f"{col} IS NULL" if value else f"{col} IS NOT NULL"
        if op == "contains":
            # Case-insensitive substring (ASCII case folding — SQLite LIKE
            # semantics). Pattern metacharacters in the value are literals,
            # hence the ESCAPE clause.
            params.append(f"%{escape_like(str(value))}%")
            return f"{col} LIKE ? ESCAPE '\\'"
        if op in COMPARISON_SQL_OPS:
            # NULL columns never satisfy a comparison (SQL three-valued
            # logic) — mirrored by ``matches_operator`` on the as-of path.
            params.append(self._coerce_for_column(value))
            return f"{col} {COMPARISON_SQL_OPS[op]} ?"
        params.append(self._coerce_for_column(value))
        return f"{col} = ?"

    def _reference_edge(
        self, entity_type: str, edge: str, quantifier: Optional[str]
    ) -> tuple[str, bool, Optional[str]]:
        """Resolve a relationship-predicate ``edge`` (M5a/M5b) to
        ``(target_class, multivalued, inverse_of)``, raising loudly on an
        unknown edge or a quantifier/cardinality mismatch: to-one edges
        take the bare ``{edge, where}`` shape; to-many (relationship-backed
        multivalued) edges require a ``some``/``none`` quantifier.

        ``inverse_of`` is the forward FK slot on ``target_class`` when
        ``edge`` is a virtual inverse slot (ADR-0011) — also to-many, also
        quantified, but compiled against the target table's FK column
        rather than the relationships link table — and ``None`` otherwise.
        """
        from mosaic.core.exceptions import ValidationError

        registry = self.schema_registry
        refs: dict[str, tuple[str, bool]] = {}
        if registry is not None:
            known = set(registry.class_names())
            for slot in registry.induced_slots(entity_type):
                if slot.range in known:
                    refs[slot.name] = (slot.range, bool(slot.multivalued))
        if edge not in refs:
            raise ValidationError(
                message=(
                    f"Unknown relationship-predicate edge {edge!r} for "
                    f"{entity_type}; reference slots: {sorted(refs)}."
                ),
                field_name=edge,
            )
        target, multivalued = refs[edge]
        inverse = self._inverse_slots(entity_type).get(edge)
        inverse_of = inverse.forward_slot if inverse is not None else None
        if multivalued and quantifier is None:
            raise ValidationError(
                message=(
                    f"{entity_type}.{edge} is a multivalued reference edge: "
                    f"quantify it with 'some' or 'none' (ADR-0006 M5b)."
                ),
                field_name=edge,
            )
        if not multivalued and quantifier is not None:
            raise ValidationError(
                message=(
                    f"{entity_type}.{edge} is a to-one reference edge: it "
                    f"takes the bare {{edge, where}} predicate, not a "
                    f"some/none quantifier (ADR-0006 M5a)."
                ),
                field_name=edge,
            )
        return target, multivalued, inverse_of

    def _tree_predicate(
        self,
        node: dict[str, Any],
        valid_columns: set[str],
        params: list[Any],
        *,
        entity_type: str,
        scope: str = "",
        alias_seq: Optional[list[int]] = None,
    ) -> str:
        """Compile a normalized ``where`` tree to one SQL predicate.

        Unlike the flat path's legacy answer-with-zero-rows behavior, an
        unknown field inside a tree raises: a bad leaf under ``or``/``not``
        cannot be expressed as "match nothing" without changing the tree's
        meaning.

        ``scope`` is the SQL prefix (``'"Table".'`` or ``'alias.'``) of the
        table the current subtree addresses; ``alias_seq`` numbers the
        relationship-EXISTS aliases so nested and self-referential edges
        (M5a) never collide.
        """
        if alias_seq is None:
            alias_seq = [0]
        if "and" in node:
            return (
                "("
                + " AND ".join(
                    self._tree_predicate(
                        c, valid_columns, params,
                        entity_type=entity_type, scope=scope,
                        alias_seq=alias_seq,
                    )
                    for c in node["and"]
                )
                + ")"
            )
        if "or" in node:
            return (
                "("
                + " OR ".join(
                    self._tree_predicate(
                        c, valid_columns, params,
                        entity_type=entity_type, scope=scope,
                        alias_seq=alias_seq,
                    )
                    for c in node["or"]
                )
                + ")"
            )
        if "not" in node:
            return "NOT " + self._tree_predicate(
                node["not"], valid_columns, params,
                entity_type=entity_type, scope=scope, alias_seq=alias_seq,
            )
        if "edge" in node:
            # Relationship predicate (ADR-0006 M5a/M5b). To-one: a
            # single-valued reference is a column holding the target id,
            # so the nested tree compiles to ONE correlated EXISTS against
            # the target's per-class table keyed on that FK column.
            # To-many (quantified): the ADR-0002 link table holds the
            # edges, so `some` is EXISTS over relationships joined to the
            # target table and `none` its NOT EXISTS complement.
            # Availability applies to edge and target exactly as list
            # queries and hydration see them.
            edge = node["edge"]
            quantifier = node.get("quantifier")
            target, multivalued, inverse_of = self._reference_edge(
                entity_type, edge, quantifier
            )
            alias_seq[0] += 1
            alias = f"rel{alias_seq[0]}"
            outer = scope or f'"{entity_type}".'
            target_columns = self._valid_query_columns(target)
            if inverse_of is not None:
                inv = self._inverse_slots(entity_type)[edge]
                tables = self._inverse_tables(inv)
                if tables != [target]:
                    return self._inverse_closure_predicate(
                        node, tables, inverse_of, quantifier, outer, params,
                        alias_seq=alias_seq,
                    )
                # Reverse FK edge (ADR-0011): the relationship is stored on
                # the *target* row as a single-valued FK column pointing at
                # this entity, so `some` is EXISTS over the target table
                # keyed on that column and `none` its NOT EXISTS complement.
                # No link table is involved; availability applies to the
                # target exactly as list queries see it.
                sub = self._tree_predicate(
                    node["where"], target_columns, params,
                    entity_type=target, scope=f"{alias}.", alias_seq=alias_seq,
                )
                exists = (
                    f'EXISTS (SELECT 1 FROM "{target}" {alias} '
                    f'WHERE {alias}."{inverse_of}" = {outer}"id" '
                    f"AND {alias}.is_available = 1 AND {sub})"
                )
                return exists if quantifier == "some" else f"NOT {exists}"
            if not multivalued:
                sub = self._tree_predicate(
                    node["where"], target_columns, params,
                    entity_type=target, scope=f"{alias}.", alias_seq=alias_seq,
                )
                return (
                    f'EXISTS (SELECT 1 FROM "{target}" {alias} '
                    f'WHERE {alias}."id" = {outer}"{edge}" '
                    f"AND {alias}.is_available = 1 AND {sub})"
                )
            link = f"{alias}l"
            params.append(edge)
            sub = self._tree_predicate(
                node["where"], target_columns, params,
                entity_type=target, scope=f"{alias}.", alias_seq=alias_seq,
            )
            exists = (
                f"EXISTS (SELECT 1 FROM relationships {link} "
                f'JOIN "{target}" {alias} ON {alias}."id" = {link}.target_id '
                f'WHERE {link}.source_id = {outer}"id" '
                f"AND {link}.relationship_type = ? "
                f"AND {link}.is_available = 1 "
                f"AND {alias}.is_available = 1 AND {sub})"
            )
            return exists if quantifier == "some" else f"NOT {exists}"
        field = node["field"]
        if field not in valid_columns:
            from mosaic.core.exceptions import ValidationError

            raise ValidationError(
                message=(
                    f"Unknown filter field {field!r} in where tree; valid "
                    f"columns: {sorted(valid_columns)}."
                ),
                field_name=field,
            )
        # COALESCE to two-valued logic: a NULL column makes `col = ?`
        # evaluate to SQL NULL, and `NOT NULL` is NULL — which would
        # silently exclude rows under `not` that the Python mirror
        # (``matches_tree``: absent value => leaf is False, `not` flips to
        # True) includes. Forcing every leaf to TRUE/FALSE keeps the tree
        # boolean end to end (ADR-0006's mirror-consistency rule).
        leaf = self._leaf_predicate(
            field, node["op"], node["value"], params, qual=scope
        )
        return f"COALESCE(({leaf}), 0)"

    def _inverse_closure_predicate(
        self,
        node: dict[str, Any],
        tables: list[str],
        inverse_of: str,
        quantifier: Optional[str],
        outer: str,
        params: list[Any],
        *,
        alias_seq: list[int],
    ) -> str:
        """Reverse FK edge over an abstract or subclassed range (issue #224).

        The hit may live in any concrete table of the range's closure, so
        ``some`` is an OR of one correlated EXISTS per table and ``none``
        its negation. The nested tree may only name fields every table in
        the closure owns (the range's own slots); a subclass-only field is
        rejected rather than silently matching a subset of tables. With no
        table at all, ``some`` is false and ``none`` true.
        """
        if not tables:
            return "0" if quantifier == "some" else "1"
        shared: Optional[set[str]] = None
        for table in tables:
            cols = self._valid_query_columns(table)
            shared = set(cols) if shared is None else shared & set(cols)
        clauses = []
        for table in tables:
            alias_seq[0] += 1
            alias = f"rel{alias_seq[0]}"
            sub = self._tree_predicate(
                node["where"], shared or set(), params,
                entity_type=table, scope=f"{alias}.", alias_seq=alias_seq,
            )
            clauses.append(
                f'EXISTS (SELECT 1 FROM "{table}" {alias} '
                f'WHERE {alias}."{inverse_of}" = {outer}"id" '
                f"AND {alias}.is_available = 1 AND {sub})"
            )
        some = "(" + " OR ".join(clauses) + ")"
        return some if quantifier == "some" else f"NOT {some}"

    def _query_predicate(
        self,
        entity_type: str,
        query: Query,
        valid_columns: set[str],
        params: list[Any],
    ) -> Optional[str]:
        """The WHERE clause (sans leading keyword) ``find``/``count``/the
        aggregates all share, so every surface sees exactly the same rows
        (ADR-0007's availability-consistency rule). ``None`` = an unknown
        flat-path field: match zero rows (legacy #149 behavior)."""
        clauses = ["is_available = 1"]
        if query.filters:
            joiner = " OR " if getattr(query, "filter_mode", "and") == "or" else " AND "
            filter_clauses = []
            for f in query.filters:
                for field, op, value in normalize_filter(f):
                    if field not in valid_columns:
                        # Legacy flat-path behavior: an unknown field
                        # answers with zero rows (GraphQL rejects it far
                        # earlier with a coded error, #149).
                        return None
                    filter_clauses.append(
                        self._leaf_predicate(field, op, value, params)
                    )
            if filter_clauses:
                clauses.append("(" + joiner.join(filter_clauses) + ")")

        where = getattr(query, "where", None)
        if where is not None:
            # Boolean filter tree (ADR-0006 increments 2–3); composes with
            # the flat ``filters`` by AND.
            tree = normalize_where(where)
            clauses.append(
                "("
                + self._tree_predicate(
                    tree, valid_columns, params, entity_type=entity_type
                )
                + ")"
            )
        return " AND ".join(clauses)

    def _order_clause(
        self, query: Query, valid_columns: set[str]
    ) -> str:
        """SQL ORDER BY for ``query.order_by`` (ADR-0007): stable ``id``
        tiebreak; NULLs last regardless of direction (matching Postgres so
        orderings never diverge across backends)."""
        order_by = getattr(query, "order_by", None)
        if not order_by:
            return ""
        if order_by not in valid_columns:
            from mosaic.core.exceptions import ValidationError

            raise ValidationError(
                message=(
                    f"Unknown order_by column {order_by!r}; orderable "
                    f"columns: {sorted(valid_columns)}. Computed temporal "
                    f"fields are provenance-derived, not columns, and "
                    f"cannot be ordered on (ADR-0007)."
                ),
                field_name=order_by,
            )
        direction = (
            "DESC" if getattr(query, "order_dir", "asc") == "desc" else "ASC"
        )
        return (
            f' ORDER BY ("{order_by}" IS NULL) ASC, '
            f'"{order_by}" {direction}, "id" ASC'
        )

    def _find_per_class(
        self, entity_type: str, query: Query
    ) -> Iterator[SQLiteEntity]:
        """Query a single per-class typed table.

        Filters become column-level predicates; ``data`` for the
        returned :class:`SQLiteEntity` is reconstructed from the typed
        row. ``version`` is derived from ``ProvenanceRecord`` (see
        ``_compute_version``).
        """
        slot_columns = [
            c for c in self._per_class_columns(entity_type)
            if c not in {"id", "is_available", "superseded_by"}
        ]
        select_cols = ['"id"', '"is_available"', '"superseded_by"'] + [
            f'"{c}"' for c in slot_columns
        ]
        params: list[Any] = []
        valid_columns = set(slot_columns) | {"id", "is_available"}
        predicate = self._query_predicate(entity_type, query, valid_columns, params)
        if predicate is None:
            return
        sql = (
            f"SELECT {', '.join(select_cols)} FROM \"{entity_type}\" "
            f"WHERE {predicate}"
        )
        sql += self._order_clause(query, valid_columns)

        # `is not None`, not truthiness: LIMIT 0 must return zero rows, not
        # fall through to "no limit" (issue #130).
        if query.limit is not None:
            sql += f" LIMIT {query.limit}"
            if query.offset:
                sql += f" OFFSET {query.offset}"
        elif query.offset:
            sql += f" LIMIT -1 OFFSET {query.offset}"

        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()

            version_map: dict[str, int] = {}
            row_ids = [r["id"] for r in rows]
            if row_ids:
                placeholders = ",".join("?" for _ in row_ids)
                cursor.execute(
                    f'SELECT entity_id, COUNT(*) AS c FROM "ProvenanceRecord" '
                    f"WHERE entity_id IN ({placeholders}) "
                    f"AND operation IN ('create', 'update') "
                    f"GROUP BY entity_id",
                    tuple(row_ids),
                )
                version_map = {
                    r["entity_id"]: int(r["c"]) for r in cursor.fetchall()
                }

            mv_refs_map = self._hydrate_derived_refs_batch(
                conn, entity_type, row_ids
            )

        boolean_cols = (
            self.schema_registry.boolean_slot_names(entity_type)
            if self.schema_registry is not None
            else set()
        )
        string_cols = (
            self.schema_registry.string_slot_names(entity_type)
            if self.schema_registry is not None
            else set()
        )
        for row in rows:
            data = {
                c: self._decode_column_value(
                    row[c],
                    is_boolean=c in boolean_cols,
                    is_string=c in string_cols,
                )
                for c in slot_columns
                if row[c] is not None
            }
            data.update(mv_refs_map.get(row["id"], {}))
            yield SQLiteEntity(
                id=row["id"],
                entity_type=entity_type,
                is_available=bool(row["is_available"]),
                version=version_map.get(row["id"], 1),
                data=data,
                superseded_by=row["superseded_by"],
            )

    def _valid_query_columns(self, entity_type: str) -> set[str]:
        """Columns filters/ordering/aggregation may address on a per-class
        table — the slot columns plus ``id``/``is_available`` (identical to
        the set ``_find_per_class`` validates against, by construction)."""
        slot_columns = {
            c for c in self._per_class_columns(entity_type)
            if c not in {"id", "is_available", "superseded_by"}
        }
        return slot_columns | {"id", "is_available"}

    def count(self, query: Query, *, as_of: Optional[str] = None) -> int:
        """Count matching entities without materializing them (ADR-0007).

        Pushed down as ``COUNT(*)`` under the exact predicate ``find`` uses
        (availability-consistency rule). ``query.limit``/``offset`` are
        ignored — a count is over the whole match set. Under ``as_of`` the
        count is the length of the reconstructed match set (the documented
        Python-path semantics; no cheap pushdown exists over the provenance
        log yet).
        """
        if as_of is not None:
            unpaged = Query(
                entity_type=query.entity_type,
                filters=query.filters,
                filter_mode=query.filter_mode,
                where=query.where,
            )
            return sum(1 for _ in self._find_as_of(unpaged, as_of))

        if query.entity_type:
            class_names = [query.entity_type]
        elif self.schema_registry is not None:
            class_names = list(self.schema_registry.class_names())
        else:
            return 0

        total = 0
        for class_name in class_names:
            if not self._per_class_table_exists(class_name):
                continue
            params: list[Any] = []
            predicate = self._query_predicate(
                class_name, query, self._valid_query_columns(class_name), params
            )
            if predicate is None:
                continue  # unknown flat-path field: zero rows (#149)
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f'SELECT COUNT(*) AS c FROM "{class_name}" '
                    f"WHERE {predicate}",
                    params,
                )
                total += int(cursor.fetchone()["c"])
        return total

    def facet_counts(self, query: Query, field: str) -> List[tuple]:
        """Per-value counts for ``field`` under ``query``'s predicate —
        ``[(value, count), ...]``, count desc then value asc (ADR-0007).

        NULL/absent values are not counted (absence is queried with
        ``is_null``, not enumerated as a facet bucket). Requires
        ``query.entity_type`` — facets are per-class, over that class's
        typed columns.
        """
        entity_type, valid_columns = self._require_aggregate_field(
            query, field, surface="facet_counts"
        )
        if not self._per_class_table_exists(entity_type):
            return []
        params: list[Any] = []
        predicate = self._query_predicate(
            entity_type, query, valid_columns, params
        )
        if predicate is None:
            return []
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT "{field}" AS value, COUNT(*) AS c '
                f'FROM "{entity_type}" '
                f'WHERE {predicate} AND "{field}" IS NOT NULL '
                f'GROUP BY "{field}" '
                f'ORDER BY c DESC, "{field}" ASC',
                params,
            )
            rows = cursor.fetchall()
        decode = self._aggregate_value_decoder(entity_type, field)
        return [(decode(row["value"]), int(row["c"])) for row in rows]

    def field_range(self, query: Query, field: str) -> tuple:
        """``(min, max)`` of ``field`` under ``query``'s predicate
        (ADR-0007); ``(None, None)`` when no matching entity has a value."""
        entity_type, valid_columns = self._require_aggregate_field(
            query, field, surface="field_range"
        )
        if not self._per_class_table_exists(entity_type):
            return (None, None)
        params: list[Any] = []
        predicate = self._query_predicate(
            entity_type, query, valid_columns, params
        )
        if predicate is None:
            return (None, None)
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT MIN("{field}") AS lo, MAX("{field}") AS hi '
                f'FROM "{entity_type}" WHERE {predicate}',
                params,
            )
            row = cursor.fetchone()
        decode = self._aggregate_value_decoder(entity_type, field)
        lo = decode(row["lo"]) if row["lo"] is not None else None
        hi = decode(row["hi"]) if row["hi"] is not None else None
        return (lo, hi)

    def count_relationship(
        self, entity_type: str, entity_id: str, edge: str
    ) -> int:
        """Cheap cardinality of a multivalued reference edge (issue #132).

        Single indexed ``COUNT(*)`` over the relationships table joined to
        the target's per-class table — the same shape M5b's ``some``
        quantifier compiles to (``_reference_edge`` rejects an unknown or
        to-one edge with the identical errors that path already raises).
        """
        target, _, inverse_of = self._reference_edge(entity_type, edge, "some")
        if inverse_of is not None:
            # Virtual inverse slot (ADR-0011): count the available target
            # rows whose forward FK points at this entity, summed across
            # the range's concrete closure (issue #224).
            tables = self._inverse_tables(self._inverse_slots(entity_type)[edge])
            if not tables:
                return 0
            with self._transaction() as conn:
                cursor = conn.cursor()
                total = 0
                for table in tables:
                    cursor.execute(
                        f'SELECT COUNT(*) AS c FROM "{table}" tgt '
                        f'WHERE tgt."{inverse_of}" = ? AND tgt.is_available = 1',
                        (entity_id,),
                    )
                    total += int(cursor.fetchone()["c"])
                return total
        if not self._per_class_table_exists(target):
            return 0
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT COUNT(*) AS c FROM relationships rel '
                f'JOIN "{target}" tgt ON tgt."id" = rel.target_id '
                f"WHERE rel.source_id = ? AND rel.relationship_type = ? "
                f"AND rel.is_available = 1 AND tgt.is_available = 1",
                (entity_id, edge),
            )
            return int(cursor.fetchone()["c"])

    def _require_aggregate_field(
        self, query: Query, field: str, *, surface: str
    ) -> tuple[str, set[str]]:
        """Shared validation for the per-field aggregates: an entity type is
        required and ``field`` must be one of its typed columns (computed
        temporal fields are provenance-derived, not columns — ADR-0007)."""
        from mosaic.core.exceptions import ValidationError

        if not query.entity_type:
            raise ValidationError(
                message=(
                    f"{surface}() requires an entity type: facets and "
                    f"ranges are per-class aggregates over typed columns."
                ),
                field_name=field,
            )
        valid_columns = self._valid_query_columns(query.entity_type)
        if field not in valid_columns:
            raise ValidationError(
                message=(
                    f"Unknown {surface} field {field!r} for "
                    f"{query.entity_type}; valid columns: "
                    f"{sorted(valid_columns)}. Computed temporal fields are "
                    f"provenance-derived, not columns, and cannot be "
                    f"aggregated on (ADR-0007)."
                ),
                field_name=field,
            )
        return query.entity_type, valid_columns

    def _aggregate_value_decoder(self, entity_type: str, field: str):
        """Value decoder for aggregate results on one column — the same
        boolean/string-aware decoding ``_find_per_class`` applies to row
        values, so facet buckets equal the values list queries return."""
        is_boolean = (
            self.schema_registry is not None
            and field in self.schema_registry.boolean_slot_names(entity_type)
        )
        is_string = (
            self.schema_registry is not None
            and field in self.schema_registry.string_slot_names(entity_type)
        )
        return lambda v: self._decode_column_value(
            v, is_boolean=is_boolean, is_string=is_string
        )

    @staticmethod
    def _decode_column_value(
        value: Any, is_boolean: bool = False, is_string: bool = False
    ) -> Any:
        """Best-effort reverse of ``_coerce_for_column`` for query results.

        SQLite stores JSON-encoded containers and 0/1 booleans as text /
        integers; downstream consumers expect dict-shaped payloads with
        Python-native values. Strings that parse as JSON arrays/objects
        are decoded; other values pass through unchanged.

        ``is_boolean`` must be set for columns backing a ``range: boolean``
        slot: ``_coerce_for_column`` stored the ``bool`` as integer ``0``/
        ``1``, and only the schema knows the column is boolean — without
        this reversal, LinkML validation (migration gate, end-to-end gate)
        rejects the raw integer for a boolean slot (PTS-349). The integer
        guard leaves multivalued booleans untouched: they arrive as JSON
        strings and fall through to the JSON branch with native ``bool``s.

        ``is_string`` must be set for columns backing a scalar
        ``range: string`` slot: their text is user data that must
        round-trip verbatim, even when it happens to parse as a JSON
        container (Aperture's control-plane ``payload`` carries serialized
        ``{"v": …, "data": …}`` envelopes that the JSON branch would
        otherwise corrupt into dicts, which the GraphQL String type then
        refuses to serialize).
        """
        if is_boolean and isinstance(value, int) and not isinstance(value, bool):
            return bool(value)
        if is_string:
            return value
        if isinstance(value, str) and value and value[0] in "[{":
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
        return value

    def findAll(self) -> Iterator[SQLiteEntity]:
        """Find all entities."""
        return self.find(Query())

    def findBy(self, **kwargs: Any) -> Iterator[SQLiteEntity]:
        """Find entities by field values."""
        query = Query()
        query.filters = [kwargs]
        return self.find(query)

    def track_creation(
        self, entity: SQLiteEntity, metadata: Dict[str, Any]
    ) -> ProvenanceRecordType:
        """Track the creation of an entity (in-memory record, no DB write)."""
        return ProvenanceRecordType(
            timestamp=datetime.now(timezone.utc),
            operation="create",
            entity_type=type(entity).__name__,
            entity_id=entity.id,
            actor_id="",
            schema_version=self._schema_version,
            patch=metadata,
        )

    def track_update(
        self, entity: SQLiteEntity, metadata: Dict[str, Any]
    ) -> ProvenanceRecordType:
        """Track the update of an entity (in-memory record, no DB write)."""
        return ProvenanceRecordType(
            timestamp=datetime.now(timezone.utc),
            operation="update",
            entity_type=type(entity).__name__,
            entity_id=entity.id,
            actor_id="",
            schema_version=self._schema_version,
            patch=metadata,
        )

    def track_deletion(
        self, entity_id: str, metadata: Dict[str, Any]
    ) -> ProvenanceRecordType:
        """Track the deletion of an entity (in-memory record, no DB write)."""
        return ProvenanceRecordType(
            timestamp=datetime.now(timezone.utc),
            operation="availability_change",
            entity_type="unknown",
            entity_id=entity_id,
            actor_id="",
            schema_version=self._schema_version,
            patch={"status": "deleted", **metadata},
        )

    def get_journal_mode(self) -> str:
        """Get the current journal mode."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            return cursor.fetchone()[0]

    def checkpoint(self) -> bool:
        """Execute a WAL checkpoint."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA wal_checkpoint(FULL)")
            return cursor.fetchone()[0] == 1

    def get_wal_file_size(self) -> int:
        """Get the size of the WAL file if it exists."""
        wal_path = self.database_path.with_suffix(".db-wal")
        if wal_path.exists():
            return wal_path.stat().st_size
        return 0

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None

    def history(self, entity_id: str) -> list[dict[str, Any]]:
        """Get the change history for an entity.

        Args:
            entity_id: The ID of the entity.

        Returns:
            List of history records in chronological order.
        """
        with self._transaction() as conn:
            provenance = self._get_provenance_store(conn)
            return provenance.get_history(entity_id)

    def get_temporal(
        self, entity_ids: list[str], *, as_of: Optional[str] = None
    ) -> "dict[str, TemporalRecord]":
        """Batch sec9 §9.7 temporal-field derivation. One SQL round-trip.

        ``as_of`` (ISO-8601) bounds the derivation to ``timestamp <= as_of``
        (transaction-time as-of, sec6 §6.8).
        """
        with self._transaction() as conn:
            provenance = self._get_provenance_store(conn)
            return provenance.get_temporal(entity_ids, as_of=as_of)

    def state_at(self, entity_id: str, timestamp: str) -> Optional[dict[str, Any]]:
        """Get the entity state at a specific point in time.

        Args:
            entity_id: The ID of the entity.
            timestamp: ISO format timestamp to query.

        Returns:
            The entity state at that time, or None if not found.
        """
        with self._transaction() as conn:
            provenance = self._get_provenance_store(conn)

            creation_time = provenance.get_entity_creation_time(entity_id)
            if creation_time is None:
                return None

            requested_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            creation_dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))

            if requested_dt < creation_dt:
                from mosaic.core.exceptions import TemporalQueryError

                raise TemporalQueryError(
                    message=f"Timestamp {timestamp} is before entity creation time {creation_time}",
                    entity_id=entity_id,
                    requested_timestamp=timestamp,
                    entity_creation_time=creation_time,
                )

            return provenance.get_state_at(entity_id, timestamp)

    def search_capabilities(self) -> set[str]:
        """Return the set of search modes supported by this adapter.

        The SQLite adapter supports full-text search (FTS5).

        Returns:
            A set containing "fts" to indicate FTS5 support.
        """
        return {"fts"}

    def entity_counts(self) -> dict[str, int]:
        """Count stored entities per entity type.

        Counts come from the ``_entity_registry`` shadow table (one row
        per created entity). Because Hippo never hard-deletes, the count
        for a type includes unavailable (soft-deleted / superseded)
        entities.

        Returns:
            Mapping of entity type name → total entity count, for types
            with at least one entity.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT class_name, COUNT(*) AS c FROM _entity_registry "
                "GROUP BY class_name ORDER BY class_name"
            )
            return {row["class_name"]: row["c"] for row in cursor.fetchall()}

    def explain_query(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        """Explain the execution plan for a SQL query.

        This method uses SQLite's EXPLAIN QUERY PLAN functionality to analyze
        how a query will be executed, which helps determine if partial indexes
        are being utilized effectively.

        Args:
            sql: The SQL query to explain.
            params: Optional parameters for the query.

        Returns:
            List of dictionaries containing the execution plan details.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Use EXPLAIN QUERY PLAN to analyze the plan
            explain_sql = f"EXPLAIN QUERY PLAN {sql}"

            if params:
                cursor.execute(explain_sql, params)
            else:
                cursor.execute(explain_sql)

            results = []
            for row in cursor.fetchall():
                results.append(
                    {
                        "selectid": row["selectid"],
                        "order": row["order"],
                        "from": row["from"],
                        "detail": row["detail"],
                    }
                )

            return results
