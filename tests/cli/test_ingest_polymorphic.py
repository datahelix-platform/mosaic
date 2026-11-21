"""Polymorphic tree-root ingest — designates_type dispatch (issue #80).

A schema that models polymorphism the standard LinkML way — an abstract (or
concrete) base with a ``designates_type`` discriminator and concrete
subclasses — must ingest its inlined collections under the base accessor,
with each instance routed to its concrete subclass by the discriminator:

- the abstract base gets a tree-root accessor (it was previously skipped, so
  the bundle hard-failed validation — consequence (a) of #80);
- an instance under a base accessor is stored as its concrete subclass, so
  subclass-specific fields persist and it is queryable as its real type
  (previously silently downcast to the base — consequence (b)).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import mosaic
from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml


_SCHEMA = """\
id: https://example.org/poly
name: poly
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string
classes:
  Donor:
    is_a: Entity
    attributes:
      donor_id:
        range: string
  Sample:                       # abstract polymorphic base
    is_a: Entity
    abstract: true
    attributes:
      category:
        range: string
        designates_type: true
  SolidSample:
    is_a: Sample
    attributes:
      tissue:
        range: string
  LiquidSample:
    is_a: Sample
    attributes:
      volume_ml:
        range: string
  Assay:                        # concrete polymorphic base
    is_a: Entity
    attributes:
      category:
        range: string
        designates_type: true
  RNASeqAssay:
    is_a: Assay
    attributes:
      platform:
        range: string
      read_length:
        range: integer
