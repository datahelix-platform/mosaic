"""Kind-specific lifecycle writes reject another kind's id (#289).

Runs on the closure fixture (``tests/support/closure_fixture.py``): ``br1`` is
a ``Brain``, ``bl1`` a ``Blood`` (kinds of abstract ``Sample``), ``pr1`` a
``Process`` and ``bb1`` a ``BankBrain`` (its concrete subclass), ``p1`` a
``Person`` and ``d1`` a ``Donor``.

The rules (ADR-0014, 2026-11-23 note on lifecycle writes):

- availability (single and bulk), supersede and delete addressed to kind
  ``K`` accept an id in ``concrete_class_closure(K)``: ``K``'s own rows and
  its concrete subclasses' rows. A parent's, sibling's or unrelated class's
  id is refused with ``EntityKindMismatchError`` (an ``EntityNotFoundError``;
  GraphQL ``NOT_FOUND``), naming both kinds, and nothing changes;
- supersede's ``replacement_id`` only has to exist, of any kind;
- update keeps its exact-type rule (``EntityTypeConflictError``).

The SDK cases are mixed into the SQLite (``tests/core``) and Postgres
(``tests/integration``) closure test classes, and so are the GraphQL cases.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mosaic.core.exceptions import (
    EntityKindMismatchError,
    EntityNotFoundError,
    EntityTypeConflictError,
)


def _row(client: Any, entity_type: str, entity_id: str) -> dict[str, Any]:
    return client.get(entity_type, entity_id, include_unavailable=True)


def _untouched(client: Any, entity_type: str, entity_id: str) -> None:
    row = _row(client, entity_type, entity_id)
    assert row["is_available"] is True
    assert row["superseded_by"] is None


class KindCheckSDKCases:
    """SDK: every kind-specific lifecycle write checks the id's kind."""

    # -- availability -------------------------------------------------------

    def test_availability_rejects_sibling_kind_and_changes_nothing(self, client):
        result = client.set_availability_bulk("Blood", ["br1"], is_available=False)
        assert (result["succeeded"], result["failed"]) == (0, 1)
        failure = result["failures"][0]
        assert failure["id"] == "br1"
        assert "Blood" in failure["error"] and "Brain" in failure["error"]
        assert failure["code"] == "NOT_FOUND"
        assert failure["actual_entity_type"] == "Brain"
        _untouched(client, "Brain", "br1")

    def test_availability_rejects_parent_and_unrelated_rows(self, client):
        for kind, eid in (("BankBrain", "pr1"), ("Donor", "p1"),
                          ("Process", "d1"), ("Sample", "a1")):
            result = client.set_availability_bulk(kind, [eid], is_available=False)
            assert result["failed"] == 1, (kind, eid)
            assert result["failures"][0]["code"] == "NOT_FOUND"
        for kind, eid in (("Process", "pr1"), ("Person", "p1"), ("Assay", "a1")):
            _untouched(client, kind, eid)

    def test_availability_on_own_kind_still_works(self, client):
        result = client.set_availability_bulk("Blood", ["bl1"], is_available=False)
        assert result["succeeded"] == 1
        assert _row(client, "Blood", "bl1")["is_available"] is False
        result = client.set_availability_bulk("Blood", ["bl1"], is_available=True)
        assert result["succeeded"] == 1
        assert _row(client, "Blood", "bl1")["is_available"] is True

    def test_availability_through_concrete_parent_writes_subclass_row(self, client):
        result = client.set_availability_bulk("Process", ["bb1"], is_available=False)
        assert result["succeeded"] == 1
        # The subclass's own row changed (not a no-op on Process's table).
        assert _row(client, "BankBrain", "bb1")["is_available"] is False
        assert {i["id"] for i in client.query("Process").items} == {"pr1"}

    def test_availability_bulk_mixes_successes_and_failures(self, client):
        result = client.set_availability_bulk(
            "Blood", ["bl1", "br1", "no-such-id"], is_available=False
        )
        assert (result["succeeded"], result["failed"]) == (1, 2)
        errors = {f["id"]: f["error"] for f in result["failures"]}
        assert errors["no-such-id"] == "Entity not found: no-such-id"
        assert "Brain" in errors["br1"]
        _untouched(client, "Brain", "br1")

    # -- supersede ------------------------------------------------------------

    def test_supersede_rejects_sibling_kind_and_changes_nothing(self, client):
        with pytest.raises(EntityKindMismatchError) as info:
            client.supersede_entity("br1", "bl1", entity_type="Blood")
        err = info.value
        assert isinstance(err, EntityNotFoundError)
        assert (err.entity_type, err.actual_entity_type) == ("Blood", "Brain")
        assert "Blood" in str(err) and "Brain" in str(err)
        _untouched(client, "Brain", "br1")
        assert client.query("Brain").total == 1

    def test_supersede_rejects_parent_row_through_subclass(self, client):
        with pytest.raises(EntityKindMismatchError):
            client.supersede_entity("pr1", "bb1", entity_type="BankBrain")
        _untouched(client, "Process", "pr1")

    def test_supersede_on_own_kind_still_works(self, client):
        client.put("Blood", {"id": "bl2", "label": "blood again"})
        client.supersede_entity("bl1", "bl2", entity_type="Blood")
        row = _row(client, "Blood", "bl1")
        assert (row["is_available"], row["superseded_by"]) == (False, "bl2")

    def test_supersede_through_concrete_parent_accepts_subclass_row(self, client):
        client.supersede_entity("bb1", "pr1", entity_type="Process")
        assert _row(client, "BankBrain", "bb1")["superseded_by"] == "pr1"

    def test_supersede_replacement_may_be_another_kind(self, client):
        # Reclassification: a record entered as Blood is replaced by a Brain.
        client.supersede_entity("bl1", "br1", entity_type="Blood")
        assert _row(client, "Blood", "bl1")["superseded_by"] == "br1"

    def test_supersede_replacement_must_exist(self, client):
        with pytest.raises(EntityNotFoundError):
            client.supersede_entity("bl1", "no-such-id", entity_type="Blood")
        _untouched(client, "Blood", "bl1")

    # -- delete -------------------------------------------------------------

    def test_delete_rejects_other_kinds_and_changes_nothing(self, client):
        for kind, eid, actual in (("Blood", "br1", "Brain"),
                                  ("BankBrain", "pr1", "Process"),
                                  ("Assay", "m1", "Measurement")):
            with pytest.raises(EntityKindMismatchError) as info:
                client.delete(kind, eid)
            assert info.value.actual_entity_type == actual
        _untouched(client, "Brain", "br1")
        _untouched(client, "Process", "pr1")
        _untouched(client, "Measurement", "m1")

    def test_delete_on_own_kind_and_through_parent_still_works(self, client):
        client.delete("Blood", "bl1")
        assert _row(client, "Blood", "bl1")["is_available"] is False
        client.delete("Process", "bb1")
        assert _row(client, "BankBrain", "bb1")["is_available"] is False

    # -- update (exact type, unchanged) --------------------------------------

    def test_update_keeps_exact_type_rule(self, client):
        with pytest.raises(EntityTypeConflictError):
            client.update("Blood", "br1", {"label": "x"})
        with pytest.raises(EntityTypeConflictError):
            client.update("Process", "bb1", {"name": "x"})
        assert _row(client, "Brain", "br1")["data"]["label"] == "brain"
        client.update("Blood", "bl1", {"label": "plasma", "volume_ml": 2.0})
        assert _row(client, "Blood", "bl1")["data"]["label"] == "plasma"


