"""GraphQL surface for the reverse of a multivalued forward slot (ADR-0013, #231).

``inverse: studies`` sits on the multivalued ``Study.sample_ids`` (stored as
ADR-0002 relationship rows), so by the ADR-0013 amendment's pair rule
``Study.sample_ids`` stays the stored, writable side and ``Sample.studies``
is synthesized: it grows ``studies: [Study!]``, ``studiesCount`` and
``SampleFilter.studies: {some, none}``, ``hippoSchema`` reports
``inverseOf: "sample_ids"``, and it stays out of Create/Update inputs while
the forward slot stays in ``StudyCreateInput``.
"""

from __future__ import annotations

import pytest

from mosaic.linkml_bridge import SchemaRegistry

from tests.graphql.conftest import GRAPHQL_TEST_SCHEMA

_FORWARD = "      sample_ids:\n        range: Sample\n        multivalued: true\n"
assert GRAPHQL_TEST_SCHEMA.count(_FORWARD) == 1
MV_INVERSE_SCHEMA = GRAPHQL_TEST_SCHEMA.replace(
    _FORWARD, _FORWARD + "        inverse: studies\n"
)


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(MV_INVERSE_SCHEMA)


@pytest.fixture
def seeded(hippo_client):
    hippo_client.put("Sample", {"id": "s1", "name": "S1", "is_tumor": True})
    hippo_client.put("Sample", {"id": "s2", "name": "S2", "is_tumor": False})
    hippo_client.put("Sample", {"id": "s3", "name": "S3", "is_tumor": True})
    hippo_client.put("Study", {"id": "t1", "title": "Alpha", "sample_ids": ["s1", "s2"]})
    hippo_client.put("Study", {"id": "t2", "title": "Beta", "sample_ids": ["s1"]})
    return hippo_client


def _ids(page):
    return sorted(i["id"] for i in page["items"])


class TestReverseOfMultivaluedForward:
    def test_registry_pair_rule(self, registry):
        assert registry.inverse_reference_slots("Study") == []
        (inv,) = registry.inverse_reference_slots("Sample")
        assert (inv.name, inv.forward_slot, inv.declared_on, inv.forward_multivalued) == (
            "studies", "sample_ids", "forward", True,
        )

    def test_hippo_schema_reports_inverse_of(self, gql):
        body = gql('{ hippoSchema { name fields { name inverseOf multivalued } } }')
        assert "errors" not in body, body
        sample = next(t for t in body["data"]["hippoSchema"] if t["name"] == "Sample")
        studies = next(f for f in sample["fields"] if f["name"] == "studies")
        assert studies == {"name": "studies", "inverseOf": "sample_ids", "multivalued": True}
        study = next(t for t in body["data"]["hippoSchema"] if t["name"] == "Study")
        fwd = next(f for f in study["fields"] if f["name"] == "sample_ids")
        assert fwd["inverseOf"] is None

    def test_forward_write_persists(self, seeded, gql):
        body = gql('{ study(id: "t1") { samples { id } } }')
        assert "errors" not in body, body
        assert sorted(s["id"] for s in body["data"]["study"]["samples"]) == ["s1", "s2"]

    def test_reverse_edge_and_count(self, seeded, gql):
        body = gql('{ sample(id: "s1") { studies { id } studiesCount } }')
        assert "errors" not in body, body
        s1 = body["data"]["sample"]
        assert sorted(s["id"] for s in s1["studies"]) == ["t1", "t2"]
        assert s1["studiesCount"] == 2
        body = gql('{ sample(id: "s3") { studies { id } studiesCount } }')
        assert body["data"]["sample"] == {"studies": [], "studiesCount": 0}

    def test_some_and_none_quantifiers(self, seeded, gql):
        body = gql('{ samples(where: {studies: {some: {title: {eq: "Beta"}}}}) { items { id } } }')
        assert "errors" not in body, body
        assert _ids(body["data"]["samples"]) == ["s1"]
        body = gql('{ samples(where: {studies: {none: {title: {eq: "Alpha"}}}}) { items { id } } }')
        assert _ids(body["data"]["samples"]) == ["s3"]

    def test_inputs(self, gql):
        for input_name in ("SampleCreateInput", "SampleUpdateInput"):
            body = gql('{ __type(name: "%s") { inputFields { name } } }' % input_name)
            names = {f["name"] for f in body["data"]["__type"]["inputFields"]}
            assert "studies" not in names, input_name
        body = gql('{ __type(name: "StudyCreateInput") { inputFields { name } } }')
        names = {f["name"] for f in body["data"]["__type"]["inputFields"]}
        assert names & {"sampleIds", "samples", "sample_ids"}, names
