"""REST-parity coverage (issue #45 "should close" list).

Full-text search, Mosaic schema introspection (the LinkML type model,
distinct from GraphQL's own ``__schema``), the supersession query, and
the bulk availability mutation — each mirroring its REST counterpart.
"""

from __future__ import annotations

from tests.graphql.conftest import AUTH
from tests.graphql.test_graphql_e2e import _create_sample


class TestFullTextSearch:
    """Mirrors REST GET /search (per-type, q/limit/offset)."""

    def test_search_returns_matching_entities(self, gql):
        _create_sample(gql, name="S1", notes="hippocampus lesion observed")
        _create_sample(gql, name="S2", notes="prefrontal cortex damage")
        # BREAKING (issue #157): search twins return the Page envelope.
        body = gql(
            '{ searchSamples(q: "hippocampus") '
            "{ items { name notes createdAt } total } }"
        )
        assert "errors" not in body, body
        page = body["data"]["searchSamples"]
        assert [item["name"] for item in page["items"]] == ["S1"]
        assert page["total"] == 1
        # Search results are full envelopes — computed fields included.
        assert page["items"][0]["createdAt"]

    def test_search_no_results(self, gql):
        _create_sample(gql, name="S1", notes="hippocampus lesion observed")
        body = gql('{ searchSamples(q: "nonexistentterm12345") { items { name } total } }')
        assert body["data"]["searchSamples"] == {"items": [], "total": 0}

    def test_search_offset_pages_through_hits(self, gql):
        for i in range(3):
            _create_sample(gql, name=f"S{i}", notes="shared searchable token")
        first = gql('{ searchSamples(q: "searchable", limit: 2) { items { name } total } }')
        assert len(first["data"]["searchSamples"]["items"]) == 2
        assert first["data"]["searchSamples"]["total"] == 3
        rest = gql('{ searchSamples(q: "searchable", limit: 3, offset: 2) { items { name } total } }')
        assert len(rest["data"]["searchSamples"]["items"]) == 1
        assert rest["data"]["searchSamples"]["total"] == 3
        # Regression (issue #157): offset >= limit used to return [] via
        # resolver-side slicing regardless of the hit count.
        deep = gql('{ searchSamples(q: "searchable", limit: 1, offset: 2) { items { name } total } }')
        assert len(deep["data"]["searchSamples"]["items"]) == 1
        assert deep["data"]["searchSamples"]["total"] == 3

    def test_search_on_type_without_searchable_slots_is_empty(self, gql):
        # Donor declares no mosaic_search slots — same as REST: no FTS
        # tables means no results, not an error.
        body = gql('{ searchDonors(q: "anything") { items { name } total } }')
        assert body["data"]["searchDonors"] == {"items": [], "total": 0}


class TestMosaicSchemaIntrospection:
    """Mirrors REST GET /schemas and GET /schemas/{type}/references."""

    def test_hippo_schema_lists_exposed_entity_types(self, gql, registry):
        from mosaic.core.schema_typing import exposed_class_names

        body = gql("{ mosaicSchema { name accessorName } }")
        names = [item["name"] for item in body["data"]["mosaicSchema"]]
        assert names == exposed_class_names(registry)
        assert "Sample" in names
        assert "ExternalID" in names  # exposed, like the typed client
        assert "ProvenanceRecord" not in names  # infrastructure

    def test_entity_type_slots_carry_the_model_classification(self, gql):
        body = gql(
            '{ mosaicEntityType(name: "Sample") {'
            "  name accessorName description"
            "  fields { name kind range role required multivalued"
            "           targetEntityType enumName } } }"
        )
        info = body["data"]["mosaicEntityType"]
        assert info["name"] == "Sample"
        assert info["accessorName"] == "samples"
        fields = {f["name"]: f for f in info["fields"]}
        assert fields["name"]["kind"] == "scalar"
        assert fields["name"]["required"] is True
        assert fields["name"]["role"] == "user"
        assert fields["donor_id"]["kind"] == "reference"
        assert fields["donor_id"]["targetEntityType"] == "Donor"
        assert fields["id"]["role"] == "system"
        assert fields["is_available"]["role"] == "system"

    def test_enum_slots_expose_their_values(self, gql):
        body = gql(
            '{ mosaicEntityType(name: "Donor") {'
            "  fields { name kind enumName enumValues } } }"
        )
        fields = {
            f["name"]: f for f in body["data"]["mosaicEntityType"]["fields"]
        }
        assert fields["sex"]["kind"] == "enum"
        assert fields["sex"]["enumName"] == "SexEnum"
        assert fields["sex"]["enumValues"] == ["male", "female", "unknown"]

    def test_relationships_mirror_schema_references(self, gql):
        # Same shape as REST GET /schemas/Sample/references.
        body = gql(
            '{ mosaicEntityType(name: "Sample") {'
            "  relationships { field targetEntityType } } }"
        )
        relationships = body["data"]["mosaicEntityType"]["relationships"]
        assert {"field": "donor_id", "targetEntityType": "Donor"} in relationships
        assert {"field": "parent", "targetEntityType": "Sample"} in relationships

    def test_unknown_entity_type_is_null(self, gql):
        body = gql('{ mosaicEntityType(name: "Nope") { name } }')
        assert body["data"]["mosaicEntityType"] is None
        assert "errors" not in body


