"""``DomainModule`` species and single-hop ``evolve`` (Doc 2 §2A, sec11 §11.2.4).

``DomainModule`` is the *first-party mutable-data* species of the
:class:`~mosaic.core.loaders.schema_package.SchemaPackage` genus. Where
:class:`~mosaic.core.loaders.reference.ReferenceLoader` wraps externally
maintained, reconstructible reference datasets, a ``DomainModule`` owns
the deployment's **locally authored, operational** records — the lab's
authoritative domain data — and migrates them *in place* across versions.

It satisfies the :class:`~mosaic.core.loaders.schema_package.MigratableData`
capability protocol via two members:

* :meth:`DomainModule.migration_steps` — the declared migration edges.
* :meth:`DomainModule.evolve` — runs the single declared
  ``(from_version → to_version)`` step (S2 scope).

Semantic transformation model (sec11 §11.3, Doc 2 §5.2)
-------------------------------------------------------
A migration step is a small, independently testable transform that
**reads old-shape records, writes new-shape records, and supersedes the
old**. Each migrated record becomes a *new* entity (fresh id) and the
old entity is marked superseded by it (``supersede_entity``), so:

* the append-only, id-keyed, provenanced substrate makes the migration
  auditable and replay-recoverable (Doc 2 §2 rationale);
* every step emits ``supersede`` provenance events + a ``superseded_by``
  relationship edge ⇒ traceable lineage.

A consequence worth recording: after a migration, anything that
referenced an ``old_id`` now points at a superseded / unavailable
entity; resolution is via the ``superseded_by`` edge. This does not
surface in a single-class S2 proof, but S3 (multi-hop) and S4
(cross-package orchestration) inherit it.

Hard validation gate (sec11 §11.5.2, Doc 2 §5.4)
------------------------------------------------
The transform output is **staged** and dry-run-validated against the
fully merged schema *before any committed write*; the migration commits
only on green. In-process this reuses the same primitive the
``mosaic ingest --validate-schema <merged-dir> --dry-run`` CLI path
calls — :meth:`SchemaRegistry.validate` against the merged registry the
client already operates on (:attr:`MosaicClient.registry`). Only the
*new* records are validated; the old superseded rows are v1-shape and
are never re-validated.

Migration chain (S3, sec11 §11.3)
---------------------------------
``evolve`` resolves a path through the declared migration **DAG**, not a
single edge. :meth:`migration_steps` is read as a directed graph whose
nodes are version slugs and whose edges are the declared
``(from_version → to_version)`` transforms. :meth:`_resolve_path`
breadth-first-searches that graph from the deployment's current version
to the target, so the **fewest-hop** path wins — which means a declared
*shortcut* edge (e.g. a direct ``1.2.0 → 2.0.0`` bulk transform) is
preferred over the equivalent multi-step chain automatically, with no
special-casing. A single-hop upgrade is just a one-edge path through the
same resolver. Each hop is staged → gated → committed in turn (§11.3.3),
so a 3-hop upgrade composes its intermediate steps in order.

Two fail-loud boundaries (no silent fallback, sec11 §11.3.2):

* **Below the floor.** If the current version is not a node anywhere in
  the DAG, it predates the oldest shipped step; the resolver raises
  :class:`~mosaic.core.exceptions.MigrationFloorError` (*upgrade to
  ≥<floor> first*). The chain ships every step back to that floor, like
  an Alembic ``versions/`` directory — old package code is never fetched.
* **No path to target.** If the target is unreachable from a known
  current node, :class:`~mosaic.core.exceptions.MigrationStepNotFoundError`
  is raised.

``deprovision`` (S3, sec11 §11.4)
---------------------------------
A :class:`DomainModule` **refuses to deprovision by default when it owns
live domain data** (§11.4.3): its records are the lab's authoritative
operational data, so a silent soft-delete on uninstall would be a
data-loss event. The operator must pass ``force=True`` (ideally after an
export), which soft-deletes the populated rows via the standard
availability transition. The complementary *dependents guard* (§11.4.4 —
refuse to deprovision a package others ``depends_on``) lives in the CLI
orchestrator, which alone holds the installed-package graph.

Atomicity note (sec11 §11.8 [VERIFY], resolved S3)
--------------------------------------------------
There is **no** whole-``evolve`` (or whole-``upgrade()``) multi-entity
transaction. Each ``client.put`` and each ``client.supersede_entity``
opens its own SQL transaction (``SQLiteAdapter.create`` /
``update_data`` / ``ProvenanceService.supersede_entity`` — every one
wraps a single ``_storage._transaction()``), so a failure *between*
committed writes leaves partial state with no rollback across the chain.
The pre-commit staged gate (:meth:`_run_gate`) is the mitigation — it
catches schema-invalid output before *any* write — but it cannot undo a
runtime fault during the commit loop. True end-to-end commit-or-rollback
is the S4 orchestrator's job (sec11 §11.5.2).
"""

