"""``mosaic_meta`` key/value access helpers.

The ``mosaic_meta`` table is stamped on every Mosaic database by both the
SQLite and Postgres adapters (see ``core/storage/adapters/sqlite_adapter.py``
and ``core/storage/adapters/postgres_adapter.py``). Reference-loader
install/upgrade machinery persists ``reference_versions`` and
``reference_entity_ids`` records here per spec §2.14 step 5.

The helpers operate directly on a DB-API connection so install/upgrade
verbs can keep their own transactional envelope without going through
``MosaicClient``. Values are JSON-serialised dicts; non-dict values are
rejected up-front.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def get_meta(conn: Any, key: str) -> dict | None:
    """Return the value stored under ``key`` in ``mosaic_meta``.

    Returns ``None`` when the row is absent. Raises ``ValueError`` when
    the stored value is not a JSON object.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM mosaic_meta WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row is None:
        return None
    raw = row[0] if not isinstance(row, dict) else row["value"]
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"mosaic_meta[{key!r}] is not a JSON object (got {type(parsed).__name__})"
        )
    return parsed


def set_meta(conn: Any, key: str, value: dict) -> None:
    """Upsert ``value`` under ``key`` in ``mosaic_meta``.

    Stamps ``updated_at`` with the current UTC time. The caller owns the
    transaction boundary — no implicit commit here.
    """
    if not isinstance(value, dict):
        raise TypeError(
            f"mosaic_meta values must be dicts (got {type(value).__name__})"
        )
    payload = json.dumps(value, sort_keys=True)
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO mosaic_meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, payload, now),
    )
