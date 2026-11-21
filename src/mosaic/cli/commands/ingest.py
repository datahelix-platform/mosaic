"""LinkML-native instance YAML ingest for the Mosaic CLI.

``mosaic ingest`` accepts a tree-root LinkML instance bundle: a YAML mapping
whose top-level keys are class accessors (``samples:``, ``projects:`` etc.)
and whose values are lists of instance dicts. Identity is by the
``id`` slot on each instance; re-ingest of an existing id updates that
entity in place. There is no separate wrapper format and no top-level
``external_id`` field — register external IDs by including ``external_ids:``
entries in the same bundle (see ``mosaic_core.ExternalID``).

CSV/JSON operational data files are not accepted here; those belong to
Cappella.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from mosaic.core.exceptions import EntityNotFoundError


class IngestError(Exception):
    """Raised when an instance YAML file is invalid or cannot be processed."""


@dataclass
class IngestResult:
    """Result of a LinkML-native instance ingest operation."""

    source_file: str
    created: int = 0
    updated: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "created": self.created,
            "updated": self.updated,
            "errors": self.errors,
            "error_messages": self.error_messages,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


def ingest_linkml_yaml(
    path: Path | str,
    client: Any,
    registry: Any,
) -> IngestResult:
    """Ingest a LinkML-native instance YAML bundle.

    The file must be a YAML mapping whose keys are tree-root accessor
    slots (one per concrete class in ``registry``) and whose values are
    lists of instance dicts. The bundle is validated against the
    registry's synthesized tree-root class before any writes occur; if
    validation fails the function raises :class:`IngestError` and writes
    nothing.

    Per-instance writes go through :meth:`MosaicClient.put`. Identity is
    by the ``id`` slot on each instance: when ``id`` matches an existing
    entity, ``put`` updates it in place; otherwise a new entity is
    created.

    Args:
        path: Path to the YAML file.
        client: ``MosaicClient`` instance.
        registry: ``SchemaRegistry`` whose tree-root class and accessor
            slot mapping describe the bundle shape.

    Returns:
        :class:`IngestResult` with per-entity counts.

    Raises:
        IngestError: If the file is missing, not a YAML mapping, or
            fails LinkML validation against the tree-root.
    """
    path = Path(path)

    if not path.exists():
        raise IngestError(f"File not found: {path}")

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise IngestError(f"Failed to parse YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise IngestError(
            f"Instance file must be a YAML mapping (tree-root bundle); got "
            f"{type(parsed).__name__}. Note: CSV/JSON data files are not "
            "accepted by 'mosaic ingest' — use Cappella for operational data."
        )

    tree_root = registry.tree_root_class_name()
    errors = registry.validate(parsed, tree_root)
    if errors:
        raise IngestError(
            f"Bundle does not validate against {tree_root}: "
            + "; ".join(errors)
        )

    slot_to_class: dict[str, str] = {
        slot.name: slot.range for slot in registry.tree_root_slots()
    }

    result = IngestResult(source_file=str(path))

    # Write the whole bundle inside one staged transaction so foreign-key
    # checks are deferred to commit (issue #95). This lets instances reference
    # each other in any order — forward references, and self-referential /
    # cyclic references (``A → B → A``, a self-loop) that no per-row insertion
    # order can satisfy — insert now and validate together at commit. On a
    # backend without staging this is a transparent no-op (each write commits
    # as before). A genuinely dangling reference still fails the commit and
    # rolls the whole bundle back.
    try:
        with client.staged_transaction():
            for slot_name, instances in parsed.items():
                declared_range = slot_to_class.get(slot_name)
                if declared_range is None:
                    # Tree-root validation already rejects unknown slots in
                    # closed-schema mode; guard against schema drift just in case.
                    result.errors += 1
                    result.error_messages.append(
                        f"Unknown tree-root slot {slot_name!r}"
                    )
                    continue
                if not isinstance(instances, list):
                    result.errors += 1
                    result.error_messages.append(
                        f"Slot {slot_name!r}: expected a list, got "
                        f"{type(instances).__name__}"
                    )
                    continue
                for idx, instance in enumerate(instances):
                    if not isinstance(instance, dict):
                        result.errors += 1
                        result.error_messages.append(
                            f"{slot_name}[{idx}]: expected a mapping, got "
                            f"{type(instance).__name__}"
                        )
                        continue
                    try:
                        target_class = _dispatch_class(
                            registry, declared_range, instance
                        )
                        _upsert_instance(client, target_class, instance, result)
                    except Exception as exc:
                        result.errors += 1
                        result.error_messages.append(
                            f"{slot_name}[{idx}] ({declared_range}): {exc}"
                        )
    except IngestError:
        raise
    except Exception as exc:
        # Deferred foreign-key checks (issue #95) run when the staged
        # transaction commits, so a reference that points at an id absent from
        # both the bundle and the database surfaces here — after the per-row
        # loop — and rolls the whole bundle back. Deferral trades per-row
        # attribution for the ability to insert cycles; report it as a
        # bundle-level failure so nothing is left partially written.
        raise IngestError(
            f"Bundle write failed and was rolled back: {exc}. A reference "
            f"likely points to an id that is not present in the bundle or the "
            f"database."
        ) from exc

    return result


def _dispatch_class(
    registry: Any,
    declared_range: str,
    instance: dict[str, Any],
) -> str:
    """Resolve the concrete class to instantiate for a single instance.

    When ``declared_range`` is a polymorphic base — it declares a
    ``designates_type`` slot (e.g. ``Sample.category``) — the instance's
    discriminator value selects the concrete subclass it is stored as, so
    subclass-specific fields persist and the instance is queryable as its
    real type (issue #80). When there is no designator the declared range is
    used directly.

    Raises:
        IngestError: if the discriminator value resolves to no subclass of
            ``declared_range`` or names an abstract class; or — the
            downcast guard — if ``declared_range`` is a polymorphic base
            (abstract, or concrete with subclasses and the instance carries
            fields the base cannot store) and no usable designator routes the
            instance to a concrete subclass. The message explains the fix; see
            ``docs/polymorphic-ingest.md``.
    """
    designator = registry.type_designator_slot(declared_range)
    if designator is not None:
        value = instance.get(designator.name)
        if value is not None:
            resolved = registry.resolve_designated_class(
                declared_range, str(value)
            )
            if resolved is None:
                raise IngestError(
                    f"Type designator {designator.name}={value!r} does not name "
                    f"{declared_range!r} or any of its subclasses. Set "
                    f"{designator.name!r} to one of "
                    f"{registry.concrete_subclasses(declared_range)}. "
                    f"{_DISPATCH_DOC_HINT}"
                )
            cls = registry.get_class(resolved)
            if cls is not None and getattr(cls, "abstract", False):
                raise IngestError(
                    f"Type designator {designator.name}={value!r} names abstract "
                    f"class {resolved!r}, which cannot be instantiated. Use a "
                    f"concrete subclass: one of "
                    f"{registry.concrete_subclasses(declared_range)}. "
                    f"{_DISPATCH_DOC_HINT}"
                )
            return resolved

    # No usable type designator. Storing under the declared range is safe ONLY
    # when it is concrete AND not a polymorphic base hiding subtype fields —
    # otherwise we'd silently drop the subtype's slots (issue #80).
    cls = registry.get_class(declared_range)
    is_abstract = cls is not None and getattr(cls, "abstract", False)
    if is_abstract or registry.has_subclasses(declared_range):
        base_slots = {s.name for s in registry.induced_slots(declared_range)}
        extra = sorted(k for k in instance if k not in base_slots)
        if is_abstract or extra:
            raise IngestError(
                _downcast_message(registry, declared_range, designator, extra, is_abstract)
            )
    return declared_range


#: Trailing hint appended to every dispatch error, pointing at the dedicated
#: guide so non-expert LinkML authors can self-serve the fix.
_DISPATCH_DOC_HINT = (
    "See the 'Polymorphic ingest' guide (docs/polymorphic-ingest.md)."
)


def _downcast_message(
    registry: Any,
    declared_range: str,
    designator: Any,
    extra: list[str],
    is_abstract: bool,
) -> str:
    """Build an actionable error for a refused polymorphic-base downcast.

    Two failure shapes share remediation: an abstract base cannot be stored at
    all, and a concrete base carrying subtype-only fields would silently drop
    them. The fix depends on whether the base already declares a
    ``designates_type`` discriminator (set its value per instance) or not (add
    one, or ingest under the concrete subclass collection).
    """
    subclasses = registry.concrete_subclasses(declared_range)
    if is_abstract:
        lead = (
            f"Cannot ingest an instance under the {declared_range!r} collection: "
            f"{declared_range!r} is an abstract base class and is never stored "
            f"directly — each instance must be dispatched to a concrete subclass."
        )
    else:
        lead = (
            f"An instance under the {declared_range!r} collection carries "
            f"field(s) {extra} that {declared_range!r} does not define. Because "
            f"{declared_range!r} has subclasses this is a subtype instance, but "
            f"storing it as {declared_range!r} would silently drop those "
            f"field(s)."
        )

    if designator is not None:
        fix = (
            f"Set the type-designator slot {designator.name!r} on each such "
            f"instance to its concrete subclass name (one of {subclasses})."
        )
    else:
        fix = (
            f"{declared_range!r} declares no type designator, so Mosaic cannot "
            f"tell which subclass to store. Either (1) mark a discriminator slot "
            f"on {declared_range!r} with `designates_type: true` and set it on "
            f"each instance to the concrete subclass name (one of {subclasses}), "
            f"or (2) ingest each instance under its concrete subclass collection "
            f"instead."
        )
    return f"{lead} {fix} {_DISPATCH_DOC_HINT}"


def _upsert_instance(
    client: Any,
    entity_type: str,
    data: dict[str, Any],
    result: IngestResult,
) -> None:
    """Write a single instance via ``client.put``, updating result counts."""
    entity_id = data.get("id")
    if entity_id is None:
        client.put(entity_type=entity_type, data=data)
        result.created += 1
        return

    try:
        client.get(entity_type, entity_id)
        existed = True
    except EntityNotFoundError:
        existed = False

    # EntityTypeConflictError (id already registered under a different
    # class) propagates to the caller's per-row exception handler, which
    # bumps ``result.errors`` — it must not be miscounted as created/updated.
    client.put(entity_type=entity_type, data=data, entity_id=entity_id)
    if existed:
        result.updated += 1
    else:
        result.created += 1
