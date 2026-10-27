"""PostgreSQL storage adapter with connection pooling and full-text search.

The PostgreSQL adapter provides:
- Connection pooling via psycopg3 (psycopg[pool])
- Full-text search using tsvector/tsquery with BM25-like ranking
- Trigram similarity for fuzzy matching (pg_trgm extension)
- Atomic upserts for multi-instance safety (INSERT ... ON CONFLICT)
- Provenance immutability via database triggers

## Provenance Immutability

The adapter creates BEFORE triggers on the provenance table to enforce
immutability at the database level, mirroring the SQLite adapter's guarantees:

- Blocks UPDATE on entity_id, timestamp, user_context, payload columns
- Blocks DELETE operations on provenance records
"""

import json
import hashlib
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
)

if TYPE_CHECKING:
    from mosaic.linkml_bridge import SchemaRegistry

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
from mosaic.core.types import ProvenanceRecord as ProvenanceRecordType, TemporalRecord
from mosaic.core.exceptions import AdapterError, SearchCapabilityError

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError:
    raise ImportError(
        "PostgreSQL adapter requires psycopg[pool]. "
        "Install with: pip install datahelix-mosaic[postgres]"
    )


class PostgresEntity:
    """Entity stored in PostgreSQL."""

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


from mosaic.core.storage.adapters.sqlite_adapter import (
    _LEGACY_OPERATION_MAP as _PG_LEGACY_OPERATION_MAP,  # noqa: F401
    _normalize_operation as _pg_normalize_operation,
)


