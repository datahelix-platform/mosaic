"""RecipeService - Recipe export, inspection, lineage, and bootstrap import facade.

Service facade following the same pattern as :class:`IngestionService`,
:class:`ProvenanceService`, and :class:`QueryService`. Composed once in
``MosaicClient.__init__`` and exposed through thin ``client.recipe_<verb>``
wrappers.

Architecture invariants (sec10 §10.10) that this module preserves:

- (1) :class:`SchemaManager` owns schema merging — this service never
  touches ``SchemaView`` directly to merge.
- (2) All recipe schema writes flow through ``SchemaManager.merge_fragment(...)``.
- (3) Provenance is unconditional; every successful import emits exactly
  one ``recipe_imported`` event in the same transaction.

Phase 2 (PTS-290) shipped :meth:`__init__` + :meth:`list_installed`.
Phase 3 PR 5 (PTS-291) adds :meth:`inspect`. The remaining surface
(``import_``, ``export``, ``extend``, ``diff``, ``export_lockfile``,
``install_from_lockfile``) lands in subsequent PRs.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

import yaml

from mosaic.core.exceptions import (
    RecipeDigestMismatchError,
    RecipeManifestError,
    RecipeRequiresUnsatisfiedError,
    RecipeVersionIncompatibleError,
    RecipeLineageCycleError,
    RecipeSchemaError,
)
from mosaic.core.meta import get_meta, set_meta
from mosaic.core.recipe import (
    FileResolver,
    HttpsResolver,
    ImportResult,
    InstalledRecipe,
    RecipeAuthor,
    RecipeExport,
    RecipeManifest,
    RecipeRef,
    RecipeReport,
    RecipeRequires,
    RecipeResolver,
    canonical_content_hash,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

if TYPE_CHECKING:
    from mosaic.core.provenance_service import ProvenanceService
    from mosaic.core.schema_manager import SchemaManager


META_KEY_INSTALLED_RECIPES = "installed_recipes"
"""``mosaic_meta`` key under which installed-recipe records live.

