"""JSON-safe serialization for the MCP resources.

Deliberately kept OUT of ``mosaic.core.schema_typing``: that module's
dataclasses (``SlotModel``, ``EntityTypeModel``, ``FieldCapability``,
``EntityCapability``) are transport-neutral by design (issue #47/#181) —
adding an MCP-shaped ``as_dict()`` there would be the first transport
leaking into the shared core. This module does the reshaping instead,
the same way ``mosaic.graphql.resolvers._entity_type_info`` reshapes the
same dataclasses into Strawberry types without the core module knowing
GraphQL exists.
"""

from __future__ import annotations

from typing import Any

from mosaic.core.schema_typing import EntityCapability, EntityTypeModel, SlotModel


def slot_model_to_dict(slot: SlotModel) -> dict[str, Any]:
    return {
        "name": slot.name,
        "kind": slot.kind.value,
        "range": slot.range,
        "role": slot.role.value,
        "required": slot.required,
        "multivalued": slot.multivalued,
        "identifier": slot.identifier,
        "has_default": slot.has_default,
        "description": slot.description,
        "target_entity_type": slot.target_class,
        "enum_name": slot.enum_name,
        "enum_values": list(slot.enum_values),
        "is_external_xref": slot.is_external_xref,
        # ADR-0011: non-null names the forward FK slot on target_entity_type
        # that this virtual reverse edge is derived from. Read-only.
        "inverse_of": slot.inverse_of,
    }


def entity_type_model_to_dict(entity: EntityTypeModel) -> dict[str, Any]:
    return {
        "name": entity.class_name,
        "accessor_name": entity.accessor_name,
        "description": entity.description,
        "fields": [slot_model_to_dict(f) for f in entity.fields],
    }


#: What ``QuerySpec.columns`` can name on this deployment (issue #215,
#: ADR-0029 honest degradation). ``"anchor"`` = anchor-owned slots only; a
#: deployment that predates projection omits the key, so a client tells the
#: two apart without probing. Widens (e.g. to ``"traversal"``) only if the
#: second ADR-0009 follow-up is ever approved.
COLUMN_PROJECTION = "anchor"


def entity_capability_to_dict(entity: EntityCapability) -> dict[str, Any]:
    return {
        "name": entity.class_name,
        "column_projection": COLUMN_PROJECTION,
        "accessor_name": entity.accessor_name,
        "description": entity.description,
        "search_available": entity.search_available,
        "fields": [
            {
                **slot_model_to_dict(f.slot),
                "filter_ops": [op.value for op in f.filter_ops],
                "predicate": f.predicate,
                "orderable": f.orderable,
                "aggregatable": f.aggregatable,
                "range_queryable": f.range_queryable,
                "searchable": f.searchable,
            }
            for f in entity.fields
        ],
    }
