"""Tests for mosaic.models.<namespace> direct-import surface (task 2.3).

Verifies that MosaicClient construction registers Pydantic classes under
synthetic sys.modules entries so callers can do:

    from mosaic.models import RootClass
    from mosaic.models.tissue import Sample
    from mosaic.models.assay.quant import Measurement
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reg(classes_yaml: str, schema_name: str = "t") -> SchemaRegistry:
    yaml_text = (
        f"id: https://example.org/{schema_name}\n"
        f"name: {schema_name}\n"
        "prefixes: {linkml: 'https://w3id.org/linkml/'}\n"
        "default_range: string\n"
        "imports:\n"
        "  - linkml:types\n"
        "  - mosaic_core\n"
        "classes:\n"
        f"{classes_yaml}"
    )
    return SchemaRegistry.from_yaml(yaml_text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_models_modules():
    """Strip all mosaic.models.* entries from sys.modules between tests.

    Without this, classes registered by one test bleed into the next and
    can mask failures (e.g., a test that should NOT see a class still finds
    one from a previous client construction).
    """
    to_remove = [k for k in sys.modules if k == "mosaic.models" or k.startswith("mosaic.models.")]
    for k in to_remove:
        sys.modules.pop(k, None)
    # Also clear any class attributes that survived on the existing module
    import mosaic.models as hm  # re-import to get the real module object

    yield

    to_remove = [k for k in sys.modules if k == "mosaic.models" or k.startswith("mosaic.models.")]
    for k in to_remove:
        sys.modules.pop(k, None)


@pytest.fixture
def client_factory():
    created: list[SQLiteAdapter] = []

    def _make(registry: SchemaRegistry) -> MosaicClient:
        tmpdir = tempfile.mkdtemp()
        storage = SQLiteAdapter(os.path.join(tmpdir, "models_test.db"), schema_registry=registry)
        created.append(storage)
        return MosaicClient(storage=storage, registry=registry, bypass_validation=True)

    yield _make

    for s in created:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Root-namespace classes
# ---------------------------------------------------------------------------


class TestRootNamespaceModels:
    def test_root_class_registered_on_hippo_models(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        import mosaic.models as hm

        assert hasattr(hm, "Sample"), "Root-namespace class must appear on mosaic.models"
        assert hm.Sample is not None

    def test_root_class_identity_matches_accessor(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        import mosaic.models as hm

        # The class on mosaic.models must be the same object as on the accessor.
        assert hm.Sample is client.samples.model_class

    def test_root_class_in_all(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client_factory(reg)

        import mosaic.models as hm

        assert "Sample" in hm.__all__

    def test_multiple_root_classes(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
            "  Protocol:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      title:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        import mosaic.models as hm

        assert hasattr(hm, "Sample")
        assert hasattr(hm, "Protocol")
        assert hm.Sample is client.samples.model_class
        assert hm.Protocol is client.protocols.model_class


# ---------------------------------------------------------------------------
# Non-root namespace
# ---------------------------------------------------------------------------


class TestNonRootNamespaceModels:
    def test_non_root_class_registered_on_submodule(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: tissue\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        assert "mosaic.models.tissue" in sys.modules
        tissue_mod = sys.modules["mosaic.models.tissue"]
        assert hasattr(tissue_mod, "Sample")
        assert tissue_mod.Sample is client.tissue.samples.model_class

    def test_non_root_class_not_on_root_module(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: tissue\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client_factory(reg)

        import mosaic.models as hm

        # tissue.Sample must NOT bleed onto the root mosaic.models module.
        assert not hasattr(hm, "Sample"), "Non-root class must not appear on mosaic.models"

    def test_attribute_access_on_parent_module(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: tissue\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client_factory(reg)

        import mosaic.models as hm

        # mosaic.models.tissue should be accessible as an attribute.
        assert hasattr(hm, "tissue")
        assert hm.tissue is sys.modules["mosaic.models.tissue"]


# ---------------------------------------------------------------------------
# Nested namespace (dot notation)
# ---------------------------------------------------------------------------


class TestNestedNamespaceModels:
    def test_nested_class_on_leaf_module(self, client_factory):
        reg = _reg(
            "  Measurement:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: assay.quant\n"
            "    attributes:\n"
            "      value:\n"
            "        range: float\n"
        )
        client = client_factory(reg)

        assert "mosaic.models.assay.quant" in sys.modules
        leaf = sys.modules["mosaic.models.assay.quant"]
        assert hasattr(leaf, "Measurement")
        assert leaf.Measurement is client.assay.quant.measurements.model_class

    def test_nested_intermediate_parent_registered(self, client_factory):
        """mosaic.models.assay must exist even when no class is in assay directly."""
        reg = _reg(
            "  Measurement:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: assay.quant\n"
            "    attributes:\n"
            "      value:\n"
            "        range: float\n"
        )
        client_factory(reg)

        assert "mosaic.models.assay" in sys.modules

    def test_nested_attribute_traversal(self, client_factory):
        """mosaic.models.assay.quant resolves via attribute access."""
        reg = _reg(
            "  Measurement:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: assay.quant\n"
            "    attributes:\n"
            "      value:\n"
            "        range: float\n"
        )
        client_factory(reg)

        import mosaic.models as hm

        assert hasattr(hm, "assay")
        assert hasattr(hm.assay, "quant")
        assert hasattr(hm.assay.quant, "Measurement")

    def test_nested_identity_matches_accessor(self, client_factory):
        reg = _reg(
            "  Measurement:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: assay.quant\n"
            "    attributes:\n"
            "      value:\n"
            "        range: float\n"
        )
        client = client_factory(reg)

        import mosaic.models as hm

        assert hm.assay.quant.Measurement is client.assay.quant.measurements.model_class


# ---------------------------------------------------------------------------
# Mixed root + non-root
# ---------------------------------------------------------------------------


class TestMixedNamespaces:
    def test_root_and_non_root_coexist(self, client_factory):
        reg = _reg(
            "  RootSample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
            "  TissueSample:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: tissue\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        import mosaic.models as hm

        assert hasattr(hm, "RootSample")
        assert not hasattr(hm, "TissueSample")
        assert hm.RootSample is client.root_samples.model_class

        tissue_mod = sys.modules["mosaic.models.tissue"]
        assert hasattr(tissue_mod, "TissueSample")
        assert tissue_mod.TissueSample is client.tissue.tissue_samples.model_class


# ---------------------------------------------------------------------------
# Idempotency — re-construction overwrites, doesn't crash
# ---------------------------------------------------------------------------


class TestReConstruction:
    def test_second_client_overwrites_first(self, client_factory):
        reg1 = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n",
            schema_name="s1",
        )
        reg2 = _reg(
            "  Protocol:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      title:\n"
            "        range: string\n",
            schema_name="s2",
        )
        client_factory(reg1)
        client2 = client_factory(reg2)

        import mosaic.models as hm

        # Second client's class must be present.
        assert hasattr(hm, "Protocol")
        assert hm.Protocol is client2.protocols.model_class
        # No crash on second populate().

    def test_idempotent_same_registry(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client_factory(reg)
        client_factory(reg)  # should not raise

        import mosaic.models as hm

        assert hasattr(hm, "Sample")
