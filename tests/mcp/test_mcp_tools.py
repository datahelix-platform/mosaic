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

    async def test_unknown_or_over_deep_columns_come_back_coded(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        base = {"v": 1, "anchor": "Sample", "mode": "AND", "criteria": []}
        unknown = await _call_tool(
            server, "execute_query_spec", {"query_spec": {**base, "columns": [{"path": ["nope"]}]}}
        )
        assert unknown["valid"] is False
        assert unknown["errors"][0]["code"] == "UNKNOWN_SLOT"
        deep = await _call_tool(
            server,
            "execute_query_spec",
            {"query_spec": {**base, "columns": [{"path": ["donor_id", "a", "b", "c"]}]}},
        )
        assert deep["valid"] is False
        assert deep["errors"][0]["code"] == "COLUMNS_NOT_SUPPORTED"
        assert deep["items"] is None

    async def test_a_hop_through_a_reference_returns_its_value(self, hippo_client):
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
                    "columns": [{"path": ["name"]}, {"path": ["donor_id", "name"]}],
                }
            },
        )
        assert payload["valid"] is True
        assert "grain" not in payload
        assert sorted(i["columns"]["name"] for i in payload["items"]) == ["S1", "S2"]
        assert {i["columns"]["donor_id.name"] for i in payload["items"]} == {"D1"}
        assert all(set(i["data"]) == {"name"} for i in payload["items"])

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


@pytest.fixture
def traversal_client(tmp_path):
    """The shared traversal shape (issue #236) behind a real SQLite store."""
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
    from tests.support import traversal_fixture

    registry = traversal_fixture.registry()
    client = MosaicClient(
        storage=SQLiteAdapter(str(tmp_path / "t.db"), schema_registry=registry), registry=registry
    )
    traversal_fixture.seed(client)
    return client


async def _execute(client, anchor, columns, **extra):
    return await _call_tool(
        create_mcp_server(client),
        "execute_query_spec",
        {"query_spec": {"v": 1, "anchor": anchor, "mode": "AND", "criteria": [], "columns": columns, **extra}},
    )


def _rows(payload):
    return [(i["id"], i["columns"]) for i in payload["items"]]


