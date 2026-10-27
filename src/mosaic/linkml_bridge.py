"""LinkML-backed schema loading, introspection, and validation.

Thin wrapper around ``linkml_runtime.SchemaView`` and ``linkml.validator`` that
exposes the handful of projections Mosaic needs. Mosaic-specific slot metadata is
carried via LinkML annotations under flat ``hippo_<name>`` keys (e.g.
``hippo_search: fts5``). User-authored schema YAML stays 100% valid LinkML.

Every ``hippo_*`` annotation used in a user schema must be declared in
``hippo_ext`` (ships with the package at ``src/mosaic/schemas/hippo_ext.yaml``).
``SchemaRegistry`` validates this at construction time — undeclared annotations,
value-type mismatches, and wrong-target attachments surface as
``SchemaError`` at load. See sec9 §9.4 for the design rationale and
``design/reference_hippo_ext.md`` for the per-annotation reference.
"""

from __future__ import annotations

import copy
import importlib.resources
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin
from linkml_runtime.linkml_model.meta import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView


HIPPO_SEARCH = "hippo_search"
HIPPO_INDEX = "hippo_index"
HIPPO_INDEX_PARTIAL = "hippo_index_partial"
HIPPO_UNIQUE = "hippo_unique"
HIPPO_APPEND_ONLY = "hippo_append_only"
HIPPO_ACCESSOR = "hippo_accessor"
HIPPO_NAMESPACE = "hippo_namespace"
HIPPO_EXTERNAL_XREF = "hippo_external_xref"

#: Name of the framework-provided external-reference value type (issue #48).
EXTERNAL_REFERENCE_CLASS = "ExternalReference"

#: Framework-provided value type that is *always* treated as inline,
#: independent of schema shape. ``ExternalReference`` is identifier-less so
#: schema-driven detection (:func:`is_value_type`) already classifies it as a
#: value type, but it is listed here as a documented baseline and so callers
#: that lack a ``SchemaView`` can still recognise the built-in.
#:
#: As of issue #90 value-type detection is **schema-driven**, not a hardcoded
#: allowlist: any non-tree-root class with no identifier slot is a value type
#: (see :func:`is_value_type` / :meth:`SchemaRegistry.value_type_classes`).
#: This constant is the framework floor; the authoritative per-schema set is
#: computed from the schema. Distinct from the transport-level
#: INFRASTRUCTURE_CLASSES set in ``mosaic.core.schema_typing``.
VALUE_TYPE_CLASSES: frozenset[str] = frozenset({EXTERNAL_REFERENCE_CLASS})

HIPPO_ANNOTATION_PREFIX = "hippo_"
_CLASS_ANNOTATION_SUBSET = "class_annotation"
_SLOT_ANNOTATION_SUBSET = "slot_annotation"

# Name of the synthetic tree-root class used for LinkML-native instance YAML
# (PR 3.1). Carries one multivalued slot per concrete class in the merged
# schema. The leading underscore keeps it from colliding with user classes,
# which by LinkML convention use UpperCamelCase.
TREE_ROOT_CLASS_NAME = "_HippoInstanceBundle"

# Top-level YAML keys that Mosaic recognises but LinkML's meta-schema does
# not. ``SchemaRegistry.from_path`` strips these before constructing a
# ``SchemaView`` so the LinkML parser stays strict on every other key.
_HIPPO_TOP_LEVEL_KEYS = frozenset({"requires"})


def default_accessor(class_name: str) -> str:
    """Derive the default accessor / tree-root slot name from a class name.

    ``snake_case(ClassName) + "s"``. Handles common cases:
    - ``Sample`` → ``samples``
    - ``TissueType`` → ``tissue_types``
    - ``DNASample`` → ``dna_samples``
    - ``ExternalID`` → ``external_ids``

    The naive ``+ "s"`` produces awkward plurals for words ending in ``s``
    (e.g. ``Process`` → ``processs``). Schemas override via the
    ``hippo_accessor`` class annotation; see ``class_accessor_name``.
    """
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
    return f"{snake}s"


def class_accessor_name(class_name: str, cls_obj: Any) -> str:
    """Accessor / tree-root slot name for a class, honoring ``hippo_accessor``.

    Returns the ``hippo_accessor`` annotation value if present (validated
    as a string), else ``default_accessor(class_name)``. Used by both the
    typed-client surface (``typed_client._resolve_accessor``) and the
    synthesized tree-root class so users see one mental model.

    This helper applies only the basic resolution. The typed-client wraps
    it with stricter identifier / collision validation; the tree-root
    synthesis accepts the result as-is since downstream LinkML validation
    will surface any malformed slot name.
    """
    override = annotation_value(cls_obj, HIPPO_ACCESSOR)
    if isinstance(override, str) and override:
        return override
    return default_accessor(class_name)


def annotation_value(element: Any, key: str) -> Optional[Any]:
    """Return the ``.value`` of a named annotation, or ``None`` if absent."""
    ann = getattr(element, "annotations", None)
    if ann is None or key not in ann:
        return None
    return ann[key].value


def slot_default(slot: Any) -> Optional[Any]:
    """Return a slot's default value in a DDL-friendly Python form, or None.

    LinkML stores `ifabsent` as a string (e.g. "true", "false", "active",
    "int(0)", "uuid()"). For boolean-ranged slots we coerce the literal
    "true"/"false" to Python booleans so the downstream ``_format_default``
    helpers emit native SQL (``1``/``0`` for SQLite, ``TRUE``/``FALSE`` for
    Postgres) rather than quoting the string. Other ifabsent forms pass
    through unchanged for now; richer parsing (e.g. ``uuid()``, ``int(0)``)
    is a later concern.
    """
    value = getattr(slot, "ifabsent", None)
    if value is None:
        return None
    if getattr(slot, "range", None) == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return value


# ---------------------------------------------------------------------------
# Shipped-with-Mosaic schemas — hippo_ext (annotation vocabulary) and hippo_core
# (Entity, Status, Operation, Validator, ReferenceLoader). Both are loaded as
# bundled resources; hippo_core is made available to user schemas through
# SchemaView's importmap so callers can declare `imports: [hippo_core]` and
# `is_a: Entity`.
# ---------------------------------------------------------------------------

_HIPPO_EXT_SV: Optional[SchemaView] = None


def _hippo_ext_resource_path() -> str:
    resource = importlib.resources.files("mosaic.schemas").joinpath("hippo_ext.yaml")
    return str(resource)


def _hippo_core_resource_path() -> str:
    resource = importlib.resources.files("mosaic.schemas").joinpath("hippo_core.yaml")
    return str(resource)


def _hippo_ext_schema_view() -> SchemaView:
    global _HIPPO_EXT_SV
    if _HIPPO_EXT_SV is None:
        _HIPPO_EXT_SV = SchemaView(_hippo_ext_resource_path())
    return _HIPPO_EXT_SV


def _bundled_importmap() -> dict[str, str]:
    """Importmap that lets user schemas say `imports: [hippo_core]`.

    SchemaView resolves import names against this map and appends `.yaml`
    when opening the file, so values must omit the extension.
    """
    return {
        "hippo_core": _hippo_core_resource_path().removesuffix(".yaml"),
    }


def _schema_view_from_text(
    text: str, *, source_file: Optional[Union[str, Path]] = None
) -> SchemaView:
    """Build a ``SchemaView`` from YAML *text*, anchoring relative imports.

    LinkML resolves a schema's relative ``imports:`` against the directory of
    that schema's ``source_file``. But ``linkml_runtime`` only records
    ``source_file`` when a schema is loaded from a *path* — a schema built
    from an in-memory *string* has ``source_file = None``, so its relative
    imports fall back to the process's current working directory (datahelix
    #66: ``mosaic migrate --schema-dir schemas`` run from ``/project`` looked
    for ``/project/pathology.yaml`` instead of ``/project/schemas/``).

    Mosaic constructs a SchemaView from text whenever it must pre-process a
    schema before handing it to LinkML — stripping the Mosaic-only
    ``requires:`` directive, or merging every file in a directory. Setting
    ``source_file`` to the originating path restores the LinkML contract:
    relative sibling imports resolve relative to the *referencing schema*, not
    the CWD. ``source_file`` need not exist on disk — only its parent
    directory is consulted, and only to resolve the imports themselves.
    """
    sv = SchemaView(text, importmap=_bundled_importmap())
    if source_file is not None:
        sv.schema.source_file = str(source_file)
    return sv


