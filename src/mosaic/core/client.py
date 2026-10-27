"""MosaicClient - Main SDK client for Mosaic Metadata Tracking Service."""

import hashlib
import os
import urllib.request
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from mosaic.core.ingestion_service import IngestionService
from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.provenance_service import ProvenanceService
from mosaic.core.query_service import QueryService
from mosaic.core.recipe import (
    ImportResult,
    InstalledRecipe,
    RecipeDiff,
    RecipeExport,
    RecipeReport,
)
from mosaic.core.recipe_service import RecipeService
from mosaic.core.relationship import RelationshipManager
from mosaic.core.schema_manager import SchemaManager
from mosaic.core.storage import EntityStore
from mosaic.core.storage.fts import FTSTableMetadata
from mosaic.core.validation.validators import (
    BatchValidationResult,
    BatchWriteResult,
    ValidationResult,
    WriteOperation,
    WriteValidator,
)
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.core.typed_client import (
    Namespace,
    build_typed_surface,
    generate_pydantic_models,
)
from mosaic.models import populate as _populate_models


class MosaicClient:
    """Main client for Mosaic Metadata Tracking Service.

    Thin coordinator that delegates to domain facades:
    - SchemaManager: schema loading, validation, FTS metadata
    - ProvenanceService: version history, audit trail, supersession
    - QueryService: entity queries, FTS search, relationship traversal
    - IngestionService: entity writes (create, update, upsert, delete)
    """

    def __init__(
        self,
        pipeline: Optional[ValidationPipeline] = None,
        bypass_validation: bool = False,
        storage: Optional[EntityStore] = None,
        registry: Optional[SchemaRegistry] = None,
    ) -> None:
        """Initialize MosaicClient.

        Args:
            pipeline: Optional validation pipeline. If not provided,
                a default pipeline will be created when needed.
            bypass_validation: DEPRECATED. If True, skips validation pipeline.
                This parameter is deprecated and will be removed in a future version.
            storage: Storage adapter for persistence.
            registry: LinkML-backed schema registry. Used for schema
                introspection, write-time validation, and FTS metadata.
        """
        self._schema_manager = SchemaManager(
            registry=registry,
            pipeline=pipeline,
            bypass_validation=bypass_validation,
            storage=storage,
        )

        # Plumb schema_version into the storage adapter so new
        # ProvenanceRecord rows carry it (sec9 §9.6 / Decision 9.6.F).
        # Derivation: prefer the merged schema's `version` field; fall
        # back to "unversioned" when the user schema doesn't set one.
        # Reaches into adapter-private attributes (`_schema_version`,
        # `_provenance_store`) deliberately — a public setter is a
        # future cleanup once the three adapter implementations agree
        # on a shared interface.
        if storage is not None and registry is not None:
            sv = registry.schema_view.schema.version or "unversioned"
            # Only overwrite an adapter's schema_version if the adapter
            # wasn't given one explicitly at construction.
            if not getattr(storage, "_schema_version", "") and hasattr(
                storage, "_schema_version"
            ):
                storage._schema_version = sv
                # Invalidate cached provenance_store so the next write
                # picks up the new schema_version.
                if hasattr(storage, "_provenance_store"):
                    storage._provenance_store = None

        self._provenance_service = ProvenanceService(storage=storage)
        self._query_service = QueryService(
            storage=storage,
            schema_manager=self._schema_manager,
            provenance_service=self._provenance_service,
        )
        self._ingestion_service = IngestionService(
            storage=storage,
            schema_manager=self._schema_manager,
        )
        self._recipe_service = RecipeService(
            storage=storage,
            schema_manager=self._schema_manager,
            provenance_service=self._provenance_service,
        )

        self._storage = storage
        self._pipeline = pipeline
        self._bypass_validation = bypass_validation
        self._registry = registry
        self._fts_table_metadata = self._schema_manager.fts_table_metadata

        # Reference loader context (sec2 §2.14.9 / Decision 2.14.J).
        # Set by ``load_context()`` for the duration of a single
        # ``loader.load()``/``loader.upgrade()`` invocation; ``None``
        # outside that window. Held on the instance (not a ContextVar)
        # so the boundary is visible at every call site. Nested entries
        # raise: overlapping loads are intentionally unsupported in v2.
        self._loader_context: Optional[tuple[str, str]] = None

        # sec9 §9.8 typed-client surface. Generated at load time when a
        # registry is available. Pydantic class generation is best-
        # effort — failures yield empty models and the accessor tree
        # still works against plain dicts.
        self._typed_root: Optional[Namespace] = None
        self._models: dict[str, type] = {}
        if registry is not None:
            self._models = generate_pydantic_models(registry)
            self._typed_root = build_typed_surface(
                self, registry, models=self._models
            )
            self._install_typed_accessors()
            _populate_models(self._typed_root)

    def _install_typed_accessors(self) -> None:
        """Expose typed accessors on ``self`` for flat root access, plus
        the explicit ``self.root`` alias. Non-root namespaces land as
        nested attributes (``self.tissue.samples.create(...)``).
        """
        if self._typed_root is None:
            return
        root = self._typed_root
        # Flat root-namespace access
        for name, accessor in root._accessors.items():
            setattr(self, name, accessor)
        for name, sub in root._subnamespaces.items():
            setattr(self, name, sub)
        # Explicit `client.root` alias for root-namespace classes only —
        # presents the same accessors without the sub-namespaces.
        root_alias = Namespace("root")
        for name, accessor in root._accessors.items():
            root_alias._accessors[name] = accessor
            setattr(root_alias, name, accessor)
        self.root = root_alias  # type: ignore[attr-defined]

    # -- Reference loader cache surface (sec2 §2.14.3, D2.14.E) --

    @staticmethod
    def _reference_cache_root() -> Path:
        """Resolve the root directory holding per-loader reference caches.

        ``$MOSAIC_CACHE_DIR`` wins when set (the deployment opts into a
        custom location, e.g. a CI mount; the legacy ``$HIPPO_CACHE_DIR``
        spelling is honored with a ``DeprecationWarning`` — ADR-0004);
        otherwise we default to ``~/.cache/hippo/references/`` (the
        pre-rename on-disk location, kept so installed references stay
        found). The directory is NOT created here —
        :meth:`cache_dir_for` handles per-loader mkdir.
        """
        from mosaic.config.env import get_env

        env = get_env("CACHE_DIR")
        if env:
            return Path(env)
        return Path.home() / ".cache" / "hippo" / "references"

    def cache_dir_for(self, loader_name: str) -> Path:
        """Return the per-loader cache directory, creating it on demand.

        Resolves to ``$MOSAIC_CACHE_DIR/<loader_name>/`` when the env var
        is set, else ``~/.cache/hippo/references/<loader_name>/``. The
        accessor is stateless: per-loader scoping happens via the
        ``loader_name`` argument so a single ``MosaicClient`` instance can
        serve many loaders without per-loader binding (see PTS-225
        rationale on §2.14.3).
        """
        if not loader_name:
            raise ValueError("loader_name must be a non-empty string")
        path = self._reference_cache_root() / loader_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cached_fetch(
        self,
        url: str,
        *,
        expected_sha256: Optional[str] = None,
        loader_name: str,
    ) -> Path:
        """Content-addressable HTTP fetch for reference data.

        Files are keyed by the sha256 of the request URL inside the
        loader's cache directory so repeated calls return a stable path.
        When ``expected_sha256`` is supplied the digest is verified on
        download AND on cache hit (a corrupt cached file MUST NOT pass
        silently); mismatches raise :class:`CacheIntegrityError` and the
        offending file is removed so the next call re-downloads cleanly.
        """
        from mosaic.core.exceptions import CacheIntegrityError

        cache_dir = self.cache_dir_for(loader_name)
        url_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        target = cache_dir / url_key

        if target.exists():
            if expected_sha256 is not None:
                actual = self._sha256_file(target)
                if actual.lower() != expected_sha256.lower():
                    target.unlink(missing_ok=True)
                    raise CacheIntegrityError(
                        "Cached file failed sha256 verification",
                        url=url,
                        path=str(target),
                        expected_sha256=expected_sha256,
                        actual_sha256=actual,
                    )
            return target

        # Download to a sibling tmp path then rename so partial files
        # never satisfy a later cache hit.
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with urllib.request.urlopen(url) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            if expected_sha256 is not None:
                actual = self._sha256_file(tmp)
                if actual.lower() != expected_sha256.lower():
                    tmp.unlink(missing_ok=True)
                    raise CacheIntegrityError(
                        "Downloaded file failed sha256 verification",
                        url=url,
                        path=str(target),
                        expected_sha256=expected_sha256,
                        actual_sha256=actual,
                    )
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    # -- Property accessors (backwards compatibility) --

    @property
    def storage(self) -> Optional[EntityStore]:
        """Get the storage adapter."""
        return self._storage

    @storage.setter
    def storage(self, value: Optional[EntityStore]) -> None:
        """Set the storage adapter.

        Note: Phase 2 will propagate this to facades. Currently a known
        limitation — setting storage after init does not update facades.
        """
        self._storage = value

    @property
    def registry(self) -> Optional[SchemaRegistry]:
        """The merged LinkML schema registry backing this client (read-only).

        Exposes the in-memory merged schema (user schema + reference
        loaders + ``hippo_core``/``hippo_ext``) so lifecycle hooks can
        validate staged records against it in-process — notably
        :meth:`DomainModule.evolve`'s staged dry-run gate, the SDK
        equivalent of ``mosaic ingest --validate-schema <merged-dir>
        --dry-run``. ``None`` on a schemaless client.
        """
        return self._registry

    @contextmanager
    def staged_transaction(self) -> Iterator[None]:
        """Single commit-or-rollback scope spanning a whole migration chain.

        The S4 lifecycle orchestrator (sec11 §11.5.2) wraps a multi-package,
        multi-hop migration in this scope: every inner write defers its
        commit, reads still observe the staged (uncommitted) write-set, and
        the entire chain commits on clean exit or rolls back together if the
        end-to-end validation gate (or anything else) raises. Delegates to
        the storage adapter's :meth:`staged_transaction`; on a backend that
        does not support staging it is a transparent no-op (each inner write
        commits as before).
        """
        storage = self._storage
        if storage is not None and hasattr(storage, "staged_transaction"):
            with storage.staged_transaction():
                yield
        else:
            yield

    @property
    def pipeline(self) -> Optional[ValidationPipeline]:
        """Get the validation pipeline."""
        return self._schema_manager.pipeline

    @pipeline.setter
    def pipeline(self, value: Optional[ValidationPipeline]) -> None:
        """Set the validation pipeline."""
        self._pipeline = value
        self._schema_manager.pipeline = value

    @property
    def relationships(self) -> RelationshipManager:
        """Get the relationship manager."""
        return self._query_service.relationships

    @relationships.setter
    def relationships(self, value: RelationshipManager) -> None:
        """Set the relationship manager."""
        self._query_service.relationships = value

    # -- SchemaManager delegations --

    def _build_fts_metadata(self) -> None:
        self._schema_manager._build_fts_metadata()

    def _validate_search_capabilities(self) -> None:
        self._schema_manager._validate_search_capabilities()

    def schema_references(self, entity_type: str) -> list[dict]:
        return self._schema_manager.schema_references(entity_type)

    def _get_fts_tables_for_entity_type(
        self, entity_type: str
    ) -> list[FTSTableMetadata]:
        return self._schema_manager.get_fts_tables_for_entity_type(entity_type)

    def add_validator(self, validator: WriteValidator) -> None:
        self._schema_manager.add_validator(validator)

    def validate(self, operation: WriteOperation) -> ValidationResult:
        return self._schema_manager.validate(operation)

    def validate_batch(
        self,
        operations: list[WriteOperation],
        *,
        assign_ids: bool = True,
    ) -> BatchValidationResult:
        """Validate a *set* of write operations without writing (whole-set dry-run).

        Increment 1 of the batch unit-of-work (BU-Neuromics/hippo#84). Runs the
        standard per-entity validation pipeline (LinkML → CEL → Python) over
        each operation and aggregates the results so callers see every problem
        across the proposed set at once. **Performs zero writes** — neither the
        entity tables nor the provenance log are touched.

        Provisional ids are assigned (in-memory, on a copy of each operation's
        data) to id-less operations so every per-entity result is addressable
        and so a future atomic batch write (increment 2) can resolve references
        between members of the set. The provisional ids are never persisted.

        Note: intra-batch *referential existence* (an entity referencing another
        entity in the same set) is enforced at write time — relationship edges
        require their targets to exist — so it lands with the atomic-write
        increment, not here. This method validates entity *data* shape/rules.

        Args:
            operations: The proposed set of write operations to validate.
            assign_ids: When True (default), assign an in-memory provisional id
                to any operation whose ``data`` lacks one, so results are
                addressable. Set False to validate exactly as given.

        Returns:
            BatchValidationResult: overall validity plus per-entity results in
            input order.
        """
        import uuid

        results: list[ValidationResult] = []
        for op in operations:
            data = op.data
            if (
                assign_ids
                and isinstance(data, dict)
                and not data.get("id")
            ):
                # Copy so the caller's dict is never mutated; the provisional
                # id exists only for this validation pass and is never written.
                data = {**data, "id": str(uuid.uuid4())}
                op = WriteOperation(
                    operation=op.operation,
                    entity_type=op.entity_type,
                    data=data,
                )
            result = self.validate(op)
            if result.entity_id is None and isinstance(op.data, dict):
                result.entity_id = op.data.get("id")
            results.append(result)

        return BatchValidationResult(
            is_valid=all(r.is_valid for r in results),
            results=results,
        )

    def batch_put(
        self,
        operations: list[WriteOperation],
        *,
        relationships: Optional[list[dict[str, Any]]] = None,
        dry_run: bool = False,
    ) -> BatchWriteResult:
        """Atomically write a *set* of related entities (batch unit-of-work).

        Increment 2 of the batch unit-of-work (BU-Neuromics/hippo#84). The whole
        set is validated first (see :meth:`validate_batch`); if valid and not a
        dry run, every entity — and any intra-batch ``relationships`` — is
        written inside a single ``staged_transaction`` so the group commits
        **all-or-nothing**. If any write raises, the entire set is rolled back
        and the exception propagates; nothing is left partially committed.

        Real ids are assigned to id-less operations up front (on copies — the
        caller's dicts are never mutated) and used for both validation and the
        write. Relationships are created **after** all entities, within the same
        transaction; because staged reads observe staged writes, a relationship
        whose source/target is created earlier in the *same* batch resolves
        without the target having to pre-exist (intra-batch forward reference).

        Args:
            operations: The set of write operations to commit together.
            relationships: Optional edges to create after the entities, each a
                dict with ``source_id``, ``target_id``, ``relationship_type``
                (or ``type``), and optional ``metadata``. Ids must reference
                entities in this batch or already-persisted ones.
            dry_run: When True, validate and compute a write plan but touch no
                storage. ``committed`` is False and ``entities`` holds the plan.

        Returns:
            BatchWriteResult: ``committed``/``dry_run`` flags, the whole-set
            ``validation``, and per-entity (and relationship) results.
        """
        import uuid

        # Normalize: copy each op's data and assign a real id when missing, so
        # validation and the write share ids and relationships can reference
        # batch members. The caller's dicts are never mutated.
        normalized: list[WriteOperation] = []
        for op in operations:
            data = dict(op.data)
            if not data.get("id"):
                data["id"] = str(uuid.uuid4())
            normalized.append(
                WriteOperation(
                    operation=op.operation,
                    entity_type=op.entity_type,
                    data=data,
                )
            )

        validation = self.validate_batch(normalized, assign_ids=False)
        if not validation.is_valid:
            return BatchWriteResult(
                committed=False, dry_run=dry_run, validation=validation
            )

        if dry_run:
            plan = [
                {
                    "id": op.data["id"],
                    "entity_type": op.entity_type,
                    "operation": op.operation,
                }
                for op in normalized
            ]
            return BatchWriteResult(
                committed=False, dry_run=True, validation=validation, entities=plan
            )

        rels = relationships or []
        written_entities: list[dict[str, Any]] = []
        written_rels: list[dict[str, Any]] = []
        # One staged scope: all inner writes defer their commit and roll back
        # together if anything raises (all-or-nothing).
        with self.staged_transaction():
            for op in normalized:
                written_entities.append(
                    self._put_internal(op.entity_type, op.data, op.data["id"])
                )
            for rel in rels:
                written_rels.append(
                    self.relationships.relate(
                        source_id=rel["source_id"],
                        target_id=rel["target_id"],
                        relationship_type=(
                            rel.get("relationship_type") or rel.get("type")
                        ),
                        metadata=rel.get("metadata"),
                    )
                )

        return BatchWriteResult(
            committed=True,
            dry_run=False,
            validation=validation,
            entities=written_entities,
            relationships=written_rels,
        )

    # -- Ingestion methods --
    # These keep the original call chain (put -> _put_internal -> _create_internal etc.)
    # on MosaicClient so subclass overrides of private methods continue to work.
    # The IngestionService facade exists for direct standalone use.

    def _sync_entity_to_fts(
        self,
        entity_id: str,
        entity_type: str,
        data: dict[str, Any],
        is_available: bool = True,
    ) -> None:
        self._ingestion_service._sync_entity_to_fts(
            entity_id, entity_type, data, is_available
        )

    def put(
        self,
        entity_type: str,
        data: dict[str, Any],
        entity_id: Optional[str] = None,
        bypass_validation: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Create or update an entity."""
        from mosaic.core.exceptions import ValidationFailure

        if data is None or (isinstance(data, dict) and len(data) == 0):
            raise ValidationFailure(
                message="Entity data cannot be null or empty",
                input_context=data,
                entity_type=entity_type,
                entity_id=entity_id,
            )

        should_bypass = (
            bypass_validation
            if bypass_validation is not None
            else self._bypass_validation
        )

        if not should_bypass:
            operation = WriteOperation(
                operation="insert" if entity_id is None else "update",
                entity_type=entity_type,
                data=data,
            )
            result = self.validate(operation)
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
        """Internal put implementation.

        Threads ``self._loader_context`` (set by ``load_context()``)
        down to the storage adapter so the write-log row insert shares
        the entity write's SQL transaction (sec2 §2.14.9). Outside an
        active ``load_context()``, ``_loader_context`` is ``None`` and
        the log is bypassed.
        """
        import uuid
        from datetime import datetime, timezone

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
            return self._ingestion_service._put_with_sqlite(
                entity_type,
                data,
                entity_id,
                loader_context=self._loader_context,
            )

        if (
            entity_id
            and hasattr(self._storage, "exists")
            and self._storage.exists(entity_type, entity_id)
        ):
            return self._update_internal(entity_type, entity_id, data)
        return self._create_internal(entity_type, data)

    @contextmanager
    def load_context(
        self, loader_name: str, version: str
    ) -> Iterator[None]:
        """Open a reference-loader write-log scope (sec2 §2.14.9, D2.14.J).

        Inside the ``with`` block every successful :meth:`put` appends a
        row to ``reference_write_log`` keyed by ``(loader_name, version,
        entity_id, entity_type)``. The log insert shares the entity
        write's SQL transaction so committed entity writes always have
        a matching log row and a mid-write failure rolls back both.

        Outside the block, :meth:`put` is a no-op for the log — user
        data writes, REST handler writes, and ad-hoc ingestion calls
        never appear in ``reference_write_log``.

        Nested entries are intentionally unsupported in v2 and raise
        :class:`RuntimeError` on the inner ``__enter__``. ``MosaicClient``
        instances are not designed for concurrent use across threads;
        the context is plain instance state.

        Args:
            loader_name: ``ReferenceLoader.name`` for the loader running
                the current install/upgrade.
            version: Resolved version slug for the load (the same string
                that flows into ``hippo_meta.reference_versions``).

        Raises:
            RuntimeError: If ``load_context`` is already active on this
                client (overlapping loads are unsupported in v2).
        """
        if self._loader_context is not None:
            active_loader, active_version = self._loader_context
            raise RuntimeError(
                f"load_context is already active for "
                f"({active_loader!r}, {active_version!r}); nested "
                f"load_context() calls are not supported"
            )
        self._loader_context = (loader_name, version)
        try:
            yield
        finally:
            self._loader_context = None

    def _create_internal(
        self, entity_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Internal create implementation (overridable by subclasses)."""
        return {"id": data.get("id"), "entity_type": entity_type, "data": data}

    def _update_internal(
        self, entity_type: str, entity_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Internal update implementation (overridable by subclasses)."""
        return {"id": entity_id, "entity_type": entity_type, "data": data}

    def _delete_internal(self, entity_type: str, entity_id: str) -> bool:
        """Internal delete implementation (overridable by subclasses)."""
        if self._storage is None:
            return True

        if hasattr(self._storage, "delete"):
            entity = self._storage.read(entity_id)
            data = entity.data if entity else {}
            result = self._storage.delete(entity_id)
            self._sync_entity_to_fts(entity_id, entity_type, data, is_available=False)
            return result

        return True

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
        provenance event. Returns 404 if entity does not exist.
        """
        return self._ingestion_service.replace(
            entity_type, entity_id, data, bypass_validation
        )

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
        from mosaic.core.exceptions import EntityNotFoundError

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
        from mosaic.core.exceptions import ValidationFailure

        should_bypass = (
            bypass_validation
            if bypass_validation is not None
            else self._bypass_validation
        )

        if not should_bypass:
            operation = WriteOperation(
                operation="delete",
                entity_type=entity_type,
                data={"id": entity_id},
            )
            result = self.validate(operation)
            if not result.is_valid:
                raise ValidationFailure(
                    message="; ".join(result.errors),
                    input_context={"id": entity_id},
                    entity_type=entity_type,
                    entity_id=entity_id,
                )

        return self._delete_internal(entity_type, entity_id)

    def set_availability_bulk(
        self,
        entity_type: str,
        entity_ids: list[str],
        is_available: bool,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        """Change availability status for multiple entities at once.

        Returns a summary with per-entity successes and failures.
        Records provenance events for each changed entity.
        """
        return self._ingestion_service.set_availability_bulk(
            entity_type, entity_ids, is_available, reason, actor
        )

    # -- QueryService delegations --

    def get(
        self,
        entity_type: str,
        entity_id: str,
        expand: Optional[str] = None,
        include_unavailable: bool = False,
    ) -> dict[str, Any]:
        """Get an entity by its ID."""
        return self._query_service.get(
            entity_type, entity_id, expand, include_unavailable
        )

    def resolve_type(self, entity_id: str) -> Optional[str]:
        """Resolve a UUID to its entity_type via the storage adapter.

        Per sec9 §9.5's identity model — given only a UUID, return the entity
        class (FQN in the merged schema). Returns None when the UUID is not
        known to the storage. The concrete resolution mechanism is
        adapter-specific; relational adapters query the `entities` table's
        type discriminator, future graph adapters use labels.
        """
        if self._storage is None or not hasattr(self._storage, "resolve_type"):
            return None
        return self._storage.resolve_type(entity_id)

    def resolve_types(self, entity_ids: list[str]) -> dict[str, str]:
        """Batch variant of ``resolve_type``; one round-trip to the adapter.

        Returns a dict mapping id → entity_type for every known id. Unknown
        ids are absent from the result; callers can compute the missing set
        by diffing the input list against the returned keys.
        """
        if (
            self._storage is None
            or not hasattr(self._storage, "resolve_types")
            or not entity_ids
        ):
            return {}
        return self._storage.resolve_types(entity_ids)

    def query(
        self,
        entity_type: Optional[str] = None,
        filters: Optional[list[dict[str, Any]]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filter_mode: str = "and",
        as_of: Optional[str] = None,
        where: Optional[dict[str, Any]] = None,
        order_by: Optional[str] = None,
        order_dir: str = "asc",
        fields: Optional[list[str]] = None,
    ) -> "PaginatedResult":
        """Query entities with filter criteria.

        Args:
            entity_type: Restrict the query to one entity type. ``None``
                queries across all types (relational adapters scan every
                concrete per-class table / drop the type predicate).
            filter_mode: How to combine filters — "and" (all must match,
                default) or "or" (any may match).
            as_of: Optional ISO-8601 transaction-time; when given, results are
                reconstructed as the graph stood at that time (sec6 §6.8 /
                ADR-0001). Omitted = current state.
            where: Optional boolean filter tree (ADR-0006 increment 2) — a
                leaf ``{"field", "op", "value"}`` or nested ``{"and": [...]}``
                / ``{"or": [...]}`` / ``{"not": node}``; composes with
                ``filters`` by AND. The GraphQL ``where:`` argument compiles
                to exactly this shape.
            order_by: Optional stored-slot name to order by (ADR-0007);
                ordering and pagination push down to storage (NULLs last,
                stable ``id`` tiebreak). Computed temporal fields are not
                orderable; not combinable with ``as_of`` or
                ``date_from``/``date_to``. Omitted = the historical
                ``created_at``-ascending default.
            order_dir: "asc" (default) or "desc"; only used with order_by.
            fields: Optional anchor slot names to keep (issue #215). Each
                item's ``data`` is trimmed to these slots; envelope metadata
                (``id``, ``entity_type``, version/provenance fields) is always
                kept, and ``total``/ordering/pagination are unaffected — one
                entity is still one row. Omitted/``None`` = full envelopes.
                Names are not checked here (the QuerySpec validator does that
                against the capability manifest); an unknown name is simply
                absent from the result.
        """
        page = self._query_service.query(
            entity_type, filters, date_from, date_to, limit, offset, filter_mode,
            as_of=as_of, where=where, order_by=order_by, order_dir=order_dir,
        )
        if fields is None:
            return page
        keep = set(fields)
        page.items = [
            {**item, "data": {k: v for k, v in item.get("data", {}).items() if k in keep}}
            if isinstance(item, dict)
            else item
            for item in page.items
        ]
        return page

    def count(
        self,
        entity_type: Optional[str] = None,
        filters: Optional[list[dict[str, Any]]] = None,
        filter_mode: str = "and",
        as_of: Optional[str] = None,
        where: Optional[dict[str, Any]] = None,
    ) -> int:
        """Count matching entities without materializing them (ADR-0007).

        Availability-consistent: counts exactly the entities ``query()``
        would return under the same criteria. Under ``as_of``, the count is
        over the reconstructed match set (Python-path semantics).
        """
        return self._query_service.count(
            entity_type, filters, filter_mode, as_of=as_of, where=where
        )

    def facet_counts(
        self,
        entity_type: str,
        field: str,
        filters: Optional[list[dict[str, Any]]] = None,
        filter_mode: str = "and",
        where: Optional[dict[str, Any]] = None,
    ) -> list[tuple]:
        """Per-value counts for ``field`` under the given criteria —
        ``[(value, count), ...]``, count desc then value asc (ADR-0007).
        Entities with no stored value are not counted (query absence with
        ``is_null``). Not defined under as-of in this increment."""
        return self._query_service.facet_counts(
            entity_type, field, filters, filter_mode, where=where
        )

    def field_range(
        self,
        entity_type: str,
        field: str,
        filters: Optional[list[dict[str, Any]]] = None,
        filter_mode: str = "and",
        where: Optional[dict[str, Any]] = None,
    ) -> tuple:
        """``(min, max)`` of ``field`` under the given criteria (ADR-0007);
        ``(None, None)`` when no matching entity has a value. Not defined
        under as-of in this increment."""
        return self._query_service.field_range(
            entity_type, field, filters, filter_mode, where=where
        )

    def count_relationship(
        self, entity_type: str, entity_id: str, edge: str
    ) -> int:
        """Cardinality of ``entity_id``'s ``edge`` (a multivalued reference
        slot) without resolving its member objects (issue #132, deferred
        from ADR-0005). Consumer: a relationship count badge that would
        otherwise have to resolve the whole list just to show its length.

        Availability-consistent: only counts edges whose target is also
        available, matching what resolving the list would return.
        """
        return self._query_service.count_relationship(entity_type, entity_id, edge)

    def query_updated_since(
        self,
        entity_type: Optional[str] = None,
        since: str = "",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list[dict[str, Any]]] = None,
    ) -> "PaginatedResult":
        """Query entities updated after a watermark timestamp (sec4 §4.5).

        Polling support for change detection (e.g. Cappella's
        ``hippo_poll`` trigger). Returns entities whose provenance-derived
        ``updated_at`` is strictly greater than ``since``, ordered by
        ``updated_at`` ascending so callers can advance their watermark
        incrementally. ``since`` is compared against Hippo's server-side
        provenance timestamps (UTC) to avoid clock-skew issues.

        ``entity_type`` is optional: pass ``None`` to poll across all
        entity types (composes with the issue #44/#49 cross-class scan).
        """
        return self._query_service.query_updated_since(
            entity_type, since, limit, offset, filters
        )

    def search(
        self,
        entity_type: str,
        query: str,
        limit: Optional[int] = 100,
        offset: Optional[int] = 0,
        *,
        filters: Optional[list[dict[str, Any]]] = None,
        filter_mode: str = "and",
        where: Optional[dict[str, Any]] = None,
        order_by: Optional[str] = None,
        order_dir: str = "asc",
    ) -> "PaginatedResult":
        """Full-text search composed with the list surface (issue #157).

        BREAKING (v0.15): returns the same ``PaginatedResult`` envelope as
        ``query()`` instead of a bare list. Ranked FTS ids feed one
        ``find()`` composed with the caller's ``filters``/``where`` (id-set
        composition) — availability and filter semantics are identical to
        list queries, with one batched read per page instead of a per-hit
        N+1. Default order is FTS rank; an explicit ``order_by`` overrides
        rank (ADR-0007 pushdown). ``total`` is the matching-and-filtered
        count, bounded by the 1000-hit FTS budget.
        """
        return self._query_service.search(
            entity_type, query, limit, offset,
            filters=filters, filter_mode=filter_mode, where=where,
            order_by=order_by, order_dir=order_dir,
        )

    def search_all(
        self, query: str, limit: Optional[int] = 100
    ) -> list[dict[str, Any]]:
        """Cross-class ranked full-text search (issue #158): fans over
        every FTS-indexed class, merges by normalized rank, materializes
        the page batched by type. Items carry ``entity_type`` and
        ``score``; availability parity with list queries."""
        return self._query_service.search_all(query, limit)

    def neighbors(
        self, entity_id: str, depth: int = 1, *, as_of: Optional[str] = None
    ) -> dict[str, Any]:
        """The subgraph around an entity (issue #158): nodes + edges from
        BOTH edge stores — link-table relationship edges and column-stored
        single-valued references — depth-bounded, batched-by-type
        materialization, disclosed caps. Under ``as_of``, link-table edges
        replay from provenance and node states reconstruct at ``as_of``;
        column edges are disclosed as out of scope (hippo#71)."""
        return self._query_service.neighbors(entity_id, depth, as_of=as_of)

    # -- ProvenanceService delegations --

    def _get_provenance_summary_map(self, entity_type: str) -> dict[str, dict]:
        return self._provenance_service.get_provenance_summary_map(entity_type)

    def register_external_id(
        self,
        entity_id: str,
        external_id: str,
        source_system: str = "default",
    ) -> dict[str, Any]:
        """Register an external ID for an entity.

        .. deprecated:: 0.9
            The ExternalID entity pattern is deprecated (issue #48).
            Declare an ``ExternalReference``-ranged slot on the entity
            class (annotate it ``hippo_external_xref: true`` for reverse
            lookup) and write the reference as ordinary slot data.

        ``source_system`` defaults to ``"default"`` for backward
        compatibility with legacy two-argument callers. Callers managing
        multiple source systems should pass the parameter explicitly so
        the ``(source_system, value)`` uniqueness constraint applies
        within the correct scope.
        """
        warnings.warn(
            "register_external_id() and the ExternalID entity are "
            "deprecated (issue #48); store an ExternalReference value on "
            "an entity slot annotated hippo_external_xref instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._provenance_service.register_external_id(
            entity_id, external_id, source_system=source_system
        )

    def supersede(
        self,
        entity_id: str,
        old_external_id: str,
        new_external_id: str,
        source_system: str = "default",
    ) -> dict[str, Any]:
        """Supersede an entity's external ID with a new one.

        .. deprecated:: 0.9
            Mapping-level supersession is deprecated with the ExternalID
            entity (issue #48). Updating an ``ExternalReference`` slot is
            an ordinary entity update captured by normal provenance —
            no dedicated supersession lifecycle is needed. (Entity-level
            :meth:`supersede_entity` is NOT deprecated.)

        The supersede operation is scoped to one ``source_system``;
        legacy two-argument callers default to ``"default"``.
        """
        warnings.warn(
            "Mapping-level supersede() and the ExternalID entity are "
            "deprecated (issue #48); update the entity's "
            "ExternalReference slot instead (ordinary entity update, "
            "normal provenance). Entity-level supersede_entity() is "
            "unaffected.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._provenance_service.supersede(
            entity_id,
            old_external_id,
            new_external_id,
            source_system=source_system,
        )

    def supersede_entity(
        self,
        entity_id: str,
        replacement_id: str,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        """Mark an entity as superseded by a replacement entity."""
        return self._provenance_service.supersede_entity(
            entity_id, replacement_id, reason, actor
        )

    def get_by_external_id(
        self, external_id: str, include_archived: bool = False
    ) -> dict[str, Any]:
        """Get an entity by its external ID.

        .. deprecated:: 0.9
            The ExternalID entity pattern is deprecated (issue #48).
            Use :meth:`find_by_xref` (system, value) over
            ``hippo_external_xref``-annotated slots instead.
        """
        warnings.warn(
            "get_by_external_id() and the ExternalID entity are "
            "deprecated (issue #48); use find_by_xref(system, value) "
            "over hippo_external_xref-annotated slots instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._provenance_service.get_by_external_id(
            external_id, include_archived
        )

    def list_external_ids(
        self, entity_id: str, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """List all external IDs for an entity.

        .. deprecated:: 0.9
            The ExternalID entity pattern is deprecated (issue #48).
            Use :meth:`list_xrefs` (or read the entity's
            ``ExternalReference`` slots directly) instead.
        """
        warnings.warn(
            "list_external_ids() and the ExternalID entity are "
            "deprecated (issue #48); use list_xrefs(entity_id) or read "
            "the entity's ExternalReference slots instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._provenance_service.list_external_ids(
            entity_id, include_superseded
        )

    # -- External references (hippo_external_xref, issue #48) --

    def find_by_xref(
        self, system: str, value: str
    ) -> Optional[dict[str, Any]]:
        """Reverse-lookup the entity holding external reference ``(system, value)``.

        Resolves through the ``hippo_xref_index`` side table maintained
        for ``hippo_external_xref``-annotated ``ExternalReference`` slots.
        ``(system, value)`` is globally unique among available entities,
        so at most one entity can match.

        Returns:
            The full entity envelope (as returned by :meth:`get`) or
            ``None`` when no available entity holds the pair (or the
            storage adapter does not maintain an xref index).

        Raises:
            NotImplementedError: On adapters that declare the surface but
                have not implemented it yet (PostgreSQL).
        """
        if self._storage is None or not hasattr(self._storage, "find_xref"):
            return None
        match = self._storage.find_xref(system, value)
        if match is None:
            return None
        return self.get(match["entity_type"], match["entity_id"])

    def list_xrefs(self, entity_id: str) -> list[dict[str, Any]]:
        """List the indexed external-reference pairs for an entity.

        Returns ``[{"slot", "system", "value"}, ...]`` from the
        ``hippo_xref_index`` side table — i.e. only pairs from
        ``hippo_external_xref``-annotated slots of an AVAILABLE entity.
        The full ``ExternalReference`` values (including ``retrieved_at``
        and ``version``) are ordinary slot data on the entity itself.

        Raises:
            NotImplementedError: On adapters that declare the surface but
                have not implemented it yet (PostgreSQL).
        """
        if self._storage is None or not hasattr(self._storage, "list_xrefs"):
            return []
        return self._storage.list_xrefs(entity_id)

    def history(self, entity_id: str) -> list[dict[str, Any]]:
        """Get the change history for an entity."""
        return self._provenance_service.history(entity_id)

    def state_at(self, entity_id: str, timestamp: str) -> Optional[dict[str, Any]]:
        """Get the entity state at a specific point in time."""
        return self._provenance_service.state_at(entity_id, timestamp)

    def status(self) -> dict[str, Any]:
        """Service status summary (sec4 system endpoints).

        Reports the Mosaic version, active storage adapter, schema
        version, declared entity types, per-type entity counts, and the
        adapter's capability declarations. Entity counts include
        unavailable (soft-deleted / superseded) entities — Mosaic never
        hard-deletes.

        Returns:
            Status dict with ``service``, ``version``, ``adapter``,
            ``schema_version``, ``entity_types``, ``entity_counts``, and
            ``capabilities`` keys. Storage- and registry-derived fields
            are ``None``/empty when the client has no storage adapter or
            schema registry attached.
        """
        from mosaic import __version__

        adapter_name: Optional[str] = None
        entity_counts: dict[str, int] = {}
        capabilities: dict[str, Any] = {}
        if self._storage is not None:
            adapter_name = type(self._storage).__name__
            if hasattr(self._storage, "search_capabilities"):
                capabilities["search"] = sorted(
                    self._storage.search_capabilities()
                )
            capabilities["staged_transaction"] = hasattr(
                self._storage, "staged_transaction"
            )
            if hasattr(self._storage, "entity_counts"):
                entity_counts = self._storage.entity_counts()

        schema_version: Optional[str] = None
        entity_types: list[str] = []
        if self._registry is not None:
            schema_version = (
                self._registry.schema_view.schema.version or "unversioned"
            )
            entity_types = sorted(self._registry.class_names())

        return {
            "service": "mosaic",
            "version": __version__,
            "adapter": adapter_name,
            "schema_version": schema_version,
            "entity_types": entity_types,
            "entity_counts": entity_counts,
            "capabilities": capabilities,
        }

    # -- RecipeService delegations (sec10 §10.2.1) --

    def recipe_list(self) -> list[InstalledRecipe]:
        """Return every entry in ``hippo_meta.installed_recipes``.

        Thin delegator over :meth:`RecipeService.list_installed`. Returns
        ``[]`` on a clean instance.
        """
        return self._recipe_service.list_installed()

    def recipe_inspect(
        self,
        source: str | Path,
        *,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> RecipeReport:
        """Parse, validate, and digest a recipe — no state change (sec10 §10.2.3).

        Thin delegator over :meth:`RecipeService.inspect`. Useful for
        authoring (``mosaic recipe inspect`` prints the canonical
        content hash) and for callers that want a typed report before
        committing to an import.
        """
        return self._recipe_service.inspect(
            source, base_dir=base_dir, expected_digest=expected_digest
        )

    def recipe_import(
        self,
        source: str | Path,
        *,
        dry_run: bool = False,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> ImportResult:
        """Bootstrap-install a recipe end-to-end (sec10 §10.4).

        Thin delegator over :meth:`RecipeService.import_`. Resolves
        dependencies bottom-up, merges every fragment through
        :class:`SchemaManager`, writes one ``installed_recipes`` entry
        per recipe, and emits one ``recipe_imported`` provenance event
        per recipe — all inside a single storage transaction.
        """
        return self._recipe_service.import_(
            source,
            dry_run=dry_run,
            base_dir=base_dir,
            expected_digest=expected_digest,
        )

    def recipe_export(
        self,
        *,
        scope: str = "schema",
        parent: Optional[str] = None,
    ) -> RecipeExport:
        """Package locally-authored schema for redistribution (sec10 §10.5).

        Thin delegator over :meth:`RecipeService.export`. Returns a
        :class:`RecipeExport` (manifest dict + schema-fragment dict +
        auto-resolved ``requires.recipes`` list); the CLI is the only
        caller that writes those documents to disk.
        """
        return self._recipe_service.export(scope=scope, parent=parent)

    def recipe_extend(
        self,
        installed_id: str,
        out_dir: Path,
    ) -> Path:
        """Scaffold a derivative recipe directory (sec10 §10.7.3).

        Thin delegator over :meth:`RecipeService.extend`. Writes a
        ``recipe.yaml`` whose ``parent`` block is populated from the
        installed-recipe entry and an empty ``schema.yaml`` ready for
        local additions. Returns the output directory.
        """
        return self._recipe_service.extend(installed_id, out_dir)

    def recipe_diff(
        self,
        a: str | Path,
        b: str | Path,
        *,
        base_dir_a: Optional[Path] = None,
        base_dir_b: Optional[Path] = None,
    ) -> RecipeDiff:
        """Structural diff between two recipes' schemas (sec10 §10.2.3).

        Thin delegator over :meth:`RecipeService.diff`. Returns a
        :class:`RecipeDiff` with classes/slots added, removed, and
        changed between ``a`` and ``b``.
        """
        return self._recipe_service.diff(
            a, b, base_dir_a=base_dir_a, base_dir_b=base_dir_b
        )

    def recipe_export_lockfile(self, out: Path) -> Path:
        """Dump ``installed_recipes`` as ``recipe.lock.yaml`` (sec10 §10.6).

        Thin delegator over :meth:`RecipeService.export_lockfile`.
        Writes a portable YAML document with ``lockfile_version: 1`` and
        one entry per installed recipe (id, version, source,
        sha256-prefixed digest, installed_at, parent).
        """
        return self._recipe_service.export_lockfile(out)

    def recipe_install_from_lockfile(self, lockfile: Path) -> list[ImportResult]:
        """Replay a lockfile on the current instance (sec10 §10.6).

        Thin delegator over :meth:`RecipeService.install_from_lockfile`.
        Installs every lockfile entry in dependency order, verifying
        each digest against the freshly-fetched bytes.
        """
        return self._recipe_service.install_from_lockfile(lockfile)


# Deprecated alias (ADR-0004): the class was renamed with the component.
# Assignment alias so ``isinstance`` / ``issubclass`` hold across spellings.
HippoClient = MosaicClient  # deprecated