from __future__ import annotations

import tempfile
import uuid
from abc import abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import yaml
from pydantic import BaseModel

from mosaic.core.exceptions import (
    DeprovisionRefusedError,
    MigrationFloorError,
    MigrationGateError,
    MigrationStepNotFoundError,
)
from mosaic.core.loaders.reference import EntityRef, LoadResult
from mosaic.core.loaders.schema_package import SchemaPackage

if TYPE_CHECKING:
    from mosaic.core.client import MosaicClient
    from mosaic.linkml_bridge import SchemaRegistry

__all__ = [
    "DomainModule",
    "MigrationStep",
    "MigrationContext",
    "MigrationPlan",
    "StagedWrite",
    "StagedSupersession",
]


@dataclass(frozen=True)
class StagedWrite:
    """A new-shape record staged by a migration transform, not yet committed.

    ``data`` always carries an ``id`` (assigned by :meth:`MigrationPlan.migrate`
    when the transform does not supply one) so the staged bundle is concrete
    and the matching supersession can reference it.
    """

    entity_type: str
    data: dict


@dataclass(frozen=True)
class StagedSupersession:
    """A pending ``old_id → new_id`` supersession staged by a transform.

    Committed via :meth:`MosaicClient.supersede_entity` *after* the gate
    passes and the new record exists, so both endpoints resolve.
    """

    old_id: str
    new_id: str
    reason: str | None = None


class MigrationPlan:
    """Accumulator for one ``evolve`` hop — nothing is committed until the gate passes.

    A migration transform reads old records from the client and records
    its intended new-shape writes / supersessions here. :meth:`DomainModule.evolve`
    then stages the writes, runs the dry-run validation gate, and only on
    green replays the plan against the client.

    The common 1:1 case is :meth:`migrate`. :meth:`put` (net-new record,
    no supersession) and :meth:`supersede` (explicit edge) cover splits,
    merges, and hand-tuned lineage.
    """

    def __init__(self) -> None:
        self._writes: list[StagedWrite] = []
        self._supersessions: list[StagedSupersession] = []

    @property
    def writes(self) -> list[StagedWrite]:
        """New-shape records staged for write, in declaration order."""
        return list(self._writes)

    @property
    def supersessions(self) -> list[StagedSupersession]:
        """Pending ``old_id → new_id`` supersessions, in declaration order."""
        return list(self._supersessions)

    def migrate(
        self,
        entity_type: str,
        old_id: str,
        new_data: dict,
        reason: str | None = None,
    ) -> str:
        """Stage a new-shape record that **supersedes** ``old_id`` (1:1 migration).

        Always creates a *new* entity: any ``id`` in ``new_data`` is
        replaced with a fresh UUID, because :meth:`MosaicClient.supersede_entity`
        requires two distinct entities and emits the ``supersede`` event
        (an in-place same-id upsert would emit ``update`` instead — not a
        migration lineage). Returns the new entity's id.

        System fields (``id``, ``is_available``) are filled in so authors
        write only domain slots; ``is_available`` defaults to ``True`` (a
        migration produces live records) unless ``new_data`` sets it.
        """
        new_id = str(uuid.uuid4())
        record = {"is_available": True, **new_data, "id": new_id}
        self._writes.append(StagedWrite(entity_type=entity_type, data=record))
        self._supersessions.append(
            StagedSupersession(old_id=old_id, new_id=new_id, reason=reason)
        )
        return new_id

    def put(self, entity_type: str, new_data: dict) -> str:
        """Stage a net-new record with no supersession (e.g. one half of a split).

        Assigns a fresh UUID when ``new_data`` has no ``id`` and defaults
        ``is_available`` to ``True``. Returns the record's id so callers
        can wire an explicit :meth:`supersede`.
        """
        new_id = new_data.get("id") or str(uuid.uuid4())
        record = {"is_available": True, **new_data, "id": new_id}
        self._writes.append(StagedWrite(entity_type=entity_type, data=record))
        return new_id

    def supersede(
        self, old_id: str, new_id: str, reason: str | None = None
    ) -> None:
        """Stage an explicit ``old_id → new_id`` supersession edge.

        Pairs with :meth:`put` for non-1:1 migrations (a split where two
        new records jointly supersede one old record, etc.).
        """
        self._supersessions.append(
            StagedSupersession(old_id=old_id, new_id=new_id, reason=reason)
        )

    def is_empty(self) -> bool:
        """True when the transform staged neither writes nor supersessions."""
        return not self._writes and not self._supersessions