def _check_value_type(slot_decl: SlotDefinition, value: Any, enums: dict) -> Optional[str]:
    """Return an error message if ``value`` doesn't match ``slot_decl.range``; else None."""
    rng = slot_decl.range
    if rng is None:
        return None
    if rng == "boolean":
        if not isinstance(value, bool):
            return f"expected boolean, got {type(value).__name__} ({value!r})"
        return None
    if rng == "integer":
        # bool is a subclass of int in Python — reject bool under integer
        if isinstance(value, bool) or not isinstance(value, int):
            return f"expected integer, got {type(value).__name__} ({value!r})"
        return None
    if rng == "string":
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__} ({value!r})"
        return None
    if rng in enums:
        permissible = set(enums[rng].permissible_values.keys())
        if str(value) not in permissible:
            return (
                f"value {value!r} not in enum {rng} "
                f"(permissible: {sorted(permissible)})"
            )
        return None
    # Unknown range — tolerant.
    return None


def _validate_one_annotation(
    ext_slots: dict,
    ext_enums: dict,
    key: str,
    ann: Any,
    target_kind: str,
    target_path: str,
) -> Optional[str]:
    decl = ext_slots.get(key)
    if decl is None:
        known = sorted(k for k in ext_slots if k.startswith(HIPPO_ANNOTATION_PREFIX))
        return (
            f"annotation `{key}` on {target_kind} `{target_path}` is not "
            f"declared in hippo_ext. Known annotations: {known}"
        )
    expected_subset = (
        _CLASS_ANNOTATION_SUBSET if target_kind == "class" else _SLOT_ANNOTATION_SUBSET
    )
    in_subset = list(decl.in_subset) if decl.in_subset else []
    if expected_subset not in in_subset:
        return (
            f"annotation `{key}` on {target_kind} `{target_path}` is invalid — "
            f"`{key}` may only attach to {', '.join(in_subset) or '(nothing)'}"
        )
    value = getattr(ann, "value", ann)
    type_err = _check_value_type(decl, value, ext_enums)
    if type_err:
        return f"annotation `{key}` on {target_kind} `{target_path}`: {type_err}"
    return None


def _iter_hippo_annotations(element: Any):
    """Yield (key, annotation) pairs for every hippo_* annotation on ``element``.

    LinkML exposes ``.annotations`` as a ``JsonObj`` which supports ``in`` and
    ``[]`` indexing but doesn't implement ``.items()``. We iterate by filtering
    to keys that start with the hippo_ prefix.
    """
    annotations = getattr(element, "annotations", None)
    if annotations is None:
        return
    for key in list(annotations):
        key = str(key)
        if not key.startswith(HIPPO_ANNOTATION_PREFIX):
            continue
        try:
            ann = annotations[key]
        except KeyError:
            continue
        yield key, ann


def _validate_hippo_annotations(sv: SchemaView) -> None:
    """Validate every hippo_* annotation in ``sv`` against hippo_ext.

    Raises ``SchemaError`` aggregating all failures if any annotation is
    undeclared, mistyped, or attached to the wrong kind of element.
    """
    # Skip validation if we're loading hippo_ext itself (circular).
    if sv.schema.name == "hippo_ext":
        return

    ext = _hippo_ext_schema_view()
    ext_slots = ext.all_slots()
    ext_enums = ext.all_enums()

    failures: list[str] = []

    for class_name, cls in sv.all_classes().items():
        for key, ann in _iter_hippo_annotations(cls):
            err = _validate_one_annotation(
                ext_slots, ext_enums, key, ann, "class", class_name
            )
            if err:
                failures.append(err)

        try:
            induced = sv.class_induced_slots(class_name)
        except Exception:
            # A malformed class may throw during induction; skip and let
            # LinkML's own validation surface the deeper problem.
            continue
        for slot in induced:
            for key, ann in _iter_hippo_annotations(slot):
                err = _validate_one_annotation(
                    ext_slots,
                    ext_enums,
                    key,
                    ann,
                    "slot",
                    f"{class_name}.{slot.name}",
                )
                if err:
                    failures.append(err)

    if failures:
        # Deferred import to avoid a module-load cycle with core.exceptions.
        from mosaic.core.exceptions import SchemaError

        raise SchemaError(
            f"{len(failures)} hippo_* annotation error(s) in schema:\n  - "
            + "\n  - ".join(failures),
            error_code="HIPPO_EXT_VALIDATION",
        )


def _flatten_for_validator(sv: SchemaView) -> dict[str, Any]:
    """Build a self-contained schema dict with all imports resolved inline.

    LinkML's ``Validator`` re-resolves ``imports:`` when it processes a
    SchemaDefinition, which fails for bundled schemas like ``hippo_core``
    that live outside the user-schema directory and are only visible via
    the ``SchemaRegistry``'s importmap. We work around this by flattening
    the merged ``SchemaView`` back into a dict that has every class, slot,
    enum, and type inlined and no ``imports:`` remaining.
    """
    from linkml_runtime.dumpers import yaml_dumper

    flat: dict[str, Any] = {
        "id": sv.schema.id or "https://example.org/hippo/flat",
        "name": sv.schema.name or "flat",
        "prefixes": dict(sv.schema.prefixes or {}),
        "default_range": sv.schema.default_range or "string",
        "imports": ["linkml:types"],
        "classes": {},
        "slots": {},
        "enums": {},
        "types": {},
    }

    def _to_plain(obj: Any) -> Any:
        return yaml.safe_load(yaml_dumper.dumps(obj))

    for name, cls in sv.all_classes(imports=True).items():
        flat["classes"][name] = _to_plain(cls)
    for name, slot in sv.all_slots(imports=True).items():
        flat["slots"][name] = _to_plain(slot)
    for name, enum in sv.all_enums(imports=True).items():
        flat["enums"][name] = _to_plain(enum)
    for name, typ in (sv.all_types(imports=True) or {}).items():
        flat["types"][name] = _to_plain(typ)

    for section in ("slots", "enums", "types"):
        if not flat[section]:
            del flat[section]
    return flat


def _has_type_designator(sv: SchemaView, class_name: str) -> bool:
    """True if ``class_name`` has an induced slot marked ``designates_type``.

    Identifies an explicit polymorphic dispatch base (issue #80). Uses
    ``class_induced_slots`` so a designator declared on a base is seen on the
    base itself (and would be on subclasses too).
    """
    try:
        slots = sv.class_induced_slots(class_name)
    except Exception:
        return False
    return any(getattr(s, "designates_type", False) for s in slots)


def is_value_type(sv: SchemaView, class_name: str) -> bool:
    """True if ``class_name`` is an inline VALUE type rather than an entity.

    Schema-driven definition (issue #90): a class is a value type when it
    has **no identifier slot** (neither ``identifier`` nor ``key``) and is
    **not a tree-root**. Such a class has no ``id``/lifecycle, gets no table
    or typed-client accessor, and is stored inline (one JSON TEXT column) on
    the entity slot that ranges it.

    This replaces the former hardcoded allowlist of one class
    (``ExternalReference``). The framework's ``ExternalReference`` is
    identifier-less and so still classifies as a value type, but any
    identifier-less domain value object — e.g. ``Mass``/``Volume``/
    ``Concentration`` modeled as subclasses of an abstract ``Quantity`` —
    now round-trips identically instead of being reified into its own table
    with a synthetic FK (which silently dropped the inline value on ingest).

    A polymorphic dispatch *base* (a class declaring a ``designates_type``
    slot, e.g. ``Sample``) is never a value type even if it were
    identifier-less: its concrete subclasses carry identity and get tables
    (issue #80). In practice such bases are ``is_a: Entity`` and so have an
    ``id`` anyway; the guard is belt-and-suspenders.
    """
    cls = sv.get_class(class_name)
    if cls is None:
        return False
    if getattr(cls, "tree_root", False):
        return False
    if _has_type_designator(sv, class_name):
        return False
    try:
        return sv.get_identifier_slot(class_name, use_key=True) is None
    except Exception:
        # A malformed class may throw during identifier resolution; treat it
        # as a non-value-type and let LinkML's own validation surface it.
        return False


