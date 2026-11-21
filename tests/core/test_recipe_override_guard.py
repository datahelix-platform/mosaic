"""Tests for ``SchemaManager.check_no_inplace_override`` (sec10 §10.7.2 / invariant 6).

PR 4 of PTS-290. The check is the merge seam Phase 3 will call before
``SchemaManager.merge_fragment`` — landing it now makes the invariant
enforceable as soon as the install path is in place.

Scenarios:

- A recipe redefining a class whose ``provided_by`` names another recipe
  is rejected.
- A recipe redefining a class whose ``provided_by`` names a loader is
  rejected.
- A recipe adding ``is_a: upstream:Class`` (a NEW subclass) is accepted.
- Hand-authored content (no ``provided_by``) can be redefined.
- A recipe re-merging its OWN attribution is allowed when
  ``importing_provided_by`` is passed.
"""

from __future__ import annotations

import pytest
import yaml
from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.exceptions import RecipeSchemaError
from mosaic.core.schema_manager import SchemaManager
from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap


def _build_registry(extra_classes: dict, extra_slots: dict | None = None) -> SchemaRegistry:
    """Build a SchemaRegistry whose deployed view contains the given
    classes (and optional top-level slots), each carrying its declared
    ``provided_by`` annotation. Imports ``mosaic_core`` so ``is_a: Entity``
    keeps working.
    """
    doc = {
        "id": "https://example.org/mosaic/test/override_guard",
        "name": "override_guard_test",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "upstream": "https://example.org/upstream/",
        },
        "imports": ["linkml:types", "mosaic_core"],
        "default_range": "string",
        "classes": extra_classes,
    }
    if extra_slots:
        doc["slots"] = extra_slots
    sv = SchemaView(yaml.safe_dump(doc), importmap=_bundled_importmap())
    return SchemaRegistry(sv)


def _provided_by(value: str) -> dict:
    """Annotation block stamping a class or slot with ``provided_by``."""
    return {"provided_by": {"tag": "provided_by", "value": value}}


@pytest.fixture
def deployed_with_recipe_class() -> SchemaRegistry:
    """Deployed schema where ``UpstreamThing`` is provided by another recipe."""
    return _build_registry(
        extra_classes={
            "UpstreamThing": {
                "is_a": "Entity",
                "annotations": _provided_by("recipe.org.example.upstream@1.0"),
                "attributes": {
                    "label": {"range": "string", "required": True},
                },
            },
        }
    )


@pytest.fixture
def deployed_with_loader_class() -> SchemaRegistry:
    """Deployed schema where ``LoaderThing`` is provided by a Reference Loader."""
    return _build_registry(
        extra_classes={
            "LoaderThing": {
                "is_a": "Entity",
                "annotations": _provided_by("loader.mosaic-reference-ensembl@115"),
                "attributes": {
                    "gene": {"range": "string", "required": True},
                },
            },
        }
    )


@pytest.fixture
def deployed_with_hand_authored() -> SchemaRegistry:
    """Deployed schema where ``Local`` carries no ``provided_by`` (hand-authored)."""
    return _build_registry(
        extra_classes={
            "Local": {
                "is_a": "Entity",
                "attributes": {
                    "label": {"range": "string", "required": True},
                },
            },
        }
    )


