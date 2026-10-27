"""Inverse slots over an abstract or subclassed range (issue #224).

``Donor.samples: {range: Sample, inverse: donor}`` where ``Sample`` is
abstract: rows live in the concrete subclasses' per-class tables (``Brain``,
``CSF`` and ``Cerebrum``, a subclass of the concrete ``Brain``). Before #224
the reverse edge queried a ``Sample`` table that does not exist, so
hydration came back empty, ``count_relationship`` returned 0 and ``some``
/ ``none`` failed. It must now resolve across the range's whole concrete
closure — and a concrete range with subclasses (``Donor.brains`` →
``Brain``) must include the subclass rows too.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import SchemaError
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry, concrete_class_closure

SCHEMA = """\
id: https://example.org/inverse_abstract
name: inverse_abstract
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Donor:
    is_a: Entity
    attributes:
      name:
        required: true
      samples:
        range: Sample
        multivalued: true
        inverse: donor
      brains:
        range: Brain
        multivalued: true
        inverse: donor
  Sample:
    is_a: Entity
    abstract: true
    attributes:
      label:
      donor:
        range: Donor
      derived_from:
        range: Sample
      derived_samples:
        range: Sample
        multivalued: true
        inverse: derived_from
  Brain:
    is_a: Sample
    attributes:
      hemisphere:
  Cerebrum:
    is_a: Brain
  CSF:
    is_a: Sample
    attributes:
      volume_ml:
        range: float
"""


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_yaml(SCHEMA)


@pytest.fixture
def client(registry: SchemaRegistry):
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "inv_abstract.db"), schema_registry=registry
        )
        c = MosaicClient(storage=storage, bypass_validation=True)
        c.put("Donor", {"id": "d1", "name": "D1"})
        c.put("Donor", {"id": "d2", "name": "D2"})
        c.put("Donor", {"id": "d3", "name": "D3"})  # no samples
        c.put("Brain", {"id": "b1", "label": "brain", "donor": "d1"})
        c.put("Cerebrum", {"id": "c1", "label": "cerebrum", "donor": "d1", "derived_from": "b1"})
        c.put("CSF", {"id": "f1", "label": "csf", "donor": "d1", "volume_ml": 2.0})
        c.put("CSF", {"id": "f2", "label": "csf", "donor": "d2", "volume_ml": 1.0})
        yield c


def _ids(page) -> list[str]:
    return sorted(e["id"] for e in page.items)


class TestClosure:
    def test_abstract_range_resolves_to_concrete_descendants(self, registry):
        assert concrete_class_closure(registry.schema_view, "Sample") == (
            "Brain", "CSF", "Cerebrum",
        )

    def test_concrete_range_comes_first_then_its_subclasses(self, registry):
        assert concrete_class_closure(registry.schema_view, "Brain") == (
            "Brain", "Cerebrum",
        )

    def test_inverse_slot_carries_target_classes(self, registry):
        inv = {i.name: i for i in registry.inverse_reference_slots("Donor")}
        assert inv["samples"].tables == ("Brain", "CSF", "Cerebrum")
        assert inv["brains"].tables == ("Brain", "Cerebrum")

    def test_abstract_range_without_concrete_descendant_is_rejected(self):
        bad = SCHEMA.replace("  Brain:\n    is_a: Sample", "  Brain:\n    abstract: true\n    is_a: Sample")
        bad = bad.replace("  Cerebrum:\n    is_a: Brain", "  Cerebrum:\n    abstract: true\n    is_a: Brain")
        bad = bad.replace("  CSF:\n    is_a: Sample", "  CSF:\n    abstract: true\n    is_a: Sample")
        with pytest.raises(SchemaError, match="no concrete descendant"):
            SchemaRegistry.from_yaml(bad)


class TestHydration:
    def test_abstract_range_hydrates_across_tables(self, client):
        data = client.get("Donor", "d1")["data"]
        assert sorted(data["samples"]) == ["b1", "c1", "f1"]

    def test_concrete_range_includes_subclass_rows(self, client):
        assert sorted(client.get("Donor", "d1")["data"]["brains"]) == ["b1", "c1"]

    def test_self_referencing_abstract_inverse(self, client):
        assert client.get("Brain", "b1")["data"]["derived_samples"] == ["c1"]

    def test_unavailable_targets_are_excluded(self, client):
        client.delete("Cerebrum", "c1")
        assert sorted(client.get("Donor", "d1")["data"]["samples"]) == ["b1", "f1"]


class TestCount:
    def test_count_sums_across_tables(self, client):
        assert client.count_relationship("Donor", "d1", "samples") == 3
        assert client.count_relationship("Donor", "d2", "samples") == 1
        assert client.count_relationship("Donor", "d3", "samples") == 0

    def test_count_concrete_range_with_subclass(self, client):
        assert client.count_relationship("Donor", "d1", "brains") == 2


class TestFilter:
    def _q(self, client, where):
        return _ids(client.query("Donor", where=where))

    def test_some_matches_a_row_in_any_table(self, client):
        where = {"edge": "samples", "quantifier": "some",
                 "where": {"field": "label", "op": "eq", "value": "cerebrum"}}
        assert self._q(client, where) == ["d1"]

    def test_some_shared_field_hits_multiple_tables(self, client):
        where = {"edge": "samples", "quantifier": "some",
                 "where": {"field": "label", "op": "eq", "value": "csf"}}
        assert self._q(client, where) == ["d1", "d2"]

    def test_none_includes_donors_without_samples(self, client):
        where = {"edge": "samples", "quantifier": "none",
                 "where": {"field": "label", "op": "eq", "value": "brain"}}
        assert self._q(client, where) == ["d2", "d3"]

    def test_concrete_range_filter_includes_subclass(self, client):
        where = {"edge": "brains", "quantifier": "some",
                 "where": {"field": "label", "op": "eq", "value": "cerebrum"}}
        assert self._q(client, where) == ["d1"]

    def test_subclass_only_field_is_rejected(self, client):
        from mosaic.core.exceptions import ValidationError

        where = {"edge": "samples", "quantifier": "some",
                 "where": {"field": "volume_ml", "op": "gte", "value": 1.0}}
        with pytest.raises(ValidationError, match="volume_ml"):
            self._q(client, where)
