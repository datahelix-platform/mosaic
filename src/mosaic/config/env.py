"""``MOSAIC_*`` environment-variable helper.

Single choke point for runtime environment lookups: readers ask for the
un-prefixed variable name and get ``MOSAIC_<name>``. The legacy
``HIPPO_<name>`` spellings are no longer read (ADR-0015).
"""

from __future__ import annotations

import os
from typing import Optional


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return ``$MOSAIC_<name>`` (``name`` is the un-prefixed suffix,
    e.g. ``"CACHE_DIR"``), or ``default`` when it is unset."""
    return os.environ.get(f"MOSAIC_{name}", default)
