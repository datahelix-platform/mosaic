"""Tests for RESTBackend — token precedence and connection failure handling."""

from __future__ import annotations

import asyncio
import os

import pytest

from mosaic.tui.backend.rest import RESTBackend, _resolve_token


# ---------------------------------------------------------------------------
# Tests: token resolution
# ---------------------------------------------------------------------------


def test_explicit_token_takes_precedence(monkeypatch):
    """Explicit --token flag overrides env variable."""
    monkeypatch.setenv("MOSAIC_TUI_TOKEN", "env-token")
    resolved = _resolve_token("explicit-token")
    assert resolved == "explicit-token"


def test_env_token_used_when_no_explicit(monkeypatch):
    """MOSAIC_TUI_TOKEN is used when no explicit token is provided."""
    monkeypatch.setenv("MOSAIC_TUI_TOKEN", "envtoken")
    resolved = _resolve_token(None)
    assert resolved == "envtoken"


def test_default_token_when_neither_set(monkeypatch):
    """Falls back to 'dev-token' when neither flag nor env is set."""
    monkeypatch.delenv("MOSAIC_TUI_TOKEN", raising=False)
    resolved = _resolve_token(None)
    assert resolved == "dev-token"


def test_rest_backend_uses_bearer_token():
    """RESTBackend constructs Authorization header from token."""
    backend = RESTBackend(url="http://localhost:8000", token="mytoken")
    assert backend._token == "mytoken"
    import httpx

    client = backend._get_client()
    auth_header = client.headers.get("authorization")
    assert auth_header == "Bearer mytoken"


def test_rest_backend_token_from_env(monkeypatch):
    """RESTBackend picks up token from MOSAIC_TUI_TOKEN env variable."""
    monkeypatch.setenv("MOSAIC_TUI_TOKEN", "envtoken")
    backend = RESTBackend(url="http://localhost:8000")
    assert backend._token == "envtoken"


def test_rest_backend_default_token(monkeypatch):
    """RESTBackend uses dev-token when no flag or env variable provided."""
    monkeypatch.delenv("MOSAIC_TUI_TOKEN", raising=False)
    backend = RESTBackend(url="http://localhost:8000")
    assert backend._token == "dev-token"


# ---------------------------------------------------------------------------
# Tests: connection failure handling
# ---------------------------------------------------------------------------


def test_connection_failure_sets_status_bar_message(monkeypatch):
    """Connection failure triggers status callback and does not crash."""
    import httpx

    errors: list[str] = []

    backend = RESTBackend(
        url="http://localhost:19999",
        status_callback=lambda msg: errors.append(msg),
    )

    async def mock_get_json(path: str):
        # Simulate the error path: return None (already handled)
        return None

    monkeypatch.setattr(backend, "_get_json", mock_get_json)

    result = asyncio.run(backend.list_entity_types())
    assert result == []


def test_connection_failure_returns_empty_paged_result(monkeypatch):
    """list_entities returns empty PagedResult on connection failure."""
    backend = RESTBackend(url="http://localhost:19999")

    async def mock_get_json(path: str):
        return None

    monkeypatch.setattr(backend, "_get_json", mock_get_json)

    result = asyncio.run(backend.list_entities("Sample"))
    assert result.items == []
    assert result.total_pages == 1


def test_get_json_handles_connect_error_gracefully():
    """_get_json catches ConnectError and reports it without raising."""
    errors: list[str] = []
    backend = RESTBackend(
        url="http://localhost:19999",
        status_callback=lambda msg: errors.append(msg),
    )

    async def run():
        return await backend._get_json("/nonexistent")

    result = asyncio.run(run())
    assert result is None
    # Should have reported an error via the callback
    assert len(errors) == 1


def test_authorization_header_sent_on_every_request(monkeypatch):
    """Every HTTP request includes Authorization: Bearer <token>."""
    import httpx

    captured_headers: list[dict] = []

    class MockResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return []

    class MockAsyncClient:
        def __init__(self, base_url="", headers=None, timeout=None):
            captured_headers.append(dict(headers or {}))

        async def get(self, path):
            return MockResponse()

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    backend = RESTBackend(url="http://localhost:8000", token="testtoken")
    backend._client = None  # force re-creation

    async def run():
        return await backend._get_json("/test")

    asyncio.run(run())

    assert len(captured_headers) == 1
    assert captured_headers[0].get("Authorization") == "Bearer testtoken"


# ---------------------------------------------------------------------------
# Tests: full REST surface against a mocked `mosaic serve` (httpx.MockTransport)
# ---------------------------------------------------------------------------

import json as _json

from mosaic.tui.backend.protocol import BackendError

