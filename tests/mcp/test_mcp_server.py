"""MCP resource tests (ADR-0009 / issue #182): the schema and capability-
manifest resources.

Content-correctness tests use the SDK's in-memory ``Client`` (no HTTP,
direct in-process transport) against ``create_mcp_server`` — the fast,
direct way to check what a resource actually returns. ``TestRealMount``
is the one test that goes over a REAL HTTP server: it is the only thing
that can prove the mount + lifespan wiring genuinely works end to end —
a mounted sub-app's lifespan never runs on its own (Starlette), so
``create_default_app`` must enter the MCP session manager's lifespan
itself, or every request fails with "Task group is not initialized".
"""

from __future__ import annotations

import json

import pytest
from mcp.client.client import Client

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ConfigError
from mosaic.core.schema_typing import build_capability_manifest, build_type_model
from mosaic.mcp.server import create_mcp_server
from mosaic.serve import create_default_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _read_json(server, uri: str) -> dict:
    async with Client(server) as client:
        result = await client.read_resource(uri)
        (content,) = result.contents
        return json.loads(content.text)


class TestExtraDetection:
    def test_mcp_available_when_installed(self):
        from mosaic.mcp import mcp_available

        assert mcp_available() is True

    def test_schemaless_client_raises_config_error(self):
        with pytest.raises(ConfigError, match="schema-backed"):
            create_mcp_server(MosaicClient())

    def test_create_default_app_requires_registry_for_mcp(self):
        with pytest.raises(ConfigError, match="schema-backed"):
            create_default_app(hippo_client=MosaicClient(), mcp=True)


@pytest.mark.anyio
class TestSchemaResource:
    async def test_lists_every_exposed_entity_type(self, hippo_client, registry):
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://schema")
        assert set(payload) == set(build_type_model(registry))
        assert "Donor" in payload and "Sample" in payload

    async def test_field_classification_matches_the_type_model(self, hippo_client, registry):
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://schema")
        fields = {f["name"]: f for f in payload["Sample"]["fields"]}
        assert fields["donor_id"]["kind"] == "reference"
        assert fields["donor_id"]["target_entity_type"] == "Donor"
        assert fields["volume_ml"]["kind"] == "scalar"
        assert fields["name"]["required"] is True

    async def test_every_field_carries_is_external_xref(self, hippo_client, registry):
        # Regression for issue #211: is_external_xref was silently dropped by
        # this serializer even though SlotModel carries it.
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://schema")
        for entity in payload.values():
            for field in entity["fields"]:
                assert "is_external_xref" in field


@pytest.mark.anyio
class TestCapabilitiesResource:
    async def test_lists_every_exposed_entity_type(self, hippo_client, registry):
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://capabilities")
        assert set(payload) == set(build_capability_manifest(registry))

    async def test_reference_field_has_predicate_not_filter_ops(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://capabilities")
        donor_id = next(f for f in payload["Sample"]["fields"] if f["name"] == "donor_id")
        assert donor_id["filter_ops"] == []
        assert donor_id["predicate"] is True

    async def test_search_available_reflects_hippo_search_annotation(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://capabilities")
        assert payload["Sample"]["search_available"] is True
        assert payload["Donor"]["search_available"] is False

    async def test_content_matches_build_capability_manifest_directly(self, hippo_client, registry):
        # Cross-check against #181's manifest, not just internal shape —
        # this resource must not silently diverge from what it's built on.
        from mosaic.mcp.serialize import entity_capability_to_dict

        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://capabilities")
        expected = {
            name: entity_capability_to_dict(e)
            for name, e in build_capability_manifest(registry).items()
        }
        assert payload == expected


@pytest.mark.anyio
class TestConstructQuerySpecPrompt:
    async def test_is_registered_and_returns_guidance(self, hippo_client):
        async with Client(create_mcp_server(hippo_client)) as client:
            prompts = await client.list_prompts()
            assert "construct_query_spec" in {p.name for p in prompts.prompts}
            result = await client.get_prompt("construct_query_spec", {})
            text = result.messages[0].content.text
            assert "mosaic://capabilities" in text
            assert "RelatedCondition" in text

    async def test_goal_argument_is_echoed_into_the_prompt(self, hippo_client):
        async with Client(create_mcp_server(hippo_client)) as client:
            result = await client.get_prompt(
                "construct_query_spec", {"goal": "samples over 5ml"}
            )
            text = result.messages[0].content.text
            assert "samples over 5ml" in text

    async def test_columns_guidance_matches_what_is_actually_implemented(self, hippo_client):
        # The prompt must teach the real, current behaviour (issue #215):
        # anchor-owned slots are accepted; a traversal is not. Both statements
        # must stay true at every commit, because the chat panel round-trips
        # every spec through this parser.
        async with Client(create_mcp_server(hippo_client)) as client:
            result = await client.get_prompt("construct_query_spec", {})
            text = result.messages[0].content.text
            assert "ANCHOR slots only" in text
            assert "COLUMNS_NOT_SUPPORTED" in text
            assert "omit a traversal" in text.lower()

    async def test_capabilities_advertise_column_projection(self, hippo_client):
        # ADR-0029: a client tells a projecting deployment from a
        # non-projecting one by reading the manifest, not by probing.
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://capabilities")
        assert payload and all(e["column_projection"] == "anchor" for e in payload.values())


class TestRealMount:
    """Proves the mount + session-manager lifespan wiring works over a
    real HTTP server -- the one thing that cannot be proven by reading
    the code or by the in-memory tests above."""

    def test_resources_are_reachable_through_the_real_mount(self, mcp_app):
        import threading
        import time

        import anyio
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        config = uvicorn.Config(mcp_app, host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if getattr(server, "started", False):
                    break
                time.sleep(0.02)
            assert server.started, "uvicorn server did not start in time"
            port = server.servers[0].sockets[0].getsockname()[1]

            async def _call() -> dict:
                url = f"http://127.0.0.1:{port}/mcp/"
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.read_resource("mosaic://schema")
                        (content,) = result.contents
                        return json.loads(content.text)

            payload = anyio.run(_call)
            assert "Donor" in payload and "Sample" in payload
        finally:
            server.should_exit = True
            thread.join(timeout=5)
