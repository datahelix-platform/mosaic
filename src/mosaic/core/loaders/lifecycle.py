"""Reference-loader install/upgrade/deprovision lifecycle orchestration
(Mosaic design §2.14).

Core (SDK-layer) business logic for the loader-driven install / upgrade /
deprovision / bundle-migrate lifecycle. Moved out of the CLI layer
(``mosaic.cli.commands.reference``) per issue #69 — a follow-up to #67 /
PR #68, which relocated schema-package *discovery* and ``requires:``
resolution to :mod:`mosaic.core.loaders.discovery`. This module carries no
CLI dependency (no ``typer``/``argparse``); the ``mosaic reference`` CLI
verbs are thin wrappers over the entry points here.

The flow is:

1. Resolve a registered :class:`ReferenceLoader` from the
   ``mosaic.reference_loaders`` entry-point group (PTS-224).
2. Merge the loader's :meth:`schema_fragment` into the deployed schema
   via :func:`mosaic.linkml_bridge.merge_loader_fragment` (PTS-226).
3. Apply the resulting schema additively against the DB (the same
   migration pipeline ``mosaic schema migrate`` drives).
4. Hand a :class:`MosaicClient` to ``loader.load()`` / ``loader.upgrade()``
   inside a :meth:`MosaicClient.load_context` so every ``client.put()``
   appends a row to ``reference_write_log`` (sec2 §2.14.9, D2.14.J).
5. Record ``{loader_name: version}`` in ``mosaic_meta.reference_versions``
   so ``requires:`` and "already installed" checks have a fixed source
   of truth. The v1 ``mosaic_meta.reference_entity_ids`` JSON blob is
   gone — ``--prune-old`` queries the write log instead.

Decisions D2.14.F (additive upgrade, opt-in ``--prune-old``),
D2.14.I (``"test"`` reserved slug), and D2.14.J (write-log-driven
prune) are enforced here.

The CLI module ``mosaic.cli.commands.reference`` re-exports the public
(and several private, test-monkeypatched) names here for backwards
compatibility, so existing imports of
``mosaic.cli.commands.reference.install_reference`` (etc.) keep working.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from mosaic.core.exceptions import DeprovisionRefusedError
from mosaic.core.loaders.discovery import (
    ReferenceLoaderNotFoundError,
    _build_fragment_spec,
    discover_schema_packages,
    find_loader,
)
from mosaic.core.loaders.reference import (
    EntityRef,
    ExternalData,
    LoadResult,
    SchemaPackage,
)

if TYPE_CHECKING:
    from mosaic.core.client import MosaicClient
    from mosaic.linkml_bridge import SchemaRegistry

# Reserved slug per D2.14.I. The constant lives here so internal
# tooling that synthesises version strings can refuse to emit it.
RESERVED_TEST_SLUG = "test"

# mosaic_meta key owned by the reference-loader lifecycle. The v1
# ``reference_entity_ids`` JSON blob was retired in v2 (D2.14.J); the
# substrate for ``--prune-old`` is the ``reference_write_log`` table.
META_KEY_VERSIONS = "reference_versions"


# ---------------------------------------------------------------------------
# Install / upgrade lifecycle.
# ---------------------------------------------------------------------------


def install_reference(
    name: str,
    version: str | None,
    *,
    db_path: str | Path,
    schema_dir: str | Path | None = None,
    params: "BaseModel | None" = None,
) -> dict[str, Any]:
    """Run the install lifecycle for ``name`` at ``version``.

    Mirrors §2.14 *Install lifecycle*. ``schema_dir`` is the deployed
    user-schema directory (defaults to ``schemas/``); when absent the
    bundled ``mosaic_core`` schema is used as the base. ``params`` is
    forwarded verbatim to :meth:`ReferenceLoader.load`.

    Re-installing the same version under the same loader is a no-op —
    the return dict reports ``status='already_installed'`` and exits
    without re-running the loader.
    """
    info = find_loader(name)
    loader: SchemaPackage = info["instance"]
    resolved_version = _resolve_version(loader, version)

    if resolved_version == RESERVED_TEST_SLUG:
        # ``"test"`` is supported when the loader exposes it; otherwise
        # we surface a clear "loader does not implement test fixture"
        # error rather than a confusing "unknown version" from load().
        if RESERVED_TEST_SLUG not in loader.versions():
            raise ValueError(
                f"Loader {name!r} does not expose a {RESERVED_TEST_SLUG!r} "
                f"fixture; pass an explicit --version."
            )

    db = Path(db_path)
    existing = _read_versions(db)
    if existing.get(name) == resolved_version:
        return {
            "name": name,
            "version": resolved_version,
            "status": "already_installed",
        }

    deployed_registry = _load_deployed_registry(schema_dir)
    spec = _build_fragment_spec(info, loader)
    merged_registry = _merge_fragment_into(deployed_registry, spec)

    # SQLiteAdapter.__init__ runs `_init_per_class_tables` and skips
    # already-present tables, so wiring the adapter with the merged
    # registry is sufficient to create the loader's new tables additively.
    # This mirrors the non-interactive half of `mosaic schema migrate`
    # (spec §2.14 install step 3) without the prompt branch.
    client = _build_client(merged_registry, db)
    with client.load_context(loader_name=name, version=resolved_version):
        # Genus lifecycle hook (Doc 2 §2A): ReferenceLoader.provision maps
        # to load(); a pure-schema SchemaPackage inherits the no-op (the
        # fragment is already merged above) and returns None.
        load_result = loader.provision(client, resolved_version, params)
    if load_result is None:
        load_result = LoadResult()
    _abort_on_load_errors(name, resolved_version, load_result)

    _write_versions(db, name, resolved_version)

    return {
        "name": name,
        "version": resolved_version,
        "status": "installed",
        "created": load_result.created,
        "entities": list(load_result.entities),
    }


def upgrade_reference(
    name: str,
    to_version: str | None,
    *,
    db_path: str | Path,
    schema_dir: str | Path | None = None,
    params: "BaseModel | None" = None,
    prune_old: bool = False,
) -> dict[str, Any]:
    """Run the upgrade lifecycle for ``name`` from the recorded version
    to ``to_version``.

    Additive by default (D2.14.F): old-version entities remain in place.
    Pass ``prune_old=True`` to delete the prior version's rows *after*
    the new install succeeds. If the loader raises during ``upgrade``,
    the prior rows are guaranteed to stay intact (the new-version row
    deletion is gated behind a clean LoadResult).
    """
    info = find_loader(name)
    loader: SchemaPackage = info["instance"]
    resolved_to = _resolve_version(loader, to_version)

    db = Path(db_path)
    existing = _read_versions(db)
    from_version = existing.get(name)
    if from_version is None:
        raise ValueError(
            f"Loader {name!r} is not installed; run "
            f"`mosaic reference install {name} --version {resolved_to}` first."
        )
    if from_version == resolved_to:
        return {
            "name": name,
            "from_version": from_version,
            "to_version": resolved_to,
            "status": "already_at_version",
        }

    deployed_registry = _load_deployed_registry(schema_dir)
    spec = _build_fragment_spec(info, loader)
    merged_registry = _merge_fragment_into(deployed_registry, spec)

    client = _build_client(merged_registry, db)
    with client.load_context(loader_name=name, version=resolved_to):
        # Genus lifecycle hook (Doc 2 §2A): ReferenceLoader.evolve maps to
        # upgrade() (re-ingest / diff); a pure-schema SchemaPackage
        # inherits the no-op and returns None.
        load_result = loader.evolve(client, from_version, resolved_to, params)
    if load_result is None:
        load_result = LoadResult()
    _abort_on_load_errors(name, resolved_to, load_result)

    pruned: list[EntityRef] = []
    if prune_old:
        pruned = _prune_via_write_log(db, name, from_version)

    _write_versions(db, name, resolved_to)

    return {
        "name": name,
        "from_version": from_version,
        "to_version": resolved_to,
        "status": "upgraded",
        "created": load_result.created,
        "entities": list(load_result.entities),
        "pruned": pruned,
    }


def _build_merged_registry(schema_dir, installed_infos: list[dict[str, Any]]):
    """Fold every installed package's fragment into the deployed registry.

    The S4 orchestrator validates the post-migration state against the
    *fully merged* schema — user schema + every installed package's
    fragment, **including lab extensions** (sec11 §11.5.2 / §6.3). Unlike
    install/upgrade (which merge one fragment for one operation), the
    orchestrator needs the whole merged closure, so this folds all of them.
    """
    registry = _load_deployed_registry(schema_dir)
    for info in installed_infos:
        spec = _build_fragment_spec(info, info["instance"])
        registry = _merge_fragment_into(registry, spec)
    return registry


def migrate_bundle(
    bundle_source: str | Path | dict[str, Any],
    *,
    db_path: str | Path,
    schema_dir: str | Path | None = None,
    params_by_pkg: dict[str, "BaseModel"] | None = None,
) -> dict[str, Any]:
    """Migrate a multi-package deployment to a target bundle (sec11 §11.5).

    The *one command* of the S4 acceptance criterion: resolves the
    ``depends_on`` graph across all installed packages, drives every pinned
    package through the bundle's coordinate sequence in base→dependent order
    inside a single staged commit-or-rollback scope, and validates the full
    post-migration state (incl. lab extensions) before the one commit. On a
    gate failure the whole chain rolls back and version pointers are left
    untouched.

    ``bundle_source`` is a manifest path (or parsed dict). Returns a result
    dict with the per-package transitions and the committed target versions.
    """
    from mosaic.core.loaders.bundle import Bundle
    from mosaic.core.loaders.orchestrator import migrate_to_bundle

    db = Path(db_path)
    bundle = Bundle.from_manifest(bundle_source)

    # Current installed versions (read on a separate connection BEFORE the
    # staged write-lock opens — must not run inside the scope).
    current = _read_versions(db)

    discovered = {info["name"]: info for info in discover_schema_packages()}
    installed_infos = [discovered[n] for n in current if n in discovered]
    packages = [info["instance"] for info in installed_infos]

    # A pinned target must be a discoverable, installed package — otherwise
    # its evolve would be silently skipped. Fail loud instead.
    unknown = [
        n for n in bundle.packages if n not in {info["name"] for info in installed_infos}
    ]
    if unknown:
        raise ValueError(
            f"bundle {bundle.name!r} pins package(s) {unknown!r} that are not "
            f"installed/discoverable; install them before migrating."
        )

    registry = _build_merged_registry(schema_dir, installed_infos)
    client = _build_client(registry, db)

    result = migrate_to_bundle(
        client, packages, bundle, current, params_by_pkg=params_by_pkg
    )

    # Persist version pointers only after the staged data commit returned
    # clean (the data migration is the atomic unit; mirrors the existing
    # evolve-then-record pattern). On a gate failure migrate_to_bundle
    # raised and we never reach here, so pointers stay at the old versions.
    for pkg_name, version in result.target_versions.items():
        _write_versions(db, pkg_name, version)

    return {
        "bundle": result.bundle,
        "committed": result.committed,
        "migrations": [
            {
                "package": m.package,
                "from_version": m.from_version,
                "to_version": m.to_version,
                "created": m.created,
            }
            for m in result.migrations
        ],
        "target_versions": result.target_versions,
    }


def compute_exposure(
    old_schema: str | Path,
    new_schema: str | Path,
    extension_name: str,
    *,
    extension_fragment: dict[str, Any] | None = None,
):
    """Exposure report for a proposed base migration vs. an installed extension.

    The *pre-migration warning* half of S4 (sec11 §11.6.1): intersect the
    structural write-set of a base migration (``old_schema`` → ``new_schema``,
    two merged-schema YAML files) with the elements an installed extension
    references. Empty ⇒ the base migration is safe to apply without an
    extension step; non-empty ⇒ the lab must supply a complementary step (or
    the end-to-end gate will block it at migration time).

    The extension's fragment defaults to the installed package's
    :meth:`SchemaPackage.schema_fragment`; pass ``extension_fragment`` to
    report against a fragment that is not (yet) installed.
    """
    import yaml as _yaml

    from mosaic.core.loaders.exposure import compute_write_set, exposure_report

    old = _yaml.safe_load(Path(old_schema).read_text(encoding="utf-8")) or {}
    new = _yaml.safe_load(Path(new_schema).read_text(encoding="utf-8")) or {}
    if extension_fragment is None:
        info = find_loader(extension_name)
        extension_fragment = info["instance"].schema_fragment()
    write_set = compute_write_set(old, new)
    return exposure_report(
        write_set, extension_fragment, extension_name=extension_name
    )


def deprovision_reference(
    name: str,
    *,
    db_path: str | Path,
    schema_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Tear down an installed schema package (sec11 §11.4).

    The dependency-ordered teardown:

    1. **Dependents guard (§11.4.4, all species).** Refuse if any *other
       installed* package declares ``name`` in its ``depends_on()`` — the
       error names them. This precedes everything else so a guarded
       package is never partially torn down.
    2. **Species data-retirement hook.** Call
       ``loader.deprovision(client, version, force=force)``:

       * ``DomainModule`` refuses by default when it owns live domain data
         (raises :class:`DeprovisionRefusedError` unless ``force=True``,
         then soft-deletes — §11.4.3);
       * ``ReferenceLoader`` and pure-schema packages inherit the genus
         no-op (a ``ReferenceLoader`` has no write-log handle, so the
         orchestrator prunes its rows — step 3).
    3. **Prune (``ExternalData`` only).** Hard-delete every
       ``reference_write_log``-tracked row for ``name`` across all
       versions. Safe because the source is external and reconstructible
       (D2.14.J substrate; see the wording note below).
    4. **Remove the version record** from ``mosaic_meta.reference_versions``
       so the package reads as uninstalled.

    Note on §11.4.2 wording: the spec says ``ReferenceLoader`` deprovision
    *soft-deletes*, but the authoritative ``--prune-old`` / write-log
    substrate (D2.14.J) **hard-deletes**. This orchestrator follows the
    implemented hard-delete for consistency, mirroring ``--prune-old``;
    the ``DomainModule`` path soft-deletes (its data is authoritative, not
    reconstructible). DDL table teardown is out of scope (no destructive
    schema ops in v1; that is the S4 orchestrator's concern).

    Raises ``ValueError`` when ``name`` is not installed.
    """
    info = find_loader(name)
    loader: SchemaPackage = info["instance"]

    db = Path(db_path)
    existing = _read_versions(db)
    version = existing.get(name)
    if version is None:
        raise ValueError(
            f"Loader {name!r} is not installed; nothing to deprovision."
        )

    # 1) Dependents guard — refuse if any installed package depends on this.
    dependents = _find_installed_dependents(name, db)
    if dependents:
        raise DeprovisionRefusedError(
            message=(
                f"Cannot deprovision {name!r}: installed package(s) "
                f"{dependents} depend on it. Deprovision the dependent(s) "
                f"first."
            ),
            package=name,
            reason="has_dependents",
            dependents=dependents,
        )

    deployed_registry = _load_deployed_registry(schema_dir)
    spec = _build_fragment_spec(info, loader)
    merged_registry = _merge_fragment_into(deployed_registry, spec)
    client = _build_client(merged_registry, db)

    # 2) Species hook retires its own data. DomainModule raises
    # DeprovisionRefusedError on live data unless force=True; the genus /
    # ReferenceLoader no-op leaves the prune (step 3) to the orchestrator.
    loader.deprovision(client, version, force=force)

    # 3) Prune external-data rows off the write log (the loader has no
    # write-log handle, so this can't live on the species hook).
    pruned: list[EntityRef] = []
    if isinstance(loader, ExternalData):
        pruned = _prune_all_via_write_log(db, name)

    # 4) Drop the installed-version record so the package reads as gone.
    _remove_version(db, name)

    return {
        "name": name,
        "version": version,
        "status": "deprovisioned",
        "pruned": pruned,
        "forced": force,
    }