@dataclass
class MigrationContext:
    """Everything a migration transform receives for one hop.

    Read old-shape records via ``ctx.client`` (e.g.
    ``ctx.client.query(type, include_subclasses=False).items``) and stage
    new-shape records via ``ctx.plan`` (:meth:`MigrationPlan.migrate` /
    ``put`` / ``supersede``). Pass ``include_subclasses=False``: a concrete
    parent's ``query`` lists its subclasses' rows too (ADR-0014 Decision
    1), and a transform that migrates them as the parent class would
    migrate a subclass row twice.
    The transform must **not** write to the client directly — staging
    keeps the dry-run gate ahead of every committed write.
    """

    client: "MosaicClient"
    from_version: str
    to_version: str
    plan: MigrationPlan
    params: BaseModel | None = None


@dataclass(frozen=True)
class MigrationStep:
    """One declared migration edge: a ``(from_version → to_version)`` transform.

    ``transform`` reads old records and stages new-shape records +
    supersessions onto ``ctx.plan``. Steps are bare DAG edges by design:
    the S3 resolver composes them into multi-hop paths (with shortcut
    edges and a floor) without changing this shape.
    """

    from_version: str
    to_version: str
    transform: Callable[[MigrationContext], None]
    description: str = ""


class DomainModule(SchemaPackage):
    """First-party, mutable-data species of :class:`SchemaPackage`.

    Owns the deployment's locally authored operational records and
    migrates them in place. Authors implement :meth:`schema_fragment` and
    :meth:`versions` (from the genus) plus :meth:`migration_steps`; the
    genus ``provision`` / ``deprovision`` no-ops are inherited (domain
    data arrives via ``mosaic ingest``, not at schema-install time).

    Satisfies the :class:`~mosaic.core.loaders.schema_package.MigratableData`
    protocol (``migration_steps`` + ``evolve``).
    """

    @abstractmethod
    def migration_steps(self) -> list[MigrationStep]:
        """Return every declared migration edge shipped with this module.

        All steps covering the module's supported range ship with the
        module at its current version (sec11 §11.3.2) — the resolver
        never fetches old module code. S2 uses these as a flat set of
        single-hop edges; S3 reads the same list as a DAG.
        """

    def populates_types(self) -> list[str]:
        """Entity-type names this module fills with first-party data.

        A species concern (parallel to
        :meth:`ReferenceLoader.populates_types`): declarative, for
        provenance and discoverability. Defaults to empty; authors that
        want richer reporting override it.
        """
        return []

    # ------------------------------------------------------------------
    # MigratableData: multi-hop evolve (S3). provision is the inherited
    # genus no-op; deprovision is overridden below (refuse-by-default).
    # ------------------------------------------------------------------

    def evolve(
        self,
        client: "MosaicClient",
        from_version: str,
        to_version: str,
        params: BaseModel | None = None,
    ) -> LoadResult:
        """Migrate ``from_version`` → ``to_version`` along the declared DAG.

        Resolves an ordered path through the migration DAG
        (:meth:`_resolve_path` — fewest hops, so a declared shortcut edge
        wins) and runs each hop in turn. A single-hop upgrade is a
        one-edge path; a 3-hop upgrade composes its three intermediate
        steps in order. Each hop independently:

        1. Runs its transform → stages new-shape records + supersessions
           onto a fresh :class:`MigrationPlan` (no writes yet). The
           transform reads the *current live* records via ``client.query``
           — after the previous hop these are that hop's committed output,
           never the superseded predecessors (``query`` excludes
           unavailable rows), so hops never double-process.
        2. **Gate:** stages the new records and dry-run-validates them
           against the client's merged schema (:meth:`_run_gate`); on
           failure raises :class:`MigrationGateError` and commits nothing
           further. (Per-hop validation is against the client's *current*
           merged registry, which is correct for the common case where
           every intermediate shape is an additive subset of the target
           schema; validating each hop against its *own* historical merged
           schema is the S4 orchestrator's job, sec11 §11.5.2.)
        3. **Commit (green only):** retires the superseded predecessors
           (so a ``hippo_unique`` partial index never sees the old and new
           record live at once, PTS-348), writes the new records, then
           records the supersede edges via
           :meth:`MosaicClient.supersede_entity`. Each supersession is
           tagged ``actor=<module name>`` / ``reason=<from→to>`` so the
           provenance reads as a migration.

        Returns an aggregate :class:`LoadResult` summed across hops:
        ``created`` counts every new-shape record written along the path.
        A no-op (``from_version == to_version``) returns an empty result.

        Raises :class:`~mosaic.core.exceptions.MigrationFloorError` when
        the current version is below the migration floor, and
        :class:`MigrationStepNotFoundError` when the target is unreachable.
        """
        path = self._resolve_path(from_version, to_version)

        aggregate = LoadResult()
        for step in path:
            hop = self._run_hop(client, step, params)
            aggregate.created += hop.created
            aggregate.updated += hop.updated
            aggregate.unchanged += hop.unchanged
            aggregate.entities.extend(hop.entities)
        return aggregate

    # ------------------------------------------------------------------
    # MigratableData: deprovision — refuse-by-default on live data (S3).
    # ------------------------------------------------------------------

    def deprovision(
        self,
        client: "MosaicClient",
        version: str,
        *,
        force: bool = False,
    ) -> None:
        """Retire this module's domain data — refusing by default (§11.4.3).

        Unlike :class:`ReferenceLoader` (whose external source is
        reconstructible, so the orchestrator prunes its rows willingly),
        a ``DomainModule`` owns the deployment's authoritative,
        first-party records. Tearing them down on uninstall is a data-loss
        event, so this **refuses** when any live row exists in a
        :meth:`populates_types` class:

        * ``force=False`` (default) + live data ⇒ raise
          :class:`~mosaic.core.exceptions.DeprovisionRefusedError`
          (``reason="live_domain_data"``), naming the populated types.
          Export first, then re-run with ``force=True``.
        * ``force=True`` ⇒ soft-delete every live row in those classes
          (the standard ``is_available`` availability transition — no hard
          delete, sec3), leaving the provenance trail intact.
        * No live data ⇒ nothing to retire; returns quietly.

        The dependents guard (§11.4.4) is enforced ahead of this call by
        the CLI orchestrator, which holds the installed-package graph.
        """
        live = self._live_data_types(client)
        if live and not force:
            raise DeprovisionRefusedError(
                message=(
                    f"{self.name}: refusing to deprovision — it owns live "
                    f"domain data in {live!r}. Domain records are "
                    f"authoritative; export them first, then re-run with "
                    f"force=True to soft-delete."
                ),
                package=self.name,
                reason="live_domain_data",
                live_types=live,
            )
        if not live:
            return
        # force=True: soft-delete every live row via the availability
        # transition (no hard delete). The provenance log retains the
        # lineage so a re-provision/restore stays auditable.
        for entity_type in live:
            for item in client.query(
                entity_type, include_subclasses=False
            ).items:
                client.delete(entity_type, item["id"])

    def _live_data_types(self, client: "MosaicClient") -> list[str]:
        """Return the :meth:`populates_types` classes that hold live rows.

        A class is "live" when ``client.query`` (which excludes
        unavailable / superseded rows) returns at least one item. The
        result drives the refuse-by-default decision and names the
        offending types in the error.
        """
        live: list[str] = []
        for entity_type in self.populates_types():
            try:
                if client.query(entity_type, include_subclasses=False).items:
                    live.append(entity_type)
            except Exception:
                # A type the client's schema doesn't know (or an empty
                # store) is not live data; never block teardown on it.
                continue
        return live

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_path(
        self, from_version: str, to_version: str
    ) -> list[MigrationStep]:
        """Resolve the ordered list of steps from ``from_version`` to target.

        Reads :meth:`migration_steps` as a directed graph and BFS-searches
        for the **fewest-hop** path, so a declared shortcut edge (a direct
        ``from → to``) is preferred over the longer chain automatically.
        Ties between equal-length paths break on declaration order
        (deterministic). Returns ``[]`` for a no-op
        (``from_version == to_version``).

        Fails loud (sec11 §11.3.2 — no silent fallback):

        * a duplicate ``(from, to)`` edge ⇒ :class:`MigrationStepNotFoundError`
          (a hop must be unique);
        * ``from_version`` not a node in the DAG ⇒
          :class:`~mosaic.core.exceptions.MigrationFloorError`
          (below the floor — upgrade to ≥<floor> first);
        * target unreachable from a known node ⇒
          :class:`MigrationStepNotFoundError`.
        """
        steps = self.migration_steps()
        available = [(s.from_version, s.to_version) for s in steps]

        # A duplicate (from, to) edge is an authoring error — the hop is
        # ambiguous. Reject it loudly rather than silently picking one.
        seen_edges: set[tuple[str, str]] = set()
        for s in steps:
            edge = (s.from_version, s.to_version)
            if edge in seen_edges:
                raise MigrationStepNotFoundError(
                    message=(
                        f"{self.name}: duplicate migration step declared for "
                        f"{s.from_version}→{s.to_version}; a hop must be unique."
                    ),
                    package=self.name,
                    from_version=from_version,
                    to_version=to_version,
                    available_steps=available,
                )
            seen_edges.add(edge)

        if from_version == to_version:
            return []

        adjacency: dict[str, list[MigrationStep]] = {}
        nodes: set[str] = set()
        targets: set[str] = set()
        for s in steps:
            adjacency.setdefault(s.from_version, []).append(s)
            nodes.add(s.from_version)
            nodes.add(s.to_version)
            targets.add(s.to_version)

        # Below the floor: the current version predates every shipped
        # step (it is not a node anywhere in the DAG). The floor is the
        # DAG's entry node(s) — versions that are never a step's target.
        if from_version not in nodes:
            roots = sorted(nodes - targets)
            floor = roots[0] if len(roots) == 1 else None
            floor_hint = (
                f"upgrade to ≥{floor} first" if floor
                else f"upgrade to one of {roots} first"
            )
            raise MigrationFloorError(
                message=(
                    f"{self.name}: current version {from_version!r} is below "
                    f"the migration floor; the package ships no step from it. "
                    f"{floor_hint} via package {self.name!r}."
                ),
                package=self.name,
                from_version=from_version,
                to_version=to_version,
                floor=floor,
            )

        # BFS for the fewest-hop path (shortcut edges win for free).
        queue: deque[str] = deque([from_version])
        came_from: dict[str, MigrationStep] = {}
        visited: set[str] = {from_version}
        while queue:
            current = queue.popleft()
            if current == to_version:
                break
            for step in adjacency.get(current, []):
                nxt = step.to_version
                if nxt not in visited:
                    visited.add(nxt)
                    came_from[nxt] = step
                    queue.append(nxt)

        if to_version not in came_from and to_version != from_version:
            raise MigrationStepNotFoundError(
                message=(
                    f"{self.name}: no migration path from {from_version} to "
                    f"{to_version}. The target is unreachable through the "
                    f"declared migration DAG."
                ),
                package=self.name,
                from_version=from_version,
                to_version=to_version,
                available_steps=available,
            )

        # Walk the predecessor chain back to the source, then reverse.
        path: list[MigrationStep] = []
        cursor = to_version
        while cursor != from_version:
            step = came_from[cursor]
            path.append(step)
            cursor = step.from_version
        path.reverse()
        return path

    def _run_hop(
        self,
        client: "MosaicClient",
        step: MigrationStep,
        params: BaseModel | None,
    ) -> LoadResult:
        """Stage → gate → commit a single migration hop (one DAG edge).

        Runs ``step.transform`` against a fresh :class:`MigrationPlan`,
        passes it through the hard validation gate, and — only on green —
        commits the new records then the supersessions. The same routine
        S2 used for the single declared step; S3 calls it once per edge of
        the resolved path.
        """
        plan = MigrationPlan()
        context = MigrationContext(
            client=client,
            from_version=step.from_version,
            to_version=step.to_version,
            plan=plan,
            params=params,
        )
        step.transform(context)

        # Hard validation gate — BEFORE any committed write.
        self._run_gate(client, step.from_version, step.to_version, plan)

        # Commit (green only). A hippo_unique slot is enforced by a *partial*
        # UNIQUE INDEX (``... WHERE is_available = 1``), which rejects two live
        # rows that share a business key. A 1:1 migration carries the key
        # forward, so the new record and its predecessor would momentarily both
        # be live and collide on INSERT (PTS-348). So retire each predecessor
        # *before* writing the new records, freeing its key; supersede_entity
        # then records the lineage edge (it reads the source via ``read_any``
        # and is idempotent on ``is_available``, so the already-offline
        # predecessor still resolves). The retire emits an ``availability_change``
        # event ahead of the ``supersede`` event — accepted to keep the change
        # off the core supersede contract.
        result = LoadResult()
        default_reason = (
            f"{self.name} migration {step.from_version}→{step.to_version}"
        )
        # 1. Take superseded predecessors offline so their business key frees up.
        for old_id in dict.fromkeys(sup.old_id for sup in plan.supersessions):
            old_type = client.resolve_type(old_id)
            if old_type is None:
                continue
            client.set_availability_bulk(
                old_type,
                [old_id],
                is_available=False,
                reason=default_reason,
                actor=self.name,
            )
        # 2. Write the new (live) records — no live-key collision now.
        for write in plan.writes:
            rec = client.put(
                write.entity_type, write.data, entity_id=write.data.get("id")
            )
            result.created += 1
            result.entities.append(
                EntityRef(
                    id=rec.get("id") or write.data["id"],
                    type=write.entity_type,
                )
            )
        # 3. Record the supersede edges + provenance (actor/reason = migration).
        for sup in plan.supersessions:
            client.supersede_entity(
                sup.old_id,
                sup.new_id,
                reason=sup.reason or default_reason,
                actor=self.name,
            )
        return result

    def _run_gate(
        self,
        client: "MosaicClient",
        from_version: str,
        to_version: str,
        plan: MigrationPlan,
    ) -> None:
        """Stage the plan's new records and dry-run-validate the merged schema.

        Mirrors ``mosaic ingest --validate-schema <merged-dir> --dry-run``
        in-process: builds a tree-root instance bundle from the staged
        writes, serializes it to a staging file (so the operation is
        literally *stage output → dry-run*), parses it back, and validates
        against the client's merged registry. Raises
        :class:`MigrationGateError` on any validation error — caller
        commits nothing.

        A plan with no staged writes validates trivially (an empty bundle
        is a no-op migration).
        """
        registry = client.registry
        if registry is None:
            raise MigrationGateError(
                message=(
                    f"{self.name}: cannot run the migration validation gate — "
                    f"the client has no merged schema registry. A domain "
                    f"migration must run against a schema-backed client."
                ),
                package=self.name,
                from_version=from_version,
                to_version=to_version,
            )

        bundle = self._build_bundle(registry, from_version, to_version, plan)

        with tempfile.TemporaryDirectory(prefix="mosaic-migrate-") as tmp:
            staged = Path(tmp) / "staged.yaml"
            staged.write_text(yaml.safe_dump(bundle), encoding="utf-8")
            parsed = yaml.safe_load(staged.read_text(encoding="utf-8")) or {}

        errors = registry.validate(parsed, registry.tree_root_class_name())
        if errors:
            raise MigrationGateError(
                message=(
                    f"{self.name}: staged {from_version}→{to_version} "
                    f"migration failed the schema gate "
                    f"({len(errors)} error(s)); nothing was written."
                ),
                package=self.name,
                from_version=from_version,
                to_version=to_version,
                errors=list(errors),
            )

    def _build_bundle(
        self,
        registry: "SchemaRegistry",
        from_version: str,
        to_version: str,
        plan: MigrationPlan,
    ) -> dict:
        """Group staged writes into a tree-root instance bundle by accessor slot.

        The merged registry maps each concrete class to its tree-root
        accessor slot (``Sample`` → ``samples``). A staged write whose
        ``entity_type`` is not a class in the merged schema is itself a
        gate failure (the new shape names a class the schema does not
        define), surfaced as :class:`MigrationGateError`.
        """
        accessor_by_class = {
            slot.range: slot.name for slot in registry.tree_root_slots()
        }
        bundle: dict[str, list[dict]] = {}
        for write in plan.writes:
            accessor = accessor_by_class.get(write.entity_type)
            if accessor is None:
                raise MigrationGateError(
                    message=(
                        f"{self.name}: staged record class {write.entity_type!r} "
                        f"has no tree-root accessor in the merged schema; the "
                        f"new shape references an undefined class."
                    ),
                    package=self.name,
                    from_version=from_version,
                    to_version=to_version,
                    errors=[f"unknown class {write.entity_type!r}"],
                )
            bundle.setdefault(accessor, []).append(write.data)
        return bundle
