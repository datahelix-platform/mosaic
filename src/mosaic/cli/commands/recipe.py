"""``mosaic recipe`` CLI subcommands (sec10 §10.2.2).

Phase 3 PR 5 (PTS-291) ships ``inspect`` only. ``import``, ``export``,
``extend``, ``diff``, ``export-lockfile``, and ``install-from-lockfile``
follow in PRs 6–8 and Phase 4.

CLI handlers stay thin: they construct a :class:`MosaicClient` (or
:class:`RecipeService` directly for read-only verbs) and forward to
the SDK. Output formatting is the CLI's concern; SDK return shapes
are typed dataclasses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mosaic.core.exceptions import (
    MosaicError,
    RecipeDigestMismatchError,
    RecipeFetchError,
    RecipeLineageCycleError,
    RecipeManifestError,
    RecipeRequiresUnsatisfiedError,
    RecipeSchemaError,
    RecipeVersionIncompatibleError,
)
from mosaic.core.recipe_service import RecipeService


recipe_app = typer.Typer(
    name="recipe",
    help="Manage Mosaic recipes — declarative LinkML schema bundles.",
    no_args_is_help=True,
)


@recipe_app.command(name="inspect")
def recipe_inspect(
    source: str = typer.Argument(
        ...,
        help=(
            "Recipe source: a directory path, a tarball path, or a "
            "file:/https: URI. https: support lands in PR 6."
        ),
    ),
    show_elements: bool = typer.Option(
        False,
        "--show-elements",
        help="Also list every class and slot the embedded schema declares.",
    ),
) -> None:
    """Parse, validate, and digest a recipe without any state change.

    Reads ``recipe.yaml`` and ``schema.yaml`` from ``source``, validates
    the manifest against the bundled ``recipe_manifest.yaml`` LinkML
    schema, computes the recipe's canonical content-hash digest
    (sec10 §10.4.3), and prints a one-screen summary.

    No DB writes, no provenance, no cache writes. This is the
    authoring-and-pre-flight verb.
    """
    service = RecipeService()
    try:
        report = service.inspect(source)
    except RecipeManifestError as e:
        typer.echo(f"Error: invalid manifest at {e.source}: {e.message}", err=True)
        for msg in e.errors:
            typer.echo(f"  - {msg}", err=True)
        raise typer.Exit(1)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    manifest = report.manifest
    typer.echo(f"id:           {manifest.id}")
    typer.echo(f"name:         {manifest.name}")
    typer.echo(f"version:      {manifest.version}")
    typer.echo(f"mosaic:       {manifest.mosaic_version}")
    typer.echo(f"created_at:   {manifest.created_at}")
    if manifest.description:
        typer.echo(f"description:  {manifest.description}")
    if manifest.license:
        typer.echo(f"license:      {manifest.license}")
    if manifest.parent is not None:
        typer.echo(
            f"parent:       {manifest.parent.id}@{manifest.parent.version} "
            f"({manifest.parent.source})"
        )
    if manifest.requires.recipes:
        typer.echo("requires.recipes:")
        for ref in manifest.requires.recipes:
            typer.echo(f"  - {ref.id}@{ref.version} ({ref.source})")
    if manifest.requires.reference_loaders:
        typer.echo("requires.reference_loaders:")
        for pin in manifest.requires.reference_loaders:
            typer.echo(f"  - {pin}")

    typer.echo(f"digest:       sha256:{report.digest}")
    typer.echo(f"classes:      {len(report.classes)}")
    typer.echo(f"slots:        {len(report.slots)}")

    if show_elements:
        if report.classes:
            typer.echo("\nclasses:")
            for name in report.classes:
                typer.echo(f"  - {name}")
        if report.slots:
            typer.echo("\nslots:")
            for name in report.slots:
                typer.echo(f"  - {name}")


@recipe_app.command(name="import")
def recipe_import(
    source: str = typer.Argument(
        ...,
        help=(
            "Recipe source: a directory path, tarball, or file:/https: URI. "
            "https: sources require a declared digest (sec10 invariant 4)."
        ),
    ),
    digest: Optional[str] = typer.Option(
        None,
        "--digest",
        help=(
            "Declared canonical-content-hash digest (sha256 hex, optionally "
            "prefixed `sha256:`). Required for https: sources at install time."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve dependencies and validate without writing any state.",
    ),
    db_path: str = typer.Option(
        None,
        "--db-path",
        help="SQLite database path (default: data/mosaic.db).",
    ),
    schema_dir: str = typer.Option(
        None,
        "--schema-dir",
        help="Schema directory (default: schemas/).",
    ),
) -> None:
    """Install a recipe and its dependencies into the current instance.

    Resolves every ``parent`` and ``requires.recipes`` entry bottom-up,
    merges each fragment through ``SchemaManager.merge_fragment``,
    writes one ``installed_recipes`` entry per recipe, and emits one
    ``recipe_imported`` provenance event per recipe — all inside a
    single storage transaction (sec10 §10.4 / invariant 3).
    """
    from mosaic.cli.main import _get_client

    try:
        client = _get_client(
            db_path=db_path,
            schema_path=schema_dir,
        )
    except Exception as exc:
        typer.echo(f"Error: failed to open Mosaic instance: {exc}", err=True)
        raise typer.Exit(1)

    try:
        result = client.recipe_import(
            source,
            dry_run=dry_run,
            expected_digest=digest,
        )
    except RecipeLineageCycleError as e:
        typer.echo(f"Error: lineage cycle: {' -> '.join(e.cycle)}", err=True)
        raise typer.Exit(1)
    except RecipeRequiresUnsatisfiedError as e:
        typer.echo(f"Error: unsatisfied requires: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeVersionIncompatibleError as e:
        typer.echo(f"Error: incompatible mosaic_version: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeDigestMismatchError as e:
        typer.echo(f"Error: digest mismatch: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeFetchError as e:
        typer.echo(f"Error: fetch failed: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeManifestError as e:
        typer.echo(f"Error: invalid manifest at {e.source}: {e.message}", err=True)
        for msg in e.errors:
            typer.echo(f"  - {msg}", err=True)
        raise typer.Exit(1)
    except RecipeSchemaError as e:
        typer.echo(f"Error: schema merge rejected: {e.message}", err=True)
        raise typer.Exit(1)
    except MosaicError as e:
        typer.echo(f"Error: {e.message}", err=True)
        raise typer.Exit(1)

    action = "[dry-run] Would install" if result.dry_run else "Installed"
    typer.echo(f"{action} {len(result.installed)} recipe(s) in dependency order:")
    for rec in result.installed:
        typer.echo(f"  - {rec.id}@{rec.version} (sha256:{rec.digest})")
    if result.classes_added:
        typer.echo(f"  classes added by top-level: {len(result.classes_added)}")
    if result.slots_added:
        typer.echo(f"  slots added by top-level: {len(result.slots_added)}")


@recipe_app.command(name="export")
def recipe_export(
    out: str = typer.Option(
        ...,
        "--out",
        help=(
            "Output directory. recipe.yaml and schema.yaml are written "
            "here; the directory must not already contain either file."
        ),
    ),
    parent: Optional[str] = typer.Option(
        None,
        "--parent",
        help=(
            "id of an installed recipe to declare as the parent of the "
            "exported recipe. Must match an entry from `mosaic recipe list`."
        ),
    ),
    db_path: str = typer.Option(
        None,
        "--db-path",
        help="SQLite database path (default: data/mosaic.db).",
    ),
    schema_dir: str = typer.Option(
        None,
        "--schema-dir",
        help="Schema directory (default: schemas/).",
    ),
) -> None:
    """Export locally-authored schema as a redistributable recipe (sec10 §10.5).

    Selects classes/slots whose ``provided_by`` is absent or doesn't
    start with ``recipe.``/``loader.``, AND whose ``from_schema`` is
    not a bundled framework schema. Auto-populates ``requires.recipes``
    from the ``provided_by`` of any upstream ``is_a:`` ancestor or
    slot range. Writes ``recipe.yaml`` (with author-fillable stubs for
    id/name/version — adjust before publishing) and ``schema.yaml``.
    """
    import yaml as _yaml
    from mosaic.cli.main import _get_client

    out_dir = Path(out)
    if out_dir.exists() and not out_dir.is_dir():
        typer.echo(f"Error: --out path is not a directory: {out_dir}", err=True)
        raise typer.Exit(1)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / "recipe.yaml"
    schema_path = out_dir / "schema.yaml"
    for p in (manifest_path, schema_path):
        if p.exists():
            typer.echo(
                f"Error: {p} already exists; refusing to overwrite.", err=True
            )
            raise typer.Exit(1)

    try:
        client = _get_client(db_path=db_path, schema_path=schema_dir)
    except Exception as exc:
        typer.echo(f"Error: failed to open Mosaic instance: {exc}", err=True)
        raise typer.Exit(1)

    try:
        result = client.recipe_export(parent=parent)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    manifest_path.write_text(
        _yaml.safe_dump(result.manifest, sort_keys=False)
    )
    schema_path.write_text(
        _yaml.safe_dump(result.schema_fragment, sort_keys=False)
    )

    typer.echo(f"Wrote {manifest_path}")
    typer.echo(f"Wrote {schema_path}")
    classes = (result.schema_fragment.get("classes") or {})
    slots = (result.schema_fragment.get("slots") or {})
    typer.echo(f"  classes exported: {len(classes)}")
    typer.echo(f"  slots exported:   {len(slots)}")
    if result.auto_resolved_requires:
        typer.echo(
            f"  requires.recipes auto-populated: "
            f"{len(result.auto_resolved_requires)}"
        )
        for ref in result.auto_resolved_requires:
            typer.echo(f"    - {ref.id}@{ref.version}")
    typer.echo(
        "\nNote: replace `id`, `name`, `version`, and the schema "
        "`id`/`name`/`default_prefix` stubs before sharing."
    )


@recipe_app.command(name="extend")
def recipe_extend(
    installed_id: str = typer.Argument(
        ...,
        help=(
            "id of an installed recipe to extend (matches an entry from "
            "`mosaic recipe list`)."
        ),
    ),
    out: str = typer.Option(
        ...,
        "--out",
        help=(
            "Output directory. recipe.yaml and schema.yaml are written "
            "here; the directory must not already contain either file."
        ),
    ),
    db_path: str = typer.Option(
        None,
        "--db-path",
        help="SQLite database path (default: data/mosaic.db).",
    ),
    schema_dir: str = typer.Option(
        None,
        "--schema-dir",
        help="Schema directory (default: schemas/).",
    ),
) -> None:
    """Scaffold a derivative recipe directory (sec10 §10.7.3).

    Writes ``recipe.yaml`` whose ``parent`` is populated from the
    matching ``installed_recipes`` entry, plus an empty ``schema.yaml``
    ready for local additions. This is the ONLY operation that creates
    a ``parent`` lineage pointer (invariant 5).
    """
    from mosaic.cli.main import _get_client

    try:
        client = _get_client(db_path=db_path, schema_path=schema_dir)
    except Exception as exc:
        typer.echo(f"Error: failed to open Mosaic instance: {exc}", err=True)
        raise typer.Exit(1)

    try:
        out_dir = client.recipe_extend(installed_id, Path(out))
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Scaffolded {out_dir / 'recipe.yaml'}")
    typer.echo(f"Scaffolded {out_dir / 'schema.yaml'}")
    typer.echo(
        "\nNote: replace `id`, `name`, `version`, and the schema "
        "`id`/`name`/`default_prefix` stubs before importing."
    )


@recipe_app.command(name="diff")
def recipe_diff(
    a: str = typer.Argument(
        ...,
        help="First recipe source: path, tarball, or file:/https: URI.",
    ),
    b: str = typer.Argument(
        ...,
        help="Second recipe source: path, tarball, or file:/https: URI.",
    ),
) -> None:
    """Structural diff between two recipes' schemas (sec10 §10.2.3).

    Reports classes and slots added (present only in ``b``), removed
    (present only in ``a``), and changed (present in both but with
    different bodies). No DB writes, no merge — both sides are read
    directly from their ``schema.yaml`` after the resolver chain
    fetches and validates them.
    """
    service = RecipeService()
    try:
        diff = service.diff(a, b)
    except RecipeManifestError as e:
        typer.echo(f"Error: invalid manifest at {e.source}: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeFetchError as e:
        typer.echo(f"Error: fetch failed: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeDigestMismatchError as e:
        typer.echo(f"Error: digest mismatch: {e.message}", err=True)
        raise typer.Exit(1)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    no_changes = (
        not diff.classes_added
        and not diff.classes_removed
        and not diff.classes_changed
        and not diff.slots_added
        and not diff.slots_removed
        and not diff.slots_changed
    )
    if no_changes:
        typer.echo("No structural differences.")
        return

    def _section(label: str, names: tuple[str, ...]) -> None:
        if not names:
            return
        typer.echo(f"{label}: {len(names)}")
        for name in names:
            typer.echo(f"  - {name}")

    _section("classes added", diff.classes_added)
    _section("classes removed", diff.classes_removed)
    _section("classes changed", diff.classes_changed)
    _section("slots added", diff.slots_added)
    _section("slots removed", diff.slots_removed)
    _section("slots changed", diff.slots_changed)


@recipe_app.command(name="export-lockfile")
def recipe_export_lockfile(
    out: str = typer.Option(
        "recipe.lock.yaml",
        "--out",
        help="Destination path for the lockfile (default: recipe.lock.yaml).",
    ),
    db_path: str = typer.Option(
        None,
        "--db-path",
        help="SQLite database path (default: data/mosaic.db).",
    ),
    schema_dir: str = typer.Option(
        None,
        "--schema-dir",
        help="Schema directory (default: schemas/).",
    ),
) -> None:
    """Dump ``installed_recipes`` as ``recipe.lock.yaml`` (sec10 §10.6).

    Writes a portable YAML document with ``lockfile_version: 1`` and
    one entry per installed recipe — the input artifact for
    ``mosaic recipe install-from-lockfile`` on a peer instance.
    """
    from mosaic.cli.main import _get_client

    try:
        client = _get_client(db_path=db_path, schema_path=schema_dir)
    except Exception as exc:
        typer.echo(f"Error: failed to open Mosaic instance: {exc}", err=True)
        raise typer.Exit(1)

    out_path = client.recipe_export_lockfile(Path(out))
    installed = client.recipe_list()
    typer.echo(f"Wrote {out_path}")
    typer.echo(f"  installed_recipes entries: {len(installed)}")


@recipe_app.command(name="install-from-lockfile")
def recipe_install_from_lockfile(
    lockfile: str = typer.Argument(
        ...,
        help="Path to a recipe.lock.yaml document.",
    ),
    db_path: str = typer.Option(
        None,
        "--db-path",
        help="SQLite database path (default: data/mosaic.db).",
    ),
    schema_dir: str = typer.Option(
        None,
        "--schema-dir",
        help="Schema directory (default: schemas/).",
    ),
) -> None:
    """Replay a ``recipe.lock.yaml`` on the current instance (sec10 §10.6).

    Iterates entries in dependency order (parents before children),
    fetching each via its ``source``, verifying its ``digest``, and
    installing through ``import_``. Relative ``source`` paths resolve
    against the lockfile's directory.
    """
    from mosaic.cli.main import _get_client

    try:
        client = _get_client(db_path=db_path, schema_path=schema_dir)
    except Exception as exc:
        typer.echo(f"Error: failed to open Mosaic instance: {exc}", err=True)
        raise typer.Exit(1)

    try:
        results = client.recipe_install_from_lockfile(Path(lockfile))
    except RecipeLineageCycleError as e:
        typer.echo(f"Error: lineage cycle: {' -> '.join(e.cycle)}", err=True)
        raise typer.Exit(1)
    except RecipeRequiresUnsatisfiedError as e:
        typer.echo(f"Error: unsatisfied requires: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeVersionIncompatibleError as e:
        typer.echo(f"Error: incompatible mosaic_version: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeDigestMismatchError as e:
        typer.echo(f"Error: digest mismatch: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeFetchError as e:
        typer.echo(f"Error: fetch failed: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeManifestError as e:
        typer.echo(f"Error: invalid manifest at {e.source}: {e.message}", err=True)
        raise typer.Exit(1)
    except RecipeSchemaError as e:
        typer.echo(f"Error: schema merge rejected: {e.message}", err=True)
        raise typer.Exit(1)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Installed {len(results)} lockfile entries:")
    for result in results:
        for rec in result.installed:
            typer.echo(f"  - {rec.id}@{rec.version} (sha256:{rec.digest})")
