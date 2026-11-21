"""External-reference (xref) side-index helpers (issue #48).

The ``mosaic_external_xref`` slot annotation opts an ``ExternalReference``-
ranged slot into reverse-lookup behavior. The behavior is implemented as an
annotation-driven side table — the same pattern the codebase uses for FTS5
sync (``mosaic_search``) — maintained by the storage adapter's write path in
the SAME SQL transaction as the entity write:

    mosaic_xref_index(entity_id, entity_type, slot, system, value)

Availability semantics (mirrors the ``mosaic_unique`` partial-index
precedent from PTS-348, "unique among live records"): index rows exist
ONLY for available (``is_available = 1``) entities. Every write-path hook
re-derives the entity's rows from its just-written state — rows are
deleted outright when the entity is (or becomes) unavailable, and
re-inserted when it becomes available again. A plain
``UNIQUE (system, value)`` on the side table therefore enforces global
uniqueness among live entities without any partial-index predicate.
"""

from __future__ import annotations

import json
from typing import Any

#: Name of the side-index table.
XREF_TABLE = "mosaic_xref_index"

#: DDL for the side table + its indexes. Emitted by the DDL generators
#: only when at least one slot in the schema carries the annotation.
XREF_TABLE_DDL: list[str] = [
    f"""CREATE TABLE IF NOT EXISTS {XREF_TABLE} (
\tentity_id TEXT NOT NULL,
\tentity_type TEXT NOT NULL,
\tslot TEXT NOT NULL,
\tsystem TEXT NOT NULL,
\tvalue TEXT NOT NULL
);""",
    f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{XREF_TABLE}_unique" '
    f"ON {XREF_TABLE} (system, value);",
    f'CREATE INDEX IF NOT EXISTS "idx_{XREF_TABLE}_entity" '
    f"ON {XREF_TABLE} (entity_id);",
]


def extract_xref_pairs(raw: Any) -> list[tuple[str, str]]:
    """Normalize one slot value into ``(system, value)`` pairs.

    Accepts the shapes the write path can see for an
    ``ExternalReference``-ranged slot: a dict (single-valued), a list of
    dicts (multivalued), or the JSON-encoded TEXT form of either (the
    storage representation). Entries that are not dicts or that lack a
    non-empty ``system``/``value`` are skipped — shape enforcement is the
    validator's job (the LinkML schema requires both), not storage's.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    items = raw if isinstance(raw, list) else [raw]
    pairs: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        system = item.get("system")
        value = item.get("value")
        if not system or not value:
            continue
        pairs.append((str(system), str(value)))
    return pairs
