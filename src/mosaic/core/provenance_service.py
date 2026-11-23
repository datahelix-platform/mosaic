"""ProvenanceService - Version history, audit trail, and entity supersession facade."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from mosaic.core.exceptions import (
    EntityAlreadySupersededError,
    EntityNotFoundError,
)
from mosaic.core.kind_check import check_kind
from mosaic.core.storage import Query
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter, SQLiteEntity


_DEFAULT_SOURCE_SYSTEM = "default"


class ProvenanceService:
    """Manages provenance history, external ID operations, and entity supersession.

    This facade owns all provenance-related logic extracted from MosaicClient.
    """

    def __init__(self, storage: Optional[SQLiteAdapter] = None) -> None:
        self._storage = storage

    def get_provenance_summary_map(self, entity_type: str) -> dict[str, dict]:
        """Get a map of entity_id -> provenance timestamps for all entities of a type.

        Uses the entity_provenance_summary view for efficient batch derivation.
        """
        if self._storage is None:
            return {}

        result = {}
        try:
            with self._storage._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT entity_id, created_at, updated_at
                       FROM entity_provenance_summary
                       WHERE entity_type = ?""",
                    (entity_type,),
                )
                for row in cursor.fetchall():
                    result[row["entity_id"]] = {
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
        except Exception:
            pass
        return result

    def register_external_id(
        self,
        entity_id: str,
        external_id: str,
        source_system: str = _DEFAULT_SOURCE_SYSTEM,
    ) -> dict[str, Any]:
        """Register an external ID for an entity.

        Creates an ``ExternalID`` entity via the normal entity machinery
        (per-class typed table + provenance ``create`` record) and
        records an ``external_id_add`` ``ProvenanceRecord`` on the parent
        entity tying the two together. ``source_system`` defaults to
        ``"default"`` so legacy 2-arg callers keep working; new callers
        should pass it explicitly.
        """
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        read_fn = getattr(self._storage, "read_any", self._storage.read)
        parent = read_fn(entity_id)
        if parent is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        ext_id_uuid = str(uuid.uuid4())
        ext_entity = SQLiteEntity(
            id=ext_id_uuid,
            entity_type="ExternalID",
            is_available=True,
            version=1,
            data={
                "value": external_id,
                "source_system": source_system,
                "entity": entity_id,
                "is_active": True,
            },
        )
        self._storage.create(ext_entity)

        created_at = self._creation_time(ext_id_uuid) or datetime.now(
            timezone.utc
        ).isoformat()

        with self._storage._transaction() as conn:
            prov_store = self._storage._get_provenance_store(conn)
            prov_store.record(
                entity_id=entity_id,
                entity_type=parent.entity_type,
                operation="external_id_add",
                derived_from_id=ext_id_uuid,
                patch={
                    "external_id_uuid": ext_id_uuid,
                    "value": external_id,
                    "source_system": source_system,
                },
            )

        return {
            "id": ext_id_uuid,
            "entity_id": entity_id,
            "external_id": external_id,
            "source_system": source_system,
            "created_at": created_at,
            "superseded_at": None,
        }

    def supersede(
        self,
        entity_id: str,
        old_external_id: str,
        new_external_id: str,
        source_system: str = _DEFAULT_SOURCE_SYSTEM,
    ) -> dict[str, Any]:
        """Supersede an entity's external ID with a new one.

        Soft-update: flips ``is_active = false`` on the old ``ExternalID``
        row, inserts a fresh ``ExternalID`` row for ``new_external_id``,
        and records a ``supersede`` ``ProvenanceRecord`` linking the two.
        The supersede operation is scoped to a single ``source_system``;
        callers managing multiple source systems must pass it explicitly.
        """
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        read_fn = getattr(self._storage, "read_any", self._storage.read)
        parent = read_fn(entity_id)
        if parent is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        old_record = self._find_active_external_id(
            entity_id=entity_id,
            value=old_external_id,
            source_system=source_system,
        )

        new_uuid = str(uuid.uuid4())
        new_entity = SQLiteEntity(
            id=new_uuid,
            entity_type="ExternalID",
            is_available=True,
            version=1,
            data={
                "value": new_external_id,
                "source_system": source_system,
                "entity": entity_id,
                "is_active": True,
            },
        )
        self._storage.create(new_entity)

        if old_record is not None:
            old_uuid = old_record.id
            new_old_data = dict(old_record.data)
            new_old_data["is_active"] = False
            self._storage.update_data(
                entity_id=old_uuid,
                entity_type="ExternalID",
                data=new_old_data,
                new_version=(old_record.version or 1) + 1,
                operation="update",
            )

            with self._storage._transaction() as conn:
                prov_store = self._storage._get_provenance_store(conn)
                prov_store.record(
                    entity_id=new_uuid,
                    entity_type="ExternalID",
                    operation="supersede",
                    derived_from_id=old_uuid,
                    patch={
                        "supersedes": old_uuid,
                        "old_value": old_external_id,
                        "new_value": new_external_id,
                        "source_system": source_system,
                    },
                )

        with self._storage._transaction() as conn:
            prov_store = self._storage._get_provenance_store(conn)
            prov_store.record(
                entity_id=entity_id,
                entity_type=parent.entity_type,
                operation="external_id_add",
                derived_from_id=new_uuid,
                patch={
                    "external_id_uuid": new_uuid,
                    "value": new_external_id,
                    "source_system": source_system,
                    "supersedes_value": old_external_id,
                },
            )

        created_at = self._creation_time(new_uuid) or datetime.now(
            timezone.utc
        ).isoformat()

        return {
            "id": new_uuid,
            "entity_id": entity_id,
            "external_id": new_external_id,
            "source_system": source_system,
            "created_at": created_at,
            "superseded_at": None,
        }

    def supersede_entity(
        self,
        entity_id: str,
        replacement_id: str,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
        entity_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Mark an entity as superseded by a replacement entity.

        Atomic operation: marks source unavailable, writes provenance events,
        creates superseded_by relationship edge. With ``entity_type`` the
        source must be of that kind or a concrete subclass (#289);
        ``replacement_id`` only has to exist.
        """
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        read_fn = getattr(self._storage, "read_any", self._storage.read)
        source_entity = read_fn(entity_id)
        if source_entity is None:
            raise EntityNotFoundError(
                message=f"Source entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )
        if entity_type is not None:
            check_kind(self._storage, entity_type, source_entity.entity_type, entity_id)

        replacement_entity = self._storage.read(replacement_id)
        if replacement_entity is None:
            raise EntityNotFoundError(
                message=f"Replacement entity not found: {replacement_id}",
                entity_type="unknown",
                entity_id=replacement_id,
            )

        if source_entity.superseded_by is not None:
            raise EntityAlreadySupersededError(
                message=f"Entity {entity_id} is already superseded by {source_entity.superseded_by}",
                entity_id=entity_id,
                superseded_by=source_entity.superseded_by,
            )

        now = datetime.now(timezone.utc).isoformat()

        # Per sec9 §9.6, supersession must be atomic: the entity-level
        # state change, both provenance entries, and the relationship
        # edge live in one transaction. The adapter's
        # ``_set_per_class_superseded_by`` helper participates in the
        # caller-owned cursor; the public ``mark_superseded`` wrapper is
        # reserved for standalone callers.
        with self._storage._transaction() as conn:
            cursor = conn.cursor()
            self._storage._set_per_class_superseded_by(
                cursor,
                source_entity.entity_type,
                entity_id,
                replacement_id,
                is_available=False,
            )

            prov_store = self._storage._get_provenance_store(conn)
            prov_patch: dict[str, Any] = {}
            if reason is not None:
                prov_patch["reason"] = reason
            prov_store.record(
                entity_id=entity_id,
                entity_type=source_entity.entity_type,
                operation="supersede",
                actor_id=actor,
                derived_from_id=replacement_id,
                patch=prov_patch or None,
            )

            rel_store = self._storage._get_relationship_store(conn)
            rel_store.create(
                source_id=entity_id,
                target_id=replacement_id,
                relationship_type="superseded_by",
                created_by=actor,
                metadata={"reason": reason} if reason else None,
            )

            # Record a full post-image patch (the replacement's current data),
            # NOT a sparse annotation — so the "update patch = full post-image"
            # invariant holds universally and as-of state reconstruction
            # (sec6 §6.8.2) returns the replacement's real data, not an
            # annotation. The audit note moves to ``context``.
            prov_store.record(
                entity_id=replacement_id,
                entity_type=replacement_entity.entity_type,
                operation="update",
                actor_id=actor,
                patch=dict(replacement_entity.data),
                context={
                    "note": f"Now the active replacement for superseded entity {entity_id}",
                    "supersedes": entity_id,
                },
            )

        return {
            "entity_id": entity_id,
            "replacement_id": replacement_id,
            "superseded_at": now,
            "reason": reason,
        }

    def get_by_external_id(
        self, external_id: str, include_archived: bool = False
    ) -> dict[str, Any]:
        """Get an entity by its external ID.

        Queries the ``ExternalID`` per-class table for active mappings
        (``is_active = 1``) matching ``external_id``. When multiple active
        mappings exist (different ``source_system`` values), the most
        recently created mapping wins. When ``include_archived`` is
        False (default), entries whose parent entity is unavailable are
        filtered out.
        """
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"No entity found with external ID: {external_id}",
                entity_type="unknown",
                entity_id="unknown",
            )

        candidates = self._find_active_externals_by_value(external_id)
        candidates_with_time = []
        for ext in candidates:
            created_at = self._creation_time(ext.id)
            candidates_with_time.append((created_at or "", ext))

        candidates_with_time.sort(key=lambda pair: pair[0], reverse=True)

        record = None
        record_created_at: Optional[str] = None
        parent_entity = None
        for created_at, ext in candidates_with_time:
            parent_id = ext.data.get("entity")
            if not parent_id:
                continue
            if include_archived:
                candidate_parent = self._storage.read_any(parent_id)
            else:
                candidate_parent = self._storage.read(parent_id)
            if candidate_parent is None:
                continue
            record = ext
            record_created_at = created_at or None
            parent_entity = candidate_parent
            break

        if record is None or parent_entity is None:
            raise EntityNotFoundError(
                message=f"No entity found with external ID: {external_id}",
                entity_type="unknown",
                entity_id="unknown",
            )

        temporal_map = self._storage.get_temporal([parent_entity.id])
        temporal = temporal_map.get(parent_entity.id)
        return {
            "id": parent_entity.id,
            "entity_type": parent_entity.entity_type,
            "data": parent_entity.data,
            "version": parent_entity.version,
            "created_at": temporal.created_at if temporal else None,
            "updated_at": temporal.updated_at if temporal else None,
            "external_id": record.data.get("value"),
            "source_system": record.data.get("source_system"),
            "external_id_created_at": record_created_at,
        }

    def list_external_ids(
        self, entity_id: str, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """List all external IDs for an entity.

        Queries the ``ExternalID`` per-class table for rows whose
        ``entity`` slot equals ``entity_id``. By default only currently-
        active mappings (``is_active = 1``) are returned; superseded
        mappings (``is_active = 0``) are included when
        ``include_superseded=True``.
        """
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        read_fn = getattr(self._storage, "read_any", self._storage.read)
        entity = read_fn(entity_id)
        if entity is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        records = self._list_externals_for_entity(
            entity_id=entity_id, include_superseded=include_superseded
        )

        results = []
        for ext in records:
            created_at = self._creation_time(ext.id)
            superseded_at = (
                None if ext.data.get("is_active") else self._supersede_time(ext.id)
            )
            results.append(
                {
                    "id": ext.id,
                    "entity_id": ext.data.get("entity"),
                    "external_id": ext.data.get("value"),
                    "source_system": ext.data.get("source_system"),
                    "created_at": created_at,
                    "superseded_at": superseded_at,
                }
            )
        results.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return results

    def history(self, entity_id: str) -> list[dict[str, Any]]:
        """Get the change history for an entity."""
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        read_fn = getattr(self._storage, "read_any", self._storage.read)
        entity = read_fn(entity_id)
        if entity is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        return self._storage.history(entity_id)

    def state_at(self, entity_id: str, timestamp: str) -> Optional[dict[str, Any]]:
        """Get the entity state at a specific point in time."""
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        entity = self._storage.read(entity_id)
        if entity is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type="unknown",
                entity_id=entity_id,
            )

        return self._storage.state_at(entity_id, timestamp)

    # ------------------------------------------------------------------
    # Internal helpers for ExternalID queries
    # ------------------------------------------------------------------

    def _find_active_external_id(
        self,
        entity_id: str,
        value: str,
        source_system: str,
    ) -> Optional[SQLiteEntity]:
        """Return the active ``ExternalID`` row matching the triple, if any."""
        query = Query(
            entity_type="ExternalID",
            filters=[
                {"field": "value", "value": value},
                {"field": "source_system", "value": source_system},
                {"field": "entity", "value": entity_id},
                {"field": "is_active", "value": True},
            ],
            limit=1,
        )
        for row in self._storage.find(query):
            return row
        return None

    def _find_active_externals_by_value(self, value: str) -> list[SQLiteEntity]:
        """Return all active ``ExternalID`` rows for a given value (any source)."""
        query = Query(
            entity_type="ExternalID",
            filters=[
                {"field": "value", "value": value},
                {"field": "is_active", "value": True},
            ],
        )
        return list(self._storage.find(query))

    def _list_externals_for_entity(
        self, entity_id: str, include_superseded: bool
    ) -> list[SQLiteEntity]:
        """Return ``ExternalID`` rows for a parent entity.

        ``include_superseded=True`` includes ``is_active = 0`` rows; the
        per-class ``find`` only surfaces ``is_available = 1`` entries, so
        soft-deleted ExternalID rows are still excluded.
        """
        filters: list[dict[str, Any]] = [{"field": "entity", "value": entity_id}]
        if not include_superseded:
            filters.append({"field": "is_active", "value": True})
        query = Query(entity_type="ExternalID", filters=filters)
        return list(self._storage.find(query))

    def _creation_time(self, entity_id: str) -> Optional[str]:
        """Return the ISO timestamp of the ``create`` provenance record."""
        with self._storage._transaction() as conn:
            prov_store = self._storage._get_provenance_store(conn)
            return prov_store.get_entity_creation_time(entity_id)

    def _supersede_time(self, entity_id: str) -> Optional[str]:
        """Return the ISO timestamp of the ``supersede`` provenance record."""
        with self._storage._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT timestamp FROM "ProvenanceRecord"
                   WHERE derived_from_id = ? AND operation = 'supersede'
                   ORDER BY timestamp DESC LIMIT 1""",
                (entity_id,),
            )
            row = cursor.fetchone()
            return row["timestamp"] if row else None