def _find_installed_dependents(name: str, db_path: Path) -> list[str]:
    """Return the names of *installed* packages that ``depends_on`` ``name``.

    Cross-references ``mosaic_meta.reference_versions`` (the installed set,
    §11.4.4) against each installed package's declared ``depends_on()``
    (a ``list[str]`` of package names — schema_package.py is authoritative
    over the §11.2.2 table). Installed-but-no-longer-discoverable packages
    are skipped rather than crashing the guard.
    """
    installed = _read_versions(db_path)
    dependents: list[str] = []
    for other in installed:
        if other == name:
            continue
        try:
            instance = find_loader(other)["instance"]
        except ReferenceLoaderNotFoundError:
            continue
        if name in instance.depends_on():
            dependents.append(other)
    return sorted(dependents)


def _prune_all_via_write_log(db_path: Path, name: str) -> list[EntityRef]:
    """Hard-delete every ``reference_write_log`` row for ``name`` (all versions).

    The teardown counterpart of :func:`_prune_via_write_log`, which is
    scoped to a single ``from_version`` for ``--prune-old``. Deprovision
    removes the whole package, so it prunes across every version the
    loader ever wrote. Entity rows are deleted per ``entity_type`` group
    and the matching log rows are removed in the same transaction.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        if not _has_write_log_table(cursor):
            return []
        cursor.execute(
            "SELECT entity_id, entity_type FROM reference_write_log "
            "WHERE loader_name = ?",
            (name,),
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        deleted: list[EntityRef] = []
        by_type: dict[str, list[str]] = {}
        for entity_id, entity_type in rows:
            by_type.setdefault(entity_type, []).append(entity_id)
            deleted.append(EntityRef(id=entity_id, type=entity_type))

        try:
            cursor.execute("BEGIN")
            for entity_type, ids in by_type.items():
                placeholders = ",".join("?" * len(ids))
                cursor.execute(
                    f'DELETE FROM "{entity_type}" WHERE id IN ({placeholders})',
                    ids,
                )
            cursor.execute(
                "DELETE FROM reference_write_log WHERE loader_name = ?",
                (name,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return deleted
    finally:
        conn.close()


def _remove_version(db_path: Path, name: str) -> None:
    """Drop ``name`` from ``mosaic_meta.reference_versions`` (uninstall mark).

    The inverse of :func:`_write_versions`. A no-op when the key is
    already absent so the teardown stays idempotent.
    """
    from mosaic.core.meta import get_meta, set_meta

    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        if not _has_meta_table(conn):
            return
        versions = get_meta(conn, META_KEY_VERSIONS) or {}
        if name in versions:
            del versions[name]
            set_meta(conn, META_KEY_VERSIONS, versions)
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Listing (read-only).
# ---------------------------------------------------------------------------


def list_reference_loaders(
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List every discoverable reference loader, annotated with the
    installed version recorded in ``mosaic_meta`` (if any).
    """
    from mosaic.core.loaders.discovery import discover_reference_loaders

    discovered = discover_reference_loaders()
    installed_versions: dict[str, str] = {}
    if db_path is not None and Path(db_path).exists():
        installed_versions = _read_versions(Path(db_path))

    result: list[dict[str, Any]] = []
    for loader in discovered:
        installed_version = installed_versions.get(loader["name"])
        result.append(
            {
                "name": loader["name"],
                "description": loader["description"],
                "package": loader["package_name"],
                "package_version": loader["package_version"],
                "installed_version": installed_version,
                "installed": installed_version is not None,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _resolve_version(loader: SchemaPackage, requested: str | None) -> str:
    if requested is not None:
        return requested
    available = loader.versions()
    if not available:
        raise ValueError(
            f"Loader {loader.name!r} reports no versions; cannot pick a default."
        )
    # Pick the last non-reserved slug so callers without --version don't
    # accidentally install the test fixture.
    for slug in reversed(available):
        if slug != RESERVED_TEST_SLUG:
            return slug
    return available[-1]


def _load_deployed_registry(
    schema_dir: str | Path | None,
) -> "SchemaRegistry":
    from mosaic.linkml_bridge import SchemaRegistry
    from linkml_runtime.utils.schemaview import SchemaView
    import importlib.resources

    if schema_dir is not None:
        path = Path(schema_dir)
        if path.is_dir():
            yamls = list(path.glob("*.yaml")) + list(path.glob("*.yml"))
            if yamls:
                return SchemaRegistry.from_path(path)
        elif path.exists():
            return SchemaRegistry.from_path(path)
    mosaic_core_path = importlib.resources.files("mosaic.schemas").joinpath(
        "mosaic_core.yaml"
    )
    schema_view = SchemaView(str(mosaic_core_path))
    return SchemaRegistry(schema_view)


def _merge_fragment_into(
    deployed_registry: "SchemaRegistry", spec
) -> "SchemaRegistry":
    from mosaic.linkml_bridge import SchemaRegistry, merge_loader_fragment

    merged_sv = merge_loader_fragment(deployed_registry._sv, spec)
    return SchemaRegistry(merged_sv)


def _build_client(
    registry: "SchemaRegistry", db_path: Path
) -> "MosaicClient":
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

    db_path.parent.mkdir(parents=True, exist_ok=True)
    storage = SQLiteAdapter(str(db_path), schema_registry=registry)
    return MosaicClient(storage=storage, registry=registry)


def _abort_on_load_errors(
    name: str, version: str, load_result: LoadResult
) -> None:
    if load_result.errors:
        messages = "; ".join(load_result.error_messages) or "no detail"
        raise ValueError(
            f"Loader {name!r} reported {load_result.errors} error(s) while "
            f"loading version {version!r}: {messages}"
        )


def _read_versions(db_path: Path) -> dict[str, str]:
    from mosaic.core.meta import get_meta

    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        if not _has_meta_table(conn):
            return {}
        return get_meta(conn, META_KEY_VERSIONS) or {}
    finally:
        conn.close()


def _write_versions(db_path: Path, name: str, version: str) -> None:
    """Record ``{loader_name → current_version}`` in
    ``mosaic_meta.reference_versions``.

    Drives ``requires:`` resolution and "already installed" checks.
    The per-version entity-ID JSON blob (``reference_entity_ids``) is no
    longer written — ``--prune-old`` reads ``reference_write_log``
    instead (D2.14.J).
    """
    from mosaic.core.meta import get_meta, set_meta

    conn = sqlite3.connect(str(db_path))
    try:
        versions = get_meta(conn, META_KEY_VERSIONS) or {}
        versions[name] = version
        set_meta(conn, META_KEY_VERSIONS, versions)
        conn.commit()
    finally:
        conn.close()


def _prune_via_write_log(
    db_path: Path, name: str, from_version: str
) -> list[EntityRef]:
    """Hard-delete the prior version's entity rows and write-log rows.

    Driven by the ``reference_write_log`` substrate (sec2 §2.14.9 /
    D2.14.J). Entity tables are deleted by ``id`` per ``entity_type``
    group; the matching log rows for ``(name, from_version)`` are
    removed in the same transaction. The returned list mirrors the
    deleted log rows so callers can surface a per-type breakdown.

    Stable-id upgrade overlap (sec2 §2.14.9): when the new version
    re-wrote the same ``entity_id`` under ``to_version``, prune of
    ``from_version`` removes the entity row even though the new version
    still references it. Loaders that need overlap survival must
    override ``upgrade()`` to skip unchanged writes; this code path
    makes no attempt to spare them.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT entity_id, entity_type FROM reference_write_log "
            "WHERE loader_name = ? AND version = ?",
            (name, from_version),
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        deleted: list[EntityRef] = []
        by_type: dict[str, list[str]] = {}
        for entity_id, entity_type in rows:
            by_type.setdefault(entity_type, []).append(entity_id)
            deleted.append(EntityRef(id=entity_id, type=entity_type))

        try:
            cursor.execute("BEGIN")
            for entity_type, ids in by_type.items():
                placeholders = ",".join("?" * len(ids))
                cursor.execute(
                    f'DELETE FROM "{entity_type}" WHERE id IN ({placeholders})',
                    ids,
                )
            cursor.execute(
                "DELETE FROM reference_write_log "
                "WHERE loader_name = ? AND version = ?",
                (name, from_version),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return deleted
    finally:
        conn.close()


def _has_meta_table(conn: sqlite3.Connection) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mosaic_meta'"
    )
    return cursor.fetchone() is not None


def _has_write_log_table(cursor: sqlite3.Cursor) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='reference_write_log'"
    )
    return cursor.fetchone() is not None