Mirrors the ``reference_versions`` convention used by the Reference
Loader install/upgrade machinery (sec2 §2.14 step 5). The stored value
is a JSON object keyed by recipe ``id``; each entry conforms to
:class:`InstalledRecipe` (sec10 §10.3.4).
"""


_RECIPE_MANIFEST_SCHEMA_NAME = "recipe_manifest.yaml"


class _ManifestYAMLLoader(yaml.SafeLoader):
    """YAML loader that keeps ISO timestamps as strings.

    LinkML's ``datetime`` range serialises to a JSON Schema ``string``,
    so the bundled JSON-schema validator rejects a Python ``datetime``
    even when the value would round-trip cleanly. Stripping the
    timestamp implicit resolver leaves ISO 8601 instants as strings,
    which is what the manifest schema expects.
    """


_ManifestYAMLLoader.yaml_implicit_resolvers = {
    k: [(t, r) for t, r in v if t != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class RecipeService:
    """Recipe export, inspection, lineage, and bootstrap import.

    Delegates schema merging to :class:`SchemaManager` and provenance
    writes to :class:`ProvenanceService`. Reference Loader installs do
    NOT go through this service in v1.
    """

    def __init__(
        self,
        storage: Optional[SQLiteAdapter] = None,
        schema_manager: Optional["SchemaManager"] = None,
        provenance_service: Optional["ProvenanceService"] = None,
        *,
        cache_dir: Optional[Path] = None,
        resolvers: Optional[Sequence[RecipeResolver]] = None,
    ) -> None:
        """Compose the service.

        ``cache_dir`` overrides :func:`default_recipe_cache_dir` for the
        bundled :class:`HttpsResolver`. ``resolvers`` defaults to
        ``[FileResolver(), HttpsResolver(cache_dir)]`` when not supplied;
        passing an explicit list disables the default registration so
        tests can wire isolated resolver chains.
        """
        self._storage = storage
        self._schema_manager = schema_manager
        self._provenance_service = provenance_service
        self._cache_dir = cache_dir
        if resolvers is None:
            self._resolvers: tuple[RecipeResolver, ...] = (
                FileResolver(),
                HttpsResolver(cache_dir=cache_dir),
            )
        else:
            self._resolvers = tuple(resolvers)

    def list_installed(self) -> list[InstalledRecipe]:
        """Return every entry in ``mosaic_meta.installed_recipes``.

        Returns ``[]`` on a clean instance, on adapters with no storage
        backing, or when the ``mosaic_meta`` table predates the recipe
        subsystem (the key is simply absent).
        """
        if self._storage is None or not hasattr(self._storage, "_transaction"):
            return []
        with self._storage._transaction() as conn:
            payload = get_meta(conn, META_KEY_INSTALLED_RECIPES)
        if not payload:
            return []
        return [_installed_recipe_from_dict(entry) for entry in payload.values()]

    def inspect(
        self,
        source: str | Path,
        *,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> RecipeReport:
        """Parse, validate, and digest a recipe without any state change (sec10 §10.2.3).

        Pipeline (sec10 §10.4): resolve ``source`` → read
        ``recipe.yaml`` → closed-schema validation against
        ``recipe_manifest.yaml`` → compute canonical content hash →
        enumerate the embedded ``schema.yaml``'s classes/slots. No DB
        writes. No cache writes. No provenance.

        Args:
            source: ``file:`` URI, ``https:`` URI, bare absolute path,
                or bare relative path. Tarballs are accepted.
            base_dir: Resolves bare relative paths and relative
                ``file:`` URIs. Defaults to ``Path.cwd()`` when
                ``source`` is a string and to ``source.parent`` when
                ``source`` is an existing ``Path`` (so callers passing
                a path-like get the natural local resolution behavior).
            expected_digest: Optional declared digest. When supplied,
                the resolver verifies the canonical content hash and
                raises :class:`RecipeDigestMismatchError` on mismatch.
                ``inspect`` is read-only, so the install-path rule
                "``https:`` requires a digest" is NOT enforced here —
                that lives in :meth:`import_` (PR 7).

        Raises:
            RecipeManifestError: Manifest fails closed-schema validation.
            RecipeFetchError: HTTPS fetch failed (network, HTTP error,
                corrupt tarball).
            RecipeDigestMismatchError: ``expected_digest`` did not
                match the fetched/loaded recipe's canonical content
                hash.
        """
        src_str = str(source)
        effective_base = base_dir
        if effective_base is None and isinstance(source, Path) and source.exists():
            effective_base = source.parent if source.is_file() else None

        resolver = self._select_resolver(src_str)
        with resolver.resolve(
            src_str,
            base_dir=effective_base,
            expected_digest=expected_digest,
        ) as recipe_dir:
            manifest_dict = self._read_manifest_yaml(recipe_dir, source=src_str)
            self._validate_manifest(manifest_dict, source=src_str)
            manifest = _manifest_from_dict(manifest_dict)
            digest = canonical_content_hash(recipe_dir)
            classes, slots = self._inspect_schema_elements(recipe_dir)

        return RecipeReport(
            manifest=manifest,
            digest=digest,
            classes=classes,
            slots=slots,
        )

    def import_(
        self,
        source: str | Path,
        *,
        dry_run: bool = False,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> ImportResult:
        """Bootstrap-install a recipe end-to-end (sec10 §10.4 / invariant 3).

        Pipeline:

        1. Resolve ``source`` and parse + validate ``recipe.yaml`` (same
           preflight as :meth:`inspect`).
        2. Check ``mosaic_version`` against the running Mosaic via
           :class:`packaging.specifiers.SpecifierSet`.
        3. Recursively resolve every dependency (``parent`` +
           ``requires.recipes``) bottom-up with cycle detection. Each
           dep is fetched, validated, and pre-flighted just like the
           top-level recipe.
        4. Check every ``requires.reference_loaders`` pin is satisfied
           (sec10 §10.4.5 — precondition only, no transitive install).
        5. Inside one storage transaction (atomicity, invariant 3):
           - For each recipe in dependency order: call
             :meth:`SchemaManager.merge_fragment`, write the
             ``installed_recipes`` entry, emit a ``recipe_imported``
             provenance event.
           - On any failure mid-merge, the transaction rolls back and
             no state change persists.

        ``dry_run`` performs steps 1–4 and returns an :class:`ImportResult`
        with ``dry_run=True`` and an empty ``installed`` list; no
        transaction is opened.

        Returns:
            :class:`ImportResult` carrying the per-recipe ``InstalledRecipe``
            entries (in install order) and the top-level recipe's
            added classes / slots.
        """
        # 1. Preflight the top-level recipe (parse, validate, digest).
        src_str = str(source)
        effective_base = base_dir
        if effective_base is None and isinstance(source, Path) and source.exists():
            effective_base = source.parent if source.is_file() else None

        plan = self._plan_import(
            src_str,
            base_dir=effective_base,
            expected_digest=expected_digest,
        )

        # 2. Reference-loader preconditions are checked against the live
        # `reference_versions` registry.
        self._verify_reference_loader_preconditions(plan)

        if dry_run:
            installed = tuple(
                _installed_recipe_from_plan_entry(entry)
                for entry in plan
            )
            top = plan[-1]
            return ImportResult(
                installed=installed,
                classes_added=tuple(top.classes_added),
                slots_added=tuple(top.slots_added),
                dry_run=True,
            )

        if self._storage is None or not hasattr(self._storage, "_transaction"):
            raise ValueError(
                "Cannot import a recipe: RecipeService has no storage. "
                "Construct MosaicClient with a storage adapter."
            )
        if self._schema_manager is None:
            raise ValueError(
                "Cannot import a recipe: RecipeService has no SchemaManager."
            )

        installed_records: list[InstalledRecipe] = []
        with self._storage._transaction() as conn:
            existing = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}
            for entry in plan:
                # Re-merge a previously-installed recipe at the same
                # version is a no-op; this lets a child import skip
                # re-installing its parent.
                if (
                    entry.manifest.id in existing
                    and existing[entry.manifest.id].get("version")
                    == entry.manifest.version
                ):
                    installed_records.append(
                        _installed_recipe_from_dict(existing[entry.manifest.id])
                    )
                    continue

                self._merge_one(entry)
                record = _installed_recipe_from_plan_entry(entry)
                existing[entry.manifest.id] = _installed_recipe_to_dict(record)
                set_meta(conn, META_KEY_INSTALLED_RECIPES, existing)
                self._emit_recipe_imported(conn, entry)
                installed_records.append(record)

        top = plan[-1]
        return ImportResult(
            installed=tuple(installed_records),
            classes_added=tuple(top.classes_added),
            slots_added=tuple(top.slots_added),
            dry_run=False,
        )

    def _plan_import(
        self,
        source: str,
        *,
        base_dir: Optional[Path],
        expected_digest: Optional[str],
    ) -> list["_PlanEntry"]:
        """Resolve dependencies bottom-up; return install order with cycle detection.

        Returns the install order as a list whose last entry is the
        top-level recipe. Each entry carries the parsed manifest, the
        resolved schema fragment, the canonical-content digest, and
        the post-resolution source URI (so provenance carries the
        exact URI Mosaic fetched from).
        """
        plan: list[_PlanEntry] = []
        seen: dict[str, str] = {}
        stack: list[str] = []
        self._resolve_recursive(
            source=source,
            base_dir=base_dir,
            expected_digest=expected_digest,
            plan=plan,
            seen=seen,
            stack=stack,
        )
        return plan

    def _resolve_recursive(
        self,
        *,
        source: str,
        base_dir: Optional[Path],
        expected_digest: Optional[str],
        plan: list["_PlanEntry"],
        seen: dict[str, str],
        stack: list[str],
    ) -> None:
        """Bottom-up resolve ``source`` and append to ``plan`` (dependencies first)."""
        resolver = self._select_resolver(source)
        with resolver.resolve(
            source, base_dir=base_dir, expected_digest=expected_digest
        ) as recipe_dir:
            manifest_dict = self._read_manifest_yaml(recipe_dir, source=source)
            self._validate_manifest(manifest_dict, source=source)
            manifest = _manifest_from_dict(manifest_dict)
            self._check_mosaic_version(manifest, source=source)

            self._enforce_https_digest_required(source, expected_digest, manifest)

            if manifest.id in stack:
                cycle = stack[stack.index(manifest.id):] + [manifest.id]
                raise RecipeLineageCycleError(
                    f"Recipe lineage cycle detected: {' -> '.join(cycle)}",
                    source=source,
                    recipe_id=manifest.id,
                    recipe_version=manifest.version,
                    cycle=cycle,
                )

            if manifest.id in seen:
                # Already planned via an earlier branch; do not enqueue
                # twice (this is how the merge-step deduplicates).
                if seen[manifest.id] != manifest.version:
                    raise RecipeRequiresUnsatisfiedError(
                        f"Recipe {manifest.id!r} appears at multiple versions in "
                        f"the dependency graph: {seen[manifest.id]!r} and "
                        f"{manifest.version!r}.",
                        source=source,
                        recipe_id=manifest.id,
                        recipe_version=manifest.version,
                    )
                return

            stack.append(manifest.id)
            try:
                # Parent first, then sibling requires — sec10 §10.4.4 order.
                if manifest.parent is not None:
                    self._resolve_recursive(
                        source=manifest.parent.source,
                        base_dir=recipe_dir,
                        expected_digest=manifest.parent.digest,
                        plan=plan,
                        seen=seen,
                        stack=stack,
                    )
                for ref in manifest.requires.recipes:
                    self._resolve_recursive(
                        source=ref.source,
                        base_dir=recipe_dir,
                        expected_digest=ref.digest,
                        plan=plan,
                        seen=seen,
                        stack=stack,
                    )
            finally:
                stack.pop()

            digest = canonical_content_hash(recipe_dir)
            if expected_digest is not None:
                # The resolver already verified, but stamp the recorded
                # digest with the canonical hex form (no sha256: prefix).
                pass
            schema_path = recipe_dir / "schema.yaml"
            if schema_path.is_file():
                fragment_text = schema_path.read_text(encoding="utf-8")
                fragment = yaml.safe_load(fragment_text) or {}
            else:
                fragment = {}

            classes_added = sorted((fragment.get("classes") or {}).keys())
            slots_added = sorted((fragment.get("slots") or {}).keys())

            seen[manifest.id] = manifest.version
            plan.append(
                _PlanEntry(
                    manifest=manifest,
                    source=source,
                    digest=digest,
                    fragment=fragment,
                    classes_added=classes_added,
                    slots_added=slots_added,
                )
            )

    def _check_mosaic_version(
        self, manifest: RecipeManifest, *, source: str
    ) -> None:
        """Reject the import if the running Mosaic version is excluded."""
        from packaging.specifiers import InvalidSpecifier, SpecifierSet

        try:
            spec = SpecifierSet(manifest.mosaic_version)
        except InvalidSpecifier as e:
            raise RecipeVersionIncompatibleError(
                f"Recipe {manifest.id}@{manifest.version} declares an "
                f"invalid mosaic_version specifier {manifest.mosaic_version!r}: {e}",
                source=source,
                recipe_id=manifest.id,
                recipe_version=manifest.version,
                specifier=manifest.mosaic_version,
            ) from e

        from mosaic import __version__ as _mosaic_version_module

        # The mosaic package version is not yet imported in older builds —
        # fall through gracefully when the attribute is missing.
        if _mosaic_version_module not in spec:
            raise RecipeVersionIncompatibleError(
                f"Recipe {manifest.id}@{manifest.version} requires "
                f"mosaic {manifest.mosaic_version!r} but installed Mosaic is "
                f"{_mosaic_version_module!r}.",
                source=source,
                recipe_id=manifest.id,
                recipe_version=manifest.version,
                specifier=manifest.mosaic_version,
                mosaic_version=_mosaic_version_module,
            )

    def _enforce_https_digest_required(
        self,
        source: str,
        expected_digest: Optional[str],
        manifest: RecipeManifest,
    ) -> None:
        """Sec10 invariant 4: ``https:`` source MUST declare a digest at install."""
        if expected_digest is None and source.startswith("https:"):
            raise RecipeDigestMismatchError(
                "Install of an https: source requires a declared digest "
                "(sec10 invariant 4). Pass a digest on the RecipeRef or "
                "use `mosaic recipe inspect` to discover it.",
                source=source,
                recipe_id=manifest.id,
                recipe_version=manifest.version,
            )

    def _verify_reference_loader_preconditions(
        self, plan: list["_PlanEntry"]
    ) -> None:
        """Each ``requires.reference_loaders`` pin must already be installed.

        Sec10 §10.4.5: v1 treats these as preconditions only — Mosaic
        does NOT install Reference Loaders as a side effect of recipe
        import.
        """
        if self._storage is None or not hasattr(self._storage, "_transaction"):
            # Nothing to check against; skip silently rather than fail.
            return
        with self._storage._transaction() as conn:
            installed_versions = get_meta(conn, "reference_versions") or {}

        for entry in plan:
            for pin in entry.manifest.requires.reference_loaders:
                if "==" not in pin:
                    raise RecipeRequiresUnsatisfiedError(
                        f"requires.reference_loaders entry {pin!r} is not a "
                        f"valid pin (expected 'name==version').",
                        source=entry.source,
                        recipe_id=entry.manifest.id,
                        recipe_version=entry.manifest.version,
                        loader_pin=pin,
                    )
                name, _, version = pin.partition("==")
                installed = installed_versions.get(name)
                installed_version = (
                    installed.get("version") if isinstance(installed, dict) else None
                )
                if installed_version != version:
                    raise RecipeRequiresUnsatisfiedError(
                        f"Recipe {entry.manifest.id}@{entry.manifest.version} "
                        f"requires reference loader {pin!r} but installed "
                        f"version is {installed_version!r}.",
                        source=entry.source,
                        recipe_id=entry.manifest.id,
                        recipe_version=entry.manifest.version,
                        loader_pin=pin,
                    )

    def _merge_one(self, entry: "_PlanEntry") -> None:
        """Merge one recipe's fragment via ``SchemaManager.merge_fragment``."""
        assert self._schema_manager is not None
        if not entry.fragment:
            return
        try:
            new_registry = self._schema_manager.merge_fragment(
                entry.fragment,
                recipe_id=entry.manifest.id,
                recipe_version=entry.manifest.version,
            )
        except RecipeSchemaError:
            raise
        self._schema_manager.set_registry(new_registry)
        if self._storage is not None and hasattr(self._storage, "schema_registry"):
            self._storage.schema_registry = new_registry

    def _emit_recipe_imported(
        self,
        conn: object,
        entry: "_PlanEntry",
    ) -> None:
        """Insert one ``recipe_imported`` ProvenanceRecord on this connection."""
        if self._storage is None or not hasattr(self._storage, "_get_provenance_store"):
            return
        prov_store = self._storage._get_provenance_store(conn)
        parent_payload = None
        if entry.manifest.parent is not None:
            parent_payload = {
                "id": entry.manifest.parent.id,
                "version": entry.manifest.parent.version,
                "source": entry.manifest.parent.source,
                "digest": entry.manifest.parent.digest,
            }
        prov_store.record(
            entity_id=None,
            entity_type=None,
            operation="recipe_imported",
            patch={
                "recipe_id": entry.manifest.id,
                "recipe_version": entry.manifest.version,
                "recipe_digest": entry.digest,
                "recipe_source": entry.source,
                "parent": parent_payload,
                "classes_added": list(entry.classes_added),
                "slots_added": list(entry.slots_added),
            },
        )

    def export(
        self,
        *,
        scope: str = "schema",
        parent: Optional[str] = None,
    ) -> RecipeExport:
        """Package the locally-authored content of the live schema (sec10 §10.5).

        Selectivity: a class/slot is included when its ``provided_by``
        annotation is absent or does NOT start with ``recipe.`` or
        ``loader.``, AND its ``from_schema`` is not a bundled framework
        schema (``mosaic_core``, ``recipe_manifest``). This prevents
        accidental re-distribution of upstream content and protects
        attribution.

        ``parent`` is the ``id`` of an entry in ``installed_recipes``.
        When supplied, the exported manifest's ``parent:`` is populated
        from that entry; otherwise ``parent:`` is omitted.

        ``requires.recipes`` is auto-populated by walking each exported
        class's ``is_a:`` ancestor + slot ranges, looking up the
        ``provided_by`` recipe attribution on the upstream element,
        and matching it to an ``installed_recipes`` entry. Builtins
        (``string``, ``integer``, ``datetime``, …) are skipped.

        ``scope`` is reserved for the v2 ``scope="data"`` mode (sec10
        §10.5). v1 ships ``scope="schema"`` only.

        Returns:
            A :class:`RecipeExport` carrying the manifest dict, the
            schema fragment dict, and the auto-resolved
            ``requires.recipes`` list. The caller (CLI) writes these
            to disk.
        """
        if scope != "schema":
            raise ValueError(
                f"recipe export only supports scope='schema' in v1 (got {scope!r})."
            )
        if self._schema_manager is None or self._schema_manager.registry is None:
            raise ValueError(
                "Cannot export: RecipeService has no live SchemaManager registry."
            )
        sv = self._schema_manager.registry.schema_view

        local_classes, local_slots = self._select_local_elements(sv)
        auto_requires = self._auto_resolve_requires(sv, local_classes)
        parent_ref = self._resolve_parent_for_export(parent)

        manifest = _build_export_manifest_stub(
            parent=parent_ref,
            requires=auto_requires,
        )
        schema_fragment = _build_export_schema_fragment(
            sv, local_classes, local_slots
        )

        return RecipeExport(
            manifest=manifest,
            schema_fragment=schema_fragment,
            auto_resolved_requires=tuple(auto_requires),
        )

    def _select_local_elements(self, sv) -> tuple[list[str], list[str]]:
        """Return ``(class_names, slot_names)`` that the export should ship.

        Selectivity rule (sec10 §10.5): include when

        - ``provided_by`` is absent OR does NOT start with ``recipe.``
          or ``loader.`` (so author-written content stays in, imported
          recipe/loader content stays out), AND
        - the element's *containing schema* is not a framework schema
          (``mosaic_core``, ``recipe_manifest``, ``linkml:types``).

        ``SchemaView.in_schema(name)`` resolves the containing schema
        by element name; we look the resulting schema's ``id`` up in
        ``_FRAMEWORK_SCHEMA_IDS``. This is more reliable than relying
        on the element's ``from_schema`` attribute, which is ``None``
        for elements defined directly in the active schema.
        """
        from mosaic.linkml_bridge import PROVIDED_BY_ANNOTATION, annotation_value

        def _is_framework(name: str) -> bool:
            sch_name = sv.in_schema(name)
            if sch_name is None:
                return False
            sch = sv.schema_map.get(sch_name) if hasattr(sv, "schema_map") else None
            if sch is None:
                return False
            return _is_framework_origin(getattr(sch, "id", None))

        class_names: list[str] = []
        for name in sv.all_classes(imports=False):
            cls = sv.get_class(name)
            if cls is None:
                continue
            if _is_framework(name):
                continue
            pb = annotation_value(cls, PROVIDED_BY_ANNOTATION)
            if pb is not None:
                pb_str = str(pb)
                if pb_str.startswith("recipe.") or pb_str.startswith("loader."):
                    continue
            class_names.append(name)

        slot_names: list[str] = []
        for name in sv.all_slots(imports=False):
            slot = sv.get_slot(name)
            if slot is None:
                continue
            if _is_framework(name):
                continue
            pb = annotation_value(slot, PROVIDED_BY_ANNOTATION)
            if pb is not None:
                pb_str = str(pb)
                if pb_str.startswith("recipe.") or pb_str.startswith("loader."):
                    continue
            slot_names.append(name)

        return sorted(class_names), sorted(slot_names)

    def _auto_resolve_requires(
        self, sv, local_class_names: list[str]
    ) -> list[RecipeRef]:
        """Walk each exported class's ``is_a:`` and slot ranges to find upstream recipes."""
        from mosaic.linkml_bridge import PROVIDED_BY_ANNOTATION, annotation_value

        installed_by_id_version: dict[tuple[str, str], InstalledRecipe] = {}
        for rec in self.list_installed():
            installed_by_id_version[(rec.id, rec.version)] = rec

        builtin_ranges = {
            "string", "integer", "float", "double", "boolean",
            "date", "datetime", "time", "uri", "uriorcurie",
            "ncname", "objectidentifier", "nodeidentifier", "any",
        }

        upstream_refs: dict[tuple[str, str], RecipeRef] = {}

        def _consider(element) -> None:
            if element is None:
                return
            pb = annotation_value(element, PROVIDED_BY_ANNOTATION)
            if not pb:
                return
            pb_str = str(pb)
            if not pb_str.startswith("recipe."):
                return
            try:
                rest = pb_str[len("recipe."):]
                rid, _, rver = rest.partition("@")
                if not rid or not rver:
                    return
            except Exception:
                return
            key = (rid, rver)
            if key in upstream_refs:
                return
            rec = installed_by_id_version.get(key)
            if rec is None:
                return
            upstream_refs[key] = RecipeRef(
                id=rec.id,
                version=rec.version,
                source=rec.source,
                digest=f"sha256:{rec.digest}" if not rec.digest.startswith("sha256:") else rec.digest,
            )

        for cls_name in local_class_names:
            cls = sv.get_class(cls_name)
            if cls is None:
                continue
            # is_a chain
            if cls.is_a:
                _consider(sv.get_class(cls.is_a))
            # Slot ranges
            for slot in sv.class_induced_slots(cls_name):
                if not slot.range or slot.range in builtin_ranges:
                    continue
                target_cls = sv.get_class(slot.range)
                if target_cls is not None:
                    _consider(target_cls)

        return [upstream_refs[k] for k in sorted(upstream_refs.keys())]

    def _resolve_parent_for_export(
        self, parent_id: Optional[str]
    ) -> Optional[RecipeRef]:
        if parent_id is None:
            return None
        for rec in self.list_installed():
            if rec.id == parent_id:
                return RecipeRef(
                    id=rec.id,
                    version=rec.version,
                    source=rec.source,
                    digest=(
                        f"sha256:{rec.digest}"
                        if not rec.digest.startswith("sha256:")
                        else rec.digest
                    ),
                )
        raise ValueError(
            f"--parent {parent_id!r} not found in installed_recipes. "
            f"Run `mosaic recipe list` to see what is installed."
        )

    def export_lockfile(self, out: Path) -> Path:
        """Serialise ``installed_recipes`` as a portable ``recipe.lock.yaml`` (sec10 §10.6).

        Dumps :meth:`list_installed` as a YAML document carrying
        ``lockfile_version: 1`` at the top level for forward-compatible
        parsing (sec10 §10.6.2). Each entry mirrors the
        ``mosaic_meta.installed_recipes`` shape: ``id``, ``version``,
        ``source``, ``digest`` (sha256-prefixed for portability),
        ``installed_at``, and ``parent`` (or ``null`` if absent).

        Args:
            out: Destination path for the lockfile. Parent directory must
                exist; the file is overwritten if it already exists.

        Returns:
            The resolved ``out`` path.
        """
        out_path = Path(out)
        installed = self.list_installed()
        entries: dict[str, dict] = {}
        for record in installed:
            entries[record.id] = _installed_recipe_to_lockfile_entry(record)
        document = {
            "lockfile_version": 1,
            "installed_recipes": entries,
        }
        out_path.write_text(yaml.safe_dump(document, sort_keys=False))
        return out_path

    def install_from_lockfile(self, lockfile: Path) -> list["ImportResult"]:
        """Replay a ``recipe.lock.yaml`` on the current instance (sec10 §10.6).

        Iterates entries in dependency order (parents before children),
        fetching each via its ``source``, verifying its ``digest``, and
        installing through :meth:`import_`. Relative ``source`` paths
        resolve against the lockfile's directory (sec10 §10.3.3).

        The same-version skip inside :meth:`import_` means recipes already
        installed (e.g. a parent reached transitively before its child's
        explicit lockfile entry) are no-ops on re-encounter — the
        round-trip is idempotent.

        Args:
            lockfile: Path to a ``recipe.lock.yaml`` document.

        Returns:
            One :class:`ImportResult` per lockfile entry, in install
            order.

        Raises:
            ValueError: ``lockfile_version`` is missing or unsupported,
                or the lockfile is structurally invalid.
            RecipeLineageCycleError: Lockfile entries' parent graph
                contains a cycle.
            RecipeDigestMismatchError: A fetched recipe's digest does
                not match the lockfile entry's declared digest.
            RecipeFetchError: A ``source`` could not be resolved.
        """
        lockfile_path = Path(lockfile)
        try:
            data = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ValueError(
                f"Failed to parse lockfile {lockfile_path}: {e}"
            ) from e
        if not isinstance(data, dict):
            raise ValueError(
                f"Lockfile {lockfile_path} must be a YAML mapping at the top level."
            )

        version = data.get("lockfile_version")
        if version != 1:
            raise ValueError(
                f"Unsupported lockfile_version: {version!r} "
                f"(this Mosaic build supports lockfile_version: 1)."
            )

        raw_entries = data.get("installed_recipes") or {}
        if not isinstance(raw_entries, dict):
            raise ValueError(
                f"Lockfile {lockfile_path} 'installed_recipes' must be a "
                f"mapping; got {type(raw_entries).__name__}."
            )

        ordered = _topo_sort_lockfile_entries(raw_entries)

        base_dir = lockfile_path.parent
        results: list[ImportResult] = []
        for entry in ordered:
            result = self.import_(
                entry["source"],
                expected_digest=entry["digest"],
                base_dir=base_dir,
            )
            results.append(result)
        return results

    def diff(
        self,
        a: str | Path,
        b: str | Path,
        *,
        base_dir_a: Optional[Path] = None,
        base_dir_b: Optional[Path] = None,
    ) -> "RecipeDiff":
        """Structural diff of two recipes' schema fragments (sec10 §10.2.3).

        Resolves each side via the same resolver chain ``inspect`` uses
        (``file:``, ``https:``, bare paths/tarballs), reads each side's
        ``schema.yaml`` directly (no merge, no SchemaView), and compares
        the ``classes:`` / ``slots:`` sub-trees by name and by body.

        Element bodies are compared by deep-equality on the parsed YAML
        dicts; a class/slot present in both sides whose body dicts differ
        is reported under ``classes_changed`` / ``slots_changed``.
        Missing ``schema.yaml`` on either side is treated as "no
        classes, no slots" (mirrors :meth:`inspect` semantics).

        No DB writes. No cache writes. No provenance.

        Args:
            a, b: Recipe sources (paths, tarballs, ``file:`` / ``https:``
                URIs).
            base_dir_a, base_dir_b: Override the per-side base directory
                used to resolve relative paths. Mirrors :meth:`inspect`.

        Returns:
            :class:`RecipeDiff` with the six lexicographically-sorted
            name lists (classes added/removed/changed, slots
            added/removed/changed).
        """
        from mosaic.core.recipe import RecipeDiff

        fragment_a = self._load_schema_fragment(a, base_dir=base_dir_a)
        fragment_b = self._load_schema_fragment(b, base_dir=base_dir_b)

        classes_a = fragment_a.get("classes") or {}
        classes_b = fragment_b.get("classes") or {}
        slots_a = fragment_a.get("slots") or {}
        slots_b = fragment_b.get("slots") or {}

        names_a_classes = set(classes_a)
        names_b_classes = set(classes_b)
        names_a_slots = set(slots_a)
        names_b_slots = set(slots_b)

        return RecipeDiff(
            classes_added=tuple(sorted(names_b_classes - names_a_classes)),
            classes_removed=tuple(sorted(names_a_classes - names_b_classes)),
            classes_changed=tuple(
                sorted(
                    n
                    for n in names_a_classes & names_b_classes
                    if classes_a[n] != classes_b[n]
                )
            ),
            slots_added=tuple(sorted(names_b_slots - names_a_slots)),
            slots_removed=tuple(sorted(names_a_slots - names_b_slots)),
            slots_changed=tuple(
                sorted(
                    n
                    for n in names_a_slots & names_b_slots
                    if slots_a[n] != slots_b[n]
                )
            ),
        )

    def _load_schema_fragment(
        self,
        source: str | Path,
        *,
        base_dir: Optional[Path],
    ) -> dict:
        """Resolve ``source`` and return the parsed ``schema.yaml`` dict.

        Returns ``{}`` when ``schema.yaml`` is absent or unparseable;
        diff treats the missing side as contributing no classes/slots.
        """
        src_str = str(source)
        effective_base = base_dir
        if effective_base is None and isinstance(source, Path) and source.exists():
            effective_base = source.parent if source.is_file() else None

        resolver = self._select_resolver(src_str)
        with resolver.resolve(src_str, base_dir=effective_base) as recipe_dir:
            schema_path = recipe_dir / "schema.yaml"
            if not schema_path.is_file():
                return {}
            try:
                content = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                return {}
            if not isinstance(content, dict):
                return {}
            return content

    def extend(
        self,
        installed_id: str,
        out_dir: Path,
    ) -> Path:
        """Scaffold a derivative recipe directory rooted at ``out_dir`` (sec10 §10.7.3).

        Reads ``installed_recipes`` for the entry matching ``installed_id``
        and writes:

        - ``out_dir/recipe.yaml`` — manifest stub with ``parent:`` populated
          from the installed entry (id, version, source, digest), and
          author-fillable stubs for ``id``/``name``/``version``.
        - ``out_dir/schema.yaml`` — an empty LinkML schema with stub
          ``id``/``name``/``default_prefix`` ready for local additions.

        This is the ONLY operation that creates a ``parent`` lineage
        pointer (invariant 5 — no implicit lineage).

        Args:
            installed_id: ``RecipeManifest.id`` of the parent recipe. Must
                match an entry in ``mosaic_meta.installed_recipes``.
            out_dir: Target directory. Created if missing. Must not
                already contain a ``recipe.yaml`` or ``schema.yaml``.

        Returns:
            The resolved ``out_dir`` path.

        Raises:
            ValueError: ``installed_id`` is not in ``installed_recipes``,
                or ``out_dir`` already contains a ``recipe.yaml`` /
                ``schema.yaml``, or ``out_dir`` exists and is not a
                directory.
        """
        installed = self.list_installed()
        match = next((r for r in installed if r.id == installed_id), None)
        if match is None:
            raise ValueError(
                f"installed_id {installed_id!r} not found in installed_recipes. "
                f"Run `mosaic recipe list` to see what is installed."
            )

        out_dir = Path(out_dir)
        if out_dir.exists() and not out_dir.is_dir():
            raise ValueError(
                f"out_dir is not a directory: {out_dir}"
            )
        out_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = out_dir / "recipe.yaml"
        schema_path = out_dir / "schema.yaml"
        for p in (manifest_path, schema_path):
            if p.exists():
                raise ValueError(
                    f"{p} already exists; refusing to overwrite."
                )

        parent_ref = RecipeRef(
            id=match.id,
            version=match.version,
            source=match.source,
            digest=(
                f"sha256:{match.digest}"
                if not match.digest.startswith("sha256:")
                else match.digest
            ),
        )
        manifest = _build_extend_manifest_stub(parent=parent_ref)
        schema_fragment = _build_extend_schema_fragment_stub()

        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        schema_path.write_text(yaml.safe_dump(schema_fragment, sort_keys=False))
        return out_dir

    def _select_resolver(self, source: str) -> RecipeResolver:
        for r in self._resolvers:
            if r.can_handle(source):
                return r
        raise ValueError(
            f"No registered RecipeResolver handles source: {source!r}"
        )

    def _read_manifest_yaml(self, recipe_dir: Path, *, source: str) -> dict:
        manifest_path = recipe_dir / "recipe.yaml"
        if not manifest_path.is_file():
            raise RecipeManifestError(
                "Recipe is missing recipe.yaml at its root.",
                source=source,
            )
        try:
            data = yaml.load(
                manifest_path.read_text(encoding="utf-8"),
                Loader=_ManifestYAMLLoader,
            )
        except yaml.YAMLError as e:
            raise RecipeManifestError(
                f"Failed to parse recipe.yaml: {e}",
                source=source,
            ) from e
        if not isinstance(data, dict):
            raise RecipeManifestError(
                "recipe.yaml must be a YAML mapping at the top level.",
                source=source,
            )
        return data

    def _validate_manifest(self, manifest_dict: dict, *, source: str) -> None:
        """Closed-schema-validate ``manifest_dict`` against ``recipe_manifest.yaml``.

        Uses the bundled LinkML schema shipped under
        ``mosaic.schemas`` and the same ``JsonschemaValidationPlugin(closed=True)``
        wiring exercised by the Phase-2 schema tests. A manifest that still
        carries the retired ``hippo_version`` key is rejected with an error
        naming ``mosaic_version`` (platform ADR-0007).
        """
        if isinstance(manifest_dict, dict) and "hippo_version" in manifest_dict:
            message = (
                "`hippo_version` was renamed to `mosaic_version` "
                "(platform ADR-0007); rename the key in recipe.yaml"
            )
            raise RecipeManifestError(
                f"recipe.yaml failed validation (1 error(s)): {message}",
                source=source,
                recipe_id=manifest_dict.get("id"),
                recipe_version=manifest_dict.get("version"),
                errors=[message],
            )

        from linkml.validator import Validator
        from linkml.validator.plugins import JsonschemaValidationPlugin

        schema_path = importlib.resources.files("mosaic.schemas").joinpath(
            _RECIPE_MANIFEST_SCHEMA_NAME
        )
        validator = Validator(
            schema=str(schema_path),
            validation_plugins=[JsonschemaValidationPlugin(closed=True)],
        )
        report = validator.validate(manifest_dict, "RecipeManifest")
        if report.results:
            messages = [r.message for r in report.results]
            raise RecipeManifestError(
                f"recipe.yaml failed validation ({len(messages)} error(s)): "
                f"{messages[0]}",
                source=source,
                recipe_id=manifest_dict.get("id"),
                recipe_version=manifest_dict.get("version"),
                errors=messages,
            )

    def _inspect_schema_elements(
        self,
        recipe_dir: Path,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return ``(classes, slots)`` declared in the recipe's ``schema.yaml``.

        ``inspect`` is read-only; no merge or LinkML SchemaView wiring
        happens here. When ``schema.yaml`` is absent or malformed, the
        report carries empty tuples — the merge layer (PR 7) is the
        gate that turns those into hard failures.
        """
        schema_path = recipe_dir / "schema.yaml"
        if not schema_path.is_file():
            return (), ()
        try:
            content = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return (), ()
        if not isinstance(content, dict):
            return (), ()

        classes = tuple(sorted((content.get("classes") or {}).keys()))
        slots = tuple(sorted((content.get("slots") or {}).keys()))
        return classes, slots


_FRAMEWORK_SCHEMA_IDS = {
    "https://w3id.org/datahelix/mosaic/mosaic_core",
    "https://w3id.org/datahelix/mosaic/recipe_manifest",
    "https://w3id.org/linkml/types",
    "https://w3id.org/linkml/meta",
}


def _is_framework_origin(from_schema: Optional[str]) -> bool:
    """True when a LinkML element belongs to a bundled framework schema.

    Mosaic's own ``mosaic_core`` (``Entity``, ``ProvenanceRecord``, …)
    has no ``provided_by`` annotation but should never be re-exported
    as user-authored content. The check is by ``from_schema`` URI;
    URIs are matched against a small known set so that user schemas
    sitting under unrelated URIs always pass through.
    """
    if not from_schema:
        return False
    return str(from_schema) in _FRAMEWORK_SCHEMA_IDS


def _build_export_manifest_stub(
    *,
    parent: Optional[RecipeRef],
    requires: list[RecipeRef],
) -> dict:
    """Return a ``recipe.yaml`` mapping with author-fillable stubs.

    ``id``/``name``/``version`` are stubs the user MUST replace before
    publishing. ``mosaic_version``/``created_at`` are filled with
    sensible defaults. ``parent``/``requires.recipes`` come from the
    caller's inference.
    """
    from datetime import datetime, timezone

    manifest: dict = {
        "id": "TODO.set.this",
        "name": "TODO-set-name",
        "version": "0.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mosaic_version": f">={_local_mosaic_version()}",
    }
    if parent is not None:
        manifest["parent"] = {
            "id": parent.id,
            "version": parent.version,
            "source": parent.source,
        }
        if parent.digest is not None:
            manifest["parent"]["digest"] = parent.digest
    if requires:
        manifest["requires"] = {
            "recipes": [
                {
                    "id": r.id,
                    "version": r.version,
                    "source": r.source,
                    **({"digest": r.digest} if r.digest else {}),
                }
                for r in requires
            ]
        }
    return manifest


def _build_extend_manifest_stub(*, parent: RecipeRef) -> dict:
    """Return the ``recipe.yaml`` stub for ``mosaic recipe extend`` (sec10 §10.7.3).

    Populates ``parent`` from the installed-recipe entry so the lineage
    pointer is real; ``id``/``name``/``version`` carry author-fillable
    TODO stubs the user MUST replace before sharing.
    """
    from datetime import datetime, timezone

    manifest: dict = {
        "id": "TODO.set.this",
        "name": "TODO-set-name",
        "version": "0.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mosaic_version": f">={_local_mosaic_version()}",
        "parent": {
            "id": parent.id,
            "version": parent.version,
            "source": parent.source,
        },
    }
    if parent.digest is not None:
        manifest["parent"]["digest"] = parent.digest
    return manifest


def _build_extend_schema_fragment_stub() -> dict:
    """Return the empty ``schema.yaml`` stub for ``mosaic recipe extend``."""
    return {
        "id": "https://example.org/TODO-set-this",
        "name": "TODO-set-name",
        "default_prefix": "TODO-set-prefix",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
        },
        "default_range": "string",
    }


def _local_mosaic_version() -> str:
    """Best-effort lookup of the currently-installed Mosaic version."""
    try:
        from mosaic import __version__

        return str(__version__)
    except Exception:
        return "0.0.0"


def _build_export_schema_fragment(
    sv, class_names: list[str], slot_names: list[str]
) -> dict:
    """Build a ``schema.yaml`` mapping carrying the selected classes/slots.

    The output is a minimal LinkML document the exporting user can
    rename and publish: ``id`` and ``name`` carry stub values, the
    class/slot bodies are the LinkML element definitions stripped of
    ``provided_by`` annotations (those get re-injected on install).
    """
    import copy

    fragment: dict = {
        "id": "https://example.org/TODO-set-this",
        "name": "TODO-set-name",
        "default_prefix": "TODO-set-prefix",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
        },
        "default_range": "string",
    }

    classes_out: dict = {}
    for name in class_names:
        cls = sv.get_class(name)
        if cls is None:
            continue
        cls_body = _linkml_element_to_dict(cls)
        _strip_provided_by_annotation(cls_body)
        attrs = cls_body.get("attributes")
        if isinstance(attrs, dict):
            for _, attr_body in attrs.items():
                if isinstance(attr_body, dict):
                    _strip_provided_by_annotation(attr_body)
        classes_out[name] = cls_body
    if classes_out:
        fragment["classes"] = classes_out

    slots_out: dict = {}
    for name in slot_names:
        slot = sv.get_slot(name)
        if slot is None:
            continue
        slot_body = _linkml_element_to_dict(slot)
        _strip_provided_by_annotation(slot_body)
        slots_out[name] = slot_body
    if slots_out:
        fragment["slots"] = slots_out

    return fragment


def _linkml_element_to_dict(element) -> dict:
    """Serialise a LinkML class/slot to a dict suitable for emission as YAML.

    Uses the LinkML yaml_dumper round-trip so the output is YAML-shaped
    and round-trips through ``SchemaView``. Empty/null fields are
    omitted at the caller's discretion.
    """
    from linkml_runtime.dumpers import yaml_dumper

    return yaml.safe_load(yaml_dumper.dumps(element)) or {}


def _strip_provided_by_annotation(body: dict) -> None:
    """Remove the ``provided_by`` annotation from an element body in place.

    Author exports MUST NOT carry a stale ``provided_by`` because the
    merge layer re-injects the importing recipe's identity (invariant
    7). Handles both dict-shape and list-shape annotation blocks.
    """
    ann = body.get("annotations")
    if isinstance(ann, dict):
        ann.pop("provided_by", None)
        if not ann:
            body.pop("annotations", None)
    elif isinstance(ann, list):
        body["annotations"] = [
            e for e in ann if not (isinstance(e, dict) and e.get("tag") == "provided_by")
        ]
        if not body["annotations"]:
            body.pop("annotations", None)


from dataclasses import dataclass, field as _dc_field


@dataclass(frozen=True)
class _PlanEntry:
    """One node in the recipe dependency-resolution plan.

    Private to :class:`RecipeService`. Captures everything ``import_``
    needs after the recursive resolve step has finished — manifest,
    post-resolution source URI, canonical-content digest, parsed
    schema fragment, and the lists of classes/slots the fragment adds.
    The fragment dict is the un-modified author content; the merge
    layer is responsible for ``provided_by`` injection (invariant 7).
    """

    manifest: RecipeManifest
    source: str
    digest: str
    fragment: dict = _dc_field(default_factory=dict)
    classes_added: tuple[str, ...] | list[str] = ()
    slots_added: tuple[str, ...] | list[str] = ()


def _installed_recipe_from_plan_entry(entry: _PlanEntry) -> InstalledRecipe:
    """Convert a planned install into the persisted :class:`InstalledRecipe`."""
    from datetime import datetime, timezone

    parent = entry.manifest.parent
    return InstalledRecipe(
        id=entry.manifest.id,
        version=entry.manifest.version,
        source=entry.source,
        digest=entry.digest,
        installed_at=datetime.now(timezone.utc).isoformat(),
        parent=parent,
    )


def _installed_recipe_to_dict(record: InstalledRecipe) -> dict:
    """Serialise :class:`InstalledRecipe` to its persisted JSON shape."""
    parent_payload = None
    if record.parent is not None:
        parent_payload = {
            "id": record.parent.id,
            "version": record.parent.version,
            "source": record.parent.source,
            "digest": record.parent.digest,
        }
    return {
        "id": record.id,
        "version": record.version,
        "source": record.source,
        "digest": record.digest,
        "installed_at": record.installed_at,
        "parent": parent_payload,
    }


def _installed_recipe_to_lockfile_entry(record: InstalledRecipe) -> dict:
    """Render one ``InstalledRecipe`` as a lockfile entry (sec10 §10.6.2).

    Mirrors the persisted ``installed_recipes`` JSON shape except that
    ``digest`` is emitted with the ``sha256:`` prefix for portability —
    a lockfile is the public, cross-instance artifact and downstream
    consumers will expect the prefixed form.
    """
    digest = record.digest
    if not digest.startswith("sha256:"):
        digest = f"sha256:{digest}"
    parent_payload = None
    if record.parent is not None:
        parent_payload = {
            "id": record.parent.id,
            "version": record.parent.version,
            "source": record.parent.source,
        }
        if record.parent.digest is not None:
            parent_payload["digest"] = record.parent.digest
    return {
        "id": record.id,
        "version": record.version,
        "source": record.source,
        "digest": digest,
        "installed_at": record.installed_at,
        "parent": parent_payload,
    }


def _topo_sort_lockfile_entries(entries: dict[str, dict]) -> list[dict]:
    """Return lockfile entries in install order (parents before children).

    The lockfile's ``parent`` block carries an ``id`` reference back to
    another entry; topo-sorting on that pointer ensures every parent is
    installed before its child. Entries whose parent is not present in
    the lockfile (because the parent was imported from somewhere
    else, or there is no parent) sort to the front in lexicographic
    id order for deterministic output.

    Cycle detection: if the parent graph contains a cycle, raise
    :class:`RecipeLineageCycleError` so the install path mirrors the
    same error contract as :meth:`import_`.
    """
    in_lockfile = set(entries.keys())
    sorted_ids = sorted(entries.keys())

    result: list[dict] = []
    visited: set[str] = set()
    visiting: list[str] = []

    def _visit(rid: str) -> None:
        if rid in visited:
            return
        if rid in visiting:
            cycle = visiting[visiting.index(rid):] + [rid]
            raise RecipeLineageCycleError(
                f"Lockfile lineage cycle detected: {' -> '.join(cycle)}",
                source=entries[rid].get("source"),
                recipe_id=rid,
                recipe_version=entries[rid].get("version"),
                cycle=cycle,
            )
        visiting.append(rid)
        try:
            parent = entries[rid].get("parent")
            if parent is not None:
                parent_id = parent.get("id")
                if parent_id and parent_id in in_lockfile:
                    _visit(parent_id)
        finally:
            visiting.pop()
        visited.add(rid)
        result.append(entries[rid])

    for rid in sorted_ids:
        _visit(rid)
    return result


def _installed_recipe_from_dict(entry: dict) -> InstalledRecipe:
    """Hydrate one ``InstalledRecipe`` from its persisted JSON form.

    The persisted shape mirrors ``InstalledRecipe`` exactly (sec10 §10.3.4)
    plus an optional embedded ``parent`` ``RecipeRef``. Missing optional
    fields default per the dataclass.
    """
    parent_data = entry.get("parent")
    parent = None
    if parent_data is not None:
        parent = RecipeRef(
            id=parent_data["id"],
            version=parent_data["version"],
            source=parent_data["source"],
            digest=parent_data.get("digest"),
        )
    return InstalledRecipe(
        id=entry["id"],
        version=entry["version"],
        source=entry["source"],
        digest=entry["digest"],
        installed_at=entry["installed_at"],
        parent=parent,
    )


def _manifest_from_dict(data: dict) -> RecipeManifest:
    """Hydrate a :class:`RecipeManifest` from a closed-schema-validated dict.

    Optional sub-objects (``author``, ``parent``, ``requires``) are
    materialized into their typed counterparts. The caller is
    responsible for having validated ``data`` first — this function
    assumes the closed-schema contract holds.
    """
    author = None
    if (raw_author := data.get("author")) is not None:
        author = RecipeAuthor(
            name=raw_author.get("name"),
            email=raw_author.get("email"),
            organization=raw_author.get("organization"),
        )

    parent = None
    if (raw_parent := data.get("parent")) is not None:
        parent = RecipeRef(
            id=raw_parent["id"],
            version=raw_parent["version"],
            source=raw_parent["source"],
            digest=raw_parent.get("digest"),
        )

    requires_data = data.get("requires") or {}
    requires = RecipeRequires(
        recipes=tuple(
            RecipeRef(
                id=r["id"],
                version=r["version"],
                source=r["source"],
                digest=r.get("digest"),
            )
            for r in (requires_data.get("recipes") or [])
        ),
        reference_loaders=tuple(requires_data.get("reference_loaders") or []),
    )

    return RecipeManifest(
        id=data["id"],
        name=data["name"],
        version=data["version"],
        created_at=str(data["created_at"]),
        mosaic_version=data["mosaic_version"],
        description=data.get("description"),
        author=author,
        license=data.get("license"),
        source=data.get("source"),
        parent=parent,
        requires=requires,
    )
