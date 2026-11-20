"""GraphQL coverage for ExternalReference slots and findByXref (issue #48)."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.graphql.resolvers import build_graphql_schema
from mosaic.graphql.schema_builder import GraphQLTypeBuilder
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.serve import create_default_app

XREF_GRAPHQL_SCHEMA = """
id: https://example.org/mosaic/test_xref_graphql
name: test_xref_graphql
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string

classes:
  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      starlims_ref:
        range: ExternalReference
        inlined: true
        annotations:
          mosaic_external_xref: true
      other_refs:
        range: ExternalReference
        multivalued: true
        inlined: true
        inlined_as_list: true
        annotations:
          mosaic_external_xref: true
"""

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(XREF_GRAPHQL_SCHEMA)


@pytest.fixture
def hippo_client(registry):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "xref_graphql.db")
        storage = SQLiteAdapter(db_path, schema_registry=registry)
        yield MosaicClient(storage=storage, registry=registry)


@pytest.fixture
def api(hippo_client):
    app = create_default_app(mosaic_client=hippo_client, graphql=True)
    with TestClient(app) as test_client:
        yield test_client


def _gql(api, query, variables=None):
    resp = api.post(
        "/graphql", json={"query": query, "variables": variables or {}}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSDL:
    def test_structured_slots_render_as_json(self, registry):
        builder = GraphQLTypeBuilder(registry).build()
        sdl = str(build_graphql_schema(registry, builder))
        # ExternalReference is a value type — never a GraphQL object type.
        assert "type ExternalReference" not in sdl
        assert "findByXref" in sdl
        assert "XrefMatch" in sdl

    def test_external_reference_not_in_entities(self, registry):
        builder = GraphQLTypeBuilder(registry).build()
        assert "ExternalReference" not in builder.entities
        assert "Sample" in builder.entities


class TestFindByXref:
    def test_round_trip_through_mutation(self, api):
        created = _gql(
            api,
            """
            mutation {
              createSample(data: {
                name: "s1",
                starlimsRef: {system: "STARLIMS", value: "BC-1"},
                otherRefs: [{system: "HALO", value: "H-1"}]
              }) { id starlimsRef otherRefs }
            }
            """,
        )
        assert created.get("errors") is None, created
        sample = created["data"]["createSample"]
        assert sample["starlimsRef"] == {"system": "STARLIMS", "value": "BC-1"}
        assert sample["otherRefs"] == [{"system": "HALO", "value": "H-1"}]

        found = _gql(
            api,
            """
            query {
              findByXref(system: "STARLIMS", value: "BC-1") {
                entityId entityType data version
              }
            }
            """,
        )
        assert found.get("errors") is None, found
        match = found["data"]["findByXref"]
        assert match["entityId"] == sample["id"]
        assert match["entityType"] == "Sample"
        assert match["data"]["name"] == "s1"

        # multivalued slot pair resolves to the same entity
        found2 = _gql(
            api,
            'query { findByXref(system: "HALO", value: "H-1") { entityId } }',
        )
        assert found2["data"]["findByXref"]["entityId"] == sample["id"]

    def test_unknown_pair_is_null(self, api):
        result = _gql(
            api,
            'query { findByXref(system: "NOPE", value: "missing") { entityId } }',
        )
        assert result.get("errors") is None
        assert result["data"]["findByXref"] is None

    def test_uniqueness_violation_is_validation_failed(self, api):
        first = _gql(
            api,
            """
            mutation {
              createSample(data: {
                name: "s1",
                starlimsRef: {system: "STARLIMS", value: "BC-1"}
              }) { id }
            }
            """,
        )
        assert first.get("errors") is None, first
        dup = _gql(
            api,
            """
            mutation {
              createSample(data: {
                name: "s2",
                starlimsRef: {system: "STARLIMS", value: "BC-1"}
              }) { id }
            }
            """,
        )
        assert dup.get("errors"), dup
        error = dup["errors"][0]
        assert error["extensions"]["code"] == "VALIDATION_FAILED"
        assert "BC-1" in error["message"]
