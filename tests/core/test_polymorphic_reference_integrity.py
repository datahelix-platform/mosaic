"""Referential integrity for polymorphic-base references (issue #127).

A single-valued reference whose range is a **polymorphic base** (abstract, or
concrete with concrete subclasses) gets no SQL foreign key — subtype instances
are dispatched to their own per-class tables, so no single table can be the FK
target (issue #93), and the DDL drops the constraint. Before this fix the
referential-integrity check was dropped with it: a dangling reference on such a
slot was silently accepted on both `put` and `ingest` (`errors=0`, no
exception), while a non-polymorphic reference (a real FK) still caught it.

The check is now performed in the application layer against the
``_entity_registry`` cross-class index, deferred to commit so forward
references within a staged bundle still resolve — mirroring the deferred-FK
behavior for non-polymorphic references (issue #95).
"""

import os
import tempfile

import pytest

import mosaic
from mosaic.core.exceptions import DanglingReferenceError

# Activity is an ABSTRACT polymorphic base (Assay is a concrete subtype);
# Container is a CONCRETE base with a concrete subclass (Box) — both are
# polymorphic bases per ADR/issue #93, neither gets a FK. Dataset references
# both, plus a non-polymorphic Owner (a plain FK, kept) as the control.
SCHEMA = """
id: https://example.org/bb
name: bb
prefixes: {linkml: 'https://w3id.org/linkml/'}
imports: [linkml:types, mosaic_core]
default_range: string
classes:
  Activity:
    abstract: true
    is_a: Entity
    attributes: {category: {range: string, designates_type: true}}
  Assay:
    is_a: Activity
    attributes: {label: {range: string}}
  Container:
    is_a: Entity
    attributes:
      kind: {range: string, designates_type: true}
      label: {range: string}
  Box:
    is_a: Container
  Owner:
    is_a: Entity
    attributes: {label: {range: string}}
  Dataset:
    is_a: Entity
    attributes:
      label: {range: string}
      produced_by: {range: Activity}
      stored_in: {range: Container}
      owner: {range: Owner}
"""


def _client(tmpdir: str):
    p = os.path.join(tmpdir, "s.yaml")
    with open(p, "w") as fh:
        fh.write(SCHEMA)
    return mosaic.client_for_schema(p, database_url=os.path.join(tmpdir, "h.db"))


class TestPolymorphicReferenceIntegrity:
    @pytest.fixture
    def client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _client(tmpdir)
            c.put("Assay", {"category": "Assay", "label": "a1"}, entity_id="ACT-1")
            c.put("Box", {"kind": "Box", "label": "b1"}, entity_id="BOX-1")
            c.put("Owner", {"label": "o1"}, entity_id="OWN-1")
            yield c

    def test_valid_abstract_base_reference_accepted(self, client) -> None:
        client.put("Dataset", {"label": "d", "produced_by": "ACT-1"}, entity_id="DS-1")
        assert client.get("Dataset", "DS-1")["data"]["produced_by"] == "ACT-1"

    def test_valid_concrete_base_reference_accepted(self, client) -> None:
        client.put("Dataset", {"label": "d", "stored_in": "BOX-1"}, entity_id="DS-1")
        assert client.get("Dataset", "DS-1")["data"]["stored_in"] == "BOX-1"

    def test_dangling_abstract_base_reference_rejected(self, client) -> None:
        with pytest.raises(DanglingReferenceError) as exc:
            client.put(
                "Dataset", {"label": "d", "produced_by": "NOPE"}, entity_id="DS-1"
            )
        assert exc.value.slot == "produced_by"
        assert exc.value.target_id == "NOPE"
        # Rolled back — nothing persisted.
        from mosaic.core.exceptions import EntityNotFoundError

        with pytest.raises(EntityNotFoundError):
            client.get("Dataset", "DS-1")

    def test_dangling_concrete_base_reference_rejected(self, client) -> None:
        with pytest.raises(DanglingReferenceError):
            client.put(
                "Dataset", {"label": "d", "stored_in": "GHOST"}, entity_id="DS-1"
            )

    def test_wrong_type_reference_rejected(self, client) -> None:
        # OWN-1 exists, but Owner is not within the Activity subtree.
        with pytest.raises(DanglingReferenceError, match="not 'Activity'"):
            client.put(
                "Dataset", {"label": "d", "produced_by": "OWN-1"}, entity_id="DS-1"
            )

    def test_forward_reference_in_staged_bundle_resolves(self, client) -> None:
        # A Dataset referencing an Assay created later in the same staged
        # bundle must resolve, exactly as a deferred FK would (issue #95).
        with client.staged_transaction():
            client.put(
                "Dataset", {"label": "d", "produced_by": "ACT-2"}, entity_id="DS-2"
            )
            client.put("Assay", {"category": "Assay", "label": "a2"}, entity_id="ACT-2")
        assert client.get("Dataset", "DS-2")["data"]["produced_by"] == "ACT-2"

    def test_dangling_in_staged_bundle_rolls_back(self, client) -> None:
        from mosaic.core.exceptions import EntityNotFoundError

        with pytest.raises(DanglingReferenceError):
            with client.staged_transaction():
                client.put(
                    "Dataset", {"label": "d", "produced_by": "GHOST"}, entity_id="DS-3"
                )
        with pytest.raises(EntityNotFoundError):
            client.get("Dataset", "DS-3")

    def test_update_to_dangling_reference_rejected(self, client) -> None:
        client.put("Dataset", {"label": "d", "produced_by": "ACT-1"}, entity_id="DS-1")
        with pytest.raises(DanglingReferenceError):
            client.put(
                "Dataset", {"label": "d", "produced_by": "NOPE"}, entity_id="DS-1"
            )

    def test_non_polymorphic_reference_still_enforced(self, client) -> None:
        # Control: Dataset.owner -> Owner (a concrete leaf) keeps its SQL FK,
        # so a dangling value is rejected by the database layer (not our new
        # app-level check). Either way the write must fail.
        with pytest.raises(Exception):
            client.put(
                "Dataset", {"label": "d", "owner": "NO-OWNER"}, entity_id="DS-1"
            )

    def test_null_reference_is_fine(self, client) -> None:
        # Omitting the reference entirely must not trip the check.
        client.put("Dataset", {"label": "d"}, entity_id="DS-1")
        assert client.get("Dataset", "DS-1")["data"].get("produced_by") in (None, "")
