"""SQL trigger definitions for ProvenanceRecord immutability.

Enforces ``mosaic_append_only: true`` (sec9 §9.6 / Decision 9.6.C) at the
SQL level — UPDATE and DELETE against the ``ProvenanceRecord`` table are
rejected by SQLite itself, not just by the Python adapter. Direct-SQL
access (e.g., raw ``sqlite3`` CLI) bypasses Python-level checks but not
triggers, so this is the preferred enforcement mechanism.

The single ``BEFORE UPDATE`` trigger (without a column list) fires on
any column change and replaces the earlier per-column triggers, which
were brittle when slot names evolved.
"""

PROVENANCE_TABLE = "ProvenanceRecord"

TRIGGER_PREVENT_UPDATE = """
CREATE TRIGGER IF NOT EXISTS prevent_provenance_update
BEFORE UPDATE ON "ProvenanceRecord"
BEGIN
    SELECT RAISE(ABORT, 'Cannot update ProvenanceRecord: mosaic_append_only class');
END;
"""

TRIGGER_PREVENT_DELETE = """
CREATE TRIGGER IF NOT EXISTS prevent_provenance_delete
BEFORE DELETE ON "ProvenanceRecord"
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete ProvenanceRecord: mosaic_append_only class');
END;
"""

ALL_TRIGGERS = [
    TRIGGER_PREVENT_UPDATE,
    TRIGGER_PREVENT_DELETE,
]


def get_trigger_sql_list():
    """Return list of all trigger SQL statements."""
    return ALL_TRIGGERS
