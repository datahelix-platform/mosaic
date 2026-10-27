"""Tests for the validate_query_spec/execute_query_spec MCP tools
(ADR-0009 / issue #183's remaining half).

In-memory ``Client`` tests cover the tools' behavior against real,
written-then-queried entities (fast, no real sockets). ``TestRealMount``
extends the real-HTTP pattern from ``test_mcp_server.py`` to prove the
tools work over an actual mounted server too — the in-memory transport
carries no HTTP request at all (``ctx.request_context.request`` is
``None``), so it alone cannot prove ``_client_from_context``'s app.state
lookup actually reaches the mounted app's state.
"""

from __future__ import annotations

import json

import pytest
from mcp.client.client import Client

from mosaic.mcp.server import create_mcp_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed(hippo_client):
    donor = hippo_client.create("Donor", {"name": "D1", "sex": "female"})
    hippo_client.create("Sample", {"name": "S1", "donor_id": donor["id"], "volume_ml": 5.0})
    hippo_client.create("Sample", {"name": "S2", "donor_id": donor["id"], "volume_ml": 15.0})
    return donor


async def _call_tool(server, name: str, args: dict) -> dict:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        (content,) = result.content
        return json.loads(content.text)


@pytest.mark.anyio
class TestValidateQuerySpec:
    async def test_valid_spec_reports_no_errors(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "validate_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 1.0}],
                }
            },
        )
        assert payload == {"valid": True, "errors": []}

    async def test_malformed_shape_returns_a_structured_error_not_a_crash(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server, "validate_query_spec", {"query_spec": {"anchor": "Sample"}}
        )
        assert payload["valid"] is False
        assert payload["errors"][0]["code"] == "INVALID_QUERYSPEC_SHAPE"

    async def test_unknown_slot_names_the_offending_path(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "validate_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [{"kind": "field", "slot": "nope", "op": "eq", "value": 1}],
                }
            },
        )
        assert payload["valid"] is False
        assert payload["errors"][0]["code"] == "UNKNOWN_SLOT"
        assert payload["errors"][0]["path"] == "$.criteria[0].slot"

    async def test_reference_field_direct_filter_op_is_rejected(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "validate_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [{"kind": "field", "slot": "donor_id", "op": "eq", "value": "x"}],
                }
            },
        )
        assert payload["valid"] is False
        assert payload["errors"][0]["code"] == "UNSUPPORTED_OP"