def value_type_class_names(sv: SchemaView) -> frozenset[str]:
    """Names of every value type in ``sv`` (see :func:`is_value_type`)."""
    return frozenset(
        name
        for name in sv.all_classes(imports=True)
        if is_value_type(sv, name)
    )


def _build_tree_root_class(sv: SchemaView) -> ClassDefinition:
    """Construct the synthesized ``_HippoInstanceBundle`` tree-root class.

    Per the β-handoff (PR 3.1): one multivalued, inlined slot per concrete
    (non-abstract) class in the merged schema, including bundled
    ``hippo_core`` classes. Slot name comes from ``class_accessor_name`` —
    snake_case-pluralized class name, overridable via the ``hippo_accessor``
    class annotation. This keeps the wire format aligned with the
    typed-client accessor surface (sec9 §9.8).

    Abstract classes get no accessor, with one exception (issue #80): an
    explicit polymorphic *base* — an abstract class declaring a
    ``designates_type`` slot (e.g. ``Sample``) — gets a base-ranged accessor
    so its inlined collection is ingestable, with instances dispatched to
    their concrete subclass by the discriminator at ingest time. Plain
    abstract roots such as ``Entity`` carry no designator and stay excluded.

    The returned class is NOT added to ``sv``. ``SchemaRegistry`` injects
    it only into the validator's flat schema so the DDL generator,
    schema-diff engine, and typed-client never see it as a domain class.

    Raises ``SchemaError`` if the user schema already declares a class
    named ``_HippoInstanceBundle`` or if two distinct classes resolve to
    the same tree-root slot name (the wire format is a flat namespace —
    add ``hippo_accessor`` overrides to disambiguate).
    """
    existing = sv.all_classes(imports=True)
    if TREE_ROOT_CLASS_NAME in existing:
        from mosaic.core.exceptions import SchemaError

        raise SchemaError(
            f"Class name {TREE_ROOT_CLASS_NAME!r} is reserved for the "
            f"synthesized tree-root and may not be declared in a user schema.",
            error_code="HIPPO_TREE_ROOT_COLLISION",
        )

    attributes: dict[str, SlotDefinition] = {}
    seen_slots: dict[str, str] = {}
    for cls_name, cls in existing.items():
        # Value types (e.g. ExternalReference, or any identifier-less domain
        # value object like Mass/Volume) are stored inline on entity slots —
        # they are not independently ingestable, so they get no tree-root
        # slot in the instance bundle (issue #90).
        if is_value_type(sv, cls_name):
            continue
        if cls.abstract:
            # Abstract classes normally get no accessor — except an explicit
            # polymorphic *base* (one declaring a `designates_type` slot, e.g.
            # `Sample`), which gets a base-ranged accessor so its inlined
            # collection is ingestable; instances dispatch to their concrete
            # subclass by the discriminator (issue #80). Plain abstract roots
            # like `Entity` carry no designator and stay excluded.
            if _has_type_designator(sv, cls_name):
                pass
            else:
                continue
        slot_name = class_accessor_name(cls_name, cls)
        if slot_name in seen_slots:
            from mosaic.core.exceptions import SchemaError

            raise SchemaError(
                f"Tree-root slot name {slot_name!r} would be assigned to "
                f"both {seen_slots[slot_name]!r} and {cls_name!r}. Add a "
                f"`hippo_accessor` annotation to at least one class to "
                f"disambiguate.",
                error_code="HIPPO_TREE_ROOT_SLOT_COLLISION",
            )
        seen_slots[slot_name] = cls_name
        attributes[slot_name] = SlotDefinition(
            name=slot_name,
            range=cls_name,
            multivalued=True,
            inlined=True,
            inlined_as_list=True,
        )

    return ClassDefinition(
        name=TREE_ROOT_CLASS_NAME,
        tree_root=True,
        description=(
            "Synthesized tree-root for LinkML-native instance YAML. Carries "
            "one multivalued slot per concrete class in the merged schema; "
            "slot names follow the typed-client accessor convention "
            "(`snake_case(ClassName) + 's'`, overridable via "
            "`hippo_accessor`). Not user-authored — Mosaic injects it into "
            "the validator schema so `mosaic validate` / `mosaic ingest` can "
            "target a single tree-shaped root."
        ),
        attributes=attributes,
    )


# ---------------------------------------------------------------------------
# Reference loader fragment merge engine (sec2 §2.14.5 / §2.14.6,
# decisions D2.14.G + D2.14.H).
# ---------------------------------------------------------------------------

PROVIDED_BY_ANNOTATION = "provided_by"
LOADER_DEPENDS_ON_ANNOTATION = "loader_depends_on"


@dataclass(frozen=True)
class LoaderFragmentSpec:
    """A single reference-loader fragment ready for merge.

    ``loader_name`` is the loader's stable identifier (``ReferenceLoader.name``).
    ``package_name`` is the Python distribution that ships the loader
    (e.g., ``mosaic-reference-fma``) — surfaced in collision error messages.
    ``package_version`` is the installed package version, recorded in the
    injected ``provided_by`` attribution.  ``fragment`` is the dict returned
    by ``ReferenceLoader.schema_fragment()``.
    """

    loader_name: str
    package_name: str
    package_version: str
    fragment: dict


def _validate_loader_prefix(
    spec: LoaderFragmentSpec, sibling_prefixes: dict[str, str]
) -> str:
    """Apply Rule 1. Returns the validated prefix.

    Raises ``ConfigError`` on missing prefix, mismatch with ``loader_name``,
    or collision with an already-seen sibling fragment.
    """
    from mosaic.core.exceptions import ConfigError

    prefix = spec.fragment.get("default_prefix")
    if not prefix:
        raise ConfigError(
            f"Reference loader {spec.loader_name!r} (package "
            f"{spec.package_name!r}) is missing `default_prefix:` in its "
            f"schema_fragment(). Spec §2.14.5 Rule 1 requires every fragment "
            f"to declare `default_prefix: {spec.loader_name}`.",
            field_name="default_prefix",
            loader_name=spec.loader_name,
            package_name=spec.package_name,
        )
    if prefix != spec.loader_name:
        raise ConfigError(
            f"Reference loader {spec.loader_name!r} (package "
            f"{spec.package_name!r}) declared `default_prefix: {prefix!r}` but "
            f"spec §2.14.5 Rule 1 requires it to equal the loader name "
            f"({spec.loader_name!r}).",
            field_name="default_prefix",
            loader_name=spec.loader_name,
            package_name=spec.package_name,
            actual_prefix=prefix,
        )
    if prefix in sibling_prefixes:
        other_pkg = sibling_prefixes[prefix]
        raise ConfigError(
            f"Two reference loaders declare `default_prefix: {prefix!r}` — "
            f"package {spec.package_name!r} and package {other_pkg!r}. Loader "
            f"prefixes must be unique (spec §2.14.5 Rule 1).",
            field_name="default_prefix",
            prefix=prefix,
            packages=[spec.package_name, other_pkg],
        )
    return prefix


def _inject_provided_by_annotation(element: dict, attribution: str) -> None:
    """Set ``annotations.provided_by.value = attribution`` on a class/slot dict.

    Existing ``annotations`` blocks are preserved; a pre-existing
    ``provided_by`` is overwritten so the engine remains authoritative.
    Handles both dict-shape and list-shape annotation blocks.
    """
    annotations = element.get("annotations")
    if isinstance(annotations, list):
        # List form: [{tag: ..., value: ...}, ...]. Rebuild as dict for our use.
        rebuilt: dict[str, Any] = {}
        for entry in annotations:
            if isinstance(entry, dict) and "tag" in entry:
                rebuilt[entry["tag"]] = entry
        annotations = rebuilt
        element["annotations"] = annotations
    elif annotations is None:
        annotations = {}
        element["annotations"] = annotations
    annotations[PROVIDED_BY_ANNOTATION] = {
        "tag": PROVIDED_BY_ANNOTATION,
        "value": attribution,
    }


