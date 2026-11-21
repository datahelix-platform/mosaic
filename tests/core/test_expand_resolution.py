"""End-to-end `expand=` resolution against a real SQLite-backed client (#128).

Reference slots are stored as bare ids — a single-valued reference is a plain
string id, a multivalued reference a hydrated ``list[str]`` (issue #79 /
ADR-0002). The pre-#128 `BatchFetcher` only recognized nested ``list[dict]`` /
``dict`` shapes, so `client.get(..., expand=...)` silently resolved nothing
against real entity data — every prior expand test used a `Mock()` storage with
hand-shaped nested dicts and never caught it. These tests exercise a real
adapter so that class of regression is caught going forward.
"""

import os
import tempfile

import pytest

import mosaic


def _client(tmpdir: str) -> "mosaic.MosaicClient":
    schema = """
id: https://example.org/exp
name: exp
prefixes: {linkml: 'https://w3id.org/linkml/'}
imports: [linkml:types, mosaic_core]
default_range: string
classes:
  Region:
    is_a: Entity
    attributes: {label: {range: string}}
  Owner:
    is_a: Entity
    attributes:
      label: {range: string}
      region: {range: Region}
  Widget:
    is_a: Entity
    attributes:
      owner: {range: Owner}
      tags: {range: Owner, multivalued: true}
"""
    p = os.path.join(tmpdir, "s.yaml")
    with open(p, "w") as fh:
        fh.write(schema)
    return mosaic.client_for_schema(p, database_url=os.path.join(tmpdir, "h.db"))


class TestExpandResolution:
    @pytest.fixture
    def client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            c = _client(tmpdir)
            c.put("Region", {"label": "north"}, entity_id="REG-1")
            c.put("Owner", {"label": "o1", "region": "REG-1"}, entity_id="OWN-1")
            c.put("Owner", {"label": "o2"}, entity_id="OWN-2")
            c.put("Widget", {"owner": "OWN-1", "tags": ["OWN-1", "OWN-2"]}, entity_id="W-1")
            yield c

    def test_single_valued_reference_resolves_to_entity(self, client) -> None:
        got = client.get("Widget", "W-1", expand="owner")
        owner = got["_expanded"]["owner"]
        assert isinstance(owner, dict)
        assert owner["id"] == "OWN-1"
        assert owner["data"]["label"] == "o1"

    def test_multivalued_reference_resolves_to_list(self, client) -> None:
        got = client.get("Widget", "W-1", expand="tags")
        tags = got["_expanded"]["tags"]
        assert isinstance(tags, list)
        assert {t["id"] for t in tags} == {"OWN-1", "OWN-2"}
        assert all("data" in t for t in tags)

    def test_nested_expansion_hangs_off_expanded(self, client) -> None:
        got = client.get("Widget", "W-1", expand="owner.region")
        owner = got["_expanded"]["owner"]
        region = owner["_expanded"]["region"]
        assert region["id"] == "REG-1"
        assert region["data"]["label"] == "north"

    def test_absent_slot_yields_no_expanded_key(self, client) -> None:
        # An Owner with no ``region`` set: expanding it records no key rather
        # than a spurious null (the slot is absent from the entity's data).
        got = client.get("Owner", "OWN-2", expand="region")
        assert got["_expanded"] == {}

    def test_no_expand_leaves_result_unexpanded(self, client) -> None:
        got = client.get("Widget", "W-1")
        assert "_expanded" not in got


class TestBatchFetcherReferenceIds:
    """Unit coverage for the shape handling that was the actual bug — a bare
    string id and a ``list[str]``, not just nested dicts."""

    def test_reference_ids_shapes(self) -> None:
        from mosaic.core.batch_fetcher import BatchFetcher

        f = BatchFetcher._reference_ids
        assert f("OWN-1") == (["OWN-1"], False)
        assert f(["OWN-1", "OWN-2"]) == (["OWN-1", "OWN-2"], True)
        # Backward-compat: embedded {"id": ...} / list[{"id": ...}] shapes.
        assert f({"id": "OWN-1"}) == (["OWN-1"], False)
        assert f([{"id": "OWN-1"}, {"id": "OWN-2"}]) == (["OWN-1", "OWN-2"], True)
        # Non-reference / empty shapes yield no ids.
        assert f(None) == ([], False)
        assert f(42) == ([], False)