"""


@pytest.fixture
def registry(tmp_path: Path):
    schema = tmp_path / "schema.yaml"
    schema.write_text(_SCHEMA)
    return mosaic.registry_for_schema(schema)


@pytest.fixture
def client(tmp_path: Path):
    schema = tmp_path / "schema.yaml"
    schema.write_text(_SCHEMA)
    return mosaic.client_for_schema(schema, database_url=str(tmp_path / "h.db"))


def _bundle(tmp_path: Path, data: dict, name: str = "bundle.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return p


# A schema whose base (`Widget`) has subclasses but declares NO designates_type
# discriminator — the silent-downcast hole the guard closes.
_NO_DESIGNATOR_SCHEMA = """\
id: https://example.org/nd
name: nd
prefixes: {linkml: https://w3id.org/linkml/}
imports: [linkml:types, mosaic_core]
default_range: string
classes:
  Widget:
    is_a: Entity
    attributes:
      widget_id: {range: string}
  FancyWidget:
    is_a: Widget
    attributes:
      sparkle: {range: string}
"""


# A schema exercising issue #93: single-valued references ranged on a
# polymorphic base — abstract (`Sample`, via `Measurement.sample`) and
# concrete (`Assay`, via `Sighting.assay`). Each reference points at a class
# whose subtype instances are dispatched into their own tables, so a
# base-table FK would fail for those referents.
_REF_SCHEMA = _SCHEMA + """\
  Measurement:                  # reference ranged on the abstract base `Sample`
    is_a: Entity
    attributes:
      sample:
        range: Sample
  Sighting:                     # reference ranged on the concrete base `Assay`
    is_a: Entity
    attributes:
      assay:
        range: Assay
"""


@pytest.fixture
def ref_client(tmp_path: Path):
    schema = tmp_path / "ref.yaml"
    schema.write_text(_REF_SCHEMA)
    return mosaic.client_for_schema(schema, database_url=str(tmp_path / "ref.db"))


@pytest.fixture
def ref_registry(tmp_path: Path):
    schema = tmp_path / "ref.yaml"
    schema.write_text(_REF_SCHEMA)
    return mosaic.registry_for_schema(schema)


class TestPolymorphicBaseReferences:
    """Issue #93: a reference ranged on a polymorphic base must resolve when
    the referent is a subtype instance (dispatched into its own table)."""

    def test_reference_to_abstract_base_subtype_resolves(
        self, tmp_path, ref_client, ref_registry
    ):
        bundle = _bundle(
            tmp_path,
            {
                "samples": [
                    {
                        "id": "S1",
                        "category": "SolidSample",
                        "tissue": "brain",
                        "is_available": True,
                    }
                ],
                "measurements": [
                    {"id": "M1", "sample": "S1", "is_available": True}
                ],
            },
        )
        result = ingest_linkml_yaml(bundle, ref_client, ref_registry)
        assert result.errors == 0, result.error_messages
        assert result.created == 2
        assert ref_client.get("Measurement", "M1")["data"]["sample"] == "S1"

    def test_reference_to_concrete_base_subtype_resolves(
        self, tmp_path, ref_client, ref_registry
    ):
        # This is the issue's exact shape: a concrete base (`Assay`) whose
        # subtype (`RNASeqAssay`) instance is dispatched to its own table.
        bundle = _bundle(
            tmp_path,
            {
                "assays": [
                    {
                        "id": "A1",
                        "category": "RNASeqAssay",
                        "platform": "Illumina",
                        "read_length": 150,
                        "is_available": True,
                    }
                ],
                "sightings": [
                    {"id": "SG1", "assay": "A1", "is_available": True}
                ],
            },
        )
        result = ingest_linkml_yaml(bundle, ref_client, ref_registry)
        assert result.errors == 0, result.error_messages
        assert result.created == 2
        assert ref_client.get("Sighting", "SG1")["data"]["assay"] == "A1"


@pytest.fixture
def nd_client(tmp_path: Path):
    schema = tmp_path / "nd.yaml"
    schema.write_text(_NO_DESIGNATOR_SCHEMA)
    return mosaic.client_for_schema(schema, database_url=str(tmp_path / "nd.db"))


@pytest.fixture
def nd_registry(tmp_path: Path):
    schema = tmp_path / "nd.yaml"
    schema.write_text(_NO_DESIGNATOR_SCHEMA)
    return mosaic.registry_for_schema(schema)


class TestTreeRootAccessors:
    def test_abstract_polymorphic_base_gets_accessor(self, registry):
        names = {s.name for s in registry.tree_root_slots()}
        # Abstract base `Sample` declares a designates_type slot → accessor.
        assert "samples" in names
        # Concrete subclasses still get their own accessors.
        assert {"solid_samples", "liquid_samples"} <= names

    def test_plain_abstract_root_excluded(self, registry):
        names = {s.name for s in registry.tree_root_slots()}
        # `Entity` (abstract, no designator) must NOT get an accessor.
        assert "entities" not in names and "entitys" not in names


class TestPolymorphicDispatch:
    def test_abstract_base_collection_ingests_and_dispatches(
        self, tmp_path, client, registry
    ):
        bundle = _bundle(
            tmp_path,
            {
                "samples": [
                    {
                        "id": "S1",
                        "category": "SolidSample",
                        "tissue": "brain",
                        "is_available": True,
                    },
                    {
                        "id": "S2",
                        "category": "LiquidSample",
                        "volume_ml": "5",
                        "is_available": True,
                    },
                ]
            },
        )

        result = ingest_linkml_yaml(bundle, client, registry)
        assert result.errors == 0 and result.created == 2

        assert [e["id"] for e in client.query("SolidSample").items] == ["S1"]
        assert [e["id"] for e in client.query("LiquidSample").items] == ["S2"]
        # Subclass-specific fields persisted.
        assert client.get("SolidSample", "S1")["data"]["tissue"] == "brain"
        assert client.get("LiquidSample", "S2")["data"]["volume_ml"] == "5"

    def test_concrete_base_accessor_no_longer_downcasts(
        self, tmp_path, client, registry
    ):
        # The issue's case (b): an RNASeqAssay under the base `assays:` key.
        bundle = _bundle(
            tmp_path,
            {
                "assays": [
                    {
                        "id": "A1",
                        "category": "RNASeqAssay",
                        "platform": "Illumina",
                        "read_length": 150,
                        "is_available": True,
                    }
                ]
            },
        )

        result = ingest_linkml_yaml(bundle, client, registry)
        assert result.errors == 0 and result.created == 1

        assert [e["id"] for e in client.query("RNASeqAssay").items] == ["A1"]
        got = client.get("RNASeqAssay", "A1")
        assert got["data"]["platform"] == "Illumina"
        assert got["data"]["read_length"] == 150

    def test_concrete_subclass_accessor_still_works(
        self, tmp_path, client, registry
    ):
        # The documented workaround (concrete accessor) must keep working.
        bundle = _bundle(
            tmp_path,
            {
                "solid_samples": [
                    {"id": "S9", "tissue": "liver", "is_available": True}
                ]
            },
        )
        result = ingest_linkml_yaml(bundle, client, registry)
        assert result.errors == 0 and result.created == 1
        assert client.get("SolidSample", "S9")["data"]["tissue"] == "liver"

    def test_unknown_discriminator_value_rejected(self, tmp_path, client, registry):
        # An out-of-family discriminator value is caught by up-front bundle
        # validation (the designator enum), so the whole ingest fails fast and
        # writes nothing — never reaching the per-instance dispatch guard.
        bundle = _bundle(
            tmp_path,
            {
                "samples": [
                    {"id": "S1", "category": "Nonexistent", "is_available": True}
                ]
            },
        )
        with pytest.raises(IngestError):
            ingest_linkml_yaml(bundle, client, registry)
        assert client.query("SolidSample").items == []

    def test_dispatch_guard_rejects_unknown_value_directly(self, registry):
        # The dispatch-level guard is the defense-in-depth fallback for when
        # validation is bypassed / schema drift: it refuses an unresolvable
        # discriminator value rather than mis-storing the instance.
        from mosaic.cli.commands.ingest import _dispatch_class

        with pytest.raises(IngestError, match="does not name"):
            _dispatch_class(registry, "Sample", {"id": "S1", "category": "Nope"})

    def test_abstract_base_without_designator_value_errors(
        self, tmp_path, client, registry
    ):
        # Validation may pass an instance missing the (optional) designator;
        # dispatch must refuse to instantiate the abstract base.
        bundle = _bundle(
            tmp_path,
            {"samples": [{"id": "S1", "is_available": True}]},
        )
        result = ingest_linkml_yaml(bundle, client, registry)
        assert result.errors == 1
        assert "abstract" in result.error_messages[0].lower()


class TestNoDesignatorDowncastGuard:
    """A base with subclasses but no designates_type must not silently drop
    subtype fields (issue #80 follow-up)."""

    def test_subtype_fields_under_base_accessor_are_refused(
        self, tmp_path, nd_client, nd_registry
    ):
        bundle = _bundle(
            tmp_path,
            {
                "widgets": [
                    {
                        "id": "W1",
                        "widget_id": "W1",
                        "sparkle": "high",  # FancyWidget-only field
                        "is_available": True,
                    }
                ]
            },
        )
        result = ingest_linkml_yaml(bundle, nd_client, nd_registry)
        # Refused, not silently downcast — nothing written.
        assert result.errors == 1
        msg = result.error_messages[0]
        assert "sparkle" in msg  # names the field that would be lost
        assert "designates_type" in msg  # tells the author the fix
        assert "FancyWidget" in msg  # shows the valid subclass
        assert "docs/polymorphic-ingest.md" in msg  # points at the guide
        assert nd_client.query("Widget").items == []
        assert nd_client.query("FancyWidget").items == []

    def test_plain_base_instance_still_ingests(
        self, tmp_path, nd_client, nd_registry
    ):
        # An instance with only base fields is a legitimate base entity and
        # must still ingest — the guard fires only on subtype-only fields.
        bundle = _bundle(
            tmp_path,
            {"widgets": [{"id": "W2", "widget_id": "W2", "is_available": True}]},
        )
        result = ingest_linkml_yaml(bundle, nd_client, nd_registry)
        assert result.errors == 0 and result.created == 1
        assert [e["id"] for e in nd_client.query("Widget").items] == ["W2"]
