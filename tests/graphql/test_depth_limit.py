"""Query-depth-limit hardening (issue #45).

Self-referential relationship fields (``parent: Sample``) make
arbitrarily deep traversals expressible; the transport caps nesting via
strawberry's ``QueryDepthLimiter`` (default
``mosaic.graphql.DEFAULT_MAX_QUERY_DEPTH``, configurable per mount).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mosaic.graphql import DEFAULT_MAX_QUERY_DEPTH, create_graphql_router
from tests.graphql.conftest import AUTH


def _nested_parent_query(depth: int) -> str:
    """``{ sample(id: "x") { parent { parent { ... { id } } } } }``.

    Field depth = 1 (sample) + (depth - 1) nesting levels + 1 (id).
    """
    inner = "id"
    for _ in range(depth - 1):
        inner = f"parent {{ {inner} }}"
    return f'{{ sample(id: "x") {{ {inner} }} }}'


def _depth_errors(body: dict) -> list[str]:
    return [
        e["message"]
        for e in body.get("errors", [])
        if "deeper than the max depth" in e["message"]
        or "exceeds maximum operation depth" in e["message"]
        or "depth" in e["message"].lower()
    ]


class TestDefaultDepthLimit:
    def test_default_is_ten(self):
        assert DEFAULT_MAX_QUERY_DEPTH == 10

    def test_query_within_the_limit_executes(self, gql):
        body = gql(_nested_parent_query(DEFAULT_MAX_QUERY_DEPTH - 1))
        assert "errors" not in body, body

    def test_query_beyond_the_limit_is_rejected(self, gql):
        body = gql(_nested_parent_query(DEFAULT_MAX_QUERY_DEPTH + 2))
        assert body.get("data") is None
        assert _depth_errors(body), body

    def test_introspection_is_exempt(self, gql):
        # GraphiQL's introspection query nests deeper than 10; the
        # limiter must ignore __schema fields or the IDE breaks.
        body = gql(
            "{ __schema { types { fields { type { ofType { ofType {"
            " ofType { ofType { ofType { ofType { name"
            " } } } } } } } } } } }"
        )
        assert "errors" not in body, body


class TestConfigurableDepthLimit:
    def test_max_query_depth_is_configurable_at_mount(self, hippo_client):
        from fastapi import FastAPI

        app = FastAPI()
        app.state.mosaic_client = hippo_client
        router = create_graphql_router(hippo_client, max_query_depth=3)
        app.include_router(router, prefix="/graphql")

        with TestClient(app) as api:
            ok = api.post(
                "/graphql", json={"query": _nested_parent_query(3)}
            ).json()
            assert "errors" not in ok, ok

            rejected = api.post(
                "/graphql", json={"query": _nested_parent_query(5)}
            ).json()
            assert _depth_errors(rejected), rejected

    def test_create_default_app_threads_the_depth_limit(self, hippo_client):
        from mosaic.serve import create_default_app

        app = create_default_app(
            mosaic_client=hippo_client, graphql=True, graphql_max_query_depth=3
        )
        with TestClient(app) as api:
            rejected = api.post(
                "/graphql",
                json={"query": _nested_parent_query(6)},
                headers=AUTH,
            ).json()
            assert _depth_errors(rejected), rejected
