"""Tests for PassThroughAuthMiddleware."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mosaic.api.factory import create_app


def create_test_app() -> tuple[FastAPI, TestClient]:
    """Create a test app with middleware and return app and client."""
    app = create_app()

    @app.get("/test")
    def endpoint(request: Request):
        ctx = getattr(request.state, "hippo_context", None)
        if ctx is None:
            return {"actor_id": "NO_CONTEXT", "has_ctx": False}
        return {"actor_id": ctx.actor_id, "has_ctx": True}

    @app.get("/test2")
    def endpoint2(request: Request):
        return {"actor_id": request.state.hippo_context.actor_id}

    @app.get("/test3")
    def endpoint3(request: Request):
        return {"raw": request.state.hippo_context.raw_header}

    return app, TestClient(app, raise_server_exceptions=False)


class TestValidHeaderScenario:
    """Tests for valid X-DataHelix-Actor header format (actor:<identifier>)."""

    def test_valid_header_extracts_actor(self):
        """Given valid header with identifier, extracts actor into context."""
        _, client = create_test_app()
        response = client.get("/test", headers={"x-datahelix-actor": "actor:user123"})
        assert response.status_code == 200
        assert response.json()["actor_id"] == "user123"

    def test_valid_header_with_complex_identifier(self):
        """Given valid header with complex identifier, extracts full identifier."""
        _, client = create_test_app()
        response = client.get(
            "/test2", headers={"x-datahelix-actor": "actor:service-abc-123@domain.com"}
        )
        assert response.status_code == 200
        assert response.json()["actor_id"] == "service-abc-123@domain.com"

    def test_valid_header_stores_raw_header(self):
        """Given valid header, raw header is stored in context."""
        _, client = create_test_app()
        response = client.get("/test3", headers={"x-datahelix-actor": "actor:testuser"})
        assert response.status_code == 200
        assert response.json()["raw"] == "actor:testuser"


class TestMissingHeaderScenario:
    """Tests for missing X-DataHelix-Actor header."""

    def test_no_header_continues_with_empty_actor(self):
        """Given no header, continues with empty actor identifier."""
        _, client = create_test_app()
        response = client.get("/test")
        assert response.status_code == 200
        assert response.json()["actor_id"] == ""


class TestInvalidHeaderFormatScenario:
    """Tests for invalid X-DataHelix-Actor header format."""

    def test_invalid_format_returns_401(self):
        """Given invalid format (not starting with 'actor:'), returns 401."""
        _, client = create_test_app()
        response = client.get("/test", headers={"x-datahelix-actor": "Bearer token123"})
        assert response.status_code == 401
        assert response.json()["error"] == "Invalid X-DataHelix-Actor header format"

    def test_invalid_format_no_prefix_returns_401(self):
        """Given header without 'actor:' prefix, returns 401."""
        _, client = create_test_app()
        response = client.get("/test2", headers={"x-datahelix-actor": "just-a-string"})
        assert response.status_code == 401
        assert response.json()["error"] == "Invalid X-DataHelix-Actor header format"


class TestEmptyIdentifierScenario:
    """Tests for empty identifier after 'actor:'."""

    def test_empty_identifier_returns_401(self):
        """Given empty identifier after 'actor:', returns 401."""
        _, client = create_test_app()
        response = client.get("/test", headers={"x-datahelix-actor": "actor:"})
        assert response.status_code == 401
        assert (
            response.json()["error"] == "Empty actor identifier in X-DataHelix-Actor header"
        )

    def test_whitespace_only_identifier_returns_401(self):
        """Given whitespace-only identifier, returns 401."""
        _, client = create_test_app()
        response = client.get("/test2", headers={"x-datahelix-actor": "actor:   "})
        assert response.status_code == 401
        assert (
            response.json()["error"] == "Empty actor identifier in X-DataHelix-Actor header"
        )


class TestMultipleHeadersScenario:
    """Tests for multiple X-DataHelix-Actor headers."""

    def test_multiple_headers_uses_first(self):
        """Given multiple headers, uses first value."""
        _, client = create_test_app()
        response = client.get(
            "/test",
            headers=[
                ("x-datahelix-actor", "actor:first"),
                ("x-datahelix-actor", "actor:second"),
            ],
        )
        assert response.status_code == 200
        assert response.json()["actor_id"] == "first"
