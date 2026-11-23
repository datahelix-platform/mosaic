"""Kind check for kind-specific lifecycle writes (#289).

A write addressed to a kind (``set_availability_bulk("Blood", ids)``,
``supersede_entity(id, ..., entity_type="Blood")``, ``delete("Blood", id)``)
must act only on an id whose stored row is of that kind. The accepted set is
the kind's concrete closure, the same set its collection and by-id ``get``
answer for (ADR-0014 Decision 1, D3 amendment, and the 2026-11-23 note on
lifecycle writes): the kind itself, plus its concrete subclasses. A parent's,
sibling's or unrelated class's row is refused with
:class:`~mosaic.core.exceptions.EntityKindMismatchError`.

``update`` / ``replace`` / ``put`` keep their exact-type rule
(:class:`~mosaic.core.exceptions.EntityTypeConflictError`); they do not use
this module.
"""

from __future__ import annotations

from typing import Any

from mosaic.core.exceptions import EntityKindMismatchError


def kind_accepts(storage: Any, requested: str, actual: str) -> bool:
    """Whether a row of class ``actual`` may be written as ``requested``."""
    if actual == requested:
        return True
    registry = getattr(storage, "schema_registry", None)
    if registry is None:
        return False
    return actual in registry.concrete_closure(requested)


def check_kind(storage: Any, requested: str, actual: str, entity_id: str) -> None:
    """Raise :class:`EntityKindMismatchError` unless ``actual`` is ``requested``
    or one of its concrete subclasses."""
    if kind_accepts(storage, requested, actual):
        return
    raise EntityKindMismatchError(
        message=f"Entity not found as {requested}: {entity_id} is a {actual}",
        entity_type=requested,
        entity_id=entity_id,
        actual_entity_type=actual,
    )