class KindCheckGraphQLCases:
    """GraphQL: the per-type lifecycle mutations inherit the SDK check."""

    @pytest.fixture
    def gql_raw(self, client):
        pytest.importorskip("strawberry", reason="graphql extra not installed")
        from mosaic.graphql.resolvers import build_graphql_schema
        from mosaic.graphql.schema_builder import GraphQLTypeBuilder

        registry = client.registry
        builder = GraphQLTypeBuilder(registry).build()
        schema = build_graphql_schema(registry, builder=builder)

        def run(query: str) -> Any:
            return asyncio.run(
                schema.execute(query, context_value={"client": client, "builder": builder})
            )

        return run

    @staticmethod
    def _error(result: Any) -> Any:
        assert result.errors, result.data
        return result.errors[0]

    def test_set_availability_wrong_kind_is_not_found(self, client, gql_raw):
        err = self._error(gql_raw(
            'mutation { setBloodAvailability(id: "br1", isAvailable: false, '
            'reason: "probe") { entityId } }'
        ))
        assert err.extensions["code"] == "NOT_FOUND"
        assert err.extensions["actual_entity_type"] == "Brain"
        assert "Blood" in err.message and "Brain" in err.message
        _untouched(client, "Brain", "br1")
        unknown = self._error(gql_raw(
            'mutation { setBloodAvailability(id: "no-such-id", isAvailable: false) '
            '{ entityId } }'
        ))
        assert unknown.extensions["code"] == "NOT_FOUND"

    def test_set_availability_right_kind_and_subclass_work(self, client, gql_raw):
        result = gql_raw(
            'mutation { a: setBloodAvailability(id: "bl1", isAvailable: false) '
            '{ entityId isAvailable } '
            'b: setProcessAvailability(id: "bb1", isAvailable: false) { entityId } }'
        )
        assert not result.errors, result.errors
        assert result.data["a"] == {"entityId": "bl1", "isAvailable": False}
        assert _row(client, "Blood", "bl1")["is_available"] is False
        assert _row(client, "BankBrain", "bb1")["is_available"] is False

    def test_set_availability_parent_row_through_subclass_rejected(self, client, gql_raw):
        err = self._error(gql_raw(
            'mutation { setBankBrainAvailability(id: "pr1", isAvailable: false) '
            '{ entityId } }'
        ))
        assert err.extensions["code"] == "NOT_FOUND"
        _untouched(client, "Process", "pr1")

    def test_set_availability_bulk_reports_wrong_kind(self, client, gql_raw):
        result = gql_raw(
            'mutation { setBloodAvailabilityBulk(ids: ["bl1", "br1"], '
            'isAvailable: false) { succeeded failed failures { entityId error } } }'
        )
        assert not result.errors, result.errors
        bulk = result.data["setBloodAvailabilityBulk"]
        assert (bulk["succeeded"], bulk["failed"]) == (1, 1)
        assert bulk["failures"][0]["entityId"] == "br1"
        assert "Brain" in bulk["failures"][0]["error"]
        _untouched(client, "Brain", "br1")

    def test_supersede_wrong_kind_is_not_found(self, client, gql_raw):
        err = self._error(gql_raw(
            'mutation { supersedeBlood(id: "br1", replacementId: "bl1") { entityId } }'
        ))
        assert err.extensions["code"] == "NOT_FOUND"
        assert err.extensions["actual_entity_type"] == "Brain"
        _untouched(client, "Brain", "br1")
        result = gql_raw('{ brain(id: "br1") { id } brains { total } }')
        assert result.data == {"brain": {"id": "br1"}, "brains": {"total": 1}}

    def test_supersede_right_kind_subclass_and_cross_kind_replacement(self, client, gql_raw):
        result = gql_raw(
            'mutation { a: supersedeBlood(id: "bl1", replacementId: "br1") '
            '{ entityId supersededBy } '
            'b: supersedeProcess(id: "bb1", replacementId: "pr1") { entityId } }'
        )
        assert not result.errors, result.errors
        assert result.data["a"] == {"entityId": "bl1", "supersededBy": "br1"}
        assert _row(client, "Blood", "bl1")["superseded_by"] == "br1"
        assert _row(client, "BankBrain", "bb1")["superseded_by"] == "pr1"

    def test_update_wrong_kind_rejected(self, client, gql_raw):
        err = self._error(gql_raw(
            'mutation { updateBlood(id: "br1", data: {label: "x"}) { id } }'
        ))
        assert err.extensions["code"] == "NOT_FOUND"
        # Through a concrete parent, update stays exact-type.
        err = self._error(gql_raw(
            'mutation { updateProcess(id: "bb1", data: {name: "x"}) { id } }'
        ))
        assert err.extensions["code"] == "ENTITY_TYPE_CONFLICT"
        assert _row(client, "Brain", "br1")["data"]["label"] == "brain"
        result = gql_raw(
            'mutation { updateBlood(id: "bl1", data: {label: "plasma"}) { id label } }'
        )
        assert not result.errors, result.errors
        assert result.data["updateBlood"] == {"id": "bl1", "label": "plasma"}
