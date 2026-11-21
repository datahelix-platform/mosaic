"""Tests for the reference-loader schema-fragment merge engine (sec2 §2.14.5/6).

Covers decision D2.14.G (mandatory prefix, ``imports:`` policy, ``provided_by``
injection) and D2.14.H (soft ``loader_depends_on`` warning).
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.exceptions import ConfigError
from mosaic.linkml_bridge import (
    LOADER_DEPENDS_ON_ANNOTATION,
    PROVIDED_BY_ANNOTATION,
    LoaderFragmentSpec,
    SchemaRegistry,
    merge_loader_fragment,
    merge_loader_fragments,
)


FIXTURE = Path(__file__).parent.parent / "fixtures" / "schemas" / "sample_schema.yaml"


@pytest.fixture
def deployed_registry() -> SchemaRegistry:
    return SchemaRegistry.from_path(FIXTURE)


@pytest.fixture
def deployed_sv(deployed_registry: SchemaRegistry) -> SchemaView:
    return deployed_registry.schema_view


def _fragment_with(**overrides) -> dict:
    """Build a minimal, schema-valid loader fragment, mergeable on top of
    ``sample_schema``. Caller overrides fields per test."""
    base = {
        "id": "https://example.org/mosaic/foo",
        "name": "foo",
        "default_prefix": "foo",
        "prefixes": {"foo": "https://example.org/mosaic/foo/"},
        "classes": {
            "FooThing": {
                "attributes": {
                    "label": {"range": "string", "required": True},
                },
            },
        },
    }
    base.update(overrides)
    return base


class TestRule1Prefix:
    """Rule 1: mandatory per-loader prefix."""

    def test_missing_default_prefix_raises_config_error(self, deployed_sv: SchemaView):
        fragment = _fragment_with()
        fragment.pop("default_prefix")
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.0.0", fragment)
        with pytest.raises(ConfigError, match=r"missing `default_prefix:`"):
            merge_loader_fragment(deployed_sv, spec)

    def test_prefix_must_equal_loader_name(self, deployed_sv: SchemaView):
        fragment = _fragment_with(default_prefix="bar")
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.0.0", fragment)
        with pytest.raises(ConfigError, match=r"requires it to equal the loader name"):
            merge_loader_fragment(deployed_sv, spec)

    def test_two_loaders_same_prefix_raises_naming_both_packages(
        self, deployed_sv: SchemaView
    ):
        spec_a = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo-a", "1.0.0", _fragment_with()
        )
        spec_b = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo-b", "2.0.0", _fragment_with()
        )
        with pytest.raises(ConfigError) as exc_info:
            merge_loader_fragments(deployed_sv, [spec_a, spec_b])
        msg = str(exc_info.value)
        assert "mosaic-reference-foo-a" in msg
        assert "mosaic-reference-foo-b" in msg

    def test_unique_prefixes_succeed(self, deployed_sv: SchemaView):
        spec_a = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo", "1.0.0", _fragment_with()
        )
        bar_fragment = {
            "id": "https://example.org/mosaic/bar",
            "name": "bar",
            "default_prefix": "bar",
            "prefixes": {"bar": "https://example.org/mosaic/bar/"},
            "classes": {"BarThing": {"attributes": {"label": {"range": "string"}}}},
        }
        spec_b = LoaderFragmentSpec("bar", "mosaic-reference-bar", "1.0.0", bar_fragment)
        merged = merge_loader_fragments(deployed_sv, [spec_a, spec_b])
        names = set(merged.all_classes().keys())
        assert {"FooThing", "BarThing"}.issubset(names)


class TestRule2Imports:
    """Rule 2: ``imports:`` policy."""

    def test_strips_linkml_types(self, deployed_sv: SchemaView):
        fragment = _fragment_with(imports=["linkml:types"])
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.0.0", fragment)
        merged = merge_loader_fragment(deployed_sv, spec)
        # Deployed schema already imports linkml:types; the fragment's redundant
        # import was stripped before merge, so the count does not grow.
        assert merged.schema.imports.count("linkml:types") == 1

    def test_strips_curie_imports_with_known_prefix(self, deployed_sv: SchemaView):
        # `mosaic_core` is exposed via the deployed importmap, so its prefix is
        # implicitly known. Adding a mosaic_core CURIE-form import in the
        # fragment is redundant and must be stripped.
        fragment = _fragment_with(imports=["linkml:types", "linkml:other"])
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.0.0", fragment)
        merged = merge_loader_fragment(deployed_sv, spec)
        # Both linkml-prefixed imports stripped; their prefix is already
        # in the deployed schema.
        assert "linkml:other" not in (merged.schema.imports or [])

    def test_private_imports_pass_through(self, deployed_sv: SchemaView):
        private_url = "https://example.org/mosaic/foo/private_types"
        fragment = _fragment_with(imports=["linkml:types", private_url])
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.0.0", fragment)
        merged = merge_loader_fragment(deployed_sv, spec)
        assert private_url in (merged.schema.imports or [])


class TestRule3ProvidedBy:
    """Rule 3: ``provided_by`` annotation injection survives SchemaView roundtrip."""

    def test_provided_by_on_class(self, deployed_sv: SchemaView):
        fragment = _fragment_with()
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.2.3", fragment)
        merged = merge_loader_fragment(deployed_sv, spec)
        cls = merged.get_class("FooThing")
        assert cls is not None
        ann = cls.annotations[PROVIDED_BY_ANNOTATION]
        assert ann.value == "foo@1.2.3"

    def test_provided_by_on_attribute_slot(self, deployed_sv: SchemaView):
        fragment = _fragment_with()
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.2.3", fragment)
        merged = merge_loader_fragment(deployed_sv, spec)
        # Attribute-induced slot must carry the annotation after roundtrip.
        induced = {s.name: s for s in merged.class_induced_slots("FooThing")}
        assert "label" in induced
        ann = induced["label"].annotations[PROVIDED_BY_ANNOTATION]
        assert ann.value == "foo@1.2.3"

    def test_provided_by_on_top_level_slot(self, deployed_sv: SchemaView):
        fragment = _fragment_with()
        fragment["slots"] = {"foo_shared": {"range": "string"}}
        spec = LoaderFragmentSpec("foo", "mosaic-reference-foo", "1.2.3", fragment)
        merged = merge_loader_fragment(deployed_sv, spec)
        slot = merged.get_slot("foo_shared")
        assert slot is not None
        ann = slot.annotations[PROVIDED_BY_ANNOTATION]
        assert ann.value == "foo@1.2.3"


class TestRule4LoaderDependsOn:
    """Rule 4: ``loader_depends_on`` soft warning."""

    def _fragment_with_dep(self, dep: str) -> dict:
        f = _fragment_with()
        f["annotations"] = {LOADER_DEPENDS_ON_ANNOTATION: {"value": dep}}
        return f

    def test_warning_when_dep_missing(self, deployed_sv: SchemaView):
        spec = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo", "1.0.0", self._fragment_with_dep("bar")
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            merge_loader_fragment(
                deployed_sv, spec, installed_loader_names=set()
            )
        messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert any("foo" in m and "bar" in m for m in messages), messages

    def test_silent_when_dep_installed(self, deployed_sv: SchemaView):
        spec = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo", "1.0.0", self._fragment_with_dep("bar")
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            merge_loader_fragment(
                deployed_sv, spec, installed_loader_names={"bar"}
            )
        messages = [
            str(w.message)
            for w in caught
            if issubclass(w.category, UserWarning) and "loader_depends_on" in str(w.message)
        ]
        assert messages == []

    def test_comma_separated_deps_each_checked(self, deployed_sv: SchemaView):
        spec = LoaderFragmentSpec(
            "foo",
            "mosaic-reference-foo",
            "1.0.0",
            self._fragment_with_dep("bar, baz"),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            merge_loader_fragment(
                deployed_sv, spec, installed_loader_names={"bar"}
            )
        missing = [
            str(w.message)
            for w in caught
            if issubclass(w.category, UserWarning)
        ]
        assert any("baz" in m for m in missing)
        assert not any("loader_depends_on: 'bar'" in m for m in missing)

    def test_does_not_block_install(self, deployed_sv: SchemaView):
        # Even with a missing dep the merge still produces a valid SchemaView
        # carrying the new class.
        spec = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo", "1.0.0", self._fragment_with_dep("nope")
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            merged = merge_loader_fragment(
                deployed_sv, spec, installed_loader_names=set()
            )
        assert merged.get_class("FooThing") is not None


class TestSchemaRegistryConvenience:
    def test_with_loader_fragments_returns_new_registry(
        self, deployed_registry: SchemaRegistry
    ):
        spec = LoaderFragmentSpec(
            "foo", "mosaic-reference-foo", "1.0.0", _fragment_with()
        )
        new_reg = deployed_registry.with_loader_fragments([spec])
        assert new_reg is not deployed_registry
        assert new_reg.has_class("FooThing")
        # Original registry untouched.
        assert not deployed_registry.has_class("FooThing")


class TestFakeReferenceLoaderIntegration:
    """Acceptance criterion: a loader instance built on PTS-224's ABC merges cleanly."""

    def test_fake_reference_loader_fragment_merges(
        self, deployed_registry: SchemaRegistry
    ):
        from importlib.metadata import version

        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        loader = FakeReferenceLoader()
        pkg_version = version("datahelix-mosaic")
        spec = LoaderFragmentSpec(
            loader_name=loader.name,
            package_name="mosaic",
            package_version=pkg_version,
            fragment=loader.schema_fragment(),
        )
        merged = deployed_registry.with_loader_fragments([spec])
        assert merged.has_class("FakeTerm")
        cls = merged.get_class("FakeTerm")
        assert cls is not None
        ann = cls.annotations[PROVIDED_BY_ANNOTATION]
        assert ann.value == f"fake@{pkg_version}"
