"""Tests for the sec9 §9.8 typed-client surface."""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import ValidationFailed
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry
from mosaic.core.typed_client import (
    EntityAccessor,
    Namespace,
    SDK_RESERVED_NAMES,
    TypedClientError,
    build_typed_surface,
    default_accessor,
)
from mosaic.core.validation.validators import ValidationResult, WriteOperation, WriteValidator
from mosaic.linkml_bridge import SchemaRegistry


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _reg(
    classes_yaml: str,
    schema_name: str = "t",
) -> SchemaRegistry:
    """Build a SchemaRegistry from a fragment of `classes:` YAML."""
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


@pytest.fixture
def client_factory():
    """Produce a MosaicClient with a fresh SQLite backing store."""
    created: list[SQLiteAdapter] = []

    def _make(registry: SchemaRegistry) -> MosaicClient:
        tmpdir = tempfile.mkdtemp()
        storage = SQLiteAdapter(os.path.join(tmpdir, "typed.db"), schema_registry=registry)
        created.append(storage)
        return MosaicClient(
            storage=storage, registry=registry, bypass_validation=True
        )

    yield _make

    for s in created:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Default accessor derivation
# ---------------------------------------------------------------------------


class TestDefaultAccessor:
    def test_single_word(self):
        assert default_accessor("Sample") == "samples"

    def test_camel_case(self):
        assert default_accessor("TissueType") == "tissue_types"

    def test_acronym_boundary(self):
        assert default_accessor("DNASample") == "dna_samples"

    def test_mixed_acronym_suffix(self):
        assert default_accessor("CellLineQC") == "cell_line_qcs"


# ---------------------------------------------------------------------------
# Root-namespace access — flat and via `root`
# ---------------------------------------------------------------------------


