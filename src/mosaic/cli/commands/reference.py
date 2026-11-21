"""Reference-loader CLI surface: presentation + transport only.

The install/upgrade/deprovision *lifecycle orchestration* (business logic,
no ``typer``/``argparse``) lives in :mod:`mosaic.core.loaders.lifecycle`
(moved there by issue #69, a follow-up to #67 / PR #68 which relocated
schema-package discovery to :mod:`mosaic.core.loaders.discovery`). This
module re-exports every moved name so existing
``mosaic.cli.commands.reference.<name>`` imports and test monkeypatch
targets keep working unchanged, and keeps only the genuinely transport-
bound pieces:

- argparse ``--flag`` rendering (``parse_load_params`` and helpers),
- typer sub-app mounting (``discover_reference_loader_subapps`` /
  ``mount_reference_loader_subapps``),
- breakdown table formatting (``render_breakdown`` and helpers),
- the cache verbs (``reference_cache_root`` / ``clean_reference_cache``).

See :mod:`mosaic.core.loaders.lifecycle` for the install/upgrade/
deprovision/migrate-bundle/exposure lifecycle itself (sec2 §2.14).
"""

from __future__ import annotations

import shutil
import sqlite3
from collections import Counter
from collections.abc import Sequence
import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from mosaic.core.loaders.reference import EntityRef, SchemaPackage

# Discovery + `requires:` resolution moved to the core SDK layer
# (mosaic.core.loaders.discovery) so the config-driven client factory can
# build a spanning registry without importing the CLI (issue #67). Re-exported
# here so existing `mosaic.cli.commands.reference.<name>` imports keep working.
from mosaic.core.loaders.discovery import (  # noqa: F401  (re-export)
    _CLI_FLAG_SUPPORTED_HELP,
    REFERENCE_LOADERS_GROUP,
    SCHEMA_PACKAGES_GROUP,
    ReferenceLoaderNotFoundError,
    ReferenceLoaderRegistrationError,
    SchemaPackageRegistrationError,
    _build_fragment_spec,
    _build_package_info,
    _classify_load_params_field,
    _normalize_dist_name,
    _resolve_distribution,
    _resolve_group_eps,
    _validate_load_params_schema,
    discover_reference_loaders,
    discover_schema_packages,
    find_loader,
    fragment_specs_for_requires,
)

# Install/upgrade/deprovision lifecycle orchestration moved to the core SDK
# layer (mosaic.core.loaders.lifecycle) so it's reachable without touching a
# transport module (issue #69). Re-exported here so existing
# `mosaic.cli.commands.reference.<name>` imports AND test monkeypatch targets
# (e.g. `refmod._load_deployed_registry`, `refmod._build_client`,
# `refmod._build_merged_registry`) keep resolving unchanged.
from mosaic.core.loaders.lifecycle import (  # noqa: F401  (re-export)
    META_KEY_VERSIONS,
    RESERVED_TEST_SLUG,
    _abort_on_load_errors,
    _build_client,
    _build_merged_registry,
    _find_installed_dependents,
    _has_meta_table,
    _has_write_log_table,
    _load_deployed_registry,
    _merge_fragment_into,
    _prune_all_via_write_log,
    _prune_via_write_log,
    _read_versions,
    _remove_version,
    _resolve_version,
    _write_versions,
    compute_exposure,
    deprovision_reference,
    install_reference,
    list_reference_loaders,
    migrate_bundle,
    upgrade_reference,
)

if TYPE_CHECKING:
    import typer

    from mosaic.core.client import MosaicClient
    from mosaic.linkml_bridge import SchemaRegistry