class TestRejection:
    def test_redefines_recipe_provided_class(
        self, deployed_with_recipe_class: SchemaRegistry
    ) -> None:
        sm = SchemaManager(registry=deployed_with_recipe_class)
        fragment = {
            "classes": {
                "UpstreamThing": {
                    "attributes": {"label": {"range": "integer"}},
                }
            }
        }
        with pytest.raises(RecipeSchemaError) as excinfo:
            sm.check_no_inplace_override(fragment)
        assert excinfo.value.element_name == "UpstreamThing"
        assert excinfo.value.element_kind == "class"
        assert "recipe.org.example.upstream@1.0" in str(excinfo.value)
        assert "is_a" in str(excinfo.value)  # error hints at the workaround

    def test_redefines_loader_provided_class(
        self, deployed_with_loader_class: SchemaRegistry
    ) -> None:
        sm = SchemaManager(registry=deployed_with_loader_class)
        fragment = {
            "classes": {
                "LoaderThing": {
                    "attributes": {"gene": {"range": "integer"}},
                }
            }
        }
        with pytest.raises(RecipeSchemaError) as excinfo:
            sm.check_no_inplace_override(fragment)
        assert excinfo.value.provided_by == "loader.mosaic-reference-ensembl@115"

    def test_redefines_recipe_provided_slot(self) -> None:
        registry = _build_registry(
            extra_classes={},
            extra_slots={
                "upstream_label": {
                    "range": "string",
                    "annotations": _provided_by("recipe.org.example.upstream@1.0"),
                },
            },
        )
        sm = SchemaManager(registry=registry)
        fragment = {"slots": {"upstream_label": {"range": "integer"}}}
        with pytest.raises(RecipeSchemaError) as excinfo:
            sm.check_no_inplace_override(fragment)
        assert excinfo.value.element_name == "upstream_label"
        assert excinfo.value.element_kind == "slot"


class TestAcceptance:
    def test_subclass_via_is_a_is_accepted(
        self, deployed_with_recipe_class: SchemaRegistry
    ) -> None:
        sm = SchemaManager(registry=deployed_with_recipe_class)
        # NEW class, subclassing the upstream — no redefinition.
        fragment = {
            "classes": {
                "MyThing": {
                    "is_a": "UpstreamThing",
                    "attributes": {"note": {"range": "string"}},
                }
            }
        }
        sm.check_no_inplace_override(fragment)  # no error

    def test_brand_new_class_is_accepted(
        self, deployed_with_recipe_class: SchemaRegistry
    ) -> None:
        sm = SchemaManager(registry=deployed_with_recipe_class)
        fragment = {
            "classes": {
                "BrandNew": {
                    "is_a": "Entity",
                    "attributes": {"label": {"range": "string", "required": True}},
                }
            }
        }
        sm.check_no_inplace_override(fragment)  # no error

    def test_hand_authored_redefinition_is_accepted(
        self, deployed_with_hand_authored: SchemaRegistry
    ) -> None:
        sm = SchemaManager(registry=deployed_with_hand_authored)
        # The deployed class has no provided_by annotation, so a recipe
        # is free to extend or redefine it.
        fragment = {
            "classes": {
                "Local": {
                    "attributes": {"label": {"range": "integer"}},
                }
            }
        }
        sm.check_no_inplace_override(fragment)  # no error

    def test_self_redefinition_allowed_when_importing_attribution_matches(
        self, deployed_with_recipe_class: SchemaRegistry
    ) -> None:
        # Re-merging the exact same recipe identity is not an override —
        # the higher-level idempotency / prefix-collision check decides
        # whether to skip the merge entirely. The seam itself must not
        # spuriously reject.
        sm = SchemaManager(registry=deployed_with_recipe_class)
        fragment = {
            "classes": {
                "UpstreamThing": {
                    "attributes": {"label": {"range": "string"}},
                }
            }
        }
        sm.check_no_inplace_override(
            fragment,
            importing_provided_by="recipe.org.example.upstream@1.0",
        )


class TestEdgeCases:
    def test_empty_fragment_no_error(
        self, deployed_with_recipe_class: SchemaRegistry
    ) -> None:
        sm = SchemaManager(registry=deployed_with_recipe_class)
        sm.check_no_inplace_override({})

    def test_no_registry_is_a_noop(self) -> None:
        sm = SchemaManager(registry=None)
        sm.check_no_inplace_override({"classes": {"Foo": {}}})

    def test_different_recipe_attribution_still_rejected(
        self, deployed_with_recipe_class: SchemaRegistry
    ) -> None:
        # Importing recipe is a different one than the upstream; the
        # guard must still fire.
        sm = SchemaManager(registry=deployed_with_recipe_class)
        fragment = {"classes": {"UpstreamThing": {"attributes": {}}}}
        with pytest.raises(RecipeSchemaError):
            sm.check_no_inplace_override(
                fragment,
                importing_provided_by="recipe.org.example.OTHER@1.0",
            )