@pytest.mark.anyio
class TestExecuteTraversalColumns:
    """Issue #236 acceptance, over the MCP wire: per-column values under
    `columns`, one item per (anchor x member) on explode, `grain` stated."""

    async def test_to_one_chain(self, traversal_client):
        payload = await _execute(traversal_client, "Sample", [{"path": ["donor", "age_at_death"]}])
        assert payload["valid"] is True and payload["total"] == 3
        assert _rows(payload) == [
            ("s1", {"donor.age_at_death": 71}),
            ("s2", {"donor.age_at_death": None}),
            ("s3", {"donor.age_at_death": None}),
        ]
        assert "grain" not in payload

    async def test_to_many_count_and_join_ids_keep_anchor_grain(self, traversal_client):
        payload = await _execute(
            traversal_client,
            "Sample",
            [
                {"path": ["aliquots", "volume_ul"], "mode": "count"},
                {"path": ["aliquots", "id"], "mode": "joinIds"},
            ],
        )
        assert _rows(payload) == [
            ("s1", {"aliquots.volume_ul": 2, "aliquots.id": ["a1", "a2"]}),
            ("s2", {"aliquots.volume_ul": 1, "aliquots.id": ["a3"]}),
            ("s3", {"aliquots.volume_ul": 0, "aliquots.id": []}),
        ]
        assert "grain" not in payload

    async def test_explode_repeats_the_anchor_and_states_the_grain(self, traversal_client):
        payload = await _execute(
            traversal_client, "Sample", [{"path": ["name"]}, {"path": ["aliquots", "volume_ul"], "mode": "explode"}]
        )
        assert payload["valid"] is True
        # `total` still counts anchors; the grain says what a row is.
        assert payload["total"] == 3
        assert payload["grain"] == {"anchor": 3, "rows": 4, "edge": "aliquots"}
        assert _rows(payload) == [
            ("s1", {"name": "S1", "aliquots.volume_ul": 10.0}),
            ("s1", {"name": "S1", "aliquots.volume_ul": 20.0}),
            ("s2", {"name": "S2", "aliquots.volume_ul": 5.0}),
            ("s3", {"name": "S3", "aliquots.volume_ul": None}),
        ]
        assert [i["data"] for i in payload["items"]][:2] == [{"name": "S1"}, {"name": "S1"}]

    async def test_explode_shows_all_members_whatever_the_criteria(self, traversal_client):
        payload = await _execute(
            traversal_client,
            "Sample",
            [{"path": ["aliquots", "volume_ul"], "mode": "explode"}],
            criteria=[
                {
                    "kind": "related",
                    "edge": "aliquots",
                    "quantifier": "some",
                    "criteria": [{"kind": "field", "slot": "volume_ul", "op": "gt", "value": 15.0}],
                }
            ],
        )
        # The criteria pick the anchor (s1); both its aliquots are shown.
        assert _rows(payload) == [("s1", {"aliquots.volume_ul": 10.0}), ("s1", {"aliquots.volume_ul": 20.0})]
        assert payload["grain"] == {"anchor": 1, "rows": 2, "edge": "aliquots"}

    async def test_reverse_hop_from_the_certification_fixture_shape(self, traversal_client):
        payload = await _execute(traversal_client, "Author", [{"path": ["books", "title"], "mode": "explode"}])
        assert _rows(payload) == [
            ("au1", {"books.title": "The Dispossessed"}),
            ("au1", {"books.title": "The Lathe of Heaven"}),
            ("au2", {"books.title": None}),
        ]
        assert payload["grain"] == {"anchor": 2, "rows": 3, "edge": "books"}

    async def test_two_hops_whose_list_is_the_first_hop(self, traversal_client):
        payload = await _execute(
            traversal_client, "Sample", [{"path": ["aliquots", "container", "kind"], "mode": "joinIds"}]
        )
        assert _rows(payload) == [
            ("s1", {"aliquots.container.kind": ["tube", "plate"]}),
            ("s2", {"aliquots.container.kind": []}),
            ("s3", {"aliquots.container.kind": []}),
        ]

    async def test_pagination_pages_anchors_not_rows(self, traversal_client):
        payload = await _call_tool(
            create_mcp_server(traversal_client),
            "execute_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [],
                    "columns": [{"path": ["aliquots", "label"], "mode": "explode"}],
                },
                "limit": 1,
            },
        )
        assert payload["total"] == 3
        assert _rows(payload) == [("s1", {"aliquots.label": "A1"}), ("s1", {"aliquots.label": "A2"})]
        assert payload["grain"] == {"anchor": 1, "rows": 2, "edge": "aliquots"}

    async def test_per_hop_errors_come_back_coded_with_their_path(self, traversal_client):
        payload = await _execute(traversal_client, "Sample", [{"path": ["aliquots", "volume_ul"]}])
        assert payload["valid"] is False and payload["items"] is None
        (err,) = payload["errors"]
        assert (err["code"], err["path"]) == ("COLUMNS_NOT_SUPPORTED", "$.columns[0].path[0]")
        assert "'aliquots'" in err["message"]
        validated = await _call_tool(
            create_mcp_server(traversal_client),
            "validate_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [],
                    "columns": [{"path": ["name", "x"]}],
                }
            },
        )
        assert validated["valid"] is False
        assert validated["errors"][0]["code"] == "NOT_A_REFERENCE"
        assert validated["errors"][0]["path"] == "$.columns[0].path[0]"

    async def test_aggregate_tools_still_reject_traversal_columns(self, traversal_client):
        payload = await _call_tool(
            create_mcp_server(traversal_client),
            "count_query_spec",
            {
                "query_spec": {
                    "v": 1,
                    "anchor": "Sample",
                    "mode": "AND",
                    "criteria": [],
                    "columns": [{"path": ["donor", "age_at_death"]}],
                }
            },
        )
        assert payload["errors"][0]["code"] == "COLUMNS_NOT_APPLICABLE"