def _prepare_loader_fragment(
    spec: LoaderFragmentSpec,
    *,
    deployed_imports: set[str],
    deployed_prefixes: set[str],
) -> dict:
    """Apply Rule 2 (strip colliding imports) and Rule 3 (inject ``provided_by``).

    Returns a deep-copied fragment dict; the input is not mutated.
    """
    prepared = copy.deepcopy(spec.fragment)

    # Rule 2 — imports policy. Always strip `linkml:types`. Strip any import
    # already present in the deployed schema, and any CURIE-form import whose
    # prefix is already known. Loader-private imports (full URLs, paths, or
    # CURIEs under a fragment-owned prefix) pass through unchanged.
    raw_imports = prepared.get("imports") or []
    kept: list[str] = []
    for imp in raw_imports:
        if imp == "linkml:types":
            continue
        if imp in deployed_imports:
            continue
        if ":" in imp and "//" not in imp:
            pfx = imp.split(":", 1)[0]
            if pfx in deployed_prefixes:
                continue
        kept.append(imp)
    if kept:
        prepared["imports"] = kept
    else:
        prepared.pop("imports", None)

    # Rule 3 — provided_by injection. Stamp every class, every top-level slot,
    # and every per-class `attributes:` entry the fragment introduces.
    attribution = f"{spec.loader_name}@{spec.package_version}"

    classes = prepared.get("classes") or {}
    for cls_name, cls in list(classes.items()):
        if not isinstance(cls, dict):
            # LinkML accepts `ClassName:` (null body) as shorthand — promote
            # to a dict so the annotation has somewhere to land.
            cls = {} if cls is None else cls
            classes[cls_name] = cls
            if not isinstance(cls, dict):
                continue
        _inject_provided_by_annotation(cls, attribution)
        attributes = cls.get("attributes") or {}
        for attr_name, attr in list(attributes.items()):
            if attr is None:
                attr = {}
                attributes[attr_name] = attr
            if isinstance(attr, dict):
                _inject_provided_by_annotation(attr, attribution)

    slots = prepared.get("slots") or {}
    for slot_name, slot in list(slots.items()):
        if slot is None:
            slot = {}
            slots[slot_name] = slot
        if isinstance(slot, dict):
            _inject_provided_by_annotation(slot, attribution)

    return prepared


def _emit_loader_depends_on_warnings(
    spec: LoaderFragmentSpec, installed_loader_names: set[str]
) -> None:
    """Apply Rule 4. Warn — never raise — when a declared dependency is absent."""
    annotations = spec.fragment.get("annotations") or {}
    if not isinstance(annotations, dict):
        return
    node = annotations.get(LOADER_DEPENDS_ON_ANNOTATION)
    if isinstance(node, dict):
        raw = node.get("value")
    else:
        raw = node
    if raw is None or raw == "":
        return
    deps = [d.strip() for d in str(raw).split(",") if d.strip()]
    for dep in deps:
        if dep in installed_loader_names:
            continue
        warnings.warn(
            f"Reference loader {spec.loader_name!r} declared "
            f"`loader_depends_on: {dep!r}` but no loader named {dep!r} is "
            f"installed. Cross-loader foreign keys are advisory in v1 "
            f"(spec §2.14.6) — install the dependency to silence this warning.",
            UserWarning,
            stacklevel=3,
        )


def _merge_prepared_fragment(deployed_sv: SchemaView, prepared: dict) -> SchemaView:
    """Combine a deployed ``SchemaView`` with a prepared fragment into a new view.

    Reuses ``_flatten_for_validator`` so the merged view is self-contained
    (no unresolved imports). Loader-introduced classes/slots/enums/types win
    on key collision because the prefix-uniqueness rule means a class name
    showing up in two fragments is not possible from upstream callers; if it
    does happen (e.g., a class name colliding with a deployed name), the
    fragment is the more specific contribution and overwrites.
    """
    base = _flatten_for_validator(deployed_sv)
    base.setdefault("classes", {})
    base.setdefault("slots", {})
    base.setdefault("enums", {})
    base.setdefault("types", {})
    base.setdefault("prefixes", {})
    base.setdefault("imports", [])

    for section in ("classes", "slots", "enums", "types"):
        for key, value in (prepared.get(section) or {}).items():
            base[section][key] = value

    for pfx, uri in (prepared.get("prefixes") or {}).items():
        base["prefixes"].setdefault(pfx, uri)

    for imp in prepared.get("imports") or []:
        if imp not in base["imports"]:
            base["imports"].append(imp)

    # Drop empty sections so the resulting YAML stays tidy.
    for section in ("classes", "slots", "enums", "types", "prefixes"):
        if not base[section]:
            del base[section]

    return SchemaView(yaml.safe_dump(base), importmap=_bundled_importmap())


def merge_loader_fragment(
    deployed_sv: SchemaView,
    spec: LoaderFragmentSpec,
    *,
    sibling_prefixes: Optional[dict[str, str]] = None,
    installed_loader_names: Optional[set[str]] = None,
) -> SchemaView:
    """Merge a reference-loader schema fragment into ``deployed_sv``.

    Implements decision D2.14.G (mandatory per-loader prefix, ``imports:``
    policy, ``provided_by`` injection) and D2.14.H (soft
    ``loader_depends_on`` warning).

    Parameters
    ----------
    deployed_sv:
        The active deployed ``SchemaView`` (typically Mosaic core +
        user schema) the fragment is being merged into.
    spec:
        The loader fragment to merge.
    sibling_prefixes:
        Maps ``default_prefix → package_name`` for fragments already
        merged in the same install batch. Used for prefix-collision
        detection across loaders. Callers iterating over multiple
        fragments should accumulate this map between calls.
    installed_loader_names:
        Names of every reference loader the caller treats as installed.
        Used only to silence ``loader_depends_on`` warnings — missing
        dependencies emit a ``UserWarning`` and never block the merge.

    Returns
    -------
    SchemaView
        A new ``SchemaView`` containing the merged schema. ``deployed_sv``
        is not mutated.

    Raises
    ------
    ConfigError
        On Rule 1 violations (missing/mismatched/colliding prefix).
    """
    sibling_prefixes = sibling_prefixes if sibling_prefixes is not None else {}
    installed_loader_names = (
        installed_loader_names if installed_loader_names is not None else set()
    )

    _validate_loader_prefix(spec, sibling_prefixes)

    deployed_imports = set(deployed_sv.schema.imports or [])
    deployed_prefixes = set((deployed_sv.schema.prefixes or {}).keys())
    prepared = _prepare_loader_fragment(
        spec,
        deployed_imports=deployed_imports,
        deployed_prefixes=deployed_prefixes,
    )

    _emit_loader_depends_on_warnings(spec, installed_loader_names)

    return _merge_prepared_fragment(deployed_sv, prepared)


def _loader_prefix_of(element: Any) -> Optional[str]:
    """Loader prefix that provided ``element`` (from ``provided_by``), or None.

    ``provided_by`` is injected as ``<loader_name>@<version>`` (Rule 3); the
    loader name equals the loader's ``default_prefix`` (Rule 1), so the part
    before ``@`` is exactly the prefix a consumer would use in a CURIE range.
    """
    val = annotation_value(element, PROVIDED_BY_ANNOTATION)
    if isinstance(val, str) and "@" in val:
        return val.split("@", 1)[0]
    return None


