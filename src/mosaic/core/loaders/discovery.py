"""Schema-package / reference-loader discovery and ``requires:`` resolution.

Core (SDK-layer) business logic for finding the schema packages installed
in the current environment and turning a user schema's ``requires:`` block
into the loader fragments that span it. These helpers carry no CLI
dependency (no ``typer``/``argparse``); the ``mosaic reference`` CLI verbs
and the config-driven client factory both build on top of them.

Discovery resolves two entry-point groups (Doc 2 §2A): the broad
``mosaic.schema_packages`` genus group and the ``mosaic.reference_loaders``
subset/alias carrying the external-data species. ``fragment_specs_for_requires``
is the bridge between the ``requires:`` directive (:mod:`mosaic.requires`) and
this discovery surface — it is what lets a consumer obtain a registry/client
spanning its own schema *and* an installed reference loader with no
hand-assembled registry code (issue #67).

The CLI module ``mosaic.cli.commands.reference`` re-exports the public names
here for backwards compatibility, so existing imports of
``mosaic.cli.commands.reference.discover_schema_packages`` (etc.) keep working.
"""

from __future__ import annotations

import re
import types as _types
import warnings
from importlib.metadata import (
    PackageNotFoundError,
    distributions,
    version as _dist_version,
)
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from mosaic.core.loaders.reference import ReferenceLoader, SchemaPackage

# Entry-point groups (Doc 2 §2A). ``mosaic.schema_packages`` is the broad
# genus group; ``mosaic.reference_loaders`` is a subset/alias carrying the
# external-data species. Discovery resolves both so a package registered
# under either is found, and dedups by name (genus group canonical).
#
# The legacy ``hippo.*`` spellings are no longer read (ADR-0015).
SCHEMA_PACKAGES_GROUPS = ("mosaic.schema_packages",)
REFERENCE_LOADERS_GROUPS = ("mosaic.reference_loaders",)
#: Canonical group names.
SCHEMA_PACKAGES_GROUP = SCHEMA_PACKAGES_GROUPS[0]
REFERENCE_LOADERS_GROUP = REFERENCE_LOADERS_GROUPS[0]


class SchemaPackageRegistrationError(TypeError):
    """Raised when a ``mosaic.schema_packages`` entry point does not
    resolve to a concrete :class:`SchemaPackage` subclass."""


class ReferenceLoaderRegistrationError(SchemaPackageRegistrationError):
    """Raised when a ``mosaic.reference_loaders`` entry point does not
    resolve to a concrete :class:`ReferenceLoader` subclass.

    Subclasses :class:`SchemaPackageRegistrationError` so callers that
    catch the genus error also catch the reference-loader variant.
    """


class ReferenceLoaderNotFoundError(KeyError):
    """Raised when no schema-package / reference-loader entry point
    matches a requested package name."""


def _resolve_group_eps(group: str) -> list[Any]:
    """Return the entry points registered under ``group``.

    Handles the ``select(group=...)`` API and the legacy ``eps[group]``
    mapping access across ``importlib.metadata`` versions; an absent
    group yields an empty list.
    """
    from importlib.metadata import entry_points

    try:
        eps = entry_points()
        return list(eps.select(group=group))
    except (TypeError, AttributeError):
        try:
            eps = entry_points()
            return list(eps[group])
        except (KeyError, TypeError):
            return []