@pytest.mark.anyio
class TestExecuteQuerySpec:
    async def test_executes_a_valid_spec_against_real_data(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gt", "value": 10.0}],
                }
            },
        )
        assert payload["valid"] is True
        assert payload["total"] == 1
        # Matches PaginatedResult.items' real shape (an envelope with
        # user fields nested under "data") -- REST's own list_entities
        # returns paginated.items with no flattening either; only
        # GraphQL flattens, because it renders typed fields.
        assert payload["items"][0]["data"]["name"] == "S2"

    async def test_columns_project_each_envelope_to_the_selected_slots(self, hippo_client):
        """Issue #215: the planner can say which FIELD, and the envelope
        carries only that. One anchor stays one row; total is unaffected."""
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        spec = {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []}
        full = await _call_tool(server, "execute_query_spec", {"query_spec": spec})
        picked = await _call_tool(
            server,
            "execute_query_spec",
            {"query_spec": {**spec, "columns": [{"path": ["volume_ml"]}]}},
        )
        assert picked["valid"] is True
        assert picked["total"] == full["total"]
        assert len(picked["items"]) == len(full["items"])
        for item, whole in zip(picked["items"], full["items"]):
            assert set(item["data"]) == {"volume_ml"}
            assert set(whole["data"]) > {"volume_ml"}
            # Envelope metadata survives; only the slots are trimmed.
            assert item["id"] == whole["id"]
            assert item["entity_type"] == "Sample"

    async def test_unknown_or_multi_hop_columns_come_back_coded(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        base = {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []}
        unknown = await _call_tool(
            server, "execute_query_spec", {"query_spec": {**base, "columns": [{"path": ["nope"]}]}}
        )
        assert unknown["valid"] is False
        assert unknown["errors"][0]["code"] == "UNKNOWN_SLOT"
        hop = await _call_tool(
            server,
            "execute_query_spec",
            {"query_spec": {**base, "columns": [{"path": ["donor_id", "name"]}]}},
        )
        assert hop["valid"] is False
        assert hop["errors"][0]["code"] == "COLUMNS_NOT_SUPPORTED"

    async def test_columns_are_rejected_loudly_by_aggregate_tools(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "count_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [],
                    "columns": [{"path": ["name"]}],
                }
            },
        )
        assert payload["valid"] is False
        assert payload["errors"][0]["code"] == "COLUMNS_NOT_APPLICABLE"

    async def test_related_condition_filters_through_the_donor(self, hippo_client):
        donor = _seed(hippo_client)
        other_donor = hippo_client.create("Donor", {"name": "D2"})
        hippo_client.create(
            "Sample", {"name": "S3", "donor_id": other_donor["id"], "volume_ml": 20.0}
        )
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [
                        {
                            "kind": "related",
                            "edge": "donor_id",
                            "quantifier": "some",
                            "criteria": [{"kind": "field", "slot": "name", "op": "eq", "value": "D1"}],
                        }
                    ],
                }
            },
        )
        assert payload["valid"] is True
        assert {item["data"]["name"] for item in payload["items"]} == {"S1", "S2"}
        assert donor["id"]  # sanity: fixture actually created a donor

    async def test_invalid_spec_returns_no_items_same_envelope_as_validate(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [{"kind": "field", "slot": "nope", "op": "eq", "value": 1}],
                }
            },
        )
        assert payload["valid"] is False
        assert payload["items"] is None
        assert payload["total"] is None
        assert payload["errors"][0]["code"] == "UNKNOWN_SLOT"

    async def test_limit_and_offset_paginate(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {"query_spec": {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []}, "limit": 1, "offset": 1},
        )
        assert payload["valid"] is True
        assert payload["total"] == 2
        assert len(payload["items"]) == 1

    async def test_limit_over_max_is_rejected_loudly(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {
                "query_spec": {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []},
                "limit": 100000,
            },
        )
        assert payload["valid"] is False
        assert payload["items"] is None
        assert payload["errors"][0]["code"] == "INVALID_LIMIT"

    async def test_negative_offset_is_rejected(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {
                "query_spec": {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []},
                "offset": -1,
            },
        )
        assert payload["valid"] is False
        assert payload["errors"][0]["code"] == "INVALID_OFFSET"

    async def test_sort_by_orderable_field(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(
            server,
            "execute_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [],
                    "sort": [{"slot": "volume_ml", "direction": "desc"}],
                }
            },
        )
        assert [item["data"]["name"] for item in payload["items"]] == ["S2", "S1"]


class TestRealMountTools:
    """Proves the tools work over a real mounted HTTP server, not just
    in-memory -- the in-memory transport never populates
    ctx.request_context.request, so it cannot exercise the app.state
    lookup _client_from_context relies on in a real deployment."""

    def test_execute_query_spec_over_the_real_mount(self, hippo_client, mcp_app):
        import threading
        import time

        import anyio
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        _seed(hippo_client)

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
                        result = await session.call_tool(
                            "execute_query_spec",
                            {
                                "query_spec": {
                                    "v": 1,
                                    "anchor": "Sample",
                                    "mode": "AND",
                                    "criteria": [
                                        {"kind": "field", "slot": "volume_ml", "op": "gt", "value": 10.0}
                                    ],
                                }
                            },
                        )
                        (content,) = result.content
                        return json.loads(content.text)

            payload = anyio.run(_call)
            assert payload["valid"] is True
            assert payload["total"] == 1
            assert payload["items"][0]["data"]["name"] == "S2"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
