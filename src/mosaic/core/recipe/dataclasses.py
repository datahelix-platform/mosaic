"""Recipe subsystem Python dataclasses.

Pure data containers — no behavior. Mirror the LinkML shapes declared in
``src/mosaic/schemas/recipe_manifest.yaml`` for Python-side ergonomics
inside :class:`mosaic.core.recipe_service.RecipeService` (Phase 3+).

The LinkML schema remains the source of truth for instance validation;
these dataclasses exist so the SDK can speak in typed Python instead of
dicts once the manifest has been validated.

See ``design/sec10_recipes.md`` §10.3 for per-field semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RecipeAuthor:
    """Contact metadata for the recipe author (sec10 §10.3.2)."""

    name: Optional[str] = None
    email: Optional[str] = None
    organization: Optional[str] = None


@dataclass(frozen=True)
class RecipeRef:
    """Typed reference to a recipe (sec10 §10.3.3).

    Reused for ``RecipeManifest.parent``, each entry of
    ``RecipeManifest.requires.recipes``, and the shape stored in
    ``mosaic_meta.installed_recipes``.

    ``digest`` is required at install time when ``source`` begins with
    ``https:``; for ``file:`` sources it is optional in the manifest but
    is always computed and recorded on install (sec10 invariant 4).
    """

    id: str
    version: str
    source: str
    digest: Optional[str] = None


@dataclass(frozen=True)
class RecipeRequires:
    """Dependency block on a manifest (sec10 §10.3.2)."""

    recipes: tuple[RecipeRef, ...] = ()
    reference_loaders: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecipeManifest:
    """Parsed ``recipe.yaml`` document (sec10 §10.3.2)."""

    id: str
    name: str
    version: str
    created_at: str
    mosaic_version: str
    description: Optional[str] = None
    author: Optional[RecipeAuthor] = None
    license: Optional[str] = None
    source: Optional[str] = None
    parent: Optional[RecipeRef] = None
    requires: RecipeRequires = field(default_factory=RecipeRequires)


@dataclass(frozen=True)
class InstalledRecipe:
    """An entry in ``mosaic_meta.installed_recipes`` (sec10 §10.3.4).

    Extends :class:`RecipeRef`'s shape with the install-time-captured
    fields. ``digest`` is always present after install (computed for
    ``file:`` sources, verified for ``https:`` sources). ``parent`` is
    carried from the manifest for audit.
    """

    id: str
    version: str
    source: str
    digest: str
    installed_at: str
    parent: Optional[RecipeRef] = None


@dataclass(frozen=True)
class RecipeReport:
    """Output of :meth:`RecipeService.inspect` (sec10 §10.2.3).

    Read-only summary returned by ``mosaic recipe inspect``: the parsed
    manifest, the computed digest over the canonical content hash, and
    a list of class / slot names the embedded ``schema.yaml`` would
    contribute on import. No DB writes occur to produce a report.
    """

    manifest: RecipeManifest
    digest: str
    classes: tuple[str, ...] = ()
    slots: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecipeDiff:
    """Structural diff between two recipes (sec10 §10.2.3).

    Class- and slot-level deltas only; produced by
    ``mosaic recipe diff <a> <b>``.
    """

    classes_added: tuple[str, ...] = ()
    classes_removed: tuple[str, ...] = ()
    classes_changed: tuple[str, ...] = ()
    slots_added: tuple[str, ...] = ()
    slots_removed: tuple[str, ...] = ()
    slots_changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportPlan:
    """Resolved bottom-up install order (sec10 §10.4.4).

    Produced during ``RecipeService.import_`` before any state writes.
    ``order`` is the dependency-resolved sequence
    (``parent → requires.recipes → self``); ``manifest`` is the
    top-level recipe the caller passed in.
    """

    manifest: RecipeManifest
    order: tuple[RecipeRef, ...] = ()


@dataclass(frozen=True)
class RecipeExport:
    """Output of :meth:`RecipeService.export` (sec10 §10.5).

    Selective package of the locally-authored content of the live
    schema: classes/slots whose ``provided_by`` annotation is absent
    or doesn't start with ``recipe.`` or ``loader.``, AND whose
    ``from_schema`` is not a bundled framework schema (Mosaic core
    classes do not get re-exported).

    The export is a pair of YAML documents the CLI writes to disk:
    ``manifest`` is the ``recipe.yaml`` contents (with author-fillable
    stubs for ``id``/``name``/``version``), ``schema_fragment`` is the
    ``schema.yaml`` body. ``auto_resolved_requires`` lists the
    upstream recipes the exported content references via ``is_a:`` or
    slot ranges — emitted into ``manifest.requires.recipes`` so the
    export round-trips on a peer instance that has the same upstream
    recipes installed.
    """

    manifest: dict
    schema_fragment: dict
    auto_resolved_requires: tuple[RecipeRef, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one top-level ``RecipeService.import_`` call (sec10 §10.2.3).

    ``installed`` is the list of recipes actually merged in this
    invocation, in install order. ``classes_added`` / ``slots_added``
    are the qualified names contributed by the top-level recipe (used
    when emitting the ``recipe_imported`` provenance event, sec10
    §10.8.2). ``dry_run`` mirrors the call-site flag so callers can
    branch downstream without re-passing it.
    """

    installed: tuple[InstalledRecipe, ...] = ()
    classes_added: tuple[str, ...] = ()
    slots_added: tuple[str, ...] = ()
    dry_run: bool = False
