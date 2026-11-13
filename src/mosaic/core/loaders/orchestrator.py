"""Dependency-ordered lifecycle orchestrator + staged commit-or-rollback.

sec11 §11.5; Doc 2 §2A / §5.4 / §6 / §7. This is the unified lifecycle
orchestrator that subsumes the per-loader, *unordered* ``mosaic reference
upgrade`` path. It:

1. Resolves the ``depends_on`` graph across the participating packages once
   and topologically sorts it (base before dependents — :func:`topological_sort`).
2. Drives the deployment through the target :class:`~mosaic.core.loaders.bundle.Bundle`'s
   coordinate sequence; at **each** hop it dispatches ``evolve`` per package
   in dependency order, so an extension's step always runs after its base
   has reached that hop's shape (sec11 §11.5.1).
3. Wraps the whole chain in a single staged commit-or-rollback scope
   (:meth:`MosaicClient.staged_transaction`). After all hops it runs the
   **end-to-end gate** — validating the full post-migration state, including
   the lab's existing extension records, against the merged schema
   (:func:`run_end_to_end_gate`, sec11 §11.5.2 / §6.3). One commit on green;
   roll everything back on any failure.

The end-to-end gate is the backstop that *blocks* a stranded extension
field at migration time; the exposure report
(:mod:`mosaic.core.loaders.exposure`) *warns* about the same footprint
before the migration runs.

`[VERIFY]` §8 (sec11 §11.8.2) — resolved: no runtime path orders loaders by
``loader_depends_on`` (it is warning-only in ``linkml_bridge``). This
orchestrator is therefore the explicit base→dependent sequencer; it does
not rely on any implicit loader ordering.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import yaml

from mosaic.core.exceptions import MigrationGateError, OrchestrationError

if TYPE_CHECKING:
    from mosaic.core.client import MosaicClient
    from mosaic.core.loaders.bundle import Bundle
    from mosaic.core.loaders.schema_package import SchemaPackage


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


def topological_sort(
    packages: Sequence["SchemaPackage"],
) -> list["SchemaPackage"]:
    """Order ``packages`` base → dependents by their ``depends_on`` graph.

    Kahn's algorithm with an alphabetical tie-break, so the order is
    deterministic. ``depends_on`` edges pointing outside the given set are
    ignored for ordering (those packages are not part of this operation),
    matching the orchestrator's "participating packages" scope.

    Raises :class:`~mosaic.core.exceptions.OrchestrationError` if the graph
    contains a cycle (:attr:`OrchestrationError.cycle` names the packages
    still entangled) — a structural failure surfaced before any step runs.
    """
    by_name = {p.name: p for p in packages}
    names = set(by_name)

    indeg: dict[str, int] = {n: 0 for n in names}
    dependents: dict[str, list[str]] = {n: [] for n in names}
    for pkg in packages:
        for dep in pkg.depends_on():
            if dep in names:  # in-set edge: dep must precede pkg
                indeg[pkg.name] += 1
                dependents[dep].append(pkg.name)

    ready = sorted(n for n in names if indeg[n] == 0)
    ordered: list[str] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for m in sorted(dependents[node]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()

    if len(ordered) != len(names):
        cycle = sorted(n for n in names if indeg[n] > 0)
        raise OrchestrationError(
            message=(
                "cannot resolve a dependency-ordered lifecycle: the "
                f"depends_on graph has a cycle among {cycle!r}"
            ),
            cycle=cycle,
        )
    return [by_name[n] for n in ordered]


def _populated_classes(packages: Sequence["SchemaPackage"]) -> list[str]:
    """Union of the data classes the packages populate (for the gate scope).

    Domain/extension classes only — pure-schema packages contribute none,
    and hippo_core system classes (provenance, process, …) are never in any
    package's ``populates_types``, so the end-to-end gate validates the
    deployment's *domain + extension* records, not system infrastructure.
    """
    classes: list[str] = []
    seen: set[str] = set()
    for pkg in packages:
        populated = getattr(pkg, "populates_types", None)
        if not callable(populated):
            continue
        for cls in populated():
            if cls not in seen:
                seen.add(cls)
                classes.append(cls)
    return classes


# ---------------------------------------------------------------------------
# End-to-end validation gate
# ---------------------------------------------------------------------------


def run_end_to_end_gate(
    client: "MosaicClient",
    packages: Sequence["SchemaPackage"],
    *,
    label: str = "end-to-end",
) -> None:
    """Validate the full post-migration state against the merged schema.

    Reads every live record of the participating packages' populated
    classes and validates the assembled tree-root bundle against the
    client's merged registry — which includes **all** installed lab
    extensions (sec11 §11.5.2 step 3 / §6.3).

    Run inside the staged scope, ``client.query`` sees both the staged
    (uncommitted) base-migration writes **and** the extension's existing
    rows. A *stranded extension field* — a record the base migration left
    referencing an element the merged schema no longer admits, because the
    lab supplied no complementary step — therefore surfaces here as a
    concrete record-level validation failure, raising
    :class:`~mosaic.core.exceptions.MigrationGateError` and rolling the whole
    chain back. This is the real backstop; the exposure report only warns.
    """
    registry = client.registry
    if registry is None:
        raise MigrationGateError(
            message=(
                f"{label} gate: client has no merged schema registry; a "
                f"bundle migration must run against a schema-backed client."
            ),
        )

    accessor_by_class = {slot.range: slot.name for slot in registry.tree_root_slots()}
    bundle: dict[str, list[dict]] = {}
    for cls in _populated_classes(packages):
        accessor = accessor_by_class.get(cls)
        if accessor is None:
            # The participating package claims a class the merged schema
            # does not define — itself a coherence failure of the target.
            raise MigrationGateError(
                message=(
                    f"{label} gate: populated class {cls!r} has no tree-root "
                    f"accessor in the merged schema (undefined class)."
                ),
                errors=[f"unknown class {cls!r}"],
            )
        records = []
        # Exactly this class's rows: each populated class is its own bundle
        # accessor, so a concrete parent must not re-list its subclasses'
        # rows (ADR-0014 Decision 1).
        for item in client.query(cls, include_subclasses=False).items:
            rec = dict(item.get("data") or {})
            rec["id"] = item.get("id")
            # query() only returns available rows, so is_available is True.
            rec.setdefault("is_available", True)
            records.append(rec)
        if records:
            bundle[accessor] = records

    with tempfile.TemporaryDirectory(prefix="mosaic-e2e-gate-") as tmp:
        staged = Path(tmp) / "merged_state.yaml"
        staged.write_text(yaml.safe_dump(bundle), encoding="utf-8")
        parsed = yaml.safe_load(staged.read_text(encoding="utf-8")) or {}

    errors = registry.validate(parsed, registry.tree_root_class_name())
    if errors:
        raise MigrationGateError(
            message=(
                f"{label} gate failed against the merged schema (incl. "
                f"extensions): {len(errors)} record-level error(s); the "
                f"whole bundle migration was rolled back."
            ),
            errors=list(errors),
        )


# ---------------------------------------------------------------------------
# Orchestration result + driver
# ---------------------------------------------------------------------------


@dataclass
class PackageMigration:
    """One package's transition recorded during a bundle migration."""

    package: str
    from_version: str | None
    to_version: str
    created: int = 0


