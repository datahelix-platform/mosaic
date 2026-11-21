"""Config-driven construction of storage adapters and ``MosaicClient``.

Single source of truth for turning configuration into a configured
:class:`~mosaic.core.client.MosaicClient`. Shared by the CLI, the TUI SDK
backend, and ``mosaic serve`` so every transport opens the *same* deployment
the *same* way — there is no longer one construction path for the SDK and
another (or none) for REST.

The storage backend is resolved through the ``mosaic.storage_adapters``
entry-point group (``storage_backend`` in config, default ``"sqlite"``), so a
deployment can swap adapters by configuration without code changes. Every
registered adapter shares the construction contract
``Adapter(database_url, schema_registry=registry)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from mosaic.core.exceptions import AdapterError, SchemaError
from mosaic.core.storage import EntityStore

#: Backend assumed when config does not specify ``storage_backend``.
DEFAULT_BACKEND = "sqlite"
#: Entry-point groups registering storage adapters (see ``pyproject.toml``).
#: Only ``mosaic.storage_adapters`` is read (the legacy ``hippo.*`` group
#: was removed by ADR-0015).
STORAGE_ADAPTERS_GROUPS = ("mosaic.storage_adapters",)
#: Canonical group name.
STORAGE_ADAPTERS_GROUP = STORAGE_ADAPTERS_GROUPS[0]
#: Where a *new* SQLite deployment lands when config gives no
#: ``database_url``.
DEFAULT_SQLITE_PATH = "data/mosaic.db"
#: Config filenames auto-detected in the cwd, in priority order (kept for
#: backwards compatibility; the lookup itself is centralized in
#: :func:`mosaic.config.loader.find_config_file`).
CONFIG_CANDIDATES = (
    "config.json",
    "mosaic.yaml",
    "mosaic.yml",
)
#: Backends whose import failure should hint at an optional extra.
_BACKEND_EXTRA = {"postgres": "postgres"}

PathLike = Union[str, Path]


def default_sqlite_path() -> str:
    """Resolve the default SQLite database path for a config-less deployment.

    Always :data:`DEFAULT_SQLITE_PATH` (``data/mosaic.db``). The legacy
    ``data/hippo.db`` is no longer picked up (ADR-0015).
    """
    return DEFAULT_SQLITE_PATH


def build_schema_registry(
    schema_path: Optional[PathLike] = None,
    *,
    merge_requires: bool = False,
) -> Any:
    """Build a ``SchemaRegistry`` from *schema_path* or the bundled mosaic_core.

    The bundled schema declares only Mosaic's framework classes (``Entity``,
    ``ProvenanceRecord``, ``ExternalID``, ...). Callers needing user-domain
    classes (``Sample``, ``Project``, ...) must pass an explicit
    *schema_path*.

    When *merge_requires* is true and *schema_path* declares a ``requires:``
    block, every installed reference loader it pins is resolved and its
    schema fragment is merged into the registry, so the result spans the
    user schema *and* its reference loaders (issue #67). Resolution applies
    the same installed-version gate as ``mosaic validate`` and raises
    :class:`~mosaic.core.exceptions.SchemaError` when a pinned loader is
    missing or its version disagrees with the pin. A schema with no
    ``requires:`` is unaffected.
    """
    from mosaic.linkml_bridge import SchemaRegistry

    if schema_path:
        registry = SchemaRegistry.from_path(schema_path)
        if merge_requires:
            registry = _merge_required_loaders(registry, schema_path)
        _validate_identifier_columns(registry)
        return registry

    import importlib.resources

    from linkml_runtime.utils.schemaview import SchemaView

    mosaic_core_path = importlib.resources.files("mosaic.schemas").joinpath(
        "mosaic_core.yaml"
    )
    registry = SchemaRegistry(SchemaView(str(mosaic_core_path)))
    _validate_identifier_columns(registry)
    return registry


def _validate_identifier_columns(registry: Any) -> None:
    """Reject a schema where a concrete *entity* class's identifier slot
    isn't literally named ``id``.

    Mosaic's storage layer treats a literal ``id`` column as every
    per-class entity table's system row-key — provenance, cross-class UUID
    resolution, and the generic per-class insert/update/select machinery
    all key off the literal name ``"id"`` (see ``CLAUDE.md``: "System
    fields (id, is_available) live on entity tables"). LinkML's own
    ``SQLTableGenerator``, however, honors whatever slot a class marks
    ``identifier: true`` verbatim as the SQL primary key, so a class that
    names its identifier something else (e.g. ``project_id``) gets a table
    with no ``id`` column at all — which previously surfaced as a raw
    "table X has no column named id" SQLite error deep inside ingestion
    (issue #172) instead of a schema-authoring error at load time.

    Scoped to classes carrying the ``is_available`` system slot (i.e.
    specializing the ``Entity`` mixin) — the same signal the DDL
    post-processor uses to decide the id/is_available entity invariants
    apply. Declarative, non-persisted config classes (e.g. the bundled
    ``Validator``/``ReferenceLoader``) never inherit it and are
    intentionally exempt.
    """
    value_types = registry.value_type_classes()
    offenders: list[tuple[str, str]] = []
    for name in registry.class_names():
        if name in value_types:
            continue
        cls = registry.get_class(name)
        if cls is None or getattr(cls, "abstract", False):
            continue
        slot_names = {slot.name for slot in registry.induced_slots(name)}
        if "is_available" not in slot_names:
            continue
        id_slot = registry.identifier_slot(name)
        if id_slot is not None and id_slot.name != "id":
            offenders.append((name, id_slot.name))

    if offenders:
        detail = "; ".join(f"{cls}.{slot}" for cls, slot in offenders)
        raise SchemaError(
            "Mosaic requires every entity class's identifier slot to be "
            f"named 'id'; found non-'id' identifier(s): {detail}. Rename "
            "the slot to 'id' (inheriting it from the bundled Entity mixin "
            "is the usual path), and keep any natural/business key as an "
            "ordinary unique attribute alongside it."
        )


def _merge_required_loaders(registry: Any, schema_path: PathLike) -> Any:
    """Merge the schema's installed ``requires:`` loader fragments into *registry*.

    Returns the original registry unchanged when the schema declares no
    ``requires:``. The import is function-local to keep module load cheap
    (discovery walks entry points); it stays within the core layer.
    """
    from mosaic.core.loaders.discovery import fragment_specs_for_requires

    specs = fragment_specs_for_requires(schema_path)
    if specs:
        return registry.with_loader_fragments(specs)
    return registry


def resolve_storage_adapter_class(backend: str) -> type[EntityStore]:
    """Resolve a storage-adapter class from ``mosaic.storage_adapters``.

    Raises:
        AdapterError: if *backend* is not registered, fails to import (e.g.
            an optional dependency is missing), or does not resolve to an
            :class:`EntityStore` subclass.
    """
    from importlib.metadata import entry_points

    eps: list[Any] = []
    seen: set[str] = set()
    for group in STORAGE_ADAPTERS_GROUPS:
        try:
            group_eps = list(entry_points(group=group))
        except TypeError:  # importlib.metadata < 3.10 dict-style API
            group_eps = list(entry_points().get(group, []))  # type: ignore[attr-defined]
        for ep in group_eps:
            if ep.name in seen:
                continue
            seen.add(ep.name)
            eps.append(ep)

    match = next((ep for ep in eps if ep.name == backend), None)
    if match is None:
        available = ", ".join(sorted(ep.name for ep in eps)) or "(none)"
        raise AdapterError(
            f"Unknown storage backend {backend!r}. Registered backends: {available}.",
            adapter_type=backend,
        )

    try:
        cls = match.load()
    except ImportError as exc:
        extra = _BACKEND_EXTRA.get(backend)
        hint = f" Install it with `pip install 'datahelix-mosaic[{extra}]'`." if extra else ""
        raise AdapterError(
            f"Storage backend {backend!r} could not be loaded: {exc}.{hint}",
            adapter_type=backend,
        ) from exc

    if not (isinstance(cls, type) and issubclass(cls, EntityStore)):
        raise AdapterError(
            f"Storage backend {backend!r} does not resolve to an EntityStore "
            f"subclass (got {cls!r}).",
            adapter_type=backend,
        )
    return cls


def create_storage_adapter(
    *,
    storage_backend: Optional[str] = None,
    database_url: Optional[str] = None,
    registry: Any,
) -> EntityStore:
    """Construct a storage adapter for *storage_backend*.

    Resolves the adapter class via the entry-point registry and constructs
    it with the shared ``Adapter(database_url, schema_registry=registry)``
    contract. SQLite defaults to :data:`DEFAULT_SQLITE_PATH` when no
    *database_url* is given; other backends require one.
    """
    backend = storage_backend or DEFAULT_BACKEND
    if not database_url:
        if backend == "sqlite":
            database_url = default_sqlite_path()
        else:
            raise AdapterError(
                f"Storage backend {backend!r} requires a database_url.",
                adapter_type=backend,
            )
    adapter_cls = resolve_storage_adapter_class(backend)
    return adapter_cls(database_url, schema_registry=registry)


def create_client(
    *,
    storage_backend: Optional[str] = None,
    database_url: Optional[str] = None,
    schema_path: Optional[PathLike] = None,
    validators_path: Optional[PathLike] = None,
    validation_enabled: bool = True,
    merge_requires: bool = True,
) -> Any:
    """Assemble a configured ``MosaicClient``.

    The single construction path shared by every transport: builds the
    schema registry, resolves and constructs the storage adapter, wires the
    CEL write-validation pipeline, and returns the client.

    *merge_requires* defaults to true so a deployment whose schema declares
    ``requires:`` gets a registry that spans its reference loaders with no
    extra wiring (issue #67) — every transport (CLI, ``mosaic serve``, TUI)
    opens the same spanning deployment the same way. A schema that declares
    a loader it has not installed fails fast here, mirroring
    ``mosaic validate``. Pass ``merge_requires=False`` to build a registry
    from the user schema alone.
    """
    from mosaic.core.client import MosaicClient

    registry = build_schema_registry(schema_path, merge_requires=merge_requires)
    storage = create_storage_adapter(
        storage_backend=storage_backend,
        database_url=database_url,
        registry=registry,
    )
    pipeline = _build_pipeline(validators_path, validation_enabled)
    return MosaicClient(storage=storage, registry=registry, pipeline=pipeline)


def create_client_from_config(config: Any) -> Any:
    """Assemble a ``MosaicClient`` from a :class:`~mosaic.config.MosaicConfig`."""
    return create_client(
        storage_backend=config.storage_backend,
        database_url=config.database_url,
        schema_path=config.schema_path,
        validators_path=config.validators_path,
        validation_enabled=config.validation_enabled,
    )


def load_config_autodetect(config_path: Optional[PathLike] = None) -> Any:
    """Load a ``MosaicConfig`` from *config_path*, or auto-detect one in the cwd.

    When *config_path* is given it is loaded directly (errors propagate).
    Otherwise the cwd is scanned via
    :func:`mosaic.config.loader.find_config_file` — ``config.json`` /
    ``mosaic.yaml`` / ``mosaic.yml``.
    Returns ``None`` when nothing is given and nothing is found, so
    callers can fall back to built-in defaults.
    """
    from mosaic.config import find_config_file, load_mosaic_config

    if config_path is not None:
        return load_mosaic_config(config_path)
    found = find_config_file()
    if found is not None:
        return load_mosaic_config(found)
    return None


def _build_pipeline(
    validators_path: Optional[PathLike], validation_enabled: bool = True
) -> Any:
    """Build a CEL write-validation pipeline, or ``None`` when not configured."""
    if not validation_enabled or not validators_path:
        return None

    from mosaic.core.pipeline import ValidationPipeline
    from mosaic.core.validators.write_validator import CELWriteValidator

    pipeline = ValidationPipeline()
    pipeline.add_validator(CELWriteValidator(validators_path=str(validators_path)))
    return pipeline