class PostgresProvenanceStore:
    """Postgres sibling of ``ProvenanceStore`` on the ``ProvenanceRecord`` table.

    Mirrors the SQLite store's sec9 §9.6 shape. SQL-level
    append-only enforcement is handled by a BEFORE UPDATE / BEFORE DELETE
    trigger equivalent to the SQLite one (Decision 9.6.C).
    """

    def __init__(self, conn: psycopg.Connection, schema_version: Optional[str] = None):
        self._conn = conn
        self._schema_version = schema_version or ""

    def record(
        self,
        entity_id: Optional[str],
        entity_type: Optional[str],
        operation: Any = None,
        actor_id: Optional[str] = None,
        patch: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
        derived_from_id: Optional[str] = None,
        process_id: Optional[str] = None,
        schema_version: Optional[str] = None,
        # Legacy kwargs (Decision 9.6.B)
        operation_type: Any = None,
        user_context: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        operation_id: Optional[str] = None,  # noqa: ARG002
        previous_state_hash: Optional[str] = None,  # noqa: ARG002
        state_snapshot: Optional[dict[str, Any]] = None,  # noqa: ARG002
    ) -> ProvenanceRecordType:
        op_source = operation if operation is not None else operation_type
        if op_source is None:
            raise ValueError("operation (or legacy operation_type) is required")
        op_value = _pg_normalize_operation(op_source)

        # actor_id resolution order (Decision 9.6.G): explicit kwarg → legacy
        # user_context shim → ContextVar (middleware / with_actor()) → sentinel.
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

        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO "ProvenanceRecord"
               (id, entity_id, entity_type, operation, actor_id, timestamp,
                schema_version, derived_from_id, process_id, patch, context,
                is_available)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)""",
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
        return ProvenanceRecordType(
            id=record_id,
            entity_id=entity_id,
            entity_type=entity_type,
            operation=op_value,
            actor_id=effective_actor or "",
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
        operation_type: Any = None,
    ) -> Iterator[ProvenanceRecordType]:
        op_filter = operation if operation is not None else operation_type
        cur = self._conn.cursor()
        sql = 'SELECT * FROM "ProvenanceRecord" WHERE entity_id = %s'
        params: list[Any] = [entity_id]

        if op_filter is not None:
            sql += " AND operation = %s"
            params.append(_pg_normalize_operation(op_filter))

        sql += " ORDER BY timestamp DESC"
        cur.execute(sql, params)

        for row in cur.fetchall():
            yield ProvenanceRecordType(
                id=row["id"],
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                operation=row["operation"],
                actor_id=row["actor_id"] or "",
                timestamp=datetime.fromisoformat(row["timestamp"]) if isinstance(row["timestamp"], str) else row["timestamp"],
                schema_version=row["schema_version"] or "",
                derived_from_id=row["derived_from_id"],
                process_id=row["process_id"],
                patch=json.loads(row["patch"]) if isinstance(row["patch"], str) and row["patch"] else row["patch"],
                context=json.loads(row["context"]) if isinstance(row["context"], str) and row["context"] else row["context"],
            )

    def get_history(self, entity_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """SELECT id, entity_id, entity_type, operation, timestamp,
                      actor_id, patch
               FROM "ProvenanceRecord"
               WHERE entity_id = %s
               ORDER BY timestamp ASC""",
            (entity_id,),
        )
        results = []
        for row in cur.fetchall():
            patch = row["patch"]
            if isinstance(patch, str):
                patch = json.loads(patch) if patch else None
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
        """Reconstruct entity state as of ``timestamp`` — Postgres mirror of the
        SQLite path (sec6 §6.8.2): data state = the most recent ``create`` /
        ``update`` full post-image with ``timestamp <= T``; availability decided
        by the most recent ``availability_change <= T``. Non-state-replacing
        records (availability/external_id/supersede) carry deltas and never
        define the returned state. Same known limitations as the SQLite path."""
        cur = self._conn.cursor()
        cur.execute(
            """SELECT patch, timestamp, operation
               FROM "ProvenanceRecord"
               WHERE entity_id = %s AND timestamp <= %s
               ORDER BY timestamp DESC""",
            (entity_id, timestamp),
        )

        data_state: Optional[dict[str, Any]] = None
        data_ts: Optional[Any] = None
        availability_resolved = False
        deleted = False

        for row in cur.fetchall():  # newest first
            patch_val = row["patch"]
            if isinstance(patch_val, str):
                patch_val = json.loads(patch_val) if patch_val else None
            op = row["operation"]
            if (
                not availability_resolved
                and op == "availability_change"
                and isinstance(patch_val, dict)
            ):
                availability_resolved = True
                if (
                    patch_val.get("status") == "deleted"
                    or patch_val.get("is_available") is False
                ):
                    deleted = True
            if data_state is None and op in ("create", "update"):
                data_state = patch_val
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
        cur = self._conn.cursor()
        cur.execute(
            """SELECT timestamp FROM "ProvenanceRecord"
               WHERE entity_id = %s AND operation = 'create'
               ORDER BY timestamp ASC
               LIMIT 1""",
            (entity_id,),
        )
        row = cur.fetchone()
        return row["timestamp"] if row else None

    def get_provenance_timestamps(
        self, entity_id: str
    ) -> Optional[dict[str, Optional[str]]]:
        """Legacy single-entity timestamp derivation. Delegates to get_temporal."""
        out = self.get_temporal([entity_id])
        if entity_id not in out:
            return None
        rec = out[entity_id]
        return {"created_at": rec.created_at, "updated_at": rec.updated_at}

    def get_temporal(
        self, entity_ids: list[str], *, as_of: Optional[str] = None
    ) -> "dict[str, TemporalRecord]":
        """Batch sec9 §9.7 temporal derivation (Postgres mirror of the SQLite path).

        ``as_of`` bounds derivation to ``timestamp <= as_of`` (sec6 §6.8)."""
        from mosaic.core.types import TemporalRecord

        if not entity_ids:
            return {}

        as_of_clause = " AND timestamp <= %s" if as_of else ""
        params: tuple[Any, ...] = (
            (list(entity_ids), as_of) if as_of else (list(entity_ids),)
        )
        cur = self._conn.cursor()
        cur.execute(
            f"""WITH target AS (
                    SELECT entity_id, operation, timestamp, actor_id,
                           schema_version, patch
                    FROM "ProvenanceRecord"
                    WHERE entity_id = ANY(%s){as_of_clause}
                ),
                agg AS (
                    SELECT
                        entity_id,
                        MIN(CASE WHEN operation = 'create'
                                 THEN timestamp END) AS created_at,
                        MAX(CASE
                            WHEN operation = 'availability_change'
                                 AND (patch::jsonb->>'status' = 'deleted'
                                      OR patch::jsonb->>'is_available' = 'false')
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
        for row in cur.fetchall():
            eid = row["entity_id"]
            result[eid] = TemporalRecord(
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                schema_version=row["schema_version"] or None,
                created_by=row["created_by"] or None,
                updated_by=row["updated_by"] or None,
            )
        return result

    def entities_created_by(
        self, as_of: str, entity_type: Optional[str] = None
    ) -> list[tuple[str, str]]:
        """Candidate (entity_id, entity_type) set for an as-of query (sec6
        §6.8.2) — Postgres mirror of the SQLite path."""
        cur = self._conn.cursor()
        if entity_type is not None:
            cur.execute(
                """SELECT DISTINCT entity_id, entity_type FROM "ProvenanceRecord"
                   WHERE operation = 'create' AND entity_type = %s
                     AND timestamp <= %s""",
                (entity_type, as_of),
            )
        else:
            cur.execute(
                """SELECT DISTINCT entity_id, entity_type FROM "ProvenanceRecord"
                   WHERE operation = 'create' AND timestamp <= %s""",
                (as_of,),
            )
        return [(row["entity_id"], row["entity_type"]) for row in cur.fetchall()]

    def state_version_at(self, entity_id: str, as_of: str) -> int:
        """Entity version as of T — count of create/update records <= as_of."""
        cur = self._conn.cursor()
        cur.execute(
            """SELECT COUNT(*) AS n FROM "ProvenanceRecord"
               WHERE entity_id = %s AND operation IN ('create', 'update')
                 AND timestamp <= %s""",
            (entity_id, as_of),
        )
        row = cur.fetchone()
        return int(row["n"]) if row and row["n"] else 1


class PostgresRelationshipRecord:
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


class PostgresRelationshipStore:
    """Store for relationship records in PostgreSQL."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def create(
        self,
        source_id: str,
        target_id: str,
        relationship_type: str,
        created_by: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> PostgresRelationshipRecord:
        now = datetime.now(timezone.utc).isoformat()
        rel_id = str(uuid.uuid4())

        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO relationships
               (id, source_id, target_id, relationship_type, metadata, created_at, created_by, is_available)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)""",
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

        return PostgresRelationshipRecord(
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
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.cursor()
        cur.execute(
            """UPDATE relationships SET is_available = FALSE, created_at = %s
               WHERE source_id = %s AND target_id = %s
               AND relationship_type = %s AND is_available = TRUE""",
            (now, source_id, target_id, relationship_type),
        )
        return cur.rowcount > 0

    def find_by_source(
        self, source_id: str, relationship_type: Optional[str] = None
    ) -> Iterator[PostgresRelationshipRecord]:
        cur = self._conn.cursor()
        sql = "SELECT * FROM relationships WHERE source_id = %s AND is_available = TRUE"
        params: list[Any] = [source_id]

        if relationship_type:
            sql += " AND relationship_type = %s"
            params.append(relationship_type)

        cur.execute(sql, params)
        for row in cur.fetchall():
            yield self._row_to_relationship(row)

    def find_by_target(
        self, target_id: str, relationship_type: Optional[str] = None
    ) -> Iterator[PostgresRelationshipRecord]:
        cur = self._conn.cursor()
        sql = "SELECT * FROM relationships WHERE target_id = %s AND is_available = TRUE"
        params: list[Any] = [target_id]

        if relationship_type:
            sql += " AND relationship_type = %s"
            params.append(relationship_type)

        cur.execute(sql, params)
        for row in cur.fetchall():
            yield self._row_to_relationship(row)

    def find(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        relationship_type: Optional[str] = None,
    ) -> Iterator[PostgresRelationshipRecord]:
        cur = self._conn.cursor()
        conditions = ["is_available = TRUE"]
        params: list[Any] = []

        if source_id:
            conditions.append("source_id = %s")
            params.append(source_id)
        if target_id:
            conditions.append("target_id = %s")
            params.append(target_id)
        if relationship_type:
            conditions.append("relationship_type = %s")
            params.append(relationship_type)

        sql = f"SELECT * FROM relationships WHERE {' AND '.join(conditions)}"
        cur.execute(sql, params)
        for row in cur.fetchall():
            yield self._row_to_relationship(row)

    def traverse(
        self,
        source_id: str,
        relationship_type: Optional[str] = None,
        max_depth: int = 10,
        *,
        as_of: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if as_of is not None:
            return self._traverse_as_of(
                source_id, relationship_type, max_depth, as_of
            )
        cur = self._conn.cursor()

        if relationship_type:
            cte_sql = """
                WITH RECURSIVE traversal(id, source_id, target_id, relationship_type, depth) AS (
                    SELECT id, source_id, target_id, relationship_type, 1
                    FROM relationships
                    WHERE source_id = %s AND relationship_type = %s AND is_available = TRUE
                    UNION ALL
                    SELECT r.id, r.source_id, r.target_id, r.relationship_type, t.depth + 1
                    FROM relationships r
                    INNER JOIN traversal t ON r.source_id = t.target_id
                    WHERE r.is_available = TRUE AND t.depth < %s
                )
                SELECT * FROM traversal
            """
            cur.execute(cte_sql, (source_id, relationship_type, max_depth))
        else:
            cte_sql = """
                WITH RECURSIVE traversal(id, source_id, target_id, relationship_type, depth) AS (
                    SELECT id, source_id, target_id, relationship_type, 1
                    FROM relationships
                    WHERE source_id = %s AND is_available = TRUE
                    UNION ALL
                    SELECT r.id, r.source_id, r.target_id, r.relationship_type, t.depth + 1
                    FROM relationships r
                    INNER JOIN traversal t ON r.source_id = t.target_id
                    WHERE r.is_available = TRUE AND t.depth < %s
                )
                SELECT * FROM traversal
            """
            cur.execute(cte_sql, (source_id, max_depth))

        results = []
        for row in cur.fetchall():
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
        """Edges live at as_of (sec6 §6.8.2) — Postgres mirror: replay
        relationship_add/remove provenance events <= as_of; an edge keyed by
        (source, target, type) is live iff its latest such event is an add."""
        cur = self._conn.cursor()
        cur.execute(
            """SELECT operation,
                      patch::jsonb->>'source_id'         AS s,
                      patch::jsonb->>'target_id'         AS t,
                      patch::jsonb->>'relationship_type' AS rt,
                      patch::jsonb->>'relationship_id'   AS rid
               FROM "ProvenanceRecord"
               WHERE operation IN ('relationship_add', 'relationship_remove')
                 AND timestamp <= %s
               ORDER BY timestamp ASC""",
            (as_of,),
        )
        live: dict[tuple[Any, Any, Any], Optional[str]] = {}
        for row in cur.fetchall():
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
        """Depth-bounded traversal over edges live at as_of (Postgres mirror of
        the SQLite path; same recursive-CTE semantics)."""
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

    def _row_to_relationship(self, row: dict) -> PostgresRelationshipRecord:
        return PostgresRelationshipRecord(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relationship_type=row["relationship_type"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
            created_at=row["created_at"],
            created_by=row["created_by"],
            is_available=bool(row["is_available"]),
        )


class PostgresExternalIdRecord:
    """External ID record."""

    def __init__(
        self,
        id: str,
        entity_id: str,
        external_id: str,
        created_at: str,
        superseded_at: Optional[str] = None,
    ):
        self.id = id
        self.entity_id = entity_id
        self.external_id = external_id
        self.created_at = created_at
        self.superseded_at = superseded_at


class PostgresExternalIdStore:
    """Storage adapter for managing external IDs in PostgreSQL.

    Uses atomic upserts (INSERT ... ON CONFLICT) for multi-instance safety
    as required by the design spec §2.3.
    """

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def create_external_id(
        self, entity_id: str, external_id: str
    ) -> PostgresExternalIdRecord:
        now = datetime.now(timezone.utc).isoformat()
        record_id = str(uuid.uuid4())

        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO entity_external_ids
               (id, entity_id, external_id, created_at, superseded_at)
               VALUES (%s, %s, %s, %s, NULL)
               ON CONFLICT (entity_id, external_id)
               WHERE superseded_at IS NULL
               DO NOTHING""",
            (record_id, entity_id, external_id, now),
        )

        return PostgresExternalIdRecord(
            id=record_id,
            entity_id=entity_id,
            external_id=external_id,
            created_at=now,
            superseded_at=None,
        )

    def get_entity_by_external_id(
        self, external_id: str, include_archived: bool = False
    ) -> Optional[PostgresExternalIdRecord]:
        cur = self._conn.cursor()

        if include_archived:
            cur.execute(
                """SELECT * FROM entity_external_ids
                   WHERE external_id = %s AND superseded_at IS NULL
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (external_id,),
            )
        else:
            cur.execute(
                """SELECT eei.* FROM entity_external_ids eei
                   INNER JOIN entities e ON eei.entity_id = e.id
                   WHERE eei.external_id = %s AND e.is_available = TRUE
                   AND eei.superseded_at IS NULL
                   ORDER BY eei.created_at DESC
                   LIMIT 1""",
                (external_id,),
            )

        row = cur.fetchone()
        if row is None:
            return None

        return self._row_to_external_id(row)

    def list_external_ids_for_entity(
        self, entity_id: str, include_superseded: bool = False
    ) -> Iterator[PostgresExternalIdRecord]:
        cur = self._conn.cursor()

        if include_superseded:
            cur.execute(
                """SELECT * FROM entity_external_ids
                   WHERE entity_id = %s
                   ORDER BY created_at DESC""",
                (entity_id,),
            )
        else:
            cur.execute(
                """SELECT * FROM entity_external_ids
                   WHERE entity_id = %s AND superseded_at IS NULL
                   ORDER BY created_at DESC""",
                (entity_id,),
            )

        for row in cur.fetchall():
            yield self._row_to_external_id(row)

    def supersede_external_id(
        self, entity_id: str, old_external_id: str, new_external_id: str
    ) -> PostgresExternalIdRecord:
        now = datetime.now(timezone.utc).isoformat()
        new_record_id = str(uuid.uuid4())

        cur = self._conn.cursor()
        cur.execute(
            """UPDATE entity_external_ids
               SET superseded_at = %s
               WHERE entity_id = %s AND external_id = %s AND superseded_at IS NULL""",
            (now, entity_id, old_external_id),
        )

        cur.execute(
            """INSERT INTO entity_external_ids
               (id, entity_id, external_id, created_at, superseded_at)
               VALUES (%s, %s, %s, %s, NULL)""",
            (new_record_id, entity_id, new_external_id, now),
        )

        return PostgresExternalIdRecord(
            id=new_record_id,
            entity_id=entity_id,
            external_id=new_external_id,
            created_at=now,
            superseded_at=None,
        )

    def _row_to_external_id(self, row: dict) -> PostgresExternalIdRecord:
        return PostgresExternalIdRecord(
            id=row["id"],
            entity_id=row["entity_id"],
            external_id=row["external_id"],
            created_at=row["created_at"],
            superseded_at=row["superseded_at"],
        )


class PostgresFTSStore:
    """Full-text search store using PostgreSQL tsvector/tsquery.

    Replaces SQLite FTS5 with PostgreSQL's native full-text search:
    - tsvector/tsquery for text search
    - ts_rank for relevance ranking
    - pg_trgm extension for trigram similarity (fuzzy matching)
    """

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def create_fts_table(
        self,
        table_name: str,
        columns: list[str],
    ) -> None:
        """Create an FTS shadow table with tsvector column and GIN index."""
        cur = self._conn.cursor()
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {table_name} (
                entity_id TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                content_tsvector TSVECTOR
                    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
                PRIMARY KEY (entity_id)
            )"""
        )
        cur.execute(
            f"""CREATE INDEX IF NOT EXISTS idx_{table_name}_tsvector
                ON {table_name} USING GIN (content_tsvector)"""
        )
        # Trigram index for fuzzy matching
        cur.execute(
            f"""CREATE INDEX IF NOT EXISTS idx_{table_name}_trigram
                ON {table_name} USING GIN (content gin_trgm_ops)"""
        )

    def sync_entity_to_fts(
        self,
        table_name: str,
        entity_id: str,
        content: str,
    ) -> None:
        """Upsert an entity into the FTS table."""
        cur = self._conn.cursor()
        cur.execute(
            f"""INSERT INTO {table_name} (entity_id, content)
                VALUES (%s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET content = EXCLUDED.content""",
            (entity_id, content),
        )

    def remove_entity_from_fts(self, table_name: str, entity_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            f"DELETE FROM {table_name} WHERE entity_id = %s",
            (entity_id,),
        )

    def drop_fts_table(self, table_name: str) -> None:
        cur = self._conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")

    def insert_fts_entry(self, table_name: str, entity_id: str, content: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            f"INSERT INTO {table_name} (entity_id, content) VALUES (%s, %s)",
            (entity_id, content),
        )

    def update_fts_entry(self, table_name: str, entity_id: str, content: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            f"UPDATE {table_name} SET content = %s WHERE entity_id = %s",
            (content, entity_id),
        )

    def delete_fts_entry(self, table_name: str, entity_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            f"DELETE FROM {table_name} WHERE entity_id = %s",
            (entity_id,),
        )

    def search_fts(
        self,
        table_name: str,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search using tsquery with ts_rank scoring."""
        cur = self._conn.cursor()
        # Use plainto_tsquery for user-friendly input (no need for tsquery syntax)
        cur.execute(
            f"""SELECT entity_id, content,
                       ts_rank(content_tsvector, plainto_tsquery('english', %s)) AS rank
                FROM {table_name}
                WHERE content_tsvector @@ plainto_tsquery('english', %s)
                ORDER BY rank DESC
                LIMIT %s""",
            (query, query, limit),
        )
        return [
            {"entity_id": row["entity_id"], "content": row["content"]}
            for row in cur.fetchall()
        ]

    def search_fts_fuzzy(
        self,
        table_name: str,
        query: str,
        limit: int = 100,
        similarity_threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Fuzzy search using trigram similarity (pg_trgm)."""
        cur = self._conn.cursor()
        cur.execute(
            f"""SELECT entity_id, content,
                       similarity(content, %s) AS sim_score
                FROM {table_name}
                WHERE similarity(content, %s) > %s
                ORDER BY sim_score DESC
                LIMIT %s""",
            (query, query, similarity_threshold, limit),
        )
        return [
            {
                "entity_id": row["entity_id"],
                "content": row["content"],
                "score": row["sim_score"],
            }
            for row in cur.fetchall()
        ]

    def get_fts_tables_for_entity_type(self, entity_type: str) -> list[str]:
        cur = self._conn.cursor()
        prefix = f"fts_{entity_type.lower()}_"
        cur.execute(
            """SELECT tablename FROM pg_tables
               WHERE schemaname = 'public' AND tablename LIKE %s""",
            (prefix + "%",),
        )
        return [row["tablename"] for row in cur.fetchall()]

    def fts_table_exists(self, table_name: str) -> bool:
        cur = self._conn.cursor()
        cur.execute(
            """SELECT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = %s
            )""",
            (table_name,),
        )
        row = cur.fetchone()
        return row["exists"] if row else False


# ---------------------------------------------------------------------------
# Trigger SQL for provenance immutability
# ---------------------------------------------------------------------------

POSTGRES_PROVENANCE_TRIGGERS = """
-- Enforce hippo_append_only on ProvenanceRecord at the SQL level
-- (sec9 §9.6 / Decision 9.6.C). A single BEFORE UPDATE / BEFORE DELETE
-- trigger covers any column change and any row deletion.
CREATE OR REPLACE FUNCTION prevent_provenance_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Cannot update ProvenanceRecord: hippo_append_only class';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_provenance_update ON "ProvenanceRecord";
CREATE TRIGGER trg_prevent_provenance_update
    BEFORE UPDATE ON "ProvenanceRecord"
    FOR EACH ROW
    EXECUTE FUNCTION prevent_provenance_update();

CREATE OR REPLACE FUNCTION prevent_provenance_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Cannot delete ProvenanceRecord: hippo_append_only class';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_provenance_delete ON "ProvenanceRecord";
CREATE TRIGGER trg_prevent_provenance_delete
    BEFORE DELETE ON "ProvenanceRecord"
    FOR EACH ROW
    EXECUTE FUNCTION prevent_provenance_delete();
"""


class PostgresAdapter(EntityStore):
    """PostgreSQL storage adapter with connection pooling.

    Args:
        database_url: PostgreSQL connection string (e.g. postgresql://user:pass@host:5432/db).
            Can also be set via the MOSAIC_DATABASE_URL environment variable
            (legacy HIPPO_DATABASE_URL honored — ADR-0004).
        schema_registry: LinkML schema registry for schema introspection
            and validation. Required for LinkML-native storage operations.
        min_pool_size: Minimum number of connections in the pool (default: 2).
        max_pool_size: Maximum number of connections in the pool (default: 10).
    """

    def __init__(
        self,
        database_url: str,
        schema_registry: "SchemaRegistry",
        min_pool_size: int = 2,
        max_pool_size: int = 10,
        schema_version: Optional[str] = None,
    ):
        self.database_url = database_url
        self.schema_registry = schema_registry
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self._schema_version = schema_version or ""
        self._provenance_store: Optional[PostgresProvenanceStore] = None
        # Per-class cache of multivalued reference slot names (issue #79/#81).
        self._mv_ref_slots_cache: dict[str, list[str]] = {}
        # Per-class cache of virtual inverse slots (ADR-0011): slot name ->
        # InverseSlot(target_class, forward_slot).
        self._inverse_slots_cache: dict[str, dict[str, Any]] = {}
        # Per-class cache of filter-cast suffixes for comparison predicates
        # (ADR-0006): slot name -> "::numeric" / "::timestamptz" / "" (text).
        self._filter_cast_cache: dict[str, dict[str, str]] = {}
        # Per-thread staged-transaction state (mirrors SQLiteAdapter): when a
        # ``staged_transaction`` scope is active, inner ``_transaction`` calls
        # reuse the pinned connection and defer commit to the outer scope so a
        # whole batch commits or rolls back as one unit.
        self._local = threading.local()

        try:
            self._pool = ConnectionPool(
                conninfo=database_url,
                min_size=min_pool_size,
                max_size=max_pool_size,
                kwargs={"row_factory": dict_row},
            )
        except Exception as e:
            raise AdapterError(
                message=f"Failed to create PostgreSQL connection pool: {e}",
                adapter_type="postgres",
                cause=e,
            )

        self._init_database()

    @contextmanager
    def _connection(self) -> Generator[psycopg.Connection, None, None]:
        """Get a connection from the pool."""
        try:
            with self._pool.connection() as conn:
                yield conn
        except psycopg.Error as e:
            raise AdapterError(
                message=f"PostgreSQL connection error: {e}",
                adapter_type="postgres",
                cause=e,
            )

    @contextmanager
    def _transaction(self) -> Generator[psycopg.Connection, None, None]:
        """Get a connection with explicit transaction management.

        When a :meth:`staged_transaction` scope is active on this thread, inner
        writes **defer** their commit to that outer scope: this yields the
        pinned staged connection without committing, so a whole batch (entity
        rows, provenance, relationships) commits or rolls back as one unit.
        Reads on the same connection observe the staged (uncommitted) writes,
        so an intra-batch relationship to an entity created earlier in the same
        batch resolves.
        """
        if getattr(self._local, "staging_depth", 0) > 0:
            # Inside an outer staged scope: defer commit/rollback to it. On
            # error, propagate so the staged scope rolls everything back.
            yield self._local.staged_conn
            return
        with self._connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def staged_transaction(self) -> Generator[psycopg.Connection, None, None]:
        """Outer commit-or-rollback scope spanning many writes (issue #84).

        Pins one pooled connection for the scope; every inner
        :meth:`_transaction` defers its commit (see above). All writes commit
        together on clean exit or roll back together if any exception escapes —
        the guarantee ``MosaicClient.batch_put`` relies on. Nesting is
        reference-counted: only the outermost scope commits. Mirrors
        :meth:`SQLiteAdapter.staged_transaction`.
        """
        depth = getattr(self._local, "staging_depth", 0)
        if depth > 0:
            # Already staging on this thread — join the existing scope.
            self._local.staging_depth = depth + 1
            try:
                yield self._local.staged_conn
            finally:
                self._local.staging_depth -= 1
            return
        # Outermost scope: check out a dedicated connection. ``_connection``
        # (the pool context) commits on clean exit and rolls back on exception,
        # so the whole staged write-set is atomic.
        with self._connection() as conn:
            self._local.staged_conn = conn
            self._local.staging_depth = 1
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            finally:
                self._local.staging_depth = 0
                self._local.staged_conn = None

    def _init_database(self) -> None:
        """Initialize database schema and extensions."""
        with self._transaction() as conn:
            cur = conn.cursor()

            # Enable required extensions
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS hippo_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    is_available BOOLEAN NOT NULL DEFAULT TRUE,
                    version INTEGER NOT NULL DEFAULT 1,
                    data JSONB NOT NULL,
                    superseded_by TEXT
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_entities_type
                ON entities(entity_type)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_entities_available
                ON entities(is_available)
            """)
            # GIN index on JSONB data for efficient field queries
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_entities_data
                ON entities USING GIN (data)
            """)

            # Drop legacy provenance table if it exists; data migration
            # from legacy to ProvenanceRecord is not supported (dev-only
            # deployments per Decision 9.6.D).
            cur.execute("DROP TABLE IF EXISTS provenance CASCADE")

            # ProvenanceRecord table (sec9 §9.6 / Decision 9.6.D). Shape
            # matches what the LinkML DDL generator produces from
            # hippo_core.ProvenanceRecord (verified by
            # tests/core/test_ddl_generator.py::
            # TestHippoCoreProvenanceRecordDDL).
            cur.execute("""
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
                    is_available BOOLEAN NOT NULL DEFAULT TRUE,
                    superseded_by TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_entity_id
                ON "ProvenanceRecord"(entity_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_operation
                ON "ProvenanceRecord"(operation)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_timestamp
                ON "ProvenanceRecord"(timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_process_id
                ON "ProvenanceRecord"(process_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_entity_timestamp
                ON "ProvenanceRecord"(entity_id, timestamp)
            """)
            # Type-scoped as-of set selection (sec6 §6.8.4) — Postgres parity.
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ProvenanceRecord_type_timestamp
                ON "ProvenanceRecord"(entity_type, timestamp, entity_id)
            """)

            # ``seq`` is a monotonically increasing insertion-order marker.
            # SQLite relationships rely on the implicit ``rowid`` to
            # preserve multivalued reference slot order on hydration
            # (issue #79/#81 / ADR-0002); Postgres has no equivalent
            # implicit ordering, so this column stands in for it.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    created_by TEXT,
                    is_available BOOLEAN NOT NULL DEFAULT TRUE,
                    seq BIGSERIAL
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_source
                ON relationships(source_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_target
                ON relationships(target_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationships_type
                ON relationships(relationship_type)
            """)

            # Reference-loader write log (sec2 §2.14.9 / Decision 2.14.J).
            # Substrate for ``--prune-old``: records every entity written by
            # a reference loader inside ``MosaicClient.load_context()``. The
            # composite PK collapses repeat writes of the same id within a
            # ``(loader_name, version)`` window to a single row, keeping
            # ``upgrade()`` re-writes and the v1→v2 backfill idempotent.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reference_write_log (
                    loader_name TEXT NOT NULL,
                    version     TEXT NOT NULL,
                    entity_id   TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    written_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (loader_name, version, entity_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reference_write_log_lookup
                ON reference_write_log (loader_name, version)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS entity_external_ids (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    external_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_entity_external_ids_entity
                ON entity_external_ids(entity_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_entity_external_ids_external
                ON entity_external_ids(external_id, created_at DESC)
            """)
            # Unique partial index for atomic external ID registration
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_external_ids_unique_active
                ON entity_external_ids(entity_id, external_id)
                WHERE superseded_at IS NULL
            """)

            # Provenance immutability triggers
            cur.execute(POSTGRES_PROVENANCE_TRIGGERS)

            # entity_provenance_summary view — sec9 §9.7 computed fields.
            # Reads the ProvenanceRecord table; the availability-change
            # exclusion mirrors the SOFT_DELETE logic (Decision 9.6.B):
            # availability_change with status='deleted' or is_available=false
            # does not count toward updated_at.
            cur.execute("""
                CREATE OR REPLACE VIEW entity_provenance_summary AS
                SELECT
                    p1.entity_id,
                    p1.entity_type,
                    MIN(p1.timestamp) AS created_at,
                    MAX(CASE
                        WHEN p1.operation = 'availability_change'
                             AND (p1.patch::jsonb->>'status' = 'deleted'
                                  OR p1.patch::jsonb->>'is_available' = 'false')
                            THEN NULL
                        ELSE p1.timestamp
                    END) AS updated_at,
                    (SELECT p2.schema_version FROM "ProvenanceRecord" p2
                     WHERE p2.entity_id = p1.entity_id
                     ORDER BY p2.timestamp DESC LIMIT 1) AS schema_version
                FROM "ProvenanceRecord" p1
                WHERE p1.entity_id IS NOT NULL
                GROUP BY p1.entity_id, p1.entity_type
            """)

            # Phase E: drop legacy stored temporal columns (PTS-69).
            # Temporal fields are computed exclusively from ProvenanceRecord.
            # Postgres supports DROP COLUMN IF EXISTS directly.
            for col in ("created_at", "updated_at"):
                cur.execute(
                    f"ALTER TABLE entities DROP COLUMN IF EXISTS {col}"
                )

            self._migrate_reference_entity_ids(cur)

            self._ensure_fts_tables(conn)

    def _ensure_fts_tables(self, conn: "psycopg.Connection") -> None:
        """Create FTS shadow tables for every ``hippo_search`` slot.

        SQLite creates FTS tables alongside its per-class typed tables, so
        a fresh deployment searches out of the box; the Postgres adapter's
        generic ``entities`` storage never did — ``search()`` crashed with
        ``relation "fts_<type>_<slot>" does not exist`` on any deployment
        that hadn't created them explicitly (first real DataHelix
        certification boot, datahelix#45). Idempotent: ``CREATE TABLE IF
        NOT EXISTS`` throughout. Content is synced by the ingestion
        service on write, which skips tables that don't exist — so these
        must exist from the start for search to ever see anything.
        """
        if self.schema_registry is None:
            return
        from mosaic.core.storage.fts import FTSTableMetadata

        sv = self.schema_registry.schema_view
        fts = PostgresFTSStore(conn)
        for class_name in self.schema_registry.class_names():
            cls = sv.get_class(class_name)
            if cls is None or cls.abstract:
                continue
            for slot, _mode in self.schema_registry.searchable_slots(class_name):
                fts.create_fts_table(
                    FTSTableMetadata.generate_table_name(class_name, slot.name),
                    [slot.name],
                )

    _REFERENCE_WRITE_LOG_MIGRATION_KEY = "_reference_write_log_v1_migrated"

    def _migrate_reference_entity_ids(self, cur: "psycopg.Cursor") -> None:
        """One-time backfill from ``hippo_meta.reference_entity_ids`` JSON
        blob to the ``reference_write_log`` table (sec2 §2.14.9).

        v1 stored ``{loader_name: {version: [entity_id, ...]}}`` under the
        ``reference_entity_ids`` meta key. v2 replaces that with the write
        log. The first ``_init_database`` after this code lands performs
        the backfill, then stamps a marker key so subsequent startups skip
        entirely. The marker is required because the v1 ``_write_versions``
        path is still in tree (removed in PTS-256) and would otherwise be
        clobbered on every adapter init.

        ``entity_type`` is resolved per id from the ``entities`` table
        first, then ``ProvenanceRecord`` as a fallback (survives any
        future entity-row removal). Rows whose type cannot be resolved are
        skipped — preferable to a hard failure during init, since the
        prune flow that consumes the log is opt-in.

        ``ON CONFLICT DO NOTHING`` plus the composite primary key make the
        row writes idempotent even if a previous migration crashed after
        inserting some rows but before stamping the marker.
        """
        cur.execute(
            "SELECT 1 FROM hippo_meta WHERE key = %s",
            (self._REFERENCE_WRITE_LOG_MIGRATION_KEY,),
        )
        if cur.fetchone() is not None:
            return

        cur.execute(
            "SELECT value FROM hippo_meta WHERE key = 'reference_entity_ids'"
        )
        row = cur.fetchone()
        if row is not None:
            raw = row["value"] if isinstance(row, dict) else row[0]
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
                                    cur, entity_id
                                )
                            )
                            if entity_type is None:
                                continue
                            cur.execute(
                                "INSERT INTO reference_write_log "
                                "(loader_name, version, entity_id, entity_type) "
                                "VALUES (%s, %s, %s, %s) "
                                "ON CONFLICT (loader_name, version, entity_id) DO NOTHING",
                                (loader_name, version, entity_id, entity_type),
                            )

            cur.execute(
                "DELETE FROM hippo_meta WHERE key = 'reference_entity_ids'"
            )

        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO hippo_meta (key, value, updated_at) "
            "VALUES (%s, %s, %s) "
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
        cur: "psycopg.Cursor", entity_id: str
    ) -> Optional[str]:
        """Best-effort entity_type lookup used by the v1→v2 backfill.

        Checks the ``entities`` table first; falls back to
        ``ProvenanceRecord`` in case the row has been removed in a future
        cleanup pass.
        """
        cur.execute(
            "SELECT entity_type FROM entities WHERE id = %s",
            (entity_id,),
        )
        row = cur.fetchone()
        if row is not None:
            entity_type = row["entity_type"] if isinstance(row, dict) else row[0]
            if entity_type:
                return entity_type
        cur.execute(
            'SELECT entity_type FROM "ProvenanceRecord" '
            "WHERE entity_id = %s AND entity_type IS NOT NULL LIMIT 1",
            (entity_id,),
        )
        row = cur.fetchone()
        if row is not None:
            entity_type = row["entity_type"] if isinstance(row, dict) else row[0]
            if entity_type:
                return entity_type
        return None

    # -- multivalued reference slots → relationships (issue #79/#81 / ADR-0002) --
    #
    # Postgres has no per-class typed table (entities live in one generic
    # ``entities`` row with a JSONB ``data`` document), so there is no
    # column to omit the way SQLite omits one from its per-class table.
    # Parity is achieved by stripping these slot keys out of the JSONB
    # document before it is stored (the relationships table becomes the
    # sole current-state source for them) and hydrating them back into
    # ``entity["data"][slot]`` on every read path. The unstripped dict is
    # still recorded verbatim in the ``ProvenanceRecord.patch`` (see
    # ``create``/``update_data`` below), so as-of reconstruction
    # (``get_state_at`` / ADR-0001) is unaffected — it never touches the
    # relationships table and reads the full patch as before.

    def _multivalued_ref_slot_names(self, entity_type: str) -> list[str]:
        """Names of multivalued reference slots for a class (issue #79/#81).

        Cached per class. Empty when the registry is absent or the class
        declares no multivalued slot ranged on an entity type. Mirrors
        ``SQLiteAdapter._multivalued_ref_slot_names``.
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

        Mirrors ``SQLiteAdapter._normalize_ref_list``: tolerates a bare
        scalar (treated as a single-element list) and drops ``None``
        entries; non-string ids are coerced to ``str``.
        """
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        return [str(v) for v in items if v is not None]

    def _strip_multivalued_refs(
        self, entity_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop multivalued reference slot keys before persisting to ``entities.data``.

        These slots have no column-equivalent in the stored JSONB
        document — only relationships. Returns ``data`` unchanged (no
        copy) when the class has no such slots or none are present, to
        keep the common path cheap.
        """
        slot_names = self._multivalued_ref_slot_names(entity_type)
        if not slot_names or not any(name in data for name in slot_names):
            return data
        return {k: v for k, v in data.items() if k not in slot_names}

    def _materialize_multivalued_refs(
        self,
        conn: "psycopg.Connection",
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        reconcile: bool,
        actor: Optional[str] = None,
    ) -> None:
        """Persist multivalued reference slots as relationships (issue #79/#81).

        Postgres mirror of ``SQLiteAdapter._materialize_multivalued_refs``:
        one relationship per target id, keyed by the slot name, written in
        the caller's transaction. On ``reconcile`` (update/replace) the
        existing live edges of every such slot for this source are
        soft-deleted first, then rebuilt from ``data`` — an omitted slot
        clears its edges, matching column-NULL semantics on SQLite.

        Target existence is NOT checked: single-valued reference ids
        persist as opaque strings, and this matches that, so forward
        references during bulk ingest are preserved. Each add/remove
        records a ``relationship_add``/``relationship_remove``
        ProvenanceRecord so as-of edge replay (ADR-0001) stays consistent.
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
        conn: "psycopg.Connection",
        entity_type: str,
        entity_ids: list[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Read multivalued reference slot edges for a set of entities.

        Postgres mirror of
        ``SQLiteAdapter._hydrate_multivalued_refs_batch``: returns
        ``{entity_id: {slot_name: [target_id, ...]}}`` in a single
        round-trip (no N+1). Target ids preserve insertion order via the
        ``seq`` column (Postgres has no SQLite-style implicit ``rowid``).
        Only currently-live edges are returned.
        """
        slot_names = self._multivalued_ref_slot_names(entity_type)
        if not slot_names or not entity_ids:
            return {}

        cur = conn.cursor()
        cur.execute(
            "SELECT source_id, relationship_type, target_id FROM relationships "
            "WHERE source_id = ANY(%s) "
            "AND relationship_type = ANY(%s) "
            "AND is_available = TRUE "
            "ORDER BY seq",
            (list(entity_ids), list(slot_names)),
        )
        out: dict[str, dict[str, list[str]]] = {}
        for row in cur.fetchall():
            out.setdefault(row["source_id"], {}).setdefault(
                row["relationship_type"], []
            ).append(row["target_id"])
        return out

    def _hydrate_multivalued_refs_for_entities(
        self, conn: "psycopg.Connection", entities: list["PostgresEntity"]
    ) -> None:
        """Batch-hydrate multivalued reference edges into ``entity.data``.

        Unlike SQLite (whose per-class tables are scanned one type at a
        time), a single Postgres ``find()`` can return rows spanning
        several entity types. Ids are grouped by type so each distinct
        type present in the result set costs one round-trip, not one per
        entity.
        """
        by_type: dict[str, list[str]] = {}
        for entity in entities:
            by_type.setdefault(entity.entity_type, []).append(entity.id)
        for entity_type, ids in by_type.items():
            hydrated = self._hydrate_derived_refs_batch(conn, entity_type, ids)
            if not hydrated:
                continue
            for entity in entities:
                if entity.entity_type == entity_type and entity.id in hydrated:
                    entity.data.update(hydrated[entity.id])

    # -- inverse slots → virtual reverse edges (ADR-0011) ---------------------

    def _inverse_slots(self, entity_type: str) -> dict[str, Any]:
        """Virtual inverse slots of a class, keyed by slot name (ADR-0011).

        Postgres mirror of ``SQLiteAdapter._inverse_slots``: cached per
        class; empty when the registry is absent or the class declares no
        ``inverse`` multivalued reference. These slots own no key in the
        stored JSONB document and no relationship rows; they resolve
        through the forward FK key on the *target* class's documents.
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

        Mirror of ``SQLiteAdapter._strip_inverse_slots``: the reverse edge
        is derived from the forward FK, so a payload carrying it (a
        get-then-put round-trip of a hydrated entity) is accepted and the
        key ignored — never stored, never in the provenance patch.
        """
        inverse = self._inverse_slots(entity_type)
        if not inverse or not any(name in data for name in inverse):
            return data
        return {k: v for k, v in data.items() if k not in inverse}

    def _hydrate_inverse_refs_batch(
        self,
        conn: "psycopg.Connection",
        entity_type: str,
        entity_ids: list[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Read virtual inverse slots for a set of entities (ADR-0011).

        Returns ``{entity_id: {slot_name: [target_id, ...]}}`` — one query
        per inverse slot over the target type's documents keyed by the
        forward FK key (no N+1). Only **available** targets appear,
        matching list queries and the ``some`` quantifier. Ids are sorted
        for determinism (the ``entities`` table has no insertion marker).
        """
        inverse = self._inverse_slots(entity_type)
        if not inverse or not entity_ids:
            return {}
        out: dict[str, dict[str, list[str]]] = {}
        cur = conn.cursor()
        for slot_name, inv in inverse.items():
            # entity_type over the range's concrete closure: an abstract
            # range (``Sample``) stores no rows under its own name (#224).
            cur.execute(
                "SELECT id, data->>%s AS fk FROM entities "
                "WHERE entity_type = ANY(%s) AND data->>%s = ANY(%s) "
                "AND is_available = TRUE ORDER BY id",
                (inv.forward_slot, list(inv.tables), inv.forward_slot, list(entity_ids)),
            )
            for row in cur.fetchall():
                out.setdefault(row["fk"], {}).setdefault(slot_name, []).append(
                    row["id"]
                )
        return out

    def _hydrate_derived_refs_batch(
        self,
        conn: "psycopg.Connection",
        entity_type: str,
        entity_ids: list[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Every slot absent from the stored document but present in
        ``entity.data``: relationships-backed multivalued references
        (ADR-0002) plus virtual inverse slots (ADR-0011)."""
        out = self._hydrate_multivalued_refs_batch(conn, entity_type, entity_ids)
        for eid, slots in self._hydrate_inverse_refs_batch(
            conn, entity_type, entity_ids
        ).items():
            out.setdefault(eid, {}).update(slots)
        return out

    # ------------------------------------------------------------------
    # EntityStore protocol implementation
    # ------------------------------------------------------------------

    def create(
        self,
        entity: PostgresEntity,
        user_context: Optional[str] = None,
        loader_context: Optional[tuple[str, str]] = None,
    ) -> PostgresEntity:
        """Create a new entity using atomic upsert for multi-instance safety.

        When ``loader_context`` is a ``(loader_name, version)`` tuple
        (set by ``MosaicClient.load_context()``), a row is appended to
        ``reference_write_log`` inside the same SQL transaction as the
        entity write (sec2 §2.14.9 / Decision 2.14.J).
        """
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

        # Multivalued reference slots get no column-equivalent in the
        # stored JSONB document (issue #79/#81 / ADR-0002) — only
        # relationships. The provenance patch below still records the
        # unstripped ``entity_data`` so as-of reconstruction sees the full
        # submitted payload.
        # Virtual inverse slots are derived, never written (ADR-0011).
        entity_data = self._strip_inverse_slots(entity_type, entity_data)
        stored_data = self._strip_multivalued_refs(entity_type, entity_data)

        with self._transaction() as conn:
            cur = conn.cursor()

            # Atomic upsert per design spec §2.3
            cur.execute(
                """INSERT INTO entities
                   (id, entity_type, is_available, version, data)
                   VALUES (%s, %s, TRUE, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       data = EXCLUDED.data,
                       version = entities.version + 1""",
                (
                    entity_id,
                    entity_type,
                    entity.version if hasattr(entity, "version") else 1,
                    json.dumps(stored_data),
                ),
            )

            provenance = PostgresProvenanceStore(conn, self._schema_version)
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
                cur.execute(
                    "INSERT INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (loader_name, version, entity_id) DO NOTHING",
                    (loader_name, version, entity_id, entity_type),
                )

        return entity

    @staticmethod
    def _jsonb_text(value: Any) -> str:
        """Render a filter value the way ``data->>field`` renders it.

        JSONB text extraction yields JSON literals for scalars —
        ``true``/``false`` for booleans — while ``str(False)`` is
        ``"False"``, which silently matches nothing (boolean facets on the
        DataHelix certification golden path, datahelix#45).
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    #: Base LinkML scalar range → SQL cast applied to both sides of a
    #: comparison predicate. ``data->>field`` extracts text; without the
    #: cast, numeric and temporal comparisons would be lexicographic on
    #: that text — a silently wrong answer (ADR-0006's named risk).
    _RANGE_CASTS: ClassVar[dict[str, str]] = {
        "integer": "::numeric",
        "float": "::numeric",
        "double": "::numeric",
        "decimal": "::numeric",
        "date": "::date",
        "datetime": "::timestamptz",
        "time": "::time",
    }

    def _leaf_predicate(
        self,
        field: str,
        op: str,
        value: Any,
        entity_type: Optional[str],
        params: list[Any],
        qual: str = "",
    ) -> str:
        """SQL predicate for one ``(field, op, value)`` leaf over the JSONB
        document, appending its parameters to ``params``. Shared by the flat
        ``filters`` path and the ``where`` tree compiler so operator
        rendering lives once (ADR-0006). ``qual`` scopes ``data`` to a
        table alias inside relationship EXISTS subqueries (M5a)."""
        doc = f"{qual}data"
        if op == "in":
            if not value:
                # Empty IN-list: short-circuit to "no rows match" rather
                # than `= ANY('{}')` semantics surprises.
                return "FALSE"
            params.append(field)
            params.append([self._jsonb_text(v) for v in value])
            return f"{doc}->>%s = ANY(%s)"
        if op == "is_null":
            # "No stored value" = key absent OR JSON null, matching the
            # SQLite column-IS-NULL semantics.
            params.append(field)
            params.append(field)
            return (
                f"(NOT jsonb_exists({doc}, %s) OR {doc}->%s = 'null'::jsonb)"
                if value
                else f"(jsonb_exists({doc}, %s) AND {doc}->%s <> 'null'::jsonb)"
            )
        if op == "neq":
            # Plain <> (not IS DISTINCT FROM): a missing key yields SQL
            # NULL and matches nothing, per the shared operator semantics
            # (ADR-0006).
            params.append(field)
            params.append(self._jsonb_text(value))
            return f"{doc}->>%s <> %s"
        if op == "contains":
            params.append(field)
            params.append(f"%{escape_like(str(value))}%")
            return f"{doc}->>%s ILIKE %s ESCAPE '\\'"
        if op in COMPARISON_SQL_OPS:
            cast = self._filter_cast(entity_type, field)
            sql_op = COMPARISON_SQL_OPS[op]
            params.append(field)
            params.append(value if cast else self._jsonb_text(value))
            return f"({doc}->>%s){cast} {sql_op} %s{cast}"
        params.append(field)
        params.append(self._jsonb_text(value))
        return f"{doc}->>%s = %s"

    def _reference_edge(
        self, entity_type: str, edge: str, quantifier: Optional[str]
    ) -> tuple[str, bool, Optional[str]]:
        """Resolve a relationship-predicate ``edge`` (M5a/M5b) to
        ``(target_class, multivalued, inverse_of)`` — mirror of the SQLite
        helper: loud on unknown edges and quantifier/cardinality
        mismatches. ``inverse_of`` names the forward FK slot when ``edge``
        is a virtual inverse slot (ADR-0011), else ``None``."""
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
        entity_type: Optional[str],
        params: list[Any],
        *,
        scope: str = "",
        alias_seq: Optional[list[int]] = None,
    ) -> str:
        """Compile a normalized ``where`` tree to one SQL predicate.

        ``scope`` is the SQL alias prefix (``'relN.'``) of the entities row
        the current subtree addresses (empty = the outer, unaliased
        ``entities``); ``alias_seq`` numbers relationship-EXISTS aliases so
        nested and self-referential edges (M5a) never collide.
        """
        if alias_seq is None:
            alias_seq = [0]
        if "and" in node:
            return (
                "("
                + " AND ".join(
                    self._tree_predicate(
                        c, entity_type, params,
                        scope=scope, alias_seq=alias_seq,
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
                        c, entity_type, params,
                        scope=scope, alias_seq=alias_seq,
                    )
                    for c in node["or"]
                )
                + ")"
            )
        if "not" in node:
            return "NOT " + self._tree_predicate(
                node["not"], entity_type, params,
                scope=scope, alias_seq=alias_seq,
            )
        if "edge" in node:
            # Relationship predicate (ADR-0006 M5a): one correlated EXISTS
            # against the target's entities row keyed on the FK value in
            # the JSONB document. Availability applies to the target
            # exactly as list queries would see it. Mirrors the SQLite
            # per-class-table compiler.
            from mosaic.core.exceptions import ValidationError

            if not entity_type:
                raise ValidationError(
                    message=(
                        "Relationship predicates require an entity_type on "
                        "the query: the edge resolves against the class's "
                        "reference slots (ADR-0006 M5a)."
                    ),
                    field_name=node["edge"],
                )
            edge = node["edge"]
            quantifier = node.get("quantifier")
            target, multivalued, inverse_of = self._reference_edge(
                entity_type, edge, quantifier
            )
            alias_seq[0] += 1
            alias = f"rel{alias_seq[0]}"
            outer = scope or "entities."
            if inverse_of is not None:
                # Reverse FK edge (ADR-0011): the relationship lives on the
                # *target* document as a single-valued FK key pointing at
                # this entity — `some` is EXISTS over the target type's rows
                # keyed on that key, `none` its NOT EXISTS complement. No
                # link table. Placeholder render order: forward key →
                # target type → subtree.
                params.append(inverse_of)
                params.append(list(self._inverse_slots(entity_type)[edge].tables))
                sub = self._tree_predicate(
                    node["where"], target, params,
                    scope=f"{alias}.", alias_seq=alias_seq,
                )
                exists = (
                    f"EXISTS (SELECT 1 FROM entities {alias} "
                    f"WHERE {alias}.data->>%s = {outer}id "
                    f"AND {alias}.entity_type = ANY(%s) "
                    f"AND {alias}.is_available = TRUE AND {sub})"
                )
                return exists if quantifier == "some" else f"NOT {exists}"
            if not multivalued:
                # psycopg substitutes positionally: the placeholders render
                # as edge key → target type → subtree, so the params must
                # append in exactly that order (subtree recursion last).
                params.append(edge)
                params.append(target)
                sub = self._tree_predicate(
                    node["where"], target, params,
                    scope=f"{alias}.", alias_seq=alias_seq,
                )
                return (
                    f"EXISTS (SELECT 1 FROM entities {alias} "
                    f"WHERE {alias}.id = {outer}data->>%s "
                    f"AND {alias}.entity_type = %s "
                    f"AND {alias}.is_available = TRUE AND {sub})"
                )
            # To-many (M5b): quantify over the ADR-0002 link table joined
            # to the target's entities row. Placeholder render order:
            # relationship_type → target type → subtree.
            link = f"{alias}l"
            params.append(edge)
            params.append(target)
            sub = self._tree_predicate(
                node["where"], target, params,
                scope=f"{alias}.", alias_seq=alias_seq,
            )
            exists = (
                f"EXISTS (SELECT 1 FROM relationships {link} "
                f"JOIN entities {alias} ON {alias}.id = {link}.target_id "
                f"WHERE {link}.source_id = {outer}id "
                f"AND {link}.relationship_type = %s "
                f"AND {link}.is_available = TRUE "
                f"AND {alias}.entity_type = %s "
                f"AND {alias}.is_available = TRUE AND {sub})"
            )
            return exists if quantifier == "some" else f"NOT {exists}"
        # COALESCE to two-valued logic: a missing key makes the extracted
        # text SQL NULL, and `NOT NULL` is NULL — which would silently
        # exclude rows under `not` that the Python mirror (``matches_tree``:
        # absent value => leaf is False, `not` flips to True) includes.
        # Forcing every leaf to TRUE/FALSE keeps the tree boolean end to
        # end (ADR-0006's mirror-consistency rule).
        leaf = self._leaf_predicate(
            node["field"], node["op"], node["value"], entity_type, params,
            qual=scope,
        )
        return f"COALESCE(({leaf}), FALSE)"

    def _filter_cast(self, entity_type: Optional[str], field: str) -> str:
        """SQL cast suffix for a comparison predicate on ``field``.

        Resolved from the slot's LinkML range (typeof chains included) via
        the schema registry; empty string = compare as text (strings and
        unknown ranges). Comparison operators require ``entity_type`` —
        without a class there is no slot range to drive the cast, and a
        lexicographic fallback on numeric data would be a silently wrong
        answer (issue #129's loud-over-wrong rule).
        """
        if entity_type is None:
            from mosaic.core.exceptions import ValidationError

            raise ValidationError(
                message=(
                    f"Comparison filter on field {field!r} requires an "
                    f"entity_type on the query: the slot's declared range "
                    f"drives the typed JSONB cast (ADR-0006)."
                ),
                field_name=field,
            )
        cached = self._filter_cast_cache.get(entity_type)
        if cached is None:
            registry = self.schema_registry
            cached = (
                {
                    name: self._RANGE_CASTS.get(base, "")
                    for name, base in registry.slot_base_ranges(
                        entity_type
                    ).items()
                }
                if registry is not None
                else {}
            )
            self._filter_cast_cache[entity_type] = cached
        return cached.get(field, "")

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
        """Update the stored document and record provenance.

        Postgres mirror of ``SQLiteAdapter.update_data`` — the ingestion
        service's update path calls this on any adapter, and Postgres
        previously didn't have it, so every SDK/transport update crashed
        (datahelix#45 golden path).
        """
        # See ``create`` — multivalued reference slots are stripped from the
        # stored JSONB document; the provenance patch keeps the full ``data``.
        # Virtual inverse slots are derived, never written (ADR-0011).
        data = self._strip_inverse_slots(entity_type, data)
        stored_data = self._strip_multivalued_refs(entity_type, data)

        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE entities SET data = %s, version = %s WHERE id = %s",
                (json.dumps(stored_data), new_version, entity_id),
            )

            provenance = PostgresProvenanceStore(conn, self._schema_version)
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
                cur.execute(
                    "INSERT INTO reference_write_log "
                    "(loader_name, version, entity_id, entity_type) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (loader_name, version, entity_id) DO NOTHING",
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
        """Flip ``is_available`` and record an ``availability_change``.

        Postgres mirror of the SQLite method; does not raise on missing
        rows so bulk loops can report per-entity success/failure.
        """
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE entities SET is_available = %s WHERE id = %s",
                (is_available, entity_id),
            )

            patch: dict[str, Any] = {"is_available": is_available}
            if reason is not None:
                patch["reason"] = reason
            provenance = PostgresProvenanceStore(conn, self._schema_version)
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
        """Mark an entity superseded (Postgres mirror of the SQLite method).

        Provenance is the caller's responsibility, matching SQLite.
        """
        del entity_type  # storage is generic; kept for interface parity.
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE entities "
                "SET is_available = FALSE, superseded_by = %s WHERE id = %s",
                (replacement_id, entity_id),
            )

    def read(self, entity_id: str) -> Optional[PostgresEntity]:
        """Read an entity by its ID (available entities only).

        Hydrates multivalued reference slot edges into ``entity.data``
        (issue #79/#81 / ADR-0002) — see ``_hydrate_multivalued_refs_batch``.
        """
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT id, entity_type, is_available, version, data,
                          superseded_by
                   FROM entities WHERE id = %s AND is_available = TRUE""",
                (entity_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            entity = self._row_to_entity(row)
            mv_refs = self._hydrate_derived_refs_batch(
                conn, entity.entity_type, [entity.id]
            ).get(entity.id)
            if mv_refs:
                entity.data.update(mv_refs)
            return entity

    def read_any(self, entity_id: str) -> Optional[PostgresEntity]:
        """Read an entity by its ID, regardless of availability.

        Hydrates multivalued reference slot edges into ``entity.data``,
        mirroring ``read`` (issue #79/#81 / ADR-0002).
        """
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT id, entity_type, is_available, version, data,
                          superseded_by
                   FROM entities WHERE id = %s""",
                (entity_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            entity = self._row_to_entity(row)
            mv_refs = self._hydrate_derived_refs_batch(
                conn, entity.entity_type, [entity.id]
            ).get(entity.id)
            if mv_refs:
                entity.data.update(mv_refs)
            return entity

    def resolve_type(self, entity_id: str) -> Optional[str]:
        """Return the entity_type for a given UUID, or None if unknown.

        Per sec9 §9.5's identity model — UUID → type resolution. Uses the
        existing `entities` table's type discriminator.
        """
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT entity_type FROM entities WHERE id = %s",
                (entity_id,),
            )
            row = cur.fetchone()
            return row["entity_type"] if row else None

    def resolve_types(self, entity_ids: list[str]) -> dict[str, str]:
        """Batch variant of ``resolve_type``. Returns a dict keyed by id.

        Unknown UUIDs are absent from the returned dict. One SQL round-trip
        regardless of input size.
        """
        if not entity_ids:
            return {}
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, entity_type FROM entities WHERE id = ANY(%s)",
                (list(entity_ids),),
            )
            return {row["id"]: row["entity_type"] for row in cur.fetchall()}

    def update(self, entity: PostgresEntity) -> PostgresEntity:
        """Update an existing entity (no-op on entities table; temporal tracking is via ProvenanceRecord)."""
        return entity

    def delete(self, entity_id: str, user_context: Optional[str] = None) -> bool:
        """Delete an entity by its ID (soft delete)."""
        with self._transaction() as conn:
            cur = conn.cursor()

            cur.execute(
                "SELECT id, entity_type, data FROM entities WHERE id = %s AND is_available = TRUE",
                (entity_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False

            entity_type = row["entity_type"]
            original_data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
            # The stored JSONB document has multivalued reference slots
            # stripped (issue #79/#81 / ADR-0002); re-hydrate them from
            # relationships so the deletion snapshot carries the full
            # payload, mirroring ``SQLiteAdapter.delete``'s use of
            # ``self.read()`` (which hydrates) for its snapshot.
            mv_refs = self._hydrate_multivalued_refs_batch(
                conn, entity_type, [entity_id]
            ).get(entity_id)
            if mv_refs:
                original_data = {**original_data, **mv_refs}
            # Virtual inverse slots (ADR-0011) are derived and stay out of
            # the snapshot, so as-of reconstruction never replays a stale
            # reverse list.
            original_data = self._strip_inverse_slots(entity_type, original_data)

            cur.execute(
                """UPDATE entities SET is_available = FALSE
                   WHERE id = %s AND is_available = TRUE""",
                (entity_id,),
            )

            provenance = PostgresProvenanceStore(conn, self._schema_version)
            provenance.record(
                entity_id=entity_id,
                entity_type=entity_type,
                operation="availability_change",
                actor_id=user_context,
                patch={"status": "deleted", "is_available": False, "data": original_data},
            )

            return True

    def find(
        self, query: Query, *, as_of: Optional[str] = None
    ) -> Iterator[PostgresEntity]:
        """Find entities matching a query.

        When ``as_of`` (ISO-8601) is given, the result is reconstructed from the
        provenance log as the graph stood at that transaction-time (sec6 §6.8 /
        ADR-0001) — Postgres parity with the SQLite adapter. Omitted = current.
        """
        if as_of is not None:
            yield from self._find_as_of(query, as_of)
            return
        with self._transaction() as conn:
            cur = conn.cursor()

            params: list[Any] = []
            sql = (
                "SELECT id, entity_type, is_available, version, data, "
                "superseded_by FROM entities "
                f"WHERE {self._query_predicate(query, params)}"
            )
            sql += self._order_clause(query, params)

            # `is not None`, not truthiness: LIMIT 0 must return zero rows,
            # not fall through to "no limit" (issue #130).
            if query.limit is not None:
                sql += " LIMIT %s"
                params.append(query.limit)
                if query.offset:
                    sql += " OFFSET %s"
                    params.append(query.offset)
            elif query.offset:
                sql += " OFFSET %s"
                params.append(query.offset)

            cur.execute(sql, params)
            entities = [self._row_to_entity(row) for row in cur.fetchall()]
            # Batched hydration (issue #79/#81 / ADR-0002): one round-trip
            # per distinct entity type present in the result set, not one
            # per entity. Still inside the connection's transaction scope
            # so this runs before the ``with`` block is exited.
            self._hydrate_multivalued_refs_for_entities(conn, entities)
            yield from entities

    def _query_predicate(self, query: Query, params: list[Any]) -> str:
        """The WHERE clause (sans leading keyword) ``find``/``count``/the
        aggregates all share, so every surface sees exactly the same rows
        (ADR-0007's availability-consistency rule)."""
        sql = "is_available = TRUE"
        if query.entity_type:
            sql += " AND entity_type = %s"
            params.append(query.entity_type)

        if query.filters:
            joiner = " OR " if getattr(query, "filter_mode", "and") == "or" else " AND "
            filter_clauses = []
            for f in query.filters:
                for field, op, value in normalize_filter(f):
                    filter_clauses.append(
                        self._leaf_predicate(
                            field, op, value, query.entity_type, params
                        )
                    )
            if filter_clauses:
                sql += " AND (" + joiner.join(filter_clauses) + ")"

        where = getattr(query, "where", None)
        if where is not None:
            # Boolean filter tree (ADR-0006 increment 2); composes with
            # the flat ``filters`` by AND.
            tree = normalize_where(where)
            sql += " AND (" + self._tree_predicate(
                tree, query.entity_type, params
            ) + ")"
        return sql

    def _orderable_fields(self, entity_type: str) -> set[str]:
        """Fields ordering/aggregation may address: the class's single-valued
        slots (multivalued slots store JSON arrays — no scalar order) plus
        ``id``. Mirrors the SQLite adapter's typed-column set."""
        registry = self.schema_registry
        slots = (
            set(registry.slot_base_ranges(entity_type))
            if registry is not None
            else set()
        )
        return slots | {"id"}

    def _order_clause(self, query: Query, params: list[Any]) -> str:
        """SQL ORDER BY for ``query.order_by`` (ADR-0007): stable ``id``
        tiebreak; NULLs last regardless of direction (matching SQLite so
        orderings never diverge across backends). Range-driven cast so
        numeric/temporal slots order by value, not by their JSON text."""
        order_by = getattr(query, "order_by", None)
        if not order_by:
            return ""
        from mosaic.core.exceptions import ValidationError

        if not query.entity_type:
            raise ValidationError(
                message=(
                    "order_by requires an entity type on the query: the "
                    "slot's declared range drives the typed ordering "
                    "(ADR-0007)."
                ),
                field_name="order_by",
            )
        valid = self._orderable_fields(query.entity_type)
        if order_by not in valid:
            raise ValidationError(
                message=(
                    f"Unknown order_by column {order_by!r}; orderable "
                    f"columns: {sorted(valid)}. Computed temporal "
                    f"fields are provenance-derived, not columns, and "
                    f"cannot be ordered on (ADR-0007)."
                ),
                field_name="order_by",
            )
        direction = (
            "DESC" if getattr(query, "order_dir", "asc") == "desc" else "ASC"
        )
        if order_by == "id":
            return f" ORDER BY id {direction}"
        cast = self._filter_cast(query.entity_type, order_by)
        params.append(order_by)
        return (
            f" ORDER BY (data->>%s){cast} {direction} NULLS LAST, id ASC"
        )

    def count(self, query: Query, *, as_of: Optional[str] = None) -> int:
        """Count matching entities without materializing them (ADR-0007).

        Pushed down as ``COUNT(*)`` under the exact predicate ``find`` uses
        (availability-consistency rule). ``query.limit``/``offset`` are
        ignored — a count is over the whole match set. Under ``as_of`` the
        count is the length of the reconstructed match set (the documented
        Python-path semantics)."""
        if as_of is not None:
            unpaged = Query(
                entity_type=query.entity_type,
                filters=query.filters,
                filter_mode=query.filter_mode,
                where=query.where,
            )
            return sum(1 for _ in self._find_as_of(unpaged, as_of))
        params: list[Any] = []
        sql = (
            "SELECT COUNT(*) AS c FROM entities "
            f"WHERE {self._query_predicate(query, params)}"
        )
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return int(cur.fetchone()["c"])

    def facet_counts(self, query: Query, field: str) -> List[tuple]:
        """Per-value counts for ``field`` under ``query``'s predicate —
        ``[(value, count), ...]``, count desc then value asc (ADR-0007).
        NULL/absent values are not counted. Values come back natively
        decoded (``data->field`` jsonb), matching what list queries return.
        """
        entity_type = self._require_aggregate_field(
            query, field, surface="facet_counts"
        )
        params: list[Any] = []
        predicate = self._query_predicate(query, params)
        # data->field (jsonb, native decode) both buckets and orders the
        # values: jsonb ordering is numeric for numbers and collation-based
        # for strings, matching the SQLite typed-column ordering. Ordinal
        # GROUP BY/ORDER BY: repeating the parameter would render as a
        # *different* placeholder, which Postgres rejects as an ungrouped
        # expression.
        sql = (
            "SELECT data->%s AS value, COUNT(*) AS c FROM entities "
            f"WHERE {predicate} "
            "AND jsonb_exists(data, %s) AND data->%s <> 'null'::jsonb "
            "GROUP BY 1 ORDER BY 2 DESC, 1 ASC"
        )
        all_params = [field] + params + [field, field]
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(sql, all_params)
            rows = cur.fetchall()
        return [
            (self._normalize_aggregate_value(row["value"]), int(row["c"]))
            for row in rows
        ]

    def field_range(self, query: Query, field: str) -> tuple:
        """``(min, max)`` of ``field`` under ``query``'s predicate
        (ADR-0007); ``(None, None)`` when no matching entity has a value.
        The slot's range drives the cast so numeric/temporal slots
        aggregate by value, not by JSON text."""
        entity_type = self._require_aggregate_field(
            query, field, surface="field_range"
        )
        cast = self._filter_cast(entity_type, field)
        params: list[Any] = []
        predicate = self._query_predicate(query, params)
        sql = (
            f"SELECT MIN((data->>%s){cast}) AS lo, "
            f"MAX((data->>%s){cast}) AS hi "
            f"FROM entities WHERE {predicate}"
        )
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(sql, [field, field] + params)
            row = cur.fetchone()
        return (
            self._normalize_aggregate_value(row["lo"]),
            self._normalize_aggregate_value(row["hi"]),
        )

    def count_relationship(
        self, entity_type: str, entity_id: str, edge: str
    ) -> int:
        """Cheap cardinality of a multivalued reference edge (issue #132) —
        mirrors the SQLite adapter; joins to ``entities`` filtered by
        ``entity_type`` pending ADR-0008's per-class-table convergence.
        """
        target, _, inverse_of = self._reference_edge(entity_type, edge, "some")
        if inverse_of is not None:
            # Virtual inverse slot (ADR-0011): count the available target
            # documents whose forward FK key points at this entity.
            with self._transaction() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) AS c FROM entities tgt "
                    "WHERE tgt.entity_type = ANY(%s) AND tgt.data->>%s = %s "
                    "AND tgt.is_available = TRUE",
                    (
                        list(self._inverse_slots(entity_type)[edge].tables),
                        inverse_of,
                        entity_id,
                    ),
                )
                return int(cur.fetchone()["c"])
        sql = (
            "SELECT COUNT(*) AS c FROM relationships rel "
            "JOIN entities tgt ON tgt.id = rel.target_id "
            "WHERE rel.source_id = %s AND rel.relationship_type = %s "
            "AND rel.is_available = TRUE AND tgt.entity_type = %s "
            "AND tgt.is_available = TRUE"
        )
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(sql, (entity_id, edge, target))
            return int(cur.fetchone()["c"])

    def _require_aggregate_field(
        self, query: Query, field: str, *, surface: str
    ) -> str:
        """Shared validation for the per-field aggregates — mirrors the
        SQLite adapter: an entity type is required and ``field`` must be a
        single-valued slot (or ``id``) of that class."""
        from mosaic.core.exceptions import ValidationError

        if not query.entity_type:
            raise ValidationError(
                message=(
                    f"{surface}() requires an entity type: facets and "
                    f"ranges are per-class aggregates over typed columns."
                ),
                field_name=field,
            )
        valid = self._orderable_fields(query.entity_type)
        if field not in valid:
            raise ValidationError(
                message=(
                    f"Unknown {surface} field {field!r} for "
                    f"{query.entity_type}; valid columns: {sorted(valid)}. "
                    f"Computed temporal fields are provenance-derived, not "
                    f"columns, and cannot be aggregated on (ADR-0007)."
                ),
                field_name=field,
            )
        return query.entity_type

    @staticmethod
    def _normalize_aggregate_value(value: Any) -> Any:
        """Normalize driver-native aggregate scalars to the Python types the
        SQLite adapter returns, so aggregate results never diverge across
        backends: ``Decimal`` (numeric casts) → int/float; date/datetime/time
        (temporal casts) → ISO-8601 strings (slots store ISO strings)."""
        if isinstance(value, Decimal):
            as_int = int(value)
            return as_int if value == as_int else float(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return value

    def _find_as_of(
        self, query: Query, as_of: str
    ) -> Iterator[PostgresEntity]:
        """Query-spanning as-of reconstruction (sec6 §6.8) — Postgres mirror of
        the SQLite path: candidates created <= as_of, reconstructed via
        get_state_at, filters in Python (shared operator evaluator),
        offset/limit last."""
        if getattr(query, "order_by", None):
            from mosaic.core.exceptions import ValidationError

            raise ValidationError(
                message=(
                    "order_by is not supported together with as_of: ordering "
                    "pushdown targets current-state storage, and the "
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
            prov = PostgresProvenanceStore(conn, self._schema_version)
            candidates = prov.entities_created_by(
                as_of, entity_type=query.entity_type
            )

            matched: list[PostgresEntity] = []
            for entity_id, entity_type in candidates:
                reconstructed = prov.get_state_at(entity_id, as_of)
                if reconstructed is None:
                    continue
                data = reconstructed.get("state")
                if not isinstance(data, dict):
                    continue
                if not self._matches_filters(data, entity_id, query):
                    continue
                matched.append(
                    PostgresEntity(
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
        """Match query.filters against reconstructed data (honoring
        filter_mode). Mirrors the SQLite as-of path; ``id`` resolves to
        ``entity_id``.

        Operator semantics live once in
        :func:`mosaic.core.storage.matches_operator`, shared with the
        SQLite mirror so as-of results never diverge from the SQL
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

    def findAll(self) -> Iterator[PostgresEntity]:
        """Find all entities."""
        return self.find(Query())

    def findBy(self, **kwargs: Any) -> Iterator[PostgresEntity]:
        """Find entities by field values."""
        query = Query()
        query.filters = [kwargs]
        return self.find(query)

    def search(
        self,
        query: str,
        entity_type: str,
        field_name: str,
        min_score: float = 0.0,
        limit: int = 100,
    ) -> list[ScoredMatch]:
        """Search entities using PostgreSQL full-text search with ts_rank.

        Uses tsvector/tsquery for text search with relevance ranking.
        Falls back to trigram similarity for fuzzy matching when tsquery
        returns no results.
        """
        if limit <= 0:
            limit = 100
        limit = min(limit, 1000)

        fts_table_name = f"fts_{entity_type.lower()}_{field_name.lower()}"

        with self._transaction() as conn:
            cur = conn.cursor()

            # Check FTS table exists
            cur.execute(
                """SELECT EXISTS (
                    SELECT 1 FROM pg_tables
                    WHERE schemaname = 'public' AND tablename = %s
                )""",
                (fts_table_name,),
            )
            if not cur.fetchone()["exists"]:
                raise SearchCapabilityError(
                    message=f"Field '{field_name}' on entity type '{entity_type}' is not FTS-indexed",
                    field_name=field_name,
                    entity_type=entity_type,
                )

            # Full-text search with ts_rank
            cur.execute(
                f"""SELECT fts.entity_id,
                           ts_rank(fts.content_tsvector, plainto_tsquery('english', %s)) AS rank_score
                    FROM {fts_table_name} fts
                    INNER JOIN entities e ON fts.entity_id = e.id
                    WHERE fts.content_tsvector @@ plainto_tsquery('english', %s)
                    AND e.entity_type = %s
                    AND e.is_available = TRUE
                    ORDER BY rank_score DESC
                    LIMIT %s""",
                (query, query, entity_type, limit * 2),
            )

            rows = cur.fetchall()

            # If no tsquery results, fall back to trigram similarity
            if not rows:
                cur.execute(
                    f"""SELECT fts.entity_id,
                               similarity(fts.content, %s) AS rank_score
                        FROM {fts_table_name} fts
                        INNER JOIN entities e ON fts.entity_id = e.id
                        WHERE similarity(fts.content, %s) > 0.1
                        AND e.entity_type = %s
                        AND e.is_available = TRUE
                        ORDER BY rank_score DESC
                        LIMIT %s""",
                    (query, query, entity_type, limit * 2),
                )
                rows = cur.fetchall()

            results = []
            max_score = None
            for row in rows:
                if max_score is None:
                    max_score = row["rank_score"] if row["rank_score"] > 0 else 1.0

                normalized = row["rank_score"] / max_score if max_score > 0 else 0.0

                if normalized >= min_score:
                    results.append(
                        ScoredMatch(
                            entity_id=row["entity_id"],
                            score=normalized,
                            highlights=None,
                        )
                    )

            results.sort(key=lambda x: x.score, reverse=True)
            return results[:limit]

    # ------------------------------------------------------------------
    # FTS management
    # ------------------------------------------------------------------

    def create_fts_table(
        self,
        table_name: str,
        columns: list[str],
        content_table: str = "entities",
    ) -> None:
        with self._transaction() as conn:
            fts = PostgresFTSStore(conn)
            fts.create_fts_table(table_name, columns)

    def search_fts(
        self,
        table_name: str,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            fts = PostgresFTSStore(conn)
            return fts.search_fts(table_name, query, limit)

    def get_fts_tables_for_entity_type(self, entity_type: str) -> list[str]:
        with self._transaction() as conn:
            fts = PostgresFTSStore(conn)
            return fts.get_fts_tables_for_entity_type(entity_type)

    # ------------------------------------------------------------------
    # Provenance / tracking
    # ------------------------------------------------------------------

    def track_creation(
        self, entity: PostgresEntity, metadata: Dict[str, Any]
    ) -> ProvenanceRecordType:
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
        self, entity: PostgresEntity, metadata: Dict[str, Any]
    ) -> ProvenanceRecordType:
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
        return ProvenanceRecordType(
            timestamp=datetime.now(timezone.utc),
            operation="availability_change",
            entity_type="unknown",
            entity_id=entity_id,
            actor_id="",
            schema_version=self._schema_version,
            patch={"status": "deleted", **metadata},
        )

    # -- hippo_external_xref side index (issue #48) --------------------------

    _XREF_NOT_IMPLEMENTED = (
        "hippo_external_xref reverse lookup is not implemented on the "
        "PostgreSQL adapter yet. The xref side index (hippo_xref_index) "
        "is maintained by the SQLite adapter's per-class write path; the "
        "PostgreSQL adapter still uses the legacy single-table `entities` "
        "layout and gains xref parity together with its per-class-table "
        "migration. Use the SQLite adapter for hippo_external_xref-"
        "annotated schemas, or query the ExternalReference slot data "
        "directly."
    )

    def find_xref(self, system: str, value: str) -> Optional[dict[str, Any]]:
        """Reverse-lookup an entity by ``(system, value)``. Not implemented.

        See :data:`_XREF_NOT_IMPLEMENTED` — PostgreSQL xref parity is
        scoped to the adapter's per-class-table migration.
        """
        raise NotImplementedError(self._XREF_NOT_IMPLEMENTED)

    def list_xrefs(self, entity_id: str) -> list[dict[str, Any]]:
        """List indexed xref pairs for an entity. Not implemented.

        See :data:`_XREF_NOT_IMPLEMENTED` — PostgreSQL xref parity is
        scoped to the adapter's per-class-table migration.
        """
        raise NotImplementedError(self._XREF_NOT_IMPLEMENTED)

    def search_capabilities(self) -> set[str]:
        """PostgreSQL adapter supports FTS and trigram fuzzy search."""
        return {"fts", "trigram"}

    def entity_counts(self) -> dict[str, int]:
        """Count stored entities per entity type.

        Because Hippo never hard-deletes, the count for a type includes
        unavailable (soft-deleted / superseded) entities.

        Returns:
            Mapping of entity type name → total entity count, for types
            with at least one entity.
        """
        with self._transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT entity_type, COUNT(*) AS c FROM entities "
                "GROUP BY entity_type ORDER BY entity_type"
            )
            return {row["entity_type"]: row["c"] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Temporal queries
    # ------------------------------------------------------------------

    def history(self, entity_id: str) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            provenance = PostgresProvenanceStore(conn, self._schema_version)
            return provenance.get_history(entity_id)

    def get_temporal(
        self, entity_ids: list[str], *, as_of: Optional[str] = None
    ) -> "dict[str, TemporalRecord]":
        """Batch sec9 §9.7 temporal-field derivation. One SQL round-trip.

        ``as_of`` bounds the derivation to ``timestamp <= as_of`` (sec6 §6.8)."""
        with self._transaction() as conn:
            provenance = PostgresProvenanceStore(conn, self._schema_version)
            return provenance.get_temporal(entity_ids, as_of=as_of)

    def state_at(self, entity_id: str, timestamp: str) -> Optional[dict[str, Any]]:
        with self._transaction() as conn:
            provenance = PostgresProvenanceStore(conn, self._schema_version)

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _row_to_entity(self, row: dict) -> PostgresEntity:
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)

        return PostgresEntity(
            id=row["id"],
            entity_type=row["entity_type"],
            is_available=bool(row["is_available"]),
            version=row["version"],
            data=data if data else {},
            superseded_by=row.get("superseded_by"),
        )

    def _get_provenance_store(
        self, conn: psycopg.Connection
    ) -> PostgresProvenanceStore:
        return PostgresProvenanceStore(conn, self._schema_version)

    def _get_relationship_store(
        self, conn: psycopg.Connection
    ) -> PostgresRelationshipStore:
        return PostgresRelationshipStore(conn)

    def _get_external_id_store(
        self, conn: psycopg.Connection
    ) -> PostgresExternalIdStore:
        return PostgresExternalIdStore(conn)

    def _get_fts_store(self, conn: psycopg.Connection) -> PostgresFTSStore:
        return PostgresFTSStore(conn)

    def close(self) -> None:
        """Close the connection pool."""
        if hasattr(self, "_pool") and self._pool:
            self._pool.close()