@dataclass
class OrchestrationResult:
    """Outcome of a :func:`migrate_to_bundle` run.

    :attr:`target_versions` is the ``{package → version}`` the caller should
    persist to ``hippo_meta`` once the data has committed — the staged scope
    already committed the *data* atomically; the version pointer is written
    after a clean return (matching mosaic's existing evolve-then-record
    pattern; the data migration itself is the atomic unit).
    """

    bundle: str
    migrations: list[PackageMigration] = field(default_factory=list)
    committed: bool = False
    target_versions: dict[str, str] = field(default_factory=dict)


def migrate_to_bundle(
    client: "MosaicClient",
    packages: Sequence["SchemaPackage"],
    target: "Bundle",
    current_versions: dict[str, str],
    *,
    params_by_pkg: dict[str, object] | None = None,
    run_gate: bool = True,
) -> OrchestrationResult:
    """Migrate a multi-package deployment to ``target``, dependency-ordered.

    ``packages`` are the installed packages of the deployment (used both to
    topologically sort the ``depends_on`` graph and to scope the end-to-end
    gate). ``current_versions`` is the ``{name: version}`` read from
    ``hippo_meta`` **before** the staged scope opens (a separate read
    connection, so it must not run inside the scope's write lock).

    The whole chain runs inside one :meth:`MosaicClient.staged_transaction`:
    for each hop in the target's coordinate sequence, every package with a
    pinned target at that hop is evolved in dependency order; after the last
    hop the end-to-end gate runs; then the scope commits. Any failure — a
    per-hop gate, the end-to-end gate, or a bad transform — rolls the entire
    migration back, leaving the deployment exactly as it was.

    Raises :class:`~mosaic.core.exceptions.OrchestrationError` (bad graph /
    uninstalled target package) or :class:`MigrationGateError` (validation),
    in both cases having committed nothing.
    """
    params_by_pkg = params_by_pkg or {}
    ordered = topological_sort(packages)
    current = dict(current_versions)
    result = OrchestrationResult(bundle=target.name)

    # A package pinned in the target must be installed (have a current
    # version) — migrating implies an existing deployment. Installing a
    # brand-new package is the provision path, not this one.
    for name in target.packages:
        if name not in current:
            raise OrchestrationError(
                message=(
                    f"bundle {target.name!r} pins package {name!r}, which is "
                    f"not installed; install it before migrating to the bundle."
                ),
                missing=[name],
            )

    with client.staged_transaction():
        for coord in target.coordinate_sequence():
            for pkg in ordered:  # dependency order: base before dependents
                tgt = coord.get(pkg.name)
                if tgt is None:
                    continue
                cur = current.get(pkg.name)
                if cur == tgt:
                    continue
                load_result = pkg.evolve(
                    client, cur, tgt, params_by_pkg.get(pkg.name)
                )
                created = getattr(load_result, "created", 0) or 0
                result.migrations.append(
                    PackageMigration(
                        package=pkg.name,
                        from_version=cur,
                        to_version=tgt,
                        created=created,
                    )
                )
                current[pkg.name] = tgt

        if run_gate:
            run_end_to_end_gate(
                client, packages, label=f"bundle {target.name!r}"
            )

        result.committed = True
        result.target_versions = {
            name: current[name] for name in target.packages
        }

    return result