def _build_package_info(
    ep: Any,
    *,
    group: str,
    base: type,
    base_fqn: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Validate, instantiate, and describe a single package entry point.

    ``base`` is the required superclass (``ReferenceLoader`` or
    :class:`SchemaPackage`); a mismatch raises ``error_cls`` with a
    message naming the offending entry point and the expected base.
    """
    loaded = ep.load()
    if not (isinstance(loaded, type) and issubclass(loaded, base)):
        raise error_cls(
            f"Entry point '{group}:{ep.name}' ({ep.value}) is not a "
            f"subclass of {base_fqn}"
        )
    instance = loaded()
    # D2.14.D — validate at registration that every field on the declared
    # `load_params_schema` (if any) can be rendered as a `--flag` arg.
    # Packages that ship an unsupported field type fail loud here, not at
    # first CLI invocation.
    if loaded.load_params_schema is not None:
        _validate_load_params_schema(ep.name, loaded.load_params_schema)
    package_name, package_version = _resolve_distribution(loaded.__module__)
    return {
        "name": ep.name,
        "entry_point": ep.name,
        "class": loaded.__name__,
        "module": loaded.__module__,
        "package_name": package_name,
        "package_version": package_version,
        "description": getattr(instance, "description", ""),
        "instance": instance,
    }


def discover_reference_loaders() -> list[dict[str, Any]]:
    """Discover and instantiate reference loaders via entry points.

    Each entry point in the ``mosaic.reference_loaders`` group must point
    at a concrete :class:`ReferenceLoader` subclass. The class is
    instantiated eagerly so that callers receive a ready-to-use loader
    surface; an entry point pointing at anything else raises
    :class:`ReferenceLoaderRegistrationError` with a message identifying
    the offending entry point. Entries are deduplicated by name.
    """
    loaders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in REFERENCE_LOADERS_GROUPS:
        for ep in _resolve_group_eps(group):
            if ep.name in seen:
                continue
            seen.add(ep.name)
            loaders.append(
                _build_package_info(
                    ep,
                    group=group,
                    base=ReferenceLoader,
                    base_fqn="mosaic.core.loaders.reference.ReferenceLoader",
                    error_cls=ReferenceLoaderRegistrationError,
                )
            )
    return loaders


def discover_schema_packages() -> list[dict[str, Any]]:
    """Discover and instantiate every :class:`SchemaPackage` via entry points.

    Resolves the broad ``mosaic.schema_packages`` group **and** the
    ``mosaic.reference_loaders`` subset/alias (Doc 2 §2A), so a package
    registered under either is found. Entries are deduplicated by name with the
    genus group canonical: the ``schema_packages`` group is scanned
    first, then the ``reference_loaders`` group contributes only the
    names not already seen. Each entry point must resolve to a concrete
    :class:`SchemaPackage` subclass; anything else raises
    :class:`SchemaPackageRegistrationError`.
    """
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (*SCHEMA_PACKAGES_GROUPS, *REFERENCE_LOADERS_GROUPS):
        for ep in _resolve_group_eps(group):
            if ep.name in seen:
                continue
            seen.add(ep.name)
            packages.append(
                _build_package_info(
                    ep,
                    group=group,
                    base=SchemaPackage,
                    base_fqn="mosaic.core.loaders.schema_package.SchemaPackage",
                    error_cls=SchemaPackageRegistrationError,
                )
            )
    return packages


def find_loader(name: str) -> dict[str, Any]:
    """Look up a single schema-package info dict by entry-point name.

    Resolves both the ``mosaic.schema_packages`` group and its
    ``mosaic.reference_loaders`` subset/alias, so reference loaders and
    pure-schema packages are equally discoverable. Raises
    :class:`ReferenceLoaderNotFoundError` with a clear message when no
    package is registered under ``name``.
    """
    for info in discover_schema_packages():
        if info["name"] == name:
            return info
    raise ReferenceLoaderNotFoundError(
        f"No schema package registered under name {name!r}. "
        f"Install the corresponding package or check the "
        f"``mosaic.schema_packages`` / ``mosaic.reference_loaders`` entry "
        f"point in its pyproject.toml."
    )


def _resolve_distribution(module: str) -> tuple[str, str]:
    """Best-effort lookup of the package name + version that ships ``module``.

    Falls back to ``("<unknown>", "0")`` when the module isn't owned by
    a discoverable distribution (e.g., running from a source checkout).
    """
    # Walk the installed distributions and find the one that owns the
    # top-level package of ``module``. The mapping API isn't always
    # reliable across Python versions; this loop is small and explicit.
    top_pkg = module.split(".", 1)[0]
    for dist in distributions():
        try:
            top_level = dist.read_text("top_level.txt") or ""
        except FileNotFoundError:
            top_level = ""
        top_names = {line.strip() for line in top_level.splitlines() if line.strip()}
        if top_pkg in top_names or dist.metadata["Name"].replace("-", "_") == top_pkg:
            return dist.metadata["Name"], dist.version
    try:
        return top_pkg, _dist_version(top_pkg)
    except PackageNotFoundError:
        return top_pkg, "0"


# ---------------------------------------------------------------------------
# load_params_schema field classification (D2.14.D). Pure type inspection —
# the argparse `--flag` rendering that consumes it lives in the CLI layer.
# ---------------------------------------------------------------------------


_CLI_FLAG_SUPPORTED_HELP = "str, int, bool, list[str], or Optional thereof"


def _classify_load_params_field(
    annotation: Any,
) -> tuple[str, type | None]:
    """Classify a Pydantic field annotation for CLI rendering.

    Returns a ``(kind, base_type)`` pair. ``kind`` is one of:

    - ``"str"`` / ``"int"`` — scalar arg; ``base_type`` is the converter.
    - ``"bool"`` — rendered as ``--<name>`` / ``--no-<name>``.
    - ``"list_str"`` — rendered as repeated ``--<name>`` args.
    - ``"unsupported"`` — out of scope for v1.

    Optional[T] unwraps to T; Optional[list[str]] is treated as list[str]
    (argparse can carry ``default=None`` for that case).
    """
    inner = annotation
    if get_origin(inner) in (Union, _types.UnionType):
        non_none = [arg for arg in get_args(inner) if arg is not type(None)]
        if len(non_none) == 1:
            inner = non_none[0]
        else:
            return ("unsupported", None)

    if inner is str:
        return ("str", str)
    if inner is int:
        return ("int", int)
    if inner is bool:
        return ("bool", bool)
    if get_origin(inner) is list:
        list_args = get_args(inner)
        if len(list_args) == 1 and list_args[0] is str:
            return ("list_str", str)
    return ("unsupported", None)


def _validate_load_params_schema(
    loader_name: str, schema_cls: type[BaseModel]
) -> None:
    """Raise if any field of ``schema_cls`` can't be rendered as a CLI flag.

    The error message names both the offending field and the type so the
    loader author can fix it at registration time rather than discovering
    the problem when an end user runs ``mosaic reference install``.
    """
    for field_name, field_info in schema_cls.model_fields.items():
        kind, _ = _classify_load_params_field(field_info.annotation)
        if kind == "unsupported":
            raise ReferenceLoaderRegistrationError(
                f"Reference loader {loader_name!r}: field {field_name!r} on "
                f"{schema_cls.__name__} has type {field_info.annotation!r}, "
                f"which is not supported for CLI rendering. "
                f"Supported: {_CLI_FLAG_SUPPORTED_HELP}."
            )


# ---------------------------------------------------------------------------
# requires: → installed loader fragments (issue #67).
# ---------------------------------------------------------------------------


def _build_fragment_spec(info: dict[str, Any], loader: SchemaPackage):
    from mosaic.linkml_bridge import LoaderFragmentSpec

    return LoaderFragmentSpec(
        loader_name=loader.name,
        package_name=info["package_name"],
        package_version=info["package_version"],
        fragment=loader.schema_fragment(),
    )


def _normalize_dist_name(name: str) -> str:
    """PEP 503-style normalization for matching distribution names.

    ``Mosaic-Reference-Ensembl``, ``hippo_reference_ensembl``, and
    ``hippo-reference-ensembl`` all collapse to the same key so a
    ``requires:`` pin matches the discovered package regardless of the
    dash/underscore/case spelling each side happens to use.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def fragment_specs_for_requires(
    schema_path: str | Path,
    *,
    check_versions: bool = True,
) -> list["LoaderFragmentSpec"]:
    """Resolve a schema's ``requires:`` pins to installed loader fragments.

    The public bridge between the ``requires:`` directive
    (:mod:`mosaic.requires`) and schema-package discovery, closing the
    "no public path to a spanning client" gap (issue #67). It:

    1. reads the ``requires:`` pins declared in ``schema_path`` (file or
       directory) via :func:`mosaic.requires.extract_requires`;
    2. optionally re-runs the installed-version gate
       (:func:`mosaic.requires.check_requires`) — the same check
       ``mosaic validate`` applies — raising on a missing loader or
       version mismatch;
    3. resolves each pin to a discoverable, installed schema package
       (matched by distribution name first, then by entry-point/short
       name) and builds its :class:`~mosaic.linkml_bridge.LoaderFragmentSpec`.

    The returned list is ready to hand to
    :meth:`SchemaRegistry.with_loader_fragments`, so a consumer's client
    registry can span its own schema *and* every reference loader it
    declares — with no hand-assembled registry code.

    Returns an empty list when the schema declares no ``requires:``.
    Raises :class:`~mosaic.core.exceptions.SchemaError` when the version
    gate fails (loader missing or version mismatch).

    A pin that passes the version gate but exposes no discoverable schema
    package (the distribution is installed yet registers no
    ``mosaic.schema_packages`` / ``mosaic.reference_loaders`` entry point)
    contributes no fragment and emits a :class:`UserWarning`. The version
    gate is the authoritative ``requires:`` contract (sec2 §2.14.1); a
    distribution may legitimately be pinned without shipping a mergeable
    fragment, so this is a warning rather than a hard error.
    """
    from mosaic.requires import check_requires, extract_requires
    from mosaic.core.exceptions import SchemaError

    pins = extract_requires(schema_path)
    if not pins:
        return []

    if check_versions:
        errors = check_requires(pins)
        if errors:
            raise SchemaError(
                f"{len(errors)} unsatisfied `requires:` pin(s):\n  - "
                + "\n  - ".join(errors),
                field_name="requires",
                error_code="REQUIRES_UNSATISFIED",
            )

    discovered = discover_schema_packages()
    by_dist = {_normalize_dist_name(info["package_name"]): info for info in discovered}
    by_name = {info["name"]: info for info in discovered}

    specs: list[LoaderFragmentSpec] = []
    for pin in pins:
        info = by_dist.get(_normalize_dist_name(pin.package_name)) or by_name.get(
            pin.short_name
        )
        if info is None:
            warnings.warn(
                f"`requires:` pins {pin.package_name!r}, which is installed but "
                f"registers no discoverable schema package (no "
                f"`mosaic.schema_packages` / `mosaic.reference_loaders` entry point "
                f"under that distribution or name {pin.short_name!r}); its classes "
                f"will not be merged into the registry.",
                UserWarning,
                stacklevel=2,
            )
            continue
        specs.append(_build_fragment_spec(info, info["instance"]))
    return specs
