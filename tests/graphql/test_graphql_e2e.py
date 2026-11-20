"""End-to-end GraphQL tests: FastAPI test client over a temp SQLite DB.

Mirrors the ``tests/serve`` conventions — real ``MosaicClient``, real
storage adapter, requests through the mounted ``/graphql`` route.
"""

from __future__ import annotations

import pytest

from tests.graphql.conftest import AUTH


def _create_donor(gql, name="Ada", sex="female") -> str:
    body = gql(
        "mutation($name: String!, $sex: SexEnum) {"
        "  createDonor(data: {name: $name, sex: $sex}) { id } }",
        {"name": name, "sex": sex},
    )
    assert "errors" not in body, body
    return body["data"]["createDonor"]["id"]


def _create_sample(gql, name="S1", **fields) -> str:
    parts = [f'name: "{name}"']
    for key, value in fields.items():
        if isinstance(value, str):
            parts.append(f'{key}: "{value}"')
        else:
            parts.append(f"{key}: {value}")
    body = gql(
        "mutation { createSample(data: {%s}) { id } }" % ", ".join(parts)
    )
    assert "errors" not in body, body
    return body["data"]["createSample"]["id"]


class TestAuthAndMounting:
    def test_operations_do_not_require_bearer_token(self, client):
        """Mosaic holds zero authn/authz (#54 Part A) — no header required."""
        response = client.post("/graphql", json={"query": "{ __typename }"})
        assert response.status_code == 200

    def test_graphiql_ide_loads_without_auth(self, client):
        response = client.get("/graphql", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "graphiql" in response.text.lower()

    def test_introspection_with_auth(self, client):
        response = client.post(
            "/graphql",
            json={"query": "{ __schema { queryType { name } } }"},
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["data"]["__schema"]["queryType"]["name"] == "Query"

    def test_rest_routes_still_served(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_graphql_not_mounted_by_default(self, hippo_client):
        from fastapi.testclient import TestClient

        from mosaic.serve import create_default_app

        app = create_default_app(mosaic_client=hippo_client)
        with TestClient(app) as plain:
            assert plain.get("/graphql").status_code == 404

    def test_graphql_resolvers_use_the_app_state_client(self, hippo_app, client):
        """GraphQL reads through app.state.mosaic_client — the same
        client instance the REST routers use (no separate construction).
        """
        state_client = hippo_app.state.mosaic_client
        created = state_client.create("Donor", {"name": "FromState"})
        body = client.post(
            "/graphql",
            json={
                "query": "query($id: ID!) { donor(id: $id) { name } }",
                "variables": {"id": created["id"]},
            },
            headers=AUTH,
        ).json()
        assert body["data"]["donor"]["name"] == "FromState"


class TestServeWiring:
    """Config-driven factory → create_default_app(client, graphql=True).

    Same pattern as tests/serve/test_serve_persistence.py: the app is
    built exactly the way ``mosaic serve --graphql`` builds it.
    """

    def test_factory_built_app_serves_rest_and_graphql(self, tmp_path):
        from pathlib import Path

        from fastapi.testclient import TestClient

        from mosaic.config import MosaicConfig
        from mosaic.core.factory import create_client_from_config
        from mosaic.serve import create_default_app

        fixture_schema = (
            Path(__file__).parents[1] / "fixtures" / "schemas" / "sample_schema.yaml"
        )
        cfg = MosaicConfig(
            schema_path=str(fixture_schema),
            database_url=str(tmp_path / "gql.db"),
            storage_backend="sqlite",
        )
        sdk_client = create_client_from_config(cfg)
        app = create_default_app(sdk_client, graphql=True)

        pid = sdk_client.create("Project", {"name": "Gamma"})["id"]
        with TestClient(app) as api:
            # REST sees the SDK write...
            rest = api.get(f"/entities/{pid}", headers=AUTH)
            assert rest.status_code == 200
            # ...and GraphQL sees the same row through the same client.
            body = api.post(
                "/graphql",
                json={
                    "query": "query($id: ID!) { project(id: $id) { id name } }",
                    "variables": {"id": pid},
                },
                headers=AUTH,
            ).json()
            assert body["data"]["project"] == {"id": pid, "name": "Gamma"}


class TestQueries:
    def test_create_then_get_by_id(self, gql):
        donor_id = _create_donor(gql, name="Grace")
        body = gql(
            "query($id: ID!) { donor(id: $id) {"
            "  id name sex isAvailable version"
            "  createdAt updatedAt schemaVersion } }",
            {"id": donor_id},
        )
        donor = body["data"]["donor"]
        assert donor["id"] == donor_id
        assert donor["name"] == "Grace"
        assert donor["sex"] == "female"
        assert donor["isAvailable"] is True
        assert donor["version"] == 1
        # Temporal fields are computed from the provenance log at read
        # time (sec9 §9.7) — present on every readable entity.
        assert donor["createdAt"]
        assert donor["updatedAt"]
        assert donor["schemaVersion"]

    def test_get_missing_returns_null(self, gql):
        body = gql('{ donor(id: "does-not-exist") { id } }')
        assert body["data"]["donor"] is None
        assert "errors" not in body

    def test_list_with_pagination(self, gql):
        for i in range(3):
            _create_sample(gql, name=f"P{i}")
        body = gql("{ samples(limit: 2, offset: 0) { total limit offset items { name } } }")
        page = body["data"]["samples"]
        assert page["total"] == 3
        assert page["limit"] == 2
        assert len(page["items"]) == 2
        rest = gql("{ samples(limit: 2, offset: 2) { items { name } } }")
        assert len(rest["data"]["samples"]["items"]) == 1

    def test_list_with_equality_filter(self, gql):
        _create_sample(gql, name="brain-1")
        _create_sample(gql, name="liver-1")
        body = gql(
            '{ samples(filters: [{field: "name", value: "brain-1"}]) {'
            "  total items { name } } }"
        )
        page = body["data"]["samples"]
        assert page["total"] == 1
        assert page["items"][0]["name"] == "brain-1"

    def test_list_with_or_filter_mode(self, gql):
        _create_sample(gql, name="brain-2")
        _create_sample(gql, name="liver-2")
        _create_sample(gql, name="heart-2")
        body = gql(
            "{ samples(filterMode: OR, filters: ["
            '  {field: "name", value: "brain-2"},'
            '  {field: "name", value: "liver-2"}]) { total } }'
        )
        assert body["data"]["samples"]["total"] == 2

    def test_list_with_in_filter(self, gql):
        """IN / set-membership filter (issue #102) — FilterInput.op: IN,
        value is a list. Mirrors test_list_with_equality_filter."""
        _create_sample(gql, name="brain-3")
        _create_sample(gql, name="liver-3")
        _create_sample(gql, name="heart-3")
        body = gql(
            '{ samples(filters: [{field: "name", op: IN, '
            'value: ["brain-3", "heart-3"]}]) {'
            "  total items { name } } }"
        )
        assert "errors" not in body, body
        page = body["data"]["samples"]
        assert page["total"] == 2
        names = {item["name"] for item in page["items"]}
        assert names == {"brain-3", "heart-3"}

    def test_list_with_in_filter_empty_list_matches_nothing(self, gql):
        _create_sample(gql, name="only-one")
        body = gql(
            '{ samples(filters: [{field: "name", op: IN, value: []}]) { total } }'
        )
        assert "errors" not in body, body
        assert body["data"]["samples"]["total"] == 0

    def test_list_with_eq_op_matches_default(self, gql):
        """``op`` defaults to EQ — explicit EQ behaves like the
        default/omitted case (backward compatibility)."""
        _create_sample(gql, name="explicit-eq")
        body = gql(
            '{ samples(filters: [{field: "name", op: EQ, '
            'value: "explicit-eq"}]) { total items { name } } }'
        )
        assert "errors" not in body, body
        page = body["data"]["samples"]
        assert page["total"] == 1
        assert page["items"][0]["name"] == "explicit-eq"


class TestRelationshipTraversal:
    def test_single_reference_resolves(self, gql):
        donor_id = _create_donor(gql, name="Rosalind")
        sample_id = _create_sample(gql, name="tissue-1", donorId=donor_id)
        # Edge-only (ADR-0005): the reference is read through the resolved
        # edge (donor { id }), not a raw donorId scalar (which no longer exists).
        body = gql(
            "query($id: ID!) { sample(id: $id) {"
            "  donor { id name sex } } }",
            {"id": sample_id},
        )
        sample = body["data"]["sample"]
        assert sample["donor"]["id"] == donor_id
        assert sample["donor"]["name"] == "Rosalind"

    def test_self_reference_resolves(self, gql):
        parent_id = _create_sample(gql, name="parent")
        child_id = _create_sample(gql, name="child", parent=parent_id)
        body = gql(
            "query($id: ID!) { sample(id: $id) {"
            "  parent { id name parent { id } } } }",
            {"id": child_id},
        )
        child = body["data"]["sample"]
        assert child["parent"]["id"] == parent_id
        assert child["parent"]["name"] == "parent"
        assert child["parent"]["parent"] is None

    def test_null_reference_resolves_to_null(self, gql):
        sample_id = _create_sample(gql, name="orphan")
        body = gql(
            "query($id: ID!) { sample(id: $id) { donor { id } } }",
            {"id": sample_id},
        )
        assert body["data"]["sample"]["donor"] is None

    def test_traversal_in_list_query_is_batched(self, gql, hippo_client):
        """N samples sharing a donor resolve in ONE batched donor query."""
        donor_id = _create_donor(gql, name="Shared")
        for i in range(5):
            _create_sample(gql, name=f"B{i}", donorId=donor_id)

        calls: list[str] = []
        original_query = hippo_client.query

        def counting_query(entity_type, *args, **kwargs):
            calls.append(entity_type)
            return original_query(entity_type, *args, **kwargs)

        hippo_client.query = counting_query
        try:
            body = gql("{ samples { items { name donor { id name } } } }")
        finally:
            hippo_client.query = original_query

        items = body["data"]["samples"]["items"]
        assert len(items) == 5
        assert all(item["donor"]["id"] == donor_id for item in items)
        # One query lists the samples, ONE batched query loads all
        # donors (DataLoader) — not one per sample.
        assert calls.count("Donor") == 1


class TestMutations:
    def test_create_assigns_uuid_and_provenance(self, gql):
        body = gql('mutation { createSample(data: {name: "fresh"}) { id createdAt } }')
        created = body["data"]["createSample"]
        assert created["id"]
        assert created["createdAt"]

    def test_update_is_partial_merge(self, gql):
        donor_id = _create_donor(gql)
        sample_id = _create_sample(
            gql, name="keep-me", donorId=donor_id, volumeMl=1.5
        )
        body = gql(
            "mutation($id: ID!) { updateSample(id: $id, data: {volumeMl: 9.9}) {"
            "  name volumeMl donor { id } version } }",
            {"id": sample_id},
        )
        updated = body["data"]["updateSample"]
        # Untouched fields survive: MosaicClient.update is full-replace,
        # the transport merges the patch over the stored data.
        assert updated["name"] == "keep-me"
        assert updated["donor"]["id"] == donor_id
        assert updated["volumeMl"] == 9.9
        assert updated["version"] == 2

    def test_update_missing_is_not_found(self, gql):
        body = gql(
            'mutation { updateSample(id: "missing", data: {name: "x"}) { id } }'
        )
        assert body["data"] is None
        assert body["errors"][0]["extensions"]["code"] == "NOT_FOUND"

    def test_missing_required_field_is_rejected(self, gql):
        body = gql("mutation { createSample(data: {volumeMl: 1.0}) { id } }")
        assert "errors" in body
        assert "name" in body["errors"][0]["message"]

    def test_invalid_enum_value_is_rejected(self, gql):
        body = gql('mutation { createDonor(data: {name: "X", sex: gibberish}) { id } }')
        assert "errors" in body
        assert "SexEnum" in body["errors"][0]["message"]

    def test_dangling_reference_is_coded_error(self, gql):
        body = gql(
            'mutation { createSample(data: {name: "bad", donorId: "nope"}) { id } }'
        )
        assert body["data"] is None
        assert body["errors"][0]["extensions"]["code"] == "INTERNAL_ERROR"


class TestAvailabilityLifecycle:
    """No hard deletes — availability transitions only (sec3)."""

    def test_availability_transition(self, gql):
        sample_id = _create_sample(gql, name="depletable")
        body = gql(
            "mutation($id: ID!) {"
            '  setSampleAvailability(id: $id, isAvailable: false, reason: "depleted") {'
            "    entityId isAvailable } }",
            {"id": sample_id},
        )
        result = body["data"]["setSampleAvailability"]
        assert result["entityId"] == sample_id
        assert result["isAvailable"] is False

        # Unavailable entities disappear from reads and lists...
        assert gql(
            "query($id: ID!) { sample(id: $id) { id } }", {"id": sample_id}
        )["data"]["sample"] is None
        assert gql("{ samples { total } }")["data"]["samples"]["total"] == 0

        # ...but the row and its audit trail survive (no hard delete).
        history = gql(
            "query($id: ID!) { entityHistory(entityId: $id) { operation patch } }",
            {"id": sample_id},
        )["data"]["entityHistory"]
        operations = [record["operation"] for record in history]
        assert operations == ["create", "availability_change"]
        assert history[-1]["patch"]["reason"] == "depleted"

    def test_availability_on_missing_entity(self, gql):
        body = gql(
            'mutation { setSampleAvailability(id: "ghost", isAvailable: false) {'
            "  entityId } }"
        )
        assert body["errors"][0]["extensions"]["code"] == "NOT_FOUND"

    def test_supersede(self, gql):
        old_id = _create_sample(gql, name="v1")
        new_id = _create_sample(gql, name="v2")
        body = gql(
            "mutation($a: ID!, $b: ID!) {"
            '  supersedeSample(id: $a, replacementId: $b, reason: "remeasured") {'
            "    entityId supersededBy } }",
            {"a": old_id, "b": new_id},
        )
        result = body["data"]["supersedeSample"]
        assert result["entityId"] == old_id
        assert result["supersededBy"] == new_id
        # Source is out of the default read path; replacement remains.
        assert gql(
            "query($id: ID!) { sample(id: $id) { id } }", {"id": old_id}
        )["data"]["sample"] is None
        assert gql(
            "query($id: ID!) { sample(id: $id) { id } }", {"id": new_id}
        )["data"]["sample"]["id"] == new_id

    def test_double_supersede_is_coded_error(self, gql):
        old_id = _create_sample(gql, name="v1")
        new_id = _create_sample(gql, name="v2")
        third_id = _create_sample(gql, name="v3")
        first = gql(
            "mutation($a: ID!, $b: ID!) {"
            "  supersedeSample(id: $a, replacementId: $b) { entityId } }",
            {"a": old_id, "b": new_id},
        )
        assert "errors" not in first
        again = gql(
            "mutation($a: ID!, $b: ID!) {"
            "  supersedeSample(id: $a, replacementId: $b) { entityId } }",
            {"a": old_id, "b": third_id},
        )
        assert again["errors"][0]["extensions"]["code"] == "ALREADY_SUPERSEDED"


class TestProvenanceHistory:
    def test_history_records_in_order(self, gql):
        sample_id = _create_sample(gql, name="audited")
        gql(
            'mutation($id: ID!) { updateSample(id: $id, data: {volumeMl: 2.0}) { id } }',
            {"id": sample_id},
        )
        body = gql(
            "query($id: ID!) { entityHistory(entityId: $id) {"
            "  operationId entityId entityType operation timestamp patch } }",
            {"id": sample_id},
        )
        history = body["data"]["entityHistory"]
        assert [record["operation"] for record in history] == ["create", "update"]
        assert all(record["entityId"] == sample_id for record in history)
        assert all(record["entityType"] == "Sample" for record in history)
        assert history[0]["timestamp"] <= history[1]["timestamp"]
        assert history[1]["patch"]["volume_ml"] == 2.0

    def test_history_of_unknown_entity_is_not_found(self, gql):
        body = gql('{ entityHistory(entityId: "ghost") { operation } }')
        assert body["errors"][0]["extensions"]["code"] == "NOT_FOUND"
