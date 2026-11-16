"""GraphQL surface for a reverse edge declared on the *forward* slot (ADR-0012).

Same expectations as ``test_inverse_slots.py``, but ``inverse: samples`` sits
on ``Sample.donor_id`` and ``Donor.samples`` is synthesized: the generated
schema still grows ``samples: [Sample!]``, ``samplesCount`` and
``DonorFilter.samples: {some, none}``, ``hippoSchema`` reports
``inverseOf: "donor_id"`` and the slot stays out of Create/Update inputs.
"""

from __future__ import annotations

import pytest

from mosaic.linkml_bridge import SchemaRegistry

from tests.graphql.conftest import GRAPHQL_TEST_SCHEMA

_FORWARD = "      donor_id:\n        range: Donor\n"
assert GRAPHQL_TEST_SCHEMA.count(_FORWARD) == 1
FORWARD_INVERSE_SCHEMA = GRAPHQL_TEST_SCHEMA.replace(
    _FORWARD, _FORWARD + "        inverse: samples\n"
)


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(FORWARD_INVERSE_SCHEMA)


@pytest.fixture
def seeded(hippo_client):
    hippo_client.put("Donor", {"id": "d1", "name": "D1", "sex": "female"})
    hippo_client.put("Donor", {"id": "d2", "name": "D2", "sex": "male"})
    hippo_client.put("Donor", {"id": "d3", "name": "D3"})
    hippo_client.put("Sample", {"id": "s1", "name": "S1", "donor_id": "d1", "is_tumor": True, "volume_ml": 1.0})
    hippo_client.put("Sample", {"id": "s2", "name": "S2", "donor_id": "d1", "is_tumor": False, "volume_ml": 2.0})
    hippo_client.put("Sample", {"id": "s3", "name": "S3", "donor_id": "d2", "is_tumor": True, "volume_ml": 3.0})
    return hippo_client


def _ids(page):
    return sorted(i["id"] for i in page["items"])


class TestSynthesizedEdge:
    def test_registry_reports_the_synthesized_reverse(self, registry):
        (inv,) = registry.inverse_reference_slots("Donor")
        assert (inv.name, inv.forward_slot, inv.declared_on) == ("samples", "donor_id", "forward")

    def test_hippo_schema_reports_inverse_of(self, gql):
        body = gql('{ hippoSchema { name fields { name inverseOf multivalued } } }')
        assert "errors" not in body, body
        donor = next(t for t in body["data"]["hippoSchema"] if t["name"] == "Donor")
        samples = next(f for f in donor["fields"] if f["name"] == "samples")
        assert samples == {"name": "samples", "inverseOf": "donor_id", "multivalued": True}
        sample = next(t for t in body["data"]["hippoSchema"] if t["name"] == "Sample")
        donor_id = next(f for f in sample["fields"] if f["name"] == "donor_id")
        assert donor_id["inverseOf"] is None

    def test_reverse_edge_and_count(self, seeded, gql):
        body = gql('{ donor(id: "d1") { samples { id } samplesCount } }')
        assert "errors" not in body, body
        donor = body["data"]["donor"]
        assert sorted(s["id"] for s in donor["samples"]) == ["s1", "s2"]
        assert donor["samplesCount"] == 2
        body = gql('{ donor(id: "d3") { samples { id } samplesCount } }')
        assert body["data"]["donor"] == {"samples": [], "samplesCount": 0}

    def test_some_and_none_quantifiers(self, seeded, gql):
        body = gql("{ donors(where: {samples: {some: {isTumor: {eq: true}}}}) { items { id } } }")
        assert "errors" not in body, body
        assert _ids(body["data"]["donors"]) == ["d1", "d2"]
        body = gql("{ donors(where: {samples: {none: {isTumor: {eq: true}}}}) { items { id } } }")
        assert _ids(body["data"]["donors"]) == ["d3"]

    def test_create_and_update_inputs_omit_the_synthesized_slot(self, gql):
        for input_name in ("DonorCreateInput", "DonorUpdateInput"):
            body = gql('{ __type(name: "%s") { inputFields { name } } }' % input_name)
            names = {f["name"] for f in body["data"]["__type"]["inputFields"]}
            assert "name" in names
            assert "samples" not in names, input_name