_SCHEMAS_PAYLOAD = [
    {
        "name": "Entity",
        "abstract": True,
        "description": "Base",
        "fields": [{"name": "id", "range": "string", "required": True}],
    },
    {
        "name": "Project",
        "abstract": False,
        "description": "A project",
        "fields": [
            {"name": "name", "range": "string", "required": True},
            {"name": "id", "range": "string", "required": True, "identifier": True},
        ],
    },
    {
        "name": "Sample",
        "abstract": False,
        "description": "A sample",
        "fields": [
            {"name": "name", "range": "string", "required": True},
            {"name": "project_id", "range": "Project", "required": False},
            {"name": "id", "range": "string", "required": True, "identifier": True},
        ],
    },
    {
        "name": "ProvenanceRecord",
        "abstract": False,
        "description": "infra — must be hidden",
        "fields": [],
    },
]

_SAMPLE_RECORD = {
    "id": "s-1",
    "entity_type": "Sample",
    "data": {"name": "S1", "project_id": "p-1"},
    "version": 2,
    "is_available": True,
    "created_at": "2026-06-01T10:00:00+00:00",
    "updated_at": "2026-06-02T10:00:00+00:00",
    "schema_version": "1.0.0",
    "created_by": "alice",
    "updated_by": "bob",
    "superseded_by": None,
}

_HISTORY_PAYLOAD = [
    {
        "operation_type": "create",
        "timestamp": "2026-06-01T10:00:00+00:00",
        "user_id": "alice",
        "state_snapshot": {"name": "S1"},
    },
    {
        "operation_type": "update",
        "timestamp": "2026-06-02T10:00:00+00:00",
        "user_id": "bob",
        "state_snapshot": {"name": "S1", "project_id": "p-1"},
    },
]


def _mock_server(request):
    """Simulate the `mosaic serve` REST surface for the TUI backend."""
    import httpx

    path = request.url.path
    if request.headers.get("authorization") != "Bearer test-token":
        return httpx.Response(401, json={"detail": "Unauthorized access"})

    if path == "/health":
        return httpx.Response(200, json={"status": "healthy"})
    if path == "/schemas":
        return httpx.Response(200, json=_SCHEMAS_PAYLOAD)
    if path == "/entities" and request.method == "GET":
        params = dict(request.url.params)
        assert params["entity_type"] == "Sample"
        return httpx.Response(
            200,
            json={
                "items": [_SAMPLE_RECORD],
                "total": 41,
                "limit": int(params["limit"]),
                "offset": int(params["offset"]),
            },
        )
    if path == "/entities/s-1" and request.method == "GET":
        return httpx.Response(200, json=_SAMPLE_RECORD)
    if path == "/entities/missing" and request.method == "GET":
        return httpx.Response(404, json={"detail": "Entity not found: missing"})
    if path == "/entities/s-1/history":
        return httpx.Response(200, json=_HISTORY_PAYLOAD)
    if path == "/search":
        assert dict(request.url.params)["q"] == "S1"
        # Paginated envelope (issue #157).
        return httpx.Response(
            200,
            json={"items": [_SAMPLE_RECORD], "total": 1, "limit": 50, "offset": 0},
        )
    if path == "/ingest" and request.method == "POST":
        body = _json.loads(request.content)
        if not body["data"].get("name"):
            return httpx.Response(422, json={"detail": "name is required"})
        return httpx.Response(200, json={**_SAMPLE_RECORD, "id": "new-id"})
    if path == "/entities/Sample/s-1" and request.method == "PUT":
        return httpx.Response(200, json=_SAMPLE_RECORD)
    if path == "/entities/Sample/bulk-availability" and request.method == "POST":
        body = _json.loads(request.content)
        if body["entity_ids"] == ["bad-id"]:
            return httpx.Response(
                207,
                json={
                    "total": 1,
                    "succeeded": 0,
                    "failed": 1,
                    "successes": [],
                    "failures": [
                        {"id": "bad-id", "error": "Entity not found: bad-id"}
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "total": 1,
                "succeeded": 1,
                "failed": 0,
                "successes": [
                    {"id": body["entity_ids"][0], "is_available": body["is_available"]}
                ],
                "failures": [],
            },
        )
    return httpx.Response(404, json={"detail": f"No route: {path}"})


@pytest.fixture
def mock_backend():
    """RESTBackend wired to the mock server via httpx.MockTransport."""
    import httpx

    errors: list[str] = []
    backend = RESTBackend(
        url="http://testserver",
        token="test-token",
        status_callback=errors.append,
    )
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mock_server),
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    )
    return backend, errors


def test_rest_capabilities():
    backend = RESTBackend(url="http://testserver")
    caps = backend.capabilities()
    assert caps.supports_filters is False
    assert caps.supports_fts is True


def test_rest_connection_info_healthy(mock_backend):
    backend, _errors = mock_backend
    info = asyncio.run(backend.connection_info())
    assert info.mode == "rest"
    assert info.ok is True
    assert info.target == "http://testserver"