def _resolve_loader_prefixed_ranges(sv: SchemaView) -> SchemaView:
    """Rewrite consumer slot ranges of the form ``<loader>:<Class>`` to ``<Class>``.

    After a consumer schema is merged with its ``requires:`` loader fragments
    (issue #67), a slot that referenced a loader class through the documented
    loader-prefixed CURIE form (e.g. ``range: ensembl:Gene``) is rewritten to
    the bare merged class name (``Gene``) — but only when a class of that name
    exists *and* was provided by exactly that loader (matched via the injected
    ``provided_by`` attribution). The rewrite turns an otherwise-advisory
    opaque range into a recognized cross-loader reference, so the slot
    participates in joins/expansion and is validated against the merged class.

    Loader-owned slots/classes (those carrying ``provided_by``) are left
    untouched: loader-to-loader cross-references stay advisory in v1
    (decision D2.14.H, spec §2.14.6). Returns ``sv`` unchanged when nothing
    resolves.
    """
    classes = sv.all_classes(imports=True)

    def _resolve(rng: Any) -> Any:
        if not (isinstance(rng, str) and ":" in rng and "//" not in rng):
            return rng
        prefix, _, cname = rng.partition(":")
        target = classes.get(cname)
        if target is not None and _loader_prefix_of(target) == prefix:
            return cname
        return rng

    schema = sv.schema
    changed = False

    for slot in (schema.slots or {}).values():
        if _loader_prefix_of(slot) is not None:
            continue  # loader-owned top-level slot — advisory (D2.14.H)
        new = _resolve(slot.range)
        if new != slot.range:
            slot.range = new
            changed = True

    for cls in (schema.classes or {}).values():
        if _loader_prefix_of(cls) is not None:
            continue  # loader-owned class — its attribute ranges stay advisory
        for attr in (cls.attributes or {}).values():
            new = _resolve(attr.range)
            if new != attr.range:
                attr.range = new
                changed = True
        for usage in (cls.slot_usage or {}).values():
            new = _resolve(usage.range)
            if new != usage.range:
                usage.range = new
                changed = True

    if not changed:
        return sv

    from linkml_runtime.dumpers import yaml_dumper

    return SchemaView(yaml_dumper.dumps(schema), importmap=_bundled_importmap())


def merge_loader_fragments(
    deployed_sv: SchemaView,
    specs: list[LoaderFragmentSpec],
    *,
    installed_loader_names: Optional[set[str]] = None,
) -> SchemaView:
    """Merge a batch of reference-loader fragments sequentially.

    Accumulates ``sibling_prefixes`` across iterations so prefix collisions
    across the batch surface as ``ConfigError`` from the colliding call. If
    ``installed_loader_names`` is None it defaults to ``{spec.loader_name
    for spec in specs}`` so a batch can satisfy its own internal
    cross-references without the caller having to pre-compute the set.
    """
    if installed_loader_names is None:
        installed_loader_names = {spec.loader_name for spec in specs}
    sibling_prefixes: dict[str, str] = {}
    sv = deployed_sv
    for spec in specs:
        sv = merge_loader_fragment(
            sv,
            spec,
            sibling_prefixes=sibling_prefixes,
            installed_loader_names=installed_loader_names,
        )
        sibling_prefixes[spec.loader_name] = spec.package_name
    return sv


@dataclass(frozen=True)
class InverseSlot:
    """One ``inverse``-declared slot: a *virtual* reverse edge (ADR-0011).

    ``name`` is the derived, multivalued slot on the declaring class
    (``Donor.samples``); ``target_class`` its range (``Sample``);
    ``forward_slot`` the single-valued reference slot on ``target_class``
    that stores the relationship as an FK column (``Sample.donor``). The
    derived side has no column, link table, or relationship rows of its
    own — every read, filter, and count through it delegates to
    ``forward_slot``.

    ``target_classes`` are the concrete classes whose rows the reverse edge
    can reach: ``target_class`` itself when it is concrete, plus every
    concrete descendant (issue #224). When ``target_class`` is abstract
    (``Sample`` with ``Brain``/``CSF``/... subclasses) it has no table of its
    own and every hit lives in a descendant's table, so adapters must
    resolve the edge across all of them.
    """

    name: str
    target_class: str
    forward_slot: str
    target_classes: tuple[str, ...] = ()

    @property
    def tables(self) -> tuple[str, ...]:
        """Concrete classes to resolve against (falls back to the range)."""
        return self.target_classes or (self.target_class,)


def concrete_class_closure(sv: SchemaView, class_name: str) -> tuple[str, ...]:
    """``class_name`` and its descendants that are concrete (issue #224).

    Mixins and ``abstract`` classes own no storage, so they are skipped. The
    range itself comes first when concrete; descendants follow sorted, for a
    deterministic SQL shape.
    """
    try:
        descendants = sv.class_descendants(class_name, reflexive=True, mixins=False)
    except Exception:
        descendants = [class_name]
    out: list[str] = []
    for name in descendants:
        cls = sv.get_class(name)
        if cls is None or cls.abstract or cls.mixin:
            continue
        out.append(str(name))
    head = [n for n in out if n == class_name]
    return tuple(head + sorted(n for n in out if n != class_name))


def _validate_inverse_slots(sv: SchemaView) -> None:
    """Validate every ``inverse``-declared multivalued slot (ADR-0011).

    LinkML binds ``inverse`` to ``owl:inverseOf``: the reverse direction is
    *entailed* by the forward slot, never stored independently. Mosaic
    honours that on the multivalued side only — a multivalued slot naming
    an ``inverse`` is a virtual reverse edge over the forward FK column
    and must satisfy:

    - its range is an entity class (not a value type);
    - it is not ``required``, ``identifier``, ``inlined`` or
      ``inlined_as_list`` (a derived field cannot be written);
    - ``inverse`` names a slot induced on the range class;
    - that forward slot is **single-valued** (the reverse of a
      relationships-backed multivalued slot is out of scope); and
    - the forward slot's range is the declaring class or one of its
      ancestors (so ``forward = <declaring id>`` is well-typed).

    A *single-valued* slot carrying ``inverse`` is the stored side and is
    left alone (LinkML permits declaring the pair symmetrically).

    Raises ``SchemaError`` aggregating all failures.
    """
    if sv.schema.name == "hippo_ext":
        return
    classes = sv.all_classes()
    value_types = value_type_class_names(sv)
    failures: list[str] = []
    for class_name in classes:
        try:
            induced = sv.class_induced_slots(class_name)
        except Exception:
            continue
        for slot in induced:
            inverse = getattr(slot, "inverse", None)
            if not inverse or not slot.multivalued:
                continue
            where = f"{class_name}.{slot.name}"
            rng = slot.range
            if not rng or rng not in classes or rng in value_types:
                failures.append(
                    f"{where} declares `inverse: {inverse}` but its range "
                    f"{rng!r} is not an entity class; `inverse` is only "
                    f"meaningful on a slot ranged against another class."
                )
                continue
            if not concrete_class_closure(sv, str(rng)):
                failures.append(
                    f"{where} declares `inverse: {inverse}` but its range "
                    f"{rng!r} is abstract with no concrete descendant, so no "
                    f"stored row can ever reference this entity (issue #224)."
                )
                continue
            if slot.required or slot.identifier:
                failures.append(
                    f"{where} declares `inverse: {inverse}` and is "
                    f"{'required' if slot.required else 'an identifier'}; "
                    f"an inverse slot is derived from {rng}.{inverse} and "
                    f"cannot be required on write."
                )
            if slot.inlined or slot.inlined_as_list:
                failures.append(
                    f"{where} declares `inverse: {inverse}` and is inlined; "
                    f"an inverse slot resolves to ids through the forward "
                    f"FK and cannot embed objects."
                )
            forward = next(
                (f for f in sv.class_induced_slots(rng) if f.name == str(inverse)),
                None,
            )
            if forward is None:
                names = sorted(f.name for f in sv.class_induced_slots(rng))
                failures.append(
                    f"{where} declares `inverse: {inverse}` but {rng} has no "
                    f"slot named {str(inverse)!r}. Slots on {rng}: {names}."
                )
                continue
            if forward.multivalued:
                failures.append(
                    f"{where} declares `inverse: {inverse}` but "
                    f"{rng}.{inverse} is multivalued (stored as relationship "
                    f"rows, ADR-0002); Mosaic supports the reverse of a "
                    f"single-valued (FK-column) reference only (ADR-0011)."
                )
                continue
            if forward.inlined or forward.inlined_as_list:
                failures.append(
                    f"{where} declares `inverse: {inverse}` but "
                    f"{rng}.{inverse} is inlined; LinkML's SQL generator "
                    f"emits an inlined reference's FK column as "
                    f"`{inverse}_id`, not `{inverse}`, so the reverse edge "
                    f"would resolve against a column that doesn't exist "
                    f"(ADR-0011 supports the reverse of a plain, "
                    f"non-inlined FK column only)."
                )
                continue
            ancestors = set(sv.class_ancestors(class_name, reflexive=True))
            if forward.range not in ancestors:
                failures.append(
                    f"{where} declares `inverse: {inverse}` but "
                    f"{rng}.{inverse} is ranged on {forward.range!r}, which "
                    f"is not {class_name} or one of its ancestors "
                    f"({sorted(ancestors)}); the forward slot must point "
                    f"back at the declaring class."
                )
    if failures:
        from mosaic.core.exceptions import SchemaError

        raise SchemaError(
            f"{len(failures)} inverse-slot error(s) in schema (ADR-0011):\n  - "
            + "\n  - ".join(failures),
            error_code="INVERSE_SLOT",
        )


