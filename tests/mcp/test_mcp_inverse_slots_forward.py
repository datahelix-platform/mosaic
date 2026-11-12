"""The reverse edge over MCP when ``inverse`` is declared on the forward slot (ADR-0012).

``Sample.donor_id: {range: Donor, inverse: samples}`` synthesizes
``Donor.samples``; the capability manifest advertises it with ``inverse_of``
and the ``QuerySpec`` tools answer through the reverse-FK path, exactly as
for the reverse-side declaration in ``test_mcp_inverse_slots.py``.
"""

from __future__ import annotations

import json

import pytest
from mcp.client.client import Client

from mosaic.linkml_bridge import SchemaRegistry
from mosaic.mcp.server import create_mcp_server

from tests.mcp.conftest import MCP_TEST_SCHEMA

_FORWARD = "      donor_id:\n        range: Donor\n"
assert MCP_TEST_SCHEMA.count(_FORWARD) == 1
FORWARD_INVERSE_SCHEMA = MCP_TEST_SCHEMA.replace(
    _FORWARD, _FORWARD + "        inverse: samples\n"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(FORWARD_INVERSE_SCHEMA)


async def _read_json(server, uri: str) -> dict:
    async with Client(server) as client:
        result = await client.read_resource(uri)
        (content,) = result.contents
        return json.loads(content.text)


async def _call_tool(server, name: str, args: dict) -> dict:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        (content,) = result.content
        return json.loads(content.text)


def _seed(hippo_client):
    hippo_client.put("Donor", {"id": "d1", "name": "D1", "sex": "female"})
    hippo_client.put("Donor", {"id": "d2", "name": "D2", "sex": "male"})
    hippo_client.put("Donor", {"id": "d3", "name": "D3", "sex": "unknown"})
    hippo_client.put("Sample", {"id": "s1", "name": "S1", "donor_id": "d1", "volume_ml": 5.0})
    hippo_client.put("Sample", {"id": "s2", "name": "S2", "donor_id": "d1", "volume_ml": 15.0})
    hippo_client.put("Sample", {"id": "s3", "name": "S3", "donor_id": "d2", "volume_ml": 25.0})


def _reverse_spec(quantifier: str = "some") -> dict:
    return {
        "v": 1,
        "anchor": "Donor",
        "mode": "AND",
        "criteria": [
            {
                "kind": "related",
                "edge": "samples",
                "quantifier": quantifier,
                "criteria": [{"kind": "field", "slot": "volume_ml", "op": "gte", "value": 10.0}],
            }
        ],
    }


@pytest.mark.anyio
class TestForwardDeclaredInverseOverMcp:
    async def test_capabilities_advertise_the_synthesized_edge(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _read_json(server, "mosaic://capabilities")
        samples = next(f for f in payload["Donor"]["fields"] if f["name"] == "samples")
        assert samples["kind"] == "reference"
        assert samples["multivalued"] is True
        assert samples["target_entity_type"] == "Sample"
        assert samples["predicate"] is True
        assert samples["inverse_of"] == "donor_id"
        donor_id = next(f for f in payload["Sample"]["fields"] if f["name"] == "donor_id")
        assert donor_id["inverse_of"] is None

    async def test_validate_accepts_the_reverse_edge(self, hippo_client):
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(server, "validate_query_spec", {"query_spec": _reverse_spec()})
        assert payload == {"valid": True, "errors": []}

    async def test_execute_some_and_none(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(server, "execute_query_spec", {"query_spec": _reverse_spec()})
        assert payload["valid"] is True, payload
        assert {i["id"] for i in payload["items"]} == {"d1", "d2"}
        payload = await _call_tool(server, "execute_query_spec", {"query_spec": _reverse_spec("none")})
        assert {i["id"] for i in payload["items"]} == {"d3"}

    async def test_count_query_spec_agrees(self, hippo_client):
        _seed(hippo_client)
        server = create_mcp_server(hippo_client)
        payload = await _call_tool(server, "count_query_spec", {"query_spec": _reverse_spec()})
        assert payload == {"valid": True, "errors": [], "count": 2}