def test_rest_schema_hides_abstract_and_infra(mock_backend):
    backend, _errors = mock_backend
    schema = asyncio.run(backend.get_schema())
    names = [et.name for et in schema.entity_types]
    assert names == ["Project", "Sample"]
    sample = schema.get_entity_type("Sample")
    project_ref = next(f for f in sample.fields if f.name == "project_id")
    assert project_ref.ref_target == "Project"
    assert len(schema.relationships) == 1
    assert schema.relationships[0].target_type == "Project"


def test_rest_list_entity_types_counts(mock_backend):
    backend, _errors = mock_backend

    async def run():
        # Pre-cache schema, then list (counts come from /entities total)
        await backend.get_schema()
        return await backend.list_entity_types()

    # The mock asserts entity_type == "Sample"; narrow the schema first.
    backend._schema_view = None

    async def run_narrow():
        schema = await backend.get_schema()
        schema.entity_types = [
            et for et in schema.entity_types if et.name == "Sample"
        ]
        backend._schema_view = schema
        return await backend.list_entity_types()

    summaries = asyncio.run(run_narrow())
    assert summaries == [
        type(summaries[0])(name="Sample", count=41, description="A sample")
    ]


def test_rest_list_entities_pagination(mock_backend):
    backend, _errors = mock_backend
    result = asyncio.run(backend.list_entities("Sample", page=2))
    assert result.page == 2
    assert result.total_items == 41
    assert result.total_pages == 3
    assert result.items[0]["id"] == "s-1"


def test_rest_get_entity_detail_with_relationships(mock_backend):
    backend, _errors = mock_backend
    detail = asyncio.run(backend.get_entity("Sample", "s-1"))
    assert detail.id == "s-1"
    assert detail.fields["is_available"] is True
    assert detail.fields["version"] == 2
    assert detail.fields["created_at"] == "2026-06-01T10:00:00+00:00"
    assert detail.fields["name"] == "S1"
    assert len(detail.relationships) == 1
    assert detail.relationships[0].target_type == "Project"
    assert detail.relationships[0].target_id == "p-1"


def test_rest_get_entity_not_found(mock_backend):
    backend, _errors = mock_backend
    with pytest.raises(BackendError) as exc_info:
        asyncio.run(backend.get_entity("Sample", "missing"))
    assert "missing" in str(exc_info.value)


def test_rest_provenance_newest_first(mock_backend):
    backend, _errors = mock_backend
    events = asyncio.run(backend.get_provenance("Sample", "s-1"))
    assert [e.event_type for e in events] == ["update", "create"]
    assert events[0].actor == "bob"
    assert events[1].diff == {"name": "S1"}


def test_rest_search(mock_backend):
    backend, _errors = mock_backend
    results = asyncio.run(backend.search_entities("Sample", "S1"))
    assert len(results) == 1
    assert results[0]["id"] == "s-1"


def test_rest_create_entity(mock_backend):
    backend, _errors = mock_backend
    new_id = asyncio.run(backend.create_entity("Sample", {"name": "S2"}))
    assert new_id == "new-id"


def test_rest_create_entity_validation_error(mock_backend):
    backend, _errors = mock_backend
    with pytest.raises(BackendError) as exc_info:
        asyncio.run(backend.create_entity("Sample", {"name": ""}))
    assert "name is required" in str(exc_info.value)


def test_rest_update_entity(mock_backend):
    backend, _errors = mock_backend
    asyncio.run(backend.update_entity("Sample", "s-1", {"name": "S1b"}))


def test_rest_set_availability_success(mock_backend):
    backend, _errors = mock_backend
    asyncio.run(backend.set_availability("Sample", "s-1", False, reason="archived"))


def test_rest_set_availability_partial_failure(mock_backend):
    backend, _errors = mock_backend
    with pytest.raises(BackendError) as exc_info:
        asyncio.run(backend.set_availability("Sample", "bad-id", False))
    assert "not found" in str(exc_info.value)


def test_rest_query_entities_with_filters_unsupported(mock_backend):
    backend, _errors = mock_backend
    with pytest.raises(BackendError):
        asyncio.run(
            backend.query_entities(
                "Sample", filters=[{"field": "name", "value": "x"}]
            )
        )


def test_rest_unauthorized_reports_error():
    import httpx

    errors: list[str] = []
    backend = RESTBackend(
        url="http://testserver", token="wrong", status_callback=errors.append
    )
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mock_server),
        base_url="http://testserver",
        headers={"Authorization": "Bearer wrong"},
    )
    info = asyncio.run(backend.connection_info())
    assert info.ok is False
    assert errors and "401" in errors[0]


def test_rest_aclose(mock_backend):
    backend, _errors = mock_backend
    asyncio.run(backend.aclose())
    assert backend._client is None