class TestSupersessionQuery:
    """Mirrors REST GET /entities/{id}/superseded."""

    def test_current_entity_has_no_supersession(self, gql):
        sample_id = _create_sample(gql, name="current")
        body = gql(
            "query($id: ID!) { supersededBy(id: $id) {"
            "  entityId supersededBy chain } }",
            {"id": sample_id},
        )
        info = body["data"]["supersededBy"]
        assert info["entityId"] == sample_id
        assert info["supersededBy"] is None
        assert info["chain"] == []

    def test_superseded_entity_points_at_replacement(self, gql):
        old_id = _create_sample(gql, name="v1")
        new_id = _create_sample(gql, name="v2")
        gql(
            "mutation($a: ID!, $b: ID!) {"
            "  supersedeSample(id: $a, replacementId: $b) { entityId } }",
            {"a": old_id, "b": new_id},
        )
        body = gql(
            "query($id: ID!) { supersededBy(id: $id) {"
            "  supersededBy chain } }",
            {"id": old_id},
        )
        info = body["data"]["supersededBy"]
        assert info["supersededBy"] == new_id
        assert info["chain"] == [new_id]

    def test_chain_follows_replacements_to_the_terminal_entity(self, gql):
        v1 = _create_sample(gql, name="v1")
        v2 = _create_sample(gql, name="v2")
        v3 = _create_sample(gql, name="v3")
        for old, new in ((v1, v2), (v2, v3)):
            gql(
                "mutation($a: ID!, $b: ID!) {"
                "  supersedeSample(id: $a, replacementId: $b) { entityId } }",
                {"a": old, "b": new},
            )
        body = gql(
            "query($id: ID!) { supersededBy(id: $id) { supersededBy chain } }",
            {"id": v1},
        )
        info = body["data"]["supersededBy"]
        assert info["supersededBy"] == v2
        assert info["chain"] == [v2, v3]

    def test_unknown_entity_is_not_found(self, gql):
        body = gql('{ supersededBy(id: "ghost") { entityId } }')
        assert body["errors"][0]["extensions"]["code"] == "NOT_FOUND"


class TestBulkAvailabilityMutation:
    """Mirrors REST POST /entities/{type}/bulk-availability."""

    def test_bulk_transition_succeeds_for_all(self, gql):
        ids = [_create_sample(gql, name=f"bulk-{i}") for i in range(3)]
        body = gql(
            "mutation($ids: [ID!]!) {"
            '  setSampleAvailabilityBulk(ids: $ids, isAvailable: false, reason: "audit") {'
            "    total succeeded failed"
            "    successes { entityId isAvailable }"
            "    failures { entityId error } } }",
            {"ids": ids},
        )
        result = body["data"]["setSampleAvailabilityBulk"]
        assert result["total"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert {s["entityId"] for s in result["successes"]} == set(ids)
        assert all(s["isAvailable"] is False for s in result["successes"])
        # All gone from the default read path (no hard delete).
        assert gql("{ samples { total } }")["data"]["samples"]["total"] == 0

    def test_per_record_error_isolation(self, gql):
        """A bad id never rolls back sibling successes (REST 207 contract)."""
        good = _create_sample(gql, name="good")
        body = gql(
            "mutation($ids: [ID!]!) {"
            "  setSampleAvailabilityBulk(ids: $ids, isAvailable: false) {"
            "    total succeeded failed"
            "    failures { entityId error } } }",
            {"ids": [good, "ghost"]},
        )
        result = body["data"]["setSampleAvailabilityBulk"]
        assert "errors" not in body
        assert result["total"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert result["failures"][0]["entityId"] == "ghost"
        assert "not found" in result["failures"][0]["error"].lower()
        # The good record's transition stuck.
        assert gql("{ samples { total } }")["data"]["samples"]["total"] == 0

    def test_bulk_availability_records_provenance(self, gql):
        sample_id = _create_sample(gql, name="tracked")
        gql(
            "mutation($ids: [ID!]!) {"
            '  setSampleAvailabilityBulk(ids: $ids, isAvailable: false, reason: "qc") {'
            "    succeeded } }",
            {"ids": [sample_id]},
        )
        history = gql(
            "query($id: ID!) { entityHistory(entityId: $id) { operation patch } }",
            {"id": sample_id},
        )["data"]["entityHistory"]
        assert [h["operation"] for h in history] == [
            "create",
            "availability_change",
        ]
        assert history[-1]["patch"]["reason"] == "qc"