class TestRootAccess:
    def test_root_class_has_flat_accessor(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        assert hasattr(client, "samples")
        assert isinstance(client.samples, EntityAccessor)
        assert client.samples.class_name == "Sample"

    def test_root_alias_mirrors_flat(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        assert hasattr(client, "root")
        assert hasattr(client.root, "samples")
        # Same accessor object
        assert client.root.samples is client.samples

    def test_root_accessor_writes_through_to_generic_client(
        self, client_factory
    ):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        result = client.samples.create({"name": "via-typed"})
        assert result["entity_type"] == "Sample"
        assert result["data"]["name"] == "via-typed"

        # Round-trip read via typed accessor
        got = client.samples.get(result["id"])
        assert got["id"] == result["id"]


# ---------------------------------------------------------------------------
# Non-root namespaces, nested dot notation
# ---------------------------------------------------------------------------


class TestNonRootNamespaces:
    def test_single_level_namespace(self, client_factory):
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

        # `tissue` exists as a container; `tissue.samples` is the accessor.
        assert hasattr(client, "tissue")
        assert isinstance(client.tissue, Namespace)
        assert hasattr(client.tissue, "samples")
        assert client.tissue.samples.class_name == "Sample"
        # Non-root classes must NOT be flat on the client — `client.samples`
        # does not exist as a typed accessor (a plain attribute lookup
        # should return nothing or raise).
        flat = getattr(client, "samples", None)
        assert not isinstance(flat, EntityAccessor)

    def test_nested_namespace_via_dot_notation(self, client_factory):
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

        assert hasattr(client, "assay")
        assert hasattr(client.assay, "quant")
        assert hasattr(client.assay.quant, "measurements")
        assert client.assay.quant.measurements.class_name == "Measurement"

    def test_empty_parent_container_is_legal(self, client_factory):
        """Per sec9 §9.8, a namespace `assay.quant` causes `assay` to
        materialize as a container even if no classes are in `assay`.
        """
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

        # Only `quant` is under `assay`; no direct accessors.
        assert list(client.assay.accessors()) == []
        assert "quant" in client.assay.subnamespaces()


# ---------------------------------------------------------------------------
# mosaic_accessor override
# ---------------------------------------------------------------------------


class TestAccessorOverride:
    def test_mosaic_accessor_overrides_default(self, client_factory):
        reg = _reg(
            "  Analysis:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_accessor: analytics\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        # Default would be `analysises`; override gives `analytics`.
        assert hasattr(client, "analytics")
        assert not hasattr(client, "analyses")
        assert client.analytics.class_name == "Analysis"


# ---------------------------------------------------------------------------
# Collision detection — all four cases
# ---------------------------------------------------------------------------


class TestCollisionDetection:
    def test_case1_same_namespace_duplicate_accessor(self, client_factory):
        # Two classes resolving to `dna_samples`. Since PR 3.1 introduced
        # the flat tree-root wire format, collisions on the (flat) slot
        # name are caught at SchemaRegistry construction with a
        # `SchemaError` — which fires before the typed-client surface
        # would have raised its own `duplicate_accessor` error. The
        # disambiguation message still names `mosaic_accessor`.
        from mosaic.core.exceptions import SchemaError

        with pytest.raises(SchemaError) as exc:
            _reg(
                "  DNASample:\n"
                "    is_a: Entity\n"
                "    annotations:\n"
                "      mosaic_namespace: tissue\n"
                "    attributes:\n"
                "      name: {range: string}\n"
                "  DnaSample:\n"
                "    is_a: Entity\n"
                "    annotations:\n"
                "      mosaic_namespace: tissue\n"
                "    attributes:\n"
                "      label: {range: string}\n"
            )
        assert "dna_samples" in str(exc.value)
        assert "mosaic_accessor" in str(exc.value)

    def test_case2_accessor_vs_subnamespace(self, client_factory):
        # Class `Protocol` in namespace `tissue` → accessor `protocols`.
        # Class `Step` in `tissue.protocols` → sub-namespace `protocols` at `tissue` level.
        # Both land at `client.tissue.protocols` — collision.
        reg = _reg(
            "  Protocol:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: tissue\n"
            "    attributes:\n"
            "      name: {range: string}\n"
            "  Step:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: tissue.protocols\n"
            "    attributes:\n"
            "      order: {range: integer}\n"
        )
        with pytest.raises(TypedClientError) as exc:
            client_factory(reg)
        assert exc.value.case == "accessor_vs_namespace"

    def test_case3_namespace_vs_sdk_reserved(self, client_factory):
        reg = _reg(
            "  Thing:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: query\n"  # collides with MosaicClient.query
            "    attributes:\n"
            "      name: {range: string}\n"
        )
        with pytest.raises(TypedClientError) as exc:
            client_factory(reg)
        assert exc.value.case == "namespace_reserved"

    def test_case3_namespace_root_reserved(self, client_factory):
        reg = _reg(
            "  Thing:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_namespace: root\n"
            "    attributes:\n"
            "      name: {range: string}\n"
        )
        with pytest.raises(TypedClientError) as exc:
            client_factory(reg)
        assert exc.value.case == "reserved_root"

    def test_case4_accessor_vs_sdk_reserved(self, client_factory):
        reg = _reg(
            "  Schema:\n"
            "    is_a: Entity\n"
            "    annotations:\n"
            "      mosaic_accessor: storage\n"  # conflicts with MosaicClient.storage
            "    attributes:\n"
            "      name: {range: string}\n"
        )
        with pytest.raises(TypedClientError) as exc:
            client_factory(reg)
        assert exc.value.case == "accessor_reserved"


# ---------------------------------------------------------------------------
# mosaic_core infrastructure classes are NOT exposed
# ---------------------------------------------------------------------------


class TestInfrastructureExcluded:
    def test_provenance_record_not_exposed(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name: {range: string}\n"
        )
        client = client_factory(reg)

        # ProvenanceRecord is mosaic_core infrastructure; no typed accessor.
        assert not hasattr(client, "provenance_records")

    def test_process_not_exposed(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name: {range: string}\n"
        )
        client = client_factory(reg)

        assert not hasattr(client, "processes")


# ---------------------------------------------------------------------------
# Pydantic model generation (best-effort)
# ---------------------------------------------------------------------------


class TestPydanticGeneration:
    def test_model_class_attached(self, client_factory):
        """Pydantic generation is a compulsory contract (Decision 9.8.H
        revised): every non-abstract domain class MUST have an
        attached Pydantic model. Schemas the generator can't handle
        raise at load."""
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        accessor = client.samples
        assert accessor.model_class is not None
        assert accessor.model_class.__name__ == "Sample"

    def test_create_accepts_pydantic_instance(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        accessor = client.samples
        assert accessor.model_class is not None
        instance = accessor.model_class(id="x1", name="via-pydantic")
        result = accessor.create(instance)
        assert result["data"]["name"] == "via-pydantic"


# ---------------------------------------------------------------------------
# Generic <-> typed parity
# ---------------------------------------------------------------------------


class TestGenericTypedParity:
    def test_generic_create_visible_via_typed_get(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        # Generic path
        generic = client.put("Sample", {"name": "via-generic"})

        # Typed read returns the same entity
        typed_read = client.samples.get(generic["id"])
        assert typed_read["id"] == generic["id"]

    def test_typed_create_visible_via_generic_get(self, client_factory):
        reg = _reg(
            "  Sample:\n"
            "    is_a: Entity\n"
            "    attributes:\n"
            "      name:\n"
            "        range: string\n"
        )
        client = client_factory(reg)

        typed = client.samples.create({"name": "via-typed"})

        # Generic read returns the same entity
        generic_read = client.get("Sample", typed["id"])
        assert generic_read["id"] == typed["id"]


# ---------------------------------------------------------------------------
# No-registry path: MosaicClient still works without typed accessors
# ---------------------------------------------------------------------------


class TestNoRegistry:
    def test_client_without_registry_has_no_typed_accessors(self):
        storage = SQLiteAdapter(":memory:", schema_registry=_build_minimal_schema_registry())
        try:
            client = MosaicClient(storage=storage, bypass_validation=True)
            # No registry → no typed surface built
            assert client._typed_root is None
            assert not hasattr(client, "samples")
            assert not hasattr(client, "root")
        finally:
            storage.close()


class TestReservedNamesGuard:
    """CI guard against drift: `SDK_RESERVED_NAMES` must cover every
    public attribute of ``MosaicClient``. If Mosaic gains a new public
    method/property, this test fails until the reserved set is
    updated. Prevents silent shadowing of MosaicClient attributes by
    user-schema class accessors."""

    def test_reserved_set_covers_every_hippoclient_public_attribute(self):
        public_attrs = {
            name for name in dir(MosaicClient) if not name.startswith("_")
        }
        missing = public_attrs - SDK_RESERVED_NAMES
        assert not missing, (
            f"MosaicClient has public attributes not covered by "
            f"SDK_RESERVED_NAMES: {sorted(missing)}. A user schema "
            f"could declare a class whose accessor shadows one of "
            f"these. Add them to SDK_RESERVED_NAMES in "
            f"src/mosaic/core/typed_client.py."
        )


# ---------------------------------------------------------------------------
# ValidationFailed raise-on-write (sec9 §9.9 / Decision 9.9.E, task 6.1)
# ---------------------------------------------------------------------------


class _AlwaysFailValidator(WriteValidator):
    """Deterministic failing validator for typed-client tests."""

    def validate(self, operation: WriteOperation) -> ValidationResult:
        return ValidationResult(is_valid=False, errors=["always fails"])


@pytest.fixture
def validating_client_factory():
    """MosaicClient with validation enabled (bypass_validation=False)."""
    created: list[SQLiteAdapter] = []

    def _make(registry: SchemaRegistry) -> MosaicClient:
        tmpdir = tempfile.mkdtemp()
        storage = SQLiteAdapter(os.path.join(tmpdir, "typed_val.db"), schema_registry=registry)
        created.append(storage)
        return MosaicClient(storage=storage, registry=registry)

    yield _make

    for s in created:
        try:
            s.close()
        except Exception:
            pass


def _sample_reg() -> SchemaRegistry:
    return _reg(
        "  Sample:\n"
        "    is_a: Entity\n"
        "    attributes:\n"
        "      name:\n"
        "        range: string\n"
    )


class TestValidationFailedOnWrite:
    """sec9 §9.9 task 6.1 — EntityAccessor write methods raise ValidationFailed."""

    def test_create_raises_validation_failed(self, validating_client_factory):
        client = validating_client_factory(_sample_reg())
        client.add_validator(_AlwaysFailValidator())

        with pytest.raises(ValidationFailed) as exc_info:
            client.samples.create({"name": "x"})

        exc = exc_info.value
        assert exc.entity_type == "Sample"
        assert exc.entity_id is None

    def test_put_no_id_raises_validation_failed(self, validating_client_factory):
        client = validating_client_factory(_sample_reg())
        client.add_validator(_AlwaysFailValidator())

        with pytest.raises(ValidationFailed) as exc_info:
            client.samples.put({"name": "x"})

        exc = exc_info.value
        assert exc.entity_type == "Sample"
        assert exc.entity_id is None

    def test_put_with_id_raises_validation_failed(self, validating_client_factory):
        client = validating_client_factory(_sample_reg())
        client.add_validator(_AlwaysFailValidator())

        with pytest.raises(ValidationFailed) as exc_info:
            client.samples.put({"name": "x"}, entity_id="eid-1")

        exc = exc_info.value
        assert exc.entity_id == "eid-1"

    def test_replace_raises_validation_failed(self, validating_client_factory):
        client = validating_client_factory(_sample_reg())
        client.add_validator(_AlwaysFailValidator())

        with pytest.raises(ValidationFailed) as exc_info:
            client.samples.replace("eid-1", {"name": "x"})

        exc = exc_info.value
        assert exc.entity_type == "Sample"
        assert exc.entity_id == "eid-1"

    def test_validation_failed_carries_result_envelope(self, validating_client_factory):
        client = validating_client_factory(_sample_reg())
        client.add_validator(_AlwaysFailValidator())

        with pytest.raises(ValidationFailed) as exc_info:
            client.samples.create({"name": "x"})

        result = exc_info.value.result
        assert result is not None
        assert not result.is_valid
        envelope = result.to_envelope()
        assert not envelope["passed"]
        assert len(envelope["failures"]) > 0
        assert any("always fails" in f["message"] for f in envelope["failures"])

    def test_empty_data_raises_validation_failed(self, validating_client_factory):
        client = validating_client_factory(_sample_reg())

        with pytest.raises(ValidationFailed) as exc_info:
            client.samples.create({})

        assert exc_info.value.entity_type == "Sample"

    def test_valid_data_passes_through_when_no_validators(
        self, validating_client_factory
    ):
        client = validating_client_factory(_sample_reg())
        result = client.samples.create({"name": "good"})
        assert result["id"] is not None
