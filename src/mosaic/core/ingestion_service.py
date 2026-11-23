"""IngestionService - Entity write operations (create, update, upsert, delete) facade."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from mosaic.core.exceptions import (
    EntityKindMismatchError,
    EntityNotFoundError,
    EntityTypeConflictError,
    ValidationFailure,
)
from mosaic.core.ingestion import extract_fts_content
from mosaic.core.kind_check import check_kind
from mosaic.core.schema_manager import SchemaManager
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter, SQLiteEntity
from mosaic.core.validation.validators import WriteOperation


class IngestionService:
    """Manages entity write operations: create, update, upsert (put), delete, FTS sync.

    This facade owns all write logic extracted from MosaicClient.
    """

    def __init__(
        self,
        storage: Optional[SQLiteAdapter] = None,
        schema_manager: Optional[SchemaManager] = None,
    ) -> None:
        self._storage = storage
        self._schema_manager = schema_manager

    def _sync_entity_to_fts(
        self,
        entity_id: str,
        entity_type: str,
        data: dict[str, Any],
        is_available: bool = True,
    ) -> None:
        """Sync an entity to its FTS tables."""
        if self._storage is None:
            return

        if self._schema_manager is None:
            return

        fts_tables = self._schema_manager.get_fts_tables_for_entity_type(entity_type)
        if not fts_tables:
            return

        with self._storage._transaction() as conn:
            fts_store = self._storage._get_fts_store(conn)

            for fts_meta in fts_tables:
                table_name = fts_meta.table_name
                fts_fields = fts_meta.get_fts_columns()

                # Ask the adapter's own FTS store — the sqlite module helper
                # queries sqlite_master with a `?` placeholder, which psycopg
                # rejects ("0 placeholders but 1 parameters"), breaking every
                # write on a postgres deployment whose schema declares
                # mosaic_search slots.
                if not fts_store.fts_table_exists(table_name):
                    continue

                content = extract_fts_content(data, fts_fields)

                if is_available and content:
                    fts_store.sync_entity_to_fts(table_name, entity_id, content)
                else:
                    fts_store.remove_entity_from_fts(table_name, entity_id)

    def put(
        self,
        entity_type: str,
        data: dict[str, Any],
        entity_id: Optional[str] = None,
        bypass_validation: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Create or update an entity (upsert)."""
        if data is None or (isinstance(data, dict) and len(data) == 0):
            raise ValidationFailure(
                message="Entity data cannot be null or empty",
                input_context=data,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        should_bypass = bypass_validation if bypass_validation is not None else (
            self._schema_manager.bypass_validation if self._schema_manager else False
        )

        if not should_bypass and self._schema_manager:
            operation = WriteOperation(
                operation="insert" if entity_id is None else "update",
                entity_type=entity_type,
                data=data,
            )
            result = self._schema_manager.validate(operation)
            if not result.is_valid:
                error_messages = [
                    e.message if hasattr(e, "message") else str(e)
                    for e in result.errors
                ]
                raise ValidationFailure(
                    message="; ".join(error_messages),
                    input_context=data,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )

        if entity_id is None and isinstance(data, dict) and "id" in data:
            entity_id = data["id"]

        return self._put_internal(entity_type, data, entity_id)

    def _put_internal(
        self,
        entity_type: str,
        data: dict[str, Any],
        entity_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Internal put implementation."""
        if self._storage is None:
            final_id = entity_id or str(uuid.uuid4())
            return {
                "id": final_id,
                "entity_type": entity_type,
                "data": data,
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        if hasattr(self._storage, "read"):
            return self._put_with_sqlite(entity_type, data, entity_id)

        if (
            entity_id
            and hasattr(self._storage, "exists")
            and self._storage.exists(entity_type, entity_id)
        ):
            return self._update_internal(entity_type, entity_id, data)
        return self._create_internal(entity_type, data)

    def _put_with_sqlite(
        self,
        entity_type: str,
        data: dict[str, Any],
        entity_id: Optional[str] = None,
        loader_context: Optional[tuple[str, str]] = None,
    ) -> dict[str, Any]:
        """Internal put implementation for SQLite storage.

        ``loader_context`` is threaded down to the storage adapter so the
        write-log row insert shares the entity write's SQL transaction
        (sec2 §2.14.9). It's ``None`` outside an active
        ``MosaicClient.load_context()``.
        """
        final_id = entity_id or str(uuid.uuid4())

        existing = self._storage.read(final_id)

        if existing is not None and existing.entity_type != entity_type:
            raise EntityTypeConflictError(
                message=(
                    f"Cannot put entity {final_id!r} as {entity_type!r}: "
                    f"id already registered under {existing.entity_type!r}."
                ),
                entity_id=final_id,
                requested_entity_type=entity_type,
                existing_entity_type=existing.entity_type,
            )

        if existing is not None:
            new_version = existing.version + 1
            self._storage.update_data(
                entity_id=final_id,
                entity_type=entity_type,
                data=data,
                new_version=new_version,
                loader_context=loader_context,
            )
            self._sync_entity_to_fts(final_id, entity_type, data, is_available=True)

            temporal_map = self._storage.get_temporal([final_id])
            temporal = temporal_map.get(final_id)
            return {
                "id": final_id,
                "entity_type": entity_type,
                "data": data,
                "version": new_version,
                "created_at": temporal.created_at if temporal else None,
                "updated_at": temporal.updated_at if temporal else None,
            }
        else:
            entity = SQLiteEntity(
                id=final_id,
                entity_type=entity_type,
                is_available=True,
                version=1,
                data=data,
            )
            self._storage.create(entity, loader_context=loader_context)

            self._sync_entity_to_fts(final_id, entity_type, data, is_available=True)

            temporal_map = self._storage.get_temporal([final_id])
            temporal = temporal_map.get(final_id)
            return {
                "id": final_id,
                "entity_type": entity_type,
                "data": data,
                "version": 1,
                "created_at": temporal.created_at if temporal else None,
                "updated_at": temporal.updated_at if temporal else None,
            }

    def replace(
        self,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        bypass_validation: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Full replacement of an existing entity (PUT semantics).

        Unlike update/put which merge or upsert, replace requires the entity
        to already exist and overwrites all fields. Records a 'replaced'
        provenance event.

        Raises:
            EntityNotFoundError: If the entity does not exist.
            ValidationFailure: If validation fails.
        """
        if data is None or (isinstance(data, dict) and len(data) == 0):
            raise ValidationFailure(
                message="Entity data cannot be null or empty",
                input_context=data,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        should_bypass = bypass_validation if bypass_validation is not None else (
            self._schema_manager.bypass_validation if self._schema_manager else False
        )

        if not should_bypass and self._schema_manager:
            operation = WriteOperation(
                operation="update",
                entity_type=entity_type,
                data=data,
            )
            result = self._schema_manager.validate(operation)
            if not result.is_valid:
                error_messages = [
                    e.message if hasattr(e, "message") else str(e)
                    for e in result.errors
                ]
                raise ValidationFailure(
                    message="; ".join(error_messages),
                    input_context=data,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )

        return self._replace_with_sqlite(entity_type, entity_id, data)

    def _replace_with_sqlite(
        self,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Internal replace implementation for SQLite storage."""
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type=entity_type,
                entity_id=entity_id,
            )

        existing = self._storage.read(entity_id)
        if existing is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type=entity_type,
                entity_id=entity_id,
            )
        if existing.entity_type != entity_type:
            raise EntityTypeConflictError(
                message=(
                    f"Cannot replace entity {entity_id!r} as {entity_type!r}: "
                    f"id already registered under {existing.entity_type!r}."
                ),
                entity_id=entity_id,
                requested_entity_type=entity_type,
                existing_entity_type=existing.entity_type,
            )

        new_version = existing.version + 1

        self._storage.update_data(
            entity_id=entity_id,
            entity_type=entity_type,
            data=data,
            new_version=new_version,
        )
        self._sync_entity_to_fts(entity_id, entity_type, data, is_available=True)

        temporal_map = self._storage.get_temporal([entity_id])
        temporal = temporal_map.get(entity_id)
        return {
            "id": entity_id,
            "entity_type": entity_type,
            "data": data,
            "version": new_version,
            "created_at": temporal.created_at if temporal else None,
            "updated_at": temporal.updated_at if temporal else None,
        }

    def create(
        self,
        entity_type: str,
        data: dict[str, Any],
        bypass_validation: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Create a new entity."""
        return self.put(entity_type, data, bypass_validation=bypass_validation)

    def update(
        self,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        bypass_validation: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Update an existing entity."""
        if self._storage is not None and hasattr(self._storage, "read"):
            existing = self._storage.read(entity_id)
            if existing is None:
                raise EntityNotFoundError(
                    message=f"Entity not found: {entity_id}",
                    entity_type=entity_type,
                    entity_id=entity_id,
                )

        return self.put(entity_type, data, entity_id, bypass_validation)

    def delete(
        self,
        entity_type: str,
        entity_id: str,
        bypass_validation: Optional[bool] = None,
    ) -> bool:
        """Delete an entity."""
        should_bypass = bypass_validation if bypass_validation is not None else (
            self._schema_manager.bypass_validation if self._schema_manager else False
        )

        if not should_bypass and self._schema_manager:
            operation = WriteOperation(
                operation="delete",
                entity_type=entity_type,
                data={"id": entity_id},
            )
            result = self._schema_manager.validate(operation)
            if not result.is_valid:
                raise ValidationFailure(
                    message="; ".join(result.errors),
                    input_context={"id": entity_id},
                    entity_type=entity_type,
                    entity_id=entity_id,
                )

        entity_type = self._checked_delete_type(entity_type, entity_id)
        return self._delete_internal(entity_type, entity_id)

    def _checked_delete_type(self, entity_type: str, entity_id: str) -> str:
        """#289: refuse a delete addressed to another kind's id; return the
        row's own class (a concrete subclass is accepted, ADR-0014)."""
        if self._storage is None or not hasattr(self._storage, "read"):
            return entity_type
        read_fn = getattr(self._storage, "read_any", self._storage.read)
        existing = read_fn(entity_id)
        if existing is None:
            return entity_type
        check_kind(self._storage, entity_type, existing.entity_type, entity_id)
        return existing.entity_type

    def set_availability_bulk(
        self,
        entity_type: str,
        entity_ids: list[str],
        is_available: bool,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        """Change availability status for multiple entities at once.

        Returns a summary with successes and failures. Records provenance
        events for each changed entity.
        """
        successes = []
        failures = []

        for eid in entity_ids:
            try:
                if self._storage is None:
                    raise EntityNotFoundError(
                        message=f"Entity not found: {eid}",
                        entity_type=entity_type,
                        entity_id=eid,
                    )

                read_fn = getattr(self._storage, "read_any", self._storage.read)
                existing = read_fn(eid)

                if existing is None:
                    failures.append({"id": eid, "error": f"Entity not found: {eid}"})
                    continue

                # #289: the id must be of this kind or one of its concrete
                # subclasses; the write then targets the row's own class.
                check_kind(self._storage, entity_type, existing.entity_type, eid)
                row_type = existing.entity_type

                self._storage.set_availability(
                    entity_id=eid,
                    entity_type=row_type,
                    is_available=is_available,
                    actor=actor,
                    reason=reason,
                )

                data = existing.data if existing else {}
                self._sync_entity_to_fts(eid, row_type, data, is_available=is_available)

                successes.append({"id": eid, "is_available": is_available})

            except EntityKindMismatchError as e:
                failures.append({
                    "id": eid,
                    "error": str(e),
                    "code": "NOT_FOUND",
                    "actual_entity_type": e.actual_entity_type,
                })
            except Exception as e:
                failures.append({"id": eid, "error": str(e)})

        return {
            "total": len(entity_ids),
            "succeeded": len(successes),
            "failed": len(failures),
            "successes": successes,
            "failures": failures,
        }

    def _delete_internal(self, entity_type: str, entity_id: str) -> bool:
        """Internal delete implementation."""
        if self._storage is None:
            return True

        if hasattr(self._storage, "delete"):
            entity = self._storage.read(entity_id)
            data = entity.data if entity else {}

            result = self._storage.delete(entity_id)

            self._sync_entity_to_fts(entity_id, entity_type, data, is_available=False)

            return result

        return True

    def _create_internal(
        self, entity_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Internal create implementation (placeholder for non-sqlite storage)."""
        return {"id": data.get("id"), "entity_type": entity_type, "data": data}

    def _update_internal(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Internal update implementation (placeholder for non-sqlite storage)."""
        return {"id": entity_id, "entity_type": entity_type, "data": data}