def reference_cache_root() -> Path:
    """Resolve the directory holding per-loader reference caches.

    Mirrors :meth:`MosaicClient._reference_cache_root` so the CLI can
    operate without instantiating a full client (sec2 §2.14.3,
    decision D2.14.E). ``$MOSAIC_CACHE_DIR`` wins when set; otherwise
    ``~/.cache/mosaic/references/``.
    """
    from mosaic.config.env import get_env

    env = get_env("CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "mosaic" / "references"


def clean_reference_cache(name: str | None = None) -> dict[str, Any]:
    """Remove cached reference-loader data.

    With ``name``, removes only that loader's cache subtree; other
    loaders are untouched. Without ``name``, removes the entire cache
    root. Missing targets are a silent no-op (idempotent) so the verb
    is safe to run on a fresh machine.
    """
    root = reference_cache_root()
    if name is not None:
        target = root / name
        existed = target.exists()
        if existed:
            shutil.rmtree(target)
        return {"removed": existed, "path": str(target), "scope": name}
    existed = root.exists()
    if existed:
        shutil.rmtree(root)
    return {"removed": existed, "path": str(root), "scope": None}


def get_references_dir() -> Path:
    """Return the on-disk references directory, creating it if needed.

    Kept for backwards-compatibility with the legacy installed.json
    bookkeeping. The lifecycle implemented here tracks installs in
    ``mosaic_meta`` instead.
    """
    data_dir = Path.home() / ".mosaic" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    refs_dir = data_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    return refs_dir


# Only ``mosaic.reference_loader_cli`` is read; the legacy ``hippo.*`` group
# was removed by ADR-0015.
REFERENCE_LOADER_CLI_GROUPS = ("mosaic.reference_loader_cli",)


def discover_reference_loader_subapps() -> list[tuple[str, "typer.Typer"]]:
    """Discover loader-provided Typer sub-apps via the
    ``mosaic.reference_loader_cli`` entry-point group (D2.14.A).

    Each entry point must resolve to a :class:`typer.Typer` instance.
    Anything else raises :class:`ReferenceLoaderRegistrationError` —
    consistent with the strict reference-loader registration on the
    sibling group (PTS-224). Sub-app registration is *optional*: a
    loader registered only under ``mosaic.reference_loaders`` simply
    won't surface here, leaving ``mosaic reference <name>`` to be served
    by the parent group's install/upgrade/list verbs. Entries are
    deduplicated by name, so a dual-registered sub-app mounts once.
    """
    import typer
    from importlib.metadata import entry_points

    eps_list = []
    seen: set[str] = set()
    for group in REFERENCE_LOADER_CLI_GROUPS:
        try:
            eps = entry_points()
            group_eps = list(eps.select(group=group))
        except (TypeError, AttributeError):
            try:
                eps = entry_points()
                group_eps = list(eps[group])
            except (KeyError, TypeError):
                group_eps = []
        for ep in group_eps:
            if ep.name in seen:
                continue
            seen.add(ep.name)
            eps_list.append(ep)

    subapps: list[tuple[str, typer.Typer]] = []
    for ep in eps_list:
        loaded = ep.load()
        if not isinstance(loaded, typer.Typer):
            raise ReferenceLoaderRegistrationError(
                f"Entry point 'mosaic.reference_loader_cli:{ep.name}' "
                f"({ep.value}) is not a typer.Typer instance"
            )
        subapps.append((ep.name, loaded))
    return subapps


def mount_reference_loader_subapps(reference_app: "typer.Typer") -> None:
    """Mount discovered loader sub-apps under ``reference_app`` (D2.14.A).

    Called at CLI startup so ``mosaic reference <loader> --help`` and
    ``mosaic reference <loader> <subcmd> ...`` reflect the loader's own
    Typer surface. Loaders without a ``mosaic.reference_loader_cli``
    entry are silently skipped — the install/upgrade/list verbs remain
    available on the parent group regardless.
    """
    for loader_name, sub in discover_reference_loader_subapps():
        reference_app.add_typer(sub, name=loader_name)


# ---------------------------------------------------------------------------
# load_params_schema → --flag rendering (D2.14.D). Field classification lives
# in mosaic.core.loaders.discovery (`_classify_load_params_field`); the argparse
# rendering below is the CLI half that consumes it.
# ---------------------------------------------------------------------------


def _field_default(field_info: Any) -> Any:
    """Resolve a Pydantic FieldInfo's default, handling default_factory.

    Returns :data:`pydantic_core.PydanticUndefined` when the field is
    truly required.
    """
    if field_info.default is not PydanticUndefined:
        return field_info.default
    if field_info.default_factory is not None:
        return field_info.default_factory()
    return PydanticUndefined


class _ListStrReplacingAppend(argparse.Action):
    """Append action that *replaces* the model default on first use.

    argparse's stock ``append`` extends whatever default was set, so a
    field declared as ``tags: list[str] = ["primary"]`` plus a user
    passing ``--tags secondary`` would yield ``["primary", "secondary"]``
    — surprising and wrong for our semantics. We want user-provided
    values to fully replace the default, while an omitted flag still
    produces the model default.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._user_seen = False

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if not self._user_seen:
            self._user_seen = True
            current: list[str] = []
        else:
            current = list(getattr(namespace, self.dest, None) or [])
        current.append(values)
        setattr(namespace, self.dest, current)


def _build_load_params_parser(
    schema_cls: type[BaseModel],
) -> argparse.ArgumentParser:
    """Build an argparse parser whose options mirror ``schema_cls`` fields.

    Field naming: ``my_field`` becomes ``--my-field``. Required fields
    stay required (argparse raises if omitted); optional fields carry the
    model's default so Pydantic doesn't need to recompute it.
    """
    parser = argparse.ArgumentParser(
        prog=f"<{schema_cls.__name__}>",
        add_help=False,
        exit_on_error=False,
    )
    for field_name, field_info in schema_cls.model_fields.items():
        flag = f"--{field_name.replace('_', '-')}"
        kind, base_type = _classify_load_params_field(field_info.annotation)
        default = _field_default(field_info)
        required = default is PydanticUndefined

        kwargs: dict[str, Any] = {"dest": field_name}
        if required:
            kwargs["required"] = True
        else:
            kwargs["default"] = default

        if kind == "bool":
            kwargs["action"] = argparse.BooleanOptionalAction
        elif kind == "list_str":
            kwargs["action"] = _ListStrReplacingAppend
            kwargs["type"] = str
        else:  # "str" / "int"
            assert base_type is not None
            kwargs["type"] = base_type

        parser.add_argument(flag, **kwargs)
    return parser


def parse_load_params(
    loader: SchemaPackage, extra_args: list[str]
) -> BaseModel | None:
    """Parse ``--<field>`` args against ``loader.load_params_schema``.

    Returns a validated model instance, or ``None`` when the loader
    declares no schema. Surfaces argparse and Pydantic errors as
    :class:`ValueError` so the Typer surface can convert them to a clean
    CLI exit code.
    """
    schema_cls = loader.load_params_schema
    if schema_cls is None:
        if extra_args:
            raise ValueError(
                f"Loader {loader.name!r} accepts no --flag arguments "
                f"(load_params_schema is None); got {extra_args!r}."
            )
        return None

    parser = _build_load_params_parser(schema_cls)
    try:
        namespace = parser.parse_args(extra_args)
    except argparse.ArgumentError as exc:
        raise ValueError(
            f"Invalid --flag arguments for loader {loader.name!r}: {exc}"
        ) from exc
    except SystemExit as exc:
        # Older argparse paths (notably required-arg errors before
        # exit_on_error landed across all code paths) raise SystemExit.
        # Convert so the CLI layer can render a single clean message.
        raise ValueError(
            f"Invalid --flag arguments for loader {loader.name!r} "
            f"(argparse exit {exc.code})."
        ) from exc

    try:
        return schema_cls(**vars(namespace))
    except ValidationError as exc:
        raise ValueError(
            f"Invalid --flag values for loader {loader.name!r}:\n{exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Per-entity_type breakdown rendering for the install/upgrade CLI surface
# (sec2 §2.14.8 advisory contract, decisions D2.14.J / D2.14.K).
# ---------------------------------------------------------------------------


def _resolve_breakdown_counts(
    entities: Sequence[EntityRef],
    db_path: Path,
    loader_name: str,
    version: str,
) -> Counter:
    """Choose the source for the per-``entity_type`` count breakdown.

    Honours the advisory contract: when the loader populated
    ``LoadResult.entities`` we count those directly; when it left the
    list empty (large-loader pattern) we fall back to a ``GROUP BY``
    over ``reference_write_log`` scoped to ``(loader_name, version)``.
    Both branches return a :class:`Counter` so the renderer is source-
    agnostic.
    """
    if entities:
        return Counter(e.type for e in entities)
    return _query_write_log_counts(db_path, loader_name, version)


def _query_write_log_counts(
    db_path: Path, loader_name: str, version: str
) -> Counter:
    """Return ``Counter({entity_type: count})`` from ``reference_write_log``.

    Used by the install/upgrade printers when ``LoadResult.entities``
    came back empty — the write log is the authoritative substrate for
    "what got written under ``(loader, version)``" per sec2 §2.14.9.
    A missing db (e.g., a dry-run that never opened it) yields an empty
    Counter so callers can render a stand-alone ``total`` line.
    """
    if not db_path.exists():
        return Counter()
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        if not _has_write_log_table(cursor):
            return Counter()
        cursor.execute(
            "SELECT entity_type, COUNT(*) FROM reference_write_log "
            "WHERE loader_name = ? AND version = ? "
            "GROUP BY entity_type",
            (loader_name, version),
        )
        return Counter({row[0]: row[1] for row in cursor.fetchall()})
    finally:
        conn.close()


def render_breakdown(header: str, counts: Counter, total: int) -> str:
    """Format a multi-line install/upgrade summary with per-type counts.

    ``header`` is the leading status line (without trailing colon, e.g.
    ``"Installed fake@v1"``); ``counts`` is a :class:`Counter` of
    ``entity_type → row_count``; ``total`` is the scalar preserved from
    ``LoadResult.created`` so the bottom line matches v1 output even
    when the breakdown source disagrees (advisory drift).

    Rows sort by ``(count desc, type asc)`` so the entities-path and the
    write-log-path render byte-identically for the same dataset — the
    acceptance criterion calls that parity out explicitly.
    """
    label_width = max(
        (len(name) for name in counts),
        default=0,
    )
    label_width = max(label_width, len("total"))
    count_width = max(
        (len(f"{count:,}") for count in counts.values()),
        default=0,
    )
    count_width = max(count_width, len(f"{total:,}"))

    lines = [f"{header}:"]
    for entity_type, count in sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append(
            f"  {entity_type:<{label_width}}  {count:>{count_width},}"
        )
    lines.append(f"  {'total':<{label_width}}  {total:>{count_width},}")
    return "\n".join(lines)
