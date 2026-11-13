"""QueryService - Entity queries, FTS search, and relationship traversal facade."""

from typing import Any, Optional

from mosaic.core.batch_fetcher import BatchFetcher
from mosaic.core.cycle_detector import validate_no_cycle
from mosaic.core.exceptions import EntityNotFoundError
from mosaic.core.expand_path_parser import ExpandPathParser
from mosaic.core.provenance_service import ProvenanceService
from mosaic.core.relationship import RelationshipManager
from mosaic.core.schema_manager import SchemaManager
from mosaic.core.storage import Query, normalize_where
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter


class QueryService:
    """Manages entity queries, FTS search, and relationship traversal.

    This facade owns all read/query logic extracted from MosaicClient.
    """

    def __init__(
        self,
        storage: Optional[SQLiteAdapter] = None,
        schema_manager: Optional[SchemaManager] = None,
        provenance_service: Optional[ProvenanceService] = None,
    ) -> None:
        self._storage = storage
        self._schema_manager = schema_manager
        self._provenance_service = provenance_service

    @property
    def relationships(self) -> RelationshipManager:
        """Get the relationship manager (lazy-initialized)."""
        if not hasattr(self, "_relationship_manager"):
            self._relationship_manager = RelationshipManager(storage=self._storage)
        return self._relationship_manager

    @relationships.setter
    def relationships(self, value: RelationshipManager) -> None:
        self._relationship_manager = value

    def get(
        self,
        entity_type: str,
        entity_id: str,
        expand: Optional[str] = None,
        include_unavailable: bool = False,
    ) -> dict[str, Any]:
        """Get an entity by its ID."""
        if self._storage is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type=entity_type,
                entity_id=entity_id,
            )

        if include_unavailable and hasattr(self._storage, "read_any"):
            entity = self._storage.read_any(entity_id)
        else:
            entity = self._storage.read(entity_id)
            if entity is None and hasattr(self._storage, "read_any"):
                any_entity = self._storage.read_any(entity_id)
                if any_entity is not None and any_entity.entity_type == entity_type:
                    raise EntityNotFoundError(
                        message=f"Entity not found: {entity_id}",
                        entity_type=entity_type,
                        entity_id=entity_id,
                    )

        if entity is None or entity.entity_type != entity_type:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_type=entity_type,
                entity_id=entity_id,
            )

        # sec9 §9.7: temporal fields computed from ProvenanceRecord at
        # read time. One SQL round-trip via get_temporal. Missing or
        # inconsistent provenance is loud per sec9 §9.2 (Provenance
        # integrity is transactional and loud).
        created_at: Optional[str] = None
        updated_at: Optional[str] = None
        schema_version: Optional[str] = None
        created_by: Optional[str] = None
        updated_by: Optional[str] = None
        if hasattr(self._storage, "get_temporal"):
            temporal_map = self._storage.get_temporal([entity_id])
            temporal = temporal_map.get(entity_id)
            if temporal is None:
                from mosaic.core.exceptions import ProvenanceIntegrityError

                raise ProvenanceIntegrityError(
                    message=(
                        f"Entity {entity_id!r} exists but has no "
                        f"ProvenanceRecord. Every mutation must emit a "
                        f"record transactionally (sec9 §9.6); missing "
                        f"provenance indicates adapter or data corruption."
                    ),
                    entity_id=entity_id,
                    inconsistency="missing_provenance",
                )
            if temporal.created_at is None:
                from mosaic.core.exceptions import ProvenanceIntegrityError

                raise ProvenanceIntegrityError(
                    message=(
                        f"Entity {entity_id!r} has ProvenanceRecord entries "
                        f"but none with operation='create'. The earliest "
                        f"record for every entity MUST be a 'create' per "
                        f"sec9 §9.7."
                    ),
                    entity_id=entity_id,
                    inconsistency="missing_create_record",
                )
            created_at = temporal.created_at
            updated_at = temporal.updated_at
            schema_version = temporal.schema_version
            created_by = temporal.created_by
            updated_by = temporal.updated_by

        result = {
            "id": entity.id,
            "entity_type": entity.entity_type,
            "data": entity.data,
            "version": entity.version,
            "is_available": bool(entity.is_available),
            "created_at": created_at,
            "updated_at": updated_at,
            "schema_version": schema_version,
            "created_by": created_by,
            "updated_by": updated_by,
            "superseded_by": entity.superseded_by,
        }

        if expand:
            parsed = self._parse_and_validate_expand(expand)
            fetcher = BatchFetcher(storage=self._storage)
            fetch_result = fetcher.fetch(parsed, entity_id)
            result["_expanded"] = fetch_result.expanded_data

        return result

    def _parse_and_validate_expand(self, expand: str) -> Any:
        """Parse and validate an expand path."""
        parser = ExpandPathParser()
        parsed = parser.parse(expand)
        validate_no_cycle(parsed)
        return parsed

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
        include_subclasses: bool = True,
    ) -> "PaginatedResult":
        """Query entities with filter criteria.

        Args:
            entity_type: Restrict the query to one entity type. ``None``
                queries across all types. A concrete parent resolves over
                its concrete closure (ADR-0014 Decision 1) unless
                ``include_subclasses`` is false; items carry their own
                concrete ``entity_type``.
            filter_mode: How to combine filters — "and" (all must match,
                default) or "or" (any may match).
            as_of: Optional ISO-8601 transaction-time. When given, results are
                reconstructed as the graph stood at that time (sec6 §6.8 /
                ADR-0001): entity set, per-entity state, and the computed
                temporal fields are all bound to ``as_of``. Omitted = current.
            where: Optional boolean filter tree (ADR-0006 increment 2) — a
                leaf ``{"field", "op", "value"}`` or ``{"and": [...]}`` /
                ``{"or": [...]}`` / ``{"not": node}``. Validated up front
                (``normalize_where``); composes with ``filters`` by AND.
            order_by: Optional stored-slot name to order by (ADR-0007).
                When given, ordering AND pagination push down to storage
                (SQL ORDER BY / LIMIT / OFFSET; NULLs last, stable ``id``
                tiebreak) and ``total`` comes from a ``COUNT(*)`` under the
                same predicate. Computed temporal fields are not orderable
                (they are provenance-derived, not columns); not combinable
                with ``as_of`` or ``date_from``/``date_to``. Omitted = the
                historical default ordering (``created_at`` ascending,
                computed in Python).
            order_dir: "asc" (default) or "desc"; only used with order_by.
        """
        from mosaic.core.types import PaginatedResult

        if where is not None:
            where = normalize_where(where)
        self._validate_order_args(order_by, order_dir, as_of, date_from, date_to)

        if self._storage is None:
            return PaginatedResult(
                items=[],
                total=0,
                limit=limit or 0,
                offset=offset or 0,
            )

        if order_by is not None:
            # Pushdown path (ADR-0007): storage orders and pages; total is a
            # COUNT(*) under the identical predicate (count ignores
            # limit/offset); temporal fields are derived for the page only.
            query = Query(
                entity_type=entity_type,
                filters=filters or [],
                limit=limit,
                offset=offset,
                filter_mode=filter_mode,
                where=where,
                order_by=order_by,
                order_dir=order_dir,
                include_subclasses=include_subclasses,
            )
            page_entities = list(self._storage.find(query))
            total = self._storage.count(query)
            items = self._hydrate_entities(page_entities, as_of=None)
            return PaginatedResult(
                items=items,
                total=total,
                limit=limit or 0,
                offset=offset or 0,
            )

        query = Query(
            entity_type=entity_type,
            filters=filters or [],
            filter_mode=filter_mode,
            where=where,
            include_subclasses=include_subclasses,
        )

        # Pass as_of only when set, so adapters whose find() predates the as-of
        # contract (e.g. the LinkML spike) keep working on the current-state path.
        if as_of is not None:
            all_results = list(self._storage.find(query, as_of=as_of))
        else:
            all_results = list(self._storage.find(query))

        # sec9 §9.7: one batch aggregation round-trip for the result set,
        # not N+1. The adapter's get_temporal returns TemporalRecord
        # dicts keyed by entity_id. Loud failure on missing provenance
        # mirrors client.get() — a corrupt entity in a page poisons the
        # page rather than silently returning stale stored columns
        # (Decision 9.7.A).
        entity_ids = [entity.id for entity in all_results]
        temporal_map: dict[str, Any] = {}
        if entity_ids and hasattr(self._storage, "get_temporal"):
            temporal_map = self._storage.get_temporal(entity_ids, as_of=as_of)

        filtered = []
        for entity in all_results:
            temporal = temporal_map.get(entity.id)
            if temporal is None and entity_ids:
                from mosaic.core.exceptions import ProvenanceIntegrityError

                raise ProvenanceIntegrityError(
                    message=(
                        f"Entity {entity.id!r} exists in query results but "
                        f"has no ProvenanceRecord. sec9 §9.2 requires "
                        f"transactional provenance on every mutation."
                    ),
                    entity_id=entity.id,
                    inconsistency="missing_provenance",
                )
            if temporal is not None and temporal.created_at is None:
                from mosaic.core.exceptions import ProvenanceIntegrityError

                raise ProvenanceIntegrityError(
                    message=(
                        f"Entity {entity.id!r} has ProvenanceRecord entries "
                        f"but none with operation='create'."
                    ),
                    entity_id=entity.id,
                    inconsistency="missing_create_record",
                )
            if temporal is not None:
                created_at = temporal.created_at
                updated_at = temporal.updated_at
                schema_version = temporal.schema_version
                created_by = temporal.created_by
                updated_by = temporal.updated_by
            else:
                created_at = None
                updated_at = None
                schema_version = None
                created_by = None
                updated_by = None

            if date_from and created_at and created_at < date_from:
                continue
            if date_to and created_at and created_at > date_to:
                continue

            filtered.append(
                {
                    "id": entity.id,
                    "entity_type": entity.entity_type,
                    "data": entity.data,
                    "version": entity.version,
                    "is_available": bool(entity.is_available),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "schema_version": schema_version,
                    "created_by": created_by,
                    "updated_by": updated_by,
                    "superseded_by": entity.superseded_by,
                }
            )

        filtered.sort(key=lambda x: x["created_at"] or "")

        total = len(filtered)

        actual_offset = offset or 0
        if actual_offset:
            filtered = filtered[actual_offset:]
        # `is not None`, not truthiness: limit=0 means "zero rows", not
        # "unlimited" — Python's falsy-0 would otherwise return everything
        # (issue #130).
        if limit is not None:
            filtered = filtered[:limit]

        return PaginatedResult(
            items=filtered,
            total=total,
            limit=limit or 0,
            offset=actual_offset,
        )

    @staticmethod
    def _validate_order_args(
        order_by: Optional[str],
        order_dir: str,
        as_of: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
    ) -> None:
        """Reject invalid/unsupported order_by combinations up front
        (ADR-0007 gate decisions), before any storage round-trip."""
        from mosaic.core.exceptions import ValidationError

        if order_dir not in ("asc", "desc"):
            raise ValidationError(
                message=(
                    f"order_dir must be 'asc' or 'desc', got {order_dir!r}."
                ),
                field_name="order_dir",
            )
        if order_by is None:
            return
        if as_of is not None:
            raise ValidationError(
                message=(
                    "order_by is not supported together with as_of: ordering "
                    "pushdown targets current-state storage; the "
                    "reconstructed as-of path keeps its documented Python "
                    "ordering (ADR-0007 gate decision)."
                ),
                field_name="order_by",
            )
        if date_from or date_to:
            raise ValidationError(
                message=(
                    "order_by cannot be combined with date_from/date_to: the "
                    "created_at window filters on a computed temporal field "
                    "in Python after materialization, which would page "
                    "before the window applies. Filter on a stored date "
                    "slot instead (ADR-0007)."
                ),
                field_name="order_by",
            )

    def _hydrate_entities(
        self, entities: list, as_of: Optional[str]
    ) -> list[dict[str, Any]]:
        """Convert storage entities to result dicts with computed temporal
        fields — one batch ``get_temporal`` round-trip (sec9 §9.7), loud
        failure on missing provenance (Decision 9.7.A)."""
        entity_ids = [entity.id for entity in entities]
        temporal_map: dict[str, Any] = {}
        if entity_ids and hasattr(self._storage, "get_temporal"):
            temporal_map = self._storage.get_temporal(entity_ids, as_of=as_of)

        items: list[dict[str, Any]] = []
        for entity in entities:
            temporal = temporal_map.get(entity.id)
            if temporal is None and entity_ids:
                from mosaic.core.exceptions import ProvenanceIntegrityError

                raise ProvenanceIntegrityError(
                    message=(
                        f"Entity {entity.id!r} exists in query results but "
                        f"has no ProvenanceRecord. sec9 §9.2 requires "
                        f"transactional provenance on every mutation."
                    ),
                    entity_id=entity.id,
                    inconsistency="missing_provenance",
                )
            if temporal is not None and temporal.created_at is None:
                from mosaic.core.exceptions import ProvenanceIntegrityError

                raise ProvenanceIntegrityError(
                    message=(
                        f"Entity {entity.id!r} has ProvenanceRecord entries "
                        f"but none with operation='create'."
                    ),
                    entity_id=entity.id,
                    inconsistency="missing_create_record",
                )
            items.append(
                {
                    "id": entity.id,
                    "entity_type": entity.entity_type,
                    "data": entity.data,
                    "version": entity.version,
                    "is_available": bool(entity.is_available),
                    "created_at": temporal.created_at if temporal else None,
                    "updated_at": temporal.updated_at if temporal else None,
                    "schema_version": (
                        temporal.schema_version if temporal else None
                    ),
                    "created_by": temporal.created_by if temporal else None,
                    "updated_by": temporal.updated_by if temporal else None,
                    "superseded_by": entity.superseded_by,
                }
            )
        return items

    def count(
        self,
        entity_type: Optional[str] = None,
        filters: Optional[list[dict[str, Any]]] = None,
        filter_mode: str = "and",
        as_of: Optional[str] = None,
        where: Optional[dict[str, Any]] = None,
        include_subclasses: bool = True,
    ) -> int:
        """Count matching entities without materializing them (ADR-0007).

        Sees exactly what ``query()`` sees under the same criteria
        (availability + filters/where — the availability-consistency rule).
        Under ``as_of`` the count is the length of the reconstructed match
        set (Python-path semantics, same as ``query(as_of=...).total``).
        """
        if where is not None:
            where = normalize_where(where)
        if self._storage is None:
            return 0
        query = Query(
            entity_type=entity_type,
            filters=filters or [],
            filter_mode=filter_mode,
            where=where,
            include_subclasses=include_subclasses,
        )
        if as_of is not None:
            return self._storage.count(query, as_of=as_of)
        return self._storage.count(query)

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

        Entities with no stored value for ``field`` are not counted
        (absence is queried with ``is_null``). Availability-consistent:
        buckets sum over exactly the entities ``query()`` would return.
        Not defined under as-of in this increment.
        """
        if where is not None:
            where = normalize_where(where)
        if self._storage is None:
            return []
        query = Query(
            entity_type=entity_type,
            filters=filters or [],
            filter_mode=filter_mode,
            where=where,
            include_subclasses=True,
        )
        return list(self._storage.facet_counts(query, field))

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
        if where is not None:
            where = normalize_where(where)
        if self._storage is None:
            return (None, None)
        query = Query(
            entity_type=entity_type,
            filters=filters or [],
            filter_mode=filter_mode,
            where=where,
            include_subclasses=True,
        )
        return tuple(self._storage.field_range(query, field))

    def count_relationship(
        self, entity_type: str, entity_id: str, edge: str
    ) -> int:
        """Cardinality of a multivalued reference edge without resolving
        its member objects (issue #132). See
        ``EntityStore.count_relationship`` for the availability rule.
        """
        if self._storage is None:
            return 0
        return self._storage.count_relationship(entity_type, entity_id, edge)

    def query_updated_since(
        self,
        entity_type: Optional[str] = None,
        since: str = "",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list[dict[str, Any]]] = None,
    ) -> "PaginatedResult":
        """Query entities updated after a watermark timestamp (sec4 §4.5).

        Designed for polling callers (e.g. Cappella's ``hippo_poll``
        trigger). Selects entities whose provenance-derived ``updated_at``
        is strictly greater than ``since`` and orders results by
        ``updated_at`` ascending — oldest first, so callers can process
        in order and persist the last ``updated_at`` they saw as the
        watermark for the next poll.

        ``since`` is compared against Hippo's server-side provenance
        timestamps (UTC); callers should never use their own clock. The
        watermark filter runs over the same provenance-derived read path
        :meth:`query` uses (mirroring its ``date_from``/``date_to`` in-Python
        pattern); pushing it into the ``entity_provenance_summary`` view
        (sec6 §6.6) is the documented optimization if polling volume grows.

        Args:
            entity_type: Restrict the poll to one entity type. ``None``
                polls across all types (composes with the issue #44/#49
                cross-class scan).
            since: ISO 8601 timestamp watermark (exclusive).
            limit: Maximum number of results to return.
            offset: Number of results to skip.
            filters: Optional additional field filters (AND-composed).

        Raises:
            TemporalQueryError: If ``since`` is not a parseable ISO 8601
                timestamp.
        """
        from mosaic.core.exceptions import TemporalQueryError
        from mosaic.core.types import PaginatedResult

        since_dt = self._parse_iso_timestamp(since)
        if since_dt is None:
            raise TemporalQueryError(
                message=(
                    f"updated_since must be an ISO 8601 timestamp; got {since!r}"
                ),
                requested_timestamp=since,
            )

        base = self.query(entity_type, filters=filters)

        matched: list[tuple[Any, dict[str, Any]]] = []
        for item in base.items:
            updated = self._parse_iso_timestamp(
                item.get("updated_at") or item.get("created_at") or ""
            )
            if updated is not None and updated > since_dt:
                matched.append((updated, item))

        matched.sort(key=lambda pair: pair[0])
        sorted_items = [item for _, item in matched]

        total = len(sorted_items)
        actual_offset = offset or 0
        if actual_offset:
            sorted_items = sorted_items[actual_offset:]
        if limit is not None:  # limit=0 means zero rows, not unlimited (#130)
            sorted_items = sorted_items[:limit]

        return PaginatedResult(
            items=sorted_items,
            total=total,
            limit=limit or 0,
            offset=actual_offset,
        )

    def search_all(
        self, query: str, limit: Optional[int] = 100
    ) -> list[dict[str, Any]]:
        """Cross-class ranked full-text search (issue #158).

        Fans over every FTS-indexed class server-side via the adapters'
        ranked search path, merges by the per-index-normalized score
        (deterministic ``(score desc, entity_type, id)`` order — scores are
        normalized to [0, 1] per index, making them comparable in relative
        terms across classes), truncates to ``limit``, then materializes
        the page **batched by type** (one composed read per class present —
        never per-hit). Availability applies exactly as list queries.
        Returns result dicts (the ``query()`` item envelope) each carrying
        ``entity_type`` and a ``score`` key, rank-ordered.
        """
        if self._storage is None or self._schema_manager is None:
            return []

        best: dict[tuple[str, str], float] = {}
        for class_name in self._schema_manager.fts_entity_types():
            for fts_meta in self._schema_manager.get_fts_tables_for_entity_type(
                class_name
            ):
                for field_meta in fts_meta.fields:
                    for match in self._storage.search(
                        query=query,
                        entity_type=class_name,
                        field_name=field_meta.field_name,
                        limit=self.SEARCH_HIT_BUDGET,
                    ):
                        key = (class_name, match.entity_id)
                        if match.score > best.get(key, -1.0):
                            best[key] = match.score
        ranked = sorted(best, key=lambda k: (-best[k], k[0], k[1]))
        if limit is not None:  # limit=0 → zero hits, not unlimited (#130)
            ranked = ranked[:limit]
        if not ranked:
            return []

        by_class: dict[str, list[str]] = {}
        for class_name, entity_id in ranked:
            by_class.setdefault(class_name, []).append(entity_id)
        materialized: dict[tuple[str, str], dict[str, Any]] = {}
        for class_name, ids in by_class.items():
            page = list(
                self._storage.find(
                    Query(
                        entity_type=class_name,
                        where={"field": "id", "op": "in", "value": ids},
                    )
                )
            )
            for item in self._hydrate_entities(page, as_of=None):
                materialized[(class_name, item["id"])] = item

        return [
            {**materialized[key], "score": best[key]}
            for key in ranked
            if key in materialized
        ]

    #: Depth cap and node budget for ``neighbors`` (issue #158) — visible,
    #: disclosed bounds, never silent truncation.
    NEIGHBORS_MAX_DEPTH = 5
    NEIGHBORS_NODE_BUDGET = 1000

    def neighbors(
        self, entity_id: str, depth: int = 1, *, as_of: Optional[str] = None
    ) -> dict[str, Any]:
        """The subgraph around one entity (issue #158): depth-bounded BFS
        over BOTH edge stores — the ``relationships`` link table (ADR-0002
        multivalued reference edges, both directions) and the
        column-stored single-valued references (forward off the frontier's
        data; reverse via schema-driven FK queries) — closing the
        ADR-0002 visibility gap a link-table-only traversal has.

        Returns ``{"nodes", "edges", "edge_sources", "notices"}``. Node
        payloads materialize **batched by type**; edges are kept only when
        both endpoints materialize (availability parity with list
        queries). Under ``as_of``, link-table edges replay from provenance
        (sec6 §6.8.2) and node states reconstruct at ``as_of``; column
        edges are **out of scope under as_of** (hippo#71) — disclosed via
        ``edge_sources``/``notices``, never a silently partial temporal
        graph.
        """
        from mosaic.core.exceptions import EntityNotFoundError

        depth = max(1, min(depth, self.NEIGHBORS_MAX_DEPTH))
        notices: list[str] = []
        edge_sources = (
            ["LINK_TABLE"] if as_of is not None else ["LINK_TABLE", "COLUMN"]
        )
        empty = {
            "nodes": [],
            "edges": [],
            "edge_sources": edge_sources,
            "notices": notices,
        }
        if self._storage is None:
            return empty

        if as_of is not None:
            return self._neighbors_as_of(entity_id, depth, as_of, empty)

        if self._storage.read(entity_id) is None:
            raise EntityNotFoundError(
                message=f"Entity not found: {entity_id}",
                entity_id=entity_id,
            )

        registry = getattr(self._storage, "schema_registry", None)
        # Schema-driven single-valued reference map: (class, slot, target).
        ref_slots: list[tuple[str, str, str]] = []
        fwd_by_class: dict[str, list[tuple[str, str]]] = {}
        if registry is not None:
            known = set(registry.class_names())
            for class_name in known:
                for slot in registry.induced_slots(class_name):
                    if not slot.multivalued and slot.range in known:
                        ref_slots.append((class_name, slot.name, slot.range))
                        fwd_by_class.setdefault(class_name, []).append(
                            (slot.name, slot.range)
                        )

        seen: set[str] = {entity_id}
        frontier: set[str] = {entity_id}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        truncated = False

        def add_edge(source: str, target: str, rel_type: str, origin: str) -> None:
            edges.setdefault(
                (source, target, rel_type),
                {
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "edge_source": origin,
                },
            )

        for _ in range(depth):
            if not frontier or truncated:
                break
            new_ids: set[str] = set()

            # Link-table edges, both directions.
            for fid in frontier:
                for rel in self.relationships.find_relationships(source_id=fid):
                    add_edge(fid, rel["target_id"], rel["relationship_type"], "LINK_TABLE")
                    new_ids.add(rel["target_id"])
                for rel in self.relationships.find_relationships(target_id=fid):
                    add_edge(rel["source_id"], fid, rel["relationship_type"], "LINK_TABLE")
                    new_ids.add(rel["source_id"])

            # Column edges: one batched read per frontier type.
            types = self._storage.resolve_types(list(frontier)) if hasattr(
                self._storage, "resolve_types"
            ) else {}
            by_type: dict[str, list[str]] = {}
            for fid, cls in types.items():
                by_type.setdefault(cls, []).append(fid)
            for cls, ids in by_type.items():
                fwd = fwd_by_class.get(cls, [])
                if not fwd:
                    continue
                for entity in self._storage.find(
                    Query(
                        entity_type=cls,
                        where={"field": "id", "op": "in", "value": ids},
                    )
                ):
                    for slot_name, _target in fwd:
                        value = (entity.data or {}).get(slot_name)
                        if isinstance(value, str) and value:
                            add_edge(entity.id, value, slot_name, "COLUMN")
                            new_ids.add(value)
            # Reverse column edges: schema-driven FK queries, one per
            # (class, slot) pair whose target class is on the frontier.
            for cls, slot_name, target in ref_slots:
                # The frontier row may be a subclass of the slot's range
                # (``Brain.produced_by -> Process`` pointing at a
                # ``BankBrain``): match over the range's concrete closure
                # (ADR-0014 Decision 1).
                target_ids = [
                    tid
                    for t in (registry.concrete_closure(target) or (target,))
                    for tid in by_type.get(t, ())
                ]
                if not target_ids:
                    continue
                for entity in self._storage.find(
                    Query(
                        entity_type=cls,
                        where={
                            "field": slot_name,
                            "op": "in",
                            "value": target_ids,
                        },
                    )
                ):
                    referenced = (entity.data or {}).get(slot_name)
                    if referenced in target_ids:
                        add_edge(entity.id, referenced, slot_name, "COLUMN")
                        new_ids.add(entity.id)

            new_ids -= seen
            if len(seen) + len(new_ids) > self.NEIGHBORS_NODE_BUDGET:
                keep = self.NEIGHBORS_NODE_BUDGET - len(seen)
                new_ids = set(sorted(new_ids)[: max(keep, 0)])
                truncated = True
                notices.append(
                    f"Neighborhood truncated at the "
                    f"{self.NEIGHBORS_NODE_BUDGET}-node budget; increase "
                    f"specificity or lower depth for a complete view."
                )
            seen |= new_ids
            frontier = new_ids

        nodes = self._materialize_nodes(seen)
        node_ids = {n["entity_id"] for n in nodes}
        return {
            "nodes": nodes,
            "edges": [
                e
                for e in edges.values()
                if e["source"] in node_ids and e["target"] in node_ids
            ],
            "edge_sources": edge_sources,
            "notices": notices,
        }

    def _materialize_nodes(self, ids: set[str]) -> list[dict[str, Any]]:
        """Batched-by-type node materialization (one composed read per
        class present — the get_entity_loader discipline, never
        get-per-node). Unavailable/unknown ids drop out here, which is
        what enforces availability parity on the returned subgraph."""
        if not ids or not hasattr(self._storage, "resolve_types"):
            return []
        types = self._storage.resolve_types(list(ids))
        by_type: dict[str, list[str]] = {}
        for eid, cls in types.items():
            by_type.setdefault(cls, []).append(eid)
        nodes: list[dict[str, Any]] = []
        for cls, class_ids in sorted(by_type.items()):
            page = list(
                self._storage.find(
                    Query(
                        entity_type=cls,
                        where={"field": "id", "op": "in", "value": class_ids},
                    )
                )
            )
            for item in self._hydrate_entities(page, as_of=None):
                nodes.append(
                    {
                        "entity_id": item["id"],
                        "entity_type": cls,
                        "data": item["data"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"],
                    }
                )
        nodes.sort(key=lambda n: (n["entity_type"], n["entity_id"]))
        return nodes

    def _neighbors_as_of(
        self,
        entity_id: str,
        depth: int,
        as_of: str,
        empty: dict[str, Any],
    ) -> dict[str, Any]:
        """The as-of neighborhood: BFS (both directions) over the
        link-table edges live at ``as_of`` (provenance replay), node
        states reconstructed at ``as_of``. Column edges are out of scope
        under as-of (hippo#71) — disclosed, never silent."""
        empty["notices"].append(
            "asOf neighborhoods cover link-table edges only: column-stored "
            "single-valued reference edges are not reconstructed at a "
            "transaction-time (hippo#71); current-state neighbors include "
            "both edge stores."
        )
        live = self.relationships.live_edges_at(as_of)
        out_adj: dict[str, list[dict[str, Any]]] = {}
        in_adj: dict[str, list[dict[str, Any]]] = {}
        for edge in live:
            out_adj.setdefault(edge["source_id"], []).append(edge)
            in_adj.setdefault(edge["target_id"], []).append(edge)

        seen: set[str] = {entity_id}
        frontier: set[str] = {entity_id}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for _ in range(depth):
            if not frontier:
                break
            new_ids: set[str] = set()
            for fid in frontier:
                for edge in out_adj.get(fid, []):
                    key = (edge["source_id"], edge["target_id"], edge["relationship_type"])
                    edges.setdefault(
                        key,
                        {
                            "source": edge["source_id"],
                            "target": edge["target_id"],
                            "type": edge["relationship_type"],
                            "edge_source": "LINK_TABLE",
                        },
                    )
                    new_ids.add(edge["target_id"])
                for edge in in_adj.get(fid, []):
                    key = (edge["source_id"], edge["target_id"], edge["relationship_type"])
                    edges.setdefault(
                        key,
                        {
                            "source": edge["source_id"],
                            "target": edge["target_id"],
                            "type": edge["relationship_type"],
                            "edge_source": "LINK_TABLE",
                        },
                    )
                    new_ids.add(edge["source_id"])
            frontier = new_ids - seen
            seen |= new_ids

        # Node states at as_of: per-node provenance reconstruction (the
        # documented as-of Python path; the batched discipline applies to
        # the current-state materializer).
        types = (
            self._storage.resolve_types(list(seen))
            if hasattr(self._storage, "resolve_types")
            else {}
        )
        nodes = []
        for eid in sorted(seen):
            state = (
                self._storage.state_at(eid, as_of)
                if hasattr(self._storage, "state_at")
                else None
            )
            if state is None:
                continue  # did not exist / unavailable at as_of
            data = state.get("state") if isinstance(state, dict) else None
            if not isinstance(data, dict):
                continue
            nodes.append(
                {
                    "entity_id": eid,
                    "entity_type": types.get(eid),
                    "data": data,
                    "created_at": None,
                    "updated_at": None,
                }
            )
        node_ids = {n["entity_id"] for n in nodes}
        empty["nodes"] = nodes
        empty["edges"] = [
            e
            for e in edges.values()
            if e["source"] in node_ids and e["target"] in node_ids
        ]
        return empty

    @staticmethod
    def _parse_iso_timestamp(value: str) -> Optional["datetime"]:
        """Parse an ISO 8601 timestamp, normalizing ``Z`` and naive values to UTC."""
        from datetime import datetime, timezone

        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    #: FTS hit-set budget per search (matches the adapters' ranked-search
    #: cap). ``total`` is exact within this budget; a match set larger than
    #: the budget is truncated at the ranking stage.
    SEARCH_HIT_BUDGET = 1000

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

        Id-set composition: both adapters' ranked FTS path (``storage.search``
        → ``ScoredMatch``, bm25/ts_rank ordered, availability-filtered)
        produces an ordered id list that feeds ONE ``find()`` as an
        ``id IN (…)`` predicate alongside the caller's ``filters``/``where``
        — search results obey exactly the list surface's filter semantics
        and availability rule, with one batched read per page instead of a
        per-hit N+1.

        Returns the same ``PaginatedResult`` envelope as ``query()``:
        ``total`` is the matching-AND-filtered count (bounded by
        :data:`SEARCH_HIT_BUDGET` FTS hits). With no ``order_by``, items
        come back in FTS rank order (the point of search); an explicit
        ``order_by`` overrides rank and pushes ordering + pagination down
        to storage (the pinned precedence rule).
        """
        from mosaic.core.types import PaginatedResult

        if where is not None:
            where = normalize_where(where)
        self._validate_order_args(order_by, order_dir, None, None, None)

        empty = PaginatedResult(
            items=[], total=0, limit=limit or 0, offset=offset or 0
        )
        if self._storage is None or self._schema_manager is None:
            return empty

        fts_tables = self._schema_manager.get_fts_tables_for_entity_type(entity_type)
        if not fts_tables:
            return empty

        # Ranked id set: best score per entity across the class's FTS
        # fields; score desc, first-seen order as the stable tiebreak.
        best_scores: dict[str, float] = {}
        first_seen: dict[str, int] = {}
        for fts_meta in fts_tables:
            for field_meta in fts_meta.fields:
                matches = self._storage.search(
                    query=query,
                    entity_type=entity_type,
                    field_name=field_meta.field_name,
                    limit=self.SEARCH_HIT_BUDGET,
                )
                for match in matches:
                    eid = match.entity_id
                    first_seen.setdefault(eid, len(first_seen))
                    if match.score > best_scores.get(eid, -1.0):
                        best_scores[eid] = match.score
        ranked_ids = sorted(
            best_scores, key=lambda eid: (-best_scores[eid], first_seen[eid])
        )
        if not ranked_ids:
            return empty

        # Compose the ranked id set with the caller's criteria: the id
        # membership ANDs into the `where` tree (a flat filter would be
        # subject to filter_mode="or"), flat filters keep their mode.
        id_leaf = {"field": "id", "op": "in", "value": ranked_ids}
        tree = id_leaf if where is None else {"and": [id_leaf, where]}
        composed = Query(
            entity_type=entity_type,
            filters=filters or [],
            filter_mode=filter_mode,
            where=tree,
        )

        if order_by is not None:
            # Explicit ordering overrides rank (the pinned precedence
            # rule): reuse the list surface's pushdown — SQL ORDER BY +
            # LIMIT/OFFSET, total from COUNT(*) under the same predicate.
            composed.limit = limit
            composed.offset = offset
            composed.order_by = order_by
            composed.order_dir = order_dir
            page_entities = list(self._storage.find(composed))
            total = self._storage.count(composed)
        else:
            # Rank order: one batched read of the filtered hit set,
            # re-ordered by FTS rank, then paged. Temporal fields are
            # derived for the page only.
            rank = {eid: pos for pos, eid in enumerate(ranked_ids)}
            matched = sorted(
                self._storage.find(composed),
                key=lambda e: rank.get(e.id, len(rank)),
            )
            total = len(matched)
            page_entities = matched[offset or 0 :]
            if limit is not None:  # limit=0 → zero rows, not unlimited (#130)
                page_entities = page_entities[:limit]

        return PaginatedResult(
            items=self._hydrate_entities(page_entities, as_of=None),
            total=total,
            limit=limit or 0,
            offset=offset or 0,
        )