class SchemaRegistry:
    """Mosaic-facing schema registry backed by a LinkML ``SchemaView``."""

    def __init__(self, schema_view: SchemaView) -> None:
        _validate_hippo_annotations(schema_view)
        _validate_inverse_slots(schema_view)
        self._sv = schema_view
        self._inverse_slots_cache: dict[str, list[InverseSlot]] = {}
        # Schema-driven set of inline value types (issue #90): identifier-less,
        # non-tree-root classes stored inline as JSON TEXT rather than reified
        # into their own table. Computed once — the SchemaView is immutable
        # after construction. Authoritative over the static VALUE_TYPE_CLASSES
        # baseline.
        self._value_type_classes = value_type_class_names(schema_view)
        # Synthesize the wire-format tree-root class once at construction.
        # Kept off the SchemaView so DDL generation, schema-diff, and the
        # typed-client surface stay unaware of it; the validator's flat
        # schema gets a copy so `validate(bundle, _HippoInstanceBundle)`
        # works (sec9 PR 3.1).
        self._tree_root_class = _build_tree_root_class(schema_view)
        # Validator needs a self-contained schema (no unresolved imports) so
        # it can generate JSON Schema without re-reading files from disk.
        # See _flatten_for_validator for the rationale.
        flat = _flatten_for_validator(schema_view)
        from linkml_runtime.dumpers import yaml_dumper

        flat["classes"][TREE_ROOT_CLASS_NAME] = yaml.safe_load(
            yaml_dumper.dumps(self._tree_root_class)
        )
        self._validator = Validator(
            schema=flat,
            validation_plugins=[JsonschemaValidationPlugin(closed=True)],
        )

    @classmethod
    def from_path(cls, path: Union[str, Path]) -> "SchemaRegistry":
        p = Path(path)
        if p.is_dir():
            return cls._from_directory(p)
        # Mosaic-specific top-level directives (``requires:``, PTS-227) are
        # not part of the LinkML meta-schema; strip them before handing
        # the document to SchemaView so the LinkML parser stays strict.
        # ``mosaic.requires.extract_requires`` reads the same file directly
        # to recover the directive.
        raw = p.read_text()
        doc = yaml.safe_load(raw) or {}
        if isinstance(doc, dict) and _HIPPO_TOP_LEVEL_KEYS & doc.keys():
            doc = {k: v for k, v in doc.items() if k not in _HIPPO_TOP_LEVEL_KEYS}
            # Stripping `requires:` forces construction from a string, which
            # loses the source path LinkML uses to resolve relative imports;
            # re-anchor it to ``p`` so sibling imports resolve relative to the
            # schema file rather than the CWD (datahelix #66).
            return cls(_schema_view_from_text(yaml.safe_dump(doc), source_file=p))
        return cls(SchemaView(str(p), importmap=_bundled_importmap()))

    @classmethod
    def _from_directory(cls, path: Path) -> "SchemaRegistry":
        """Merge all ``*.yaml`` / ``*.yml`` files in ``path`` into one schema."""
        files = sorted(list(path.glob("*.yaml")) + list(path.glob("*.yml")))
        if not files:
            raise FileNotFoundError(f"No schema files found in {path}")
        merged: dict[str, Any] = {
            "id": f"https://example.org/hippo/{path.name}",
            "name": path.name,
            "prefixes": {"linkml": "https://w3id.org/linkml/"},
            "imports": ["linkml:types"],
            "default_range": "string",
            "classes": {},
            "enums": {},
            "slots": {},
            "types": {},
        }
        for file_path in files:
            doc = yaml.safe_load(file_path.read_text()) or {}
            if "id" in doc and not doc.get("imports") is None:
                merged["id"] = doc.get("id", merged["id"])
            for section in ("classes", "enums", "slots", "types"):
                merged[section].update(doc.get(section) or {})
            for pfx, uri in (doc.get("prefixes") or {}).items():
                merged["prefixes"].setdefault(pfx, uri)
            for imp in doc.get("imports") or []:
                if imp not in merged["imports"]:
                    merged["imports"].append(imp)
        if not merged["enums"]:
            del merged["enums"]
        if not merged["slots"]:
            del merged["slots"]
        if not merged["types"]:
            del merged["types"]
        # Anchor the merged schema inside ``path`` so any relative ``imports:``
        # carried over from the merged files resolve relative to the schema
        # directory rather than the process CWD (datahelix #66). Sibling files
        # are already merged inline above; an anchored re-resolution of their
        # import names is idempotent, and out-of-directory relative imports
        # (e.g. ``../common/base``) now resolve correctly too.
        return cls.from_dict(merged, source_file=path / "__mosaic_merged__.yaml")

    @classmethod
    def from_dict(
        cls, data: dict, *, source_file: Optional[Union[str, Path]] = None
    ) -> "SchemaRegistry":
        """Build a registry from a schema ``dict``.

        *source_file* anchors relative ``imports:`` resolution (see
        :func:`_schema_view_from_text`); it defaults to ``None`` for callers
        whose schema dict carries no relative imports.
        """
        return cls(
            _schema_view_from_text(yaml.safe_dump(data), source_file=source_file)
        )

    @classmethod
    def from_yaml(cls, yaml_text: str) -> "SchemaRegistry":
        return cls(SchemaView(yaml_text, importmap=_bundled_importmap()))

    def with_loader_fragments(
        self,
        specs: list[LoaderFragmentSpec],
        *,
        installed_loader_names: Optional[set[str]] = None,
    ) -> "SchemaRegistry":
        """Return a new registry whose schema includes ``specs`` merged in.

        Thin convenience wrapper over :func:`merge_loader_fragments` so the
        reference-loader install path (sec2 §2.14.5) can hand off a batch of
        fragments without callers needing to thread the SchemaView themselves.
        Original registry is not mutated.
        """
        merged_sv = merge_loader_fragments(
            self._sv, specs, installed_loader_names=installed_loader_names
        )
        # Resolve consumer `range: <loader>:<Class>` CURIEs against the now-
        # merged loader classes so cross-loader references are non-advisory
        # (issue #67, layer 3). No-op when the consumer used bare class names.
        merged_sv = _resolve_loader_prefixed_ranges(merged_sv)
        return SchemaRegistry(merged_sv)

    @property
    def schema_view(self) -> SchemaView:
        return self._sv

    def value_type_classes(self) -> frozenset[str]:
        """Names of classes that are inline VALUE types, not entities.

        Schema-driven (issue #90): a class is a value type when it has no
        identifier slot and is not a tree-root — it has no ``id``/lifecycle,
        gets no table or typed-client accessor, and is stored inline (one
        JSON TEXT column) on the slot that ranges it. ``ExternalReference``
        is the framework's built-in example; identifier-less domain value
        objects (``Mass``, ``Volume``, ``Concentration``) qualify too and
        round-trip identically. See :func:`is_value_type`.
        """
        return self._value_type_classes

    def tree_root_class_name(self) -> str:
        """Name of the synthesized tree-root class (``_HippoInstanceBundle``).

        Callers pass this to :meth:`validate` to validate a LinkML-native
        instance YAML bundle (one multivalued slot per concrete class).
        See sec9 PR 3.1.

        The tree-root is not a domain class — :meth:`class_names`,
        :meth:`has_class`, :meth:`get_class`, and :meth:`induced_slots`
        do not include or accept it. Use :meth:`tree_root_class` and
        :meth:`tree_root_slots` for direct introspection.
        """
        return TREE_ROOT_CLASS_NAME

    def tree_root_class(self) -> ClassDefinition:
        """The synthesized tree-root ``ClassDefinition``.

        Held on the registry, not the SchemaView, so DDL generation,
        schema-diff, and the typed-client surface stay unaware of it.
        """
        return self._tree_root_class

    def tree_root_slots(self) -> list[SlotDefinition]:
        """Multivalued tree-root slots — one per concrete class."""
        return list(self._tree_root_class.attributes.values())

    def class_names(self) -> list[str]:
        return list(self._sv.all_classes().keys())

    def get_class(self, name: str) -> Optional[ClassDefinition]:
        return self._sv.get_class(name)

    def has_class(self, name: str) -> bool:
        return name in self._sv.all_classes()

    def induced_slots(self, class_name: str) -> list[SlotDefinition]:
        return self._sv.class_induced_slots(class_name)

    def identifier_slot(self, class_name: str) -> Optional[SlotDefinition]:
        return self._sv.get_identifier_slot(class_name)

    def required_slots(self, class_name: str) -> list[SlotDefinition]:
        return [s for s in self.induced_slots(class_name) if s.required]

    def searchable_slots(self, class_name: str) -> list[tuple[SlotDefinition, str]]:
        """Slots annotated with ``hippo_search``; returns (slot, mode) pairs."""
        pairs: list[tuple[SlotDefinition, str]] = []
        for slot in self.induced_slots(class_name):
            mode = annotation_value(slot, HIPPO_SEARCH)
            if mode is not None:
                pairs.append((slot, str(mode)))
        return pairs

    def reference_slots(self, class_name: str) -> list[tuple[str, str]]:
        """(slot_name, target_class) pairs for slots whose range is another class."""
        known = set(self._sv.all_classes().keys())
        refs: list[tuple[str, str]] = []
        for slot in self.induced_slots(class_name):
            rng = slot.range
            if rng and rng in known:
                refs.append((slot.name, rng))
        return refs

    def multivalued_reference_slots(
        self, class_name: str
    ) -> list[tuple[str, str]]:
        """(slot_name, target_class) for multivalued slots ranged on an entity class.

        These slots get neither a per-class column nor a junction table (the
        DDL generator filters LinkML's linktables), so Mosaic persists them as
        relationships keyed by the slot name (issue #79 / ADR-0002). Value
        types (``ExternalReference`` and any identifier-less value object) are
        excluded — they store inline as JSON TEXT regardless of cardinality.

        A slot explicitly marked ``inlined``/``inlined_as_list`` is also
        excluded even when its range declares an identifier: the schema
        author has said the objects embed inline, so they must round-trip as
        structured JSON rather than being coerced into id-only relationship
        rows (issue #121).

        An ``inverse``-declared slot is also excluded (ADR-0011): it is a
        *virtual* reverse edge resolved through the forward FK column and
        must never be materialized as relationship rows — see
        :meth:`inverse_reference_slots`.
        """
        known = set(self._sv.all_classes().keys())
        value_types = self.value_type_classes()
        refs: list[tuple[str, str]] = []
        for slot in self.induced_slots(class_name):
            rng = slot.range
            if (
                slot.multivalued
                and rng
                and rng in known
                and rng not in value_types
                and not slot.inlined
                and not slot.inlined_as_list
                and not getattr(slot, "inverse", None)
            ):
                refs.append((slot.name, rng))
        return refs

    def inverse_reference_slots(self, class_name: str) -> list[InverseSlot]:
        """``inverse``-declared multivalued slots on ``class_name`` (ADR-0011).

        Each is a virtual reverse edge: ``Donor.samples: {range: Sample,
        multivalued: true, inverse: donor}`` yields
        ``InverseSlot("samples", "Sample", "donor")``. Validated at
        registry construction (:func:`_validate_inverse_slots`), so every
        entry here has a single-valued forward slot on its target class
        pointing back at the declaring class. These slots get no column,
        no link table, and no relationship rows; storage adapters hydrate,
        filter, and count them through ``forward_slot``. Cached per class.
        """
        cached = self._inverse_slots_cache.get(class_name)
        if cached is not None:
            return cached
        out: list[InverseSlot] = []
        if self.has_class(class_name):
            for slot in self.induced_slots(class_name):
                inverse = getattr(slot, "inverse", None)
                if inverse and slot.multivalued and slot.range:
                    out.append(
                        InverseSlot(
                            name=slot.name,
                            target_class=str(slot.range),
                            forward_slot=str(inverse),
                            target_classes=concrete_class_closure(
                                self._sv, str(slot.range)
                            ),
                        )
                    )
        self._inverse_slots_cache[class_name] = out
        return out

    def non_column_slot_names(self, class_name: str) -> set[str]:
        """Slot names on ``class_name`` that own **no column** on its table.

        The union of relationships-backed multivalued reference slots
        (ADR-0002) and virtual inverse slots (ADR-0011). The DDL
        generators and ``schema_diff`` share this one definition so none
        of them "discovers" a column that was never supposed to exist.
        """
        names = {name for name, _ in self.multivalued_reference_slots(class_name)}
        names.update(inv.name for inv in self.inverse_reference_slots(class_name))
        return names

    def has_subclasses(self, class_name: str) -> bool:
        """True if ``class_name`` has any proper descendant class.

        Marks a class as a polymorphic *base* — instances under its tree-root
        accessor may be subtype instances that must be dispatched to a concrete
        subclass rather than stored as the base (issue #80).
        """
        return bool(self._sv.class_descendants(class_name, reflexive=False))

    def concrete_subclasses(self, class_name: str) -> list[str]:
        """Names of concrete (non-abstract) proper descendants of ``class_name``.

        The set of classes an instance under a ``class_name`` collection could
        legitimately be — surfaced in remediation messages so authors see the
        valid type-designator values (issue #80).
        """
        out: list[str] = []
        for name in self._sv.class_descendants(class_name, reflexive=False):
            cls = self._sv.get_class(name)
            if cls is not None and not getattr(cls, "abstract", False):
                out.append(name)
        return out

    def is_polymorphic_base(self, class_name: str) -> bool:
        """True if a reference *ranged on* ``class_name`` cannot be a plain FK.

        A slot's declared range is a polymorphic base when its instances do not
        all live in one table an FK could point at:

        * an **abstract** base has no SQL table of its own (the DDL generators
          emit none), so an FK to it references a non-existent table; and
        * a **concrete base with concrete subclasses** scatters subtype
          instances into their own per-subclass tables — ``mosaic ingest``
          dispatches a subtype instance to its own table via the
          ``designates_type`` discriminator (issue #80), so the base table is
          never populated for those referents and an FK to it fails
          ``FOREIGN KEY constraint`` for any subtype referent (issue #93).

        In both cases the reference is stored as a plain TEXT id column instead
        of a foreign key; the id is resolved across the subtype tables at read
        time. A concrete leaf class (no subclasses) is *not* a polymorphic base
        — its FK is safe and kept.
        """
        cls = self._sv.get_class(class_name)
        if cls is None:
            return False
        if getattr(cls, "abstract", False):
            return True
        return bool(self.concrete_subclasses(class_name))

    def type_designator_slot(self, class_name: str) -> Optional[SlotDefinition]:
        """The induced slot marked ``designates_type: true`` on ``class_name``.

        This is the polymorphism discriminator (e.g. ``category``): an
        instance's value for this slot names the concrete subclass it should
        be instantiated as (issue #80). ``None`` when the class declares no
        type designator — i.e. it is not a polymorphic dispatch base.
        Inherited from a base via ``induced_slots`` so subclasses see it too.
        """
        for slot in self.induced_slots(class_name):
            if getattr(slot, "designates_type", False):
                return slot
        return None

    def resolve_designated_class(
        self, base_class: str, value: str
    ) -> Optional[str]:
        """Resolve a ``designates_type`` value to a class under ``base_class``.

        Returns the name of the class (``base_class`` itself or one of its
        descendants) that ``value`` designates, or ``None`` if no candidate
        matches. Per LinkML the designator value may be the class name, its
        ``class_uri``, or the CURIE/short form of either — matched in that
        order (issue #80). Concreteness is the caller's check.
        """
        if value is None:
            return None
        candidates = self._sv.class_descendants(base_class, reflexive=True)
        for cand in candidates:
            cls = self._sv.get_class(cand)
            if cls is None:
                continue
            if cand == value:
                return cand
            class_uri = getattr(cls, "class_uri", None)
            if class_uri and (
                class_uri == value or class_uri.split(":")[-1] == value
            ):
                return cand
        return None

    def external_xref_slots(self, class_name: str) -> list[SlotDefinition]:
        """Slots annotated ``hippo_external_xref: true`` on ``class_name``.

        The annotation is only meaningful on slots whose range is the
        ``ExternalReference`` value type; annotating any other slot is a
        schema defect surfaced as :class:`SchemaError` (consistent with
        the fail-at-startup contract of sec9 §9.10 — schemas declare
        intent, adapters enforce capability, misdeclarations fail loudly).
        """
        out: list[SlotDefinition] = []
        for slot in self.induced_slots(class_name):
            if not annotation_value(slot, HIPPO_EXTERNAL_XREF):
                continue
            if slot.range != EXTERNAL_REFERENCE_CLASS:
                from mosaic.core.exceptions import SchemaError

                raise SchemaError(
                    f"Slot {class_name}.{slot.name} declares "
                    f"`hippo_external_xref: true` but its range is "
                    f"{slot.range!r}; the annotation is only valid on "
                    f"slots ranged against {EXTERNAL_REFERENCE_CLASS!r}.",
                    error_code="HIPPO_EXTERNAL_XREF_RANGE",
                )
            out.append(slot)
        return out

    def indexed_slots(self, class_name: str) -> list[tuple[SlotDefinition, bool]]:
        """(slot, partial) pairs for slots annotated with ``hippo_index``."""
        out: list[tuple[SlotDefinition, bool]] = []
        for slot in self.induced_slots(class_name):
            if annotation_value(slot, HIPPO_INDEX):
                partial = bool(annotation_value(slot, HIPPO_INDEX_PARTIAL))
                out.append((slot, partial))
        return out

    def unique_slot_names(self, class_name: str) -> list[str]:
        return [
            s.name
            for s in self.induced_slots(class_name)
            if annotation_value(s, HIPPO_UNIQUE)
        ]

    def boolean_slot_names(self, class_name: str) -> set[str]:
        """Names of scalar slots whose range is ``boolean``.

        Storage adapters coerce Python ``bool`` to ``0``/``1`` on write;
        this set tells the read side which columns to reverse back to
        ``bool`` (see the SQLite adapter's ``_decode_column_value``).
        Multivalued boolean slots are excluded — they are JSON-encoded as
        lists and round-trip their native ``true``/``false`` already.
        """
        return {
            s.name
            for s in self.induced_slots(class_name)
            if s.range == "boolean" and not s.multivalued
        }

    def string_slot_names(self, class_name: str) -> set[str]:
        """Names of scalar slots whose range is ``string``.

        A string slot's stored value must round-trip verbatim; the read
        side must never JSON-decode it, even when the text happens to
        parse as a JSON container (e.g. Aperture's control-plane
        ``payload`` slots carry serialized ``{"v": …, "data": …}``
        envelopes — see the SQLite adapter's ``_decode_column_value``).
        Multivalued string slots are excluded: they are stored as JSON
        arrays and DO need decoding.
        """
        return {
            s.name
            for s in self.induced_slots(class_name)
            if s.range == "string" and not s.multivalued
        }

    def base_scalar_range(self, range_name: Optional[str]) -> str:
        """Resolve a slot range to its base LinkML scalar type name.

        Custom types declared with ``typeof:`` chains resolve to the
        builtin they derive from (e.g. a ``AgeInYears`` type with
        ``typeof: integer`` resolves to ``"integer"``). Classes, enums,
        and unknown names resolve to themselves. Consumers use this to
        pick typed behavior per slot — the Postgres adapter's JSONB
        filter casts and the GraphQL filter-operator selection
        (ADR-0006) — so both read one resolution.
        """
        rng = range_name or "string"
        types = self._sv.all_types(imports=True) or {}
        seen: set[str] = set()
        while rng in types and rng not in seen:
            seen.add(rng)
            typeof = getattr(types[rng], "typeof", None)
            if not typeof:
                break
            rng = typeof
        return rng

    def slot_base_ranges(self, class_name: str) -> dict[str, str]:
        """Map each induced scalar slot name to its base scalar range.

        Only single-valued slots appear (multivalued slots store JSON
        arrays — no scalar comparison applies). Unknown classes yield an
        empty mapping.
        """
        if not self.has_class(class_name):
            return {}
        return {
            s.name: self.base_scalar_range(s.range)
            for s in self.induced_slots(class_name)
            if not s.multivalued
        }

    def reference_loaders(self) -> list[str]:
        """Names of concrete classes that are subclasses of ``ReferenceLoader``.

        Returns every non-abstract class in the merged schema whose ancestry
        includes ``ReferenceLoader``.  Plugin schema fragments that declare a
        loader-specific subclass (``is_a: ReferenceLoader``) appear here after
        their fragment is merged into the live ``SchemaView``.

        Used for discoverability — callers can enumerate which loader types are
        registered in the current deployment.  For provenance queries use the
        ``entity_type`` slot on individual instances.
        """
        out: list[str] = []
        for name, cls in self._sv.all_classes(imports=True).items():
            if cls.abstract:
                continue
            if name == "ReferenceLoader":
                continue
            ancestors = self._sv.class_ancestors(name)
            if "ReferenceLoader" in ancestors:
                out.append(name)
        return out

    def append_only_classes(self) -> set[str]:
        """Names of classes annotated ``hippo_append_only: true``.

        Adapters consult this set to reject UPDATE and DELETE against the
        corresponding tables per sec9 §9.4 / reference_hippo_ext.md. Only
        concrete (non-abstract) classes are returned — an abstract class
        with the annotation is purely declarative. Per sec9 §9.6 this set
        currently contains ``{"ProvenanceRecord"}``; domain schemas may
        mark additional append-only classes.
        """
        out: set[str] = set()
        for name in self.class_names():
            cls = self._sv.get_class(name)
            if cls is None or cls.abstract:
                continue
            if annotation_value(cls, HIPPO_APPEND_ONLY):
                out.add(name)
        return out

    def validate(self, instance: dict, target_class: str) -> list[str]:
        """Validate an instance dict; return a list of error messages (empty=valid).

        Kept for backward compatibility. New code should call
        :meth:`validate_envelope` to get the sec9 §9.9 envelope with
        tier-annotated failures.
        """
        report = self._validator.validate(instance, target_class)
        return [r.message for r in report.results]

    def validate_envelope(
        self, instance: dict, target_class: str
    ) -> "list[ValidationFailure]":
        """Validate an instance dict; return a list of ``ValidationFailure``
        objects with ``tier="linkml"`` for every schema-shape violation.

        Per sec9 §9.9 this is the LinkML-native tier output — types,
        patterns, enums, ranges, required, multivalued, unique_keys.
        """
        from mosaic.core.validation.validators import ValidationFailure

        report = self._validator.validate(instance, target_class)
        failures: list[ValidationFailure] = []
        for r in report.results:
            # LinkML's ``ValidationResult`` carries ``.instantiates`` (the
            # class), ``.type`` (rule category), and ``.message``. The
            # rule string is derived from the type; the field from the
            # message prefix when available.
            rule = getattr(r, "type", None) or "schema_violation"
            failures.append(
                ValidationFailure(
                    tier="linkml",
                    rule=str(rule),
                    message=r.message,
                    field=getattr(r, "source", None),
                    details={"target_class": target_class},
                )
            )
        return failures
