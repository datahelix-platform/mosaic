"""Sanity tests for the recipe dataclass surface (sec10 §10.3).

PR 2 of PTS-290 — these are pure-data types. Tests assert constructability,
defaults, immutability, and that the public ``mosaic.core.recipe`` namespace
re-exports each symbol.
"""

from __future__ import annotations

import dataclasses

import pytest

from mosaic.core.recipe import (
    ImportPlan,
    ImportResult,
    InstalledRecipe,
    RecipeAuthor,
    RecipeDiff,
    RecipeManifest,
    RecipeRef,
    RecipeReport,
    RecipeRequires,
)


def _ref() -> RecipeRef:
    return RecipeRef(
        id="org.example.foo",
        version="0.1.0",
        source="file:./foo",
        digest="sha256:" + "a" * 64,
    )


def _manifest() -> RecipeManifest:
    return RecipeManifest(
        id="org.example.foo",
        name="foo",
        version="0.1.0",
        created_at="2026-05-27T00:00:00+00:00",
        mosaic_version=">=0.3",
    )


class TestDataclassBasics:
    @pytest.mark.parametrize(
        "cls",
        [
            RecipeAuthor,
            RecipeRef,
            RecipeRequires,
            RecipeManifest,
            InstalledRecipe,
            RecipeReport,
            RecipeDiff,
            ImportPlan,
            ImportResult,
        ],
    )
    def test_is_frozen_dataclass(self, cls: type) -> None:
        assert dataclasses.is_dataclass(cls)
        params = getattr(cls, "__dataclass_params__")
        assert params.frozen is True, f"{cls.__name__} must be frozen"


class TestRecipeRef:
    def test_required_fields_only(self) -> None:
        ref = RecipeRef(id="x", version="1", source="file:./x")
        assert ref.digest is None

    def test_immutable(self) -> None:
        ref = _ref()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ref.version = "9.9.9"  # type: ignore[misc]


class TestRecipeRequires:
    def test_defaults_empty(self) -> None:
        req = RecipeRequires()
        assert req.recipes == ()
        assert req.reference_loaders == ()

    def test_populated(self) -> None:
        req = RecipeRequires(
            recipes=(_ref(),),
            reference_loaders=("mosaic-reference-ensembl==115",),
        )
        assert len(req.recipes) == 1
        assert req.reference_loaders == ("mosaic-reference-ensembl==115",)


class TestRecipeManifest:
    def test_minimal_required(self) -> None:
        m = _manifest()
        assert m.author is None
        assert m.parent is None
        assert isinstance(m.requires, RecipeRequires)
        assert m.requires.recipes == ()

    def test_with_author_and_parent(self) -> None:
        m = RecipeManifest(
            id="org.example.foo",
            name="foo",
            version="0.1.0",
            created_at="2026-05-27T00:00:00+00:00",
            mosaic_version=">=0.3",
            author=RecipeAuthor(name="Jane", email="j@x"),
            parent=_ref(),
            requires=RecipeRequires(reference_loaders=("mosaic-reference-ensembl==115",)),
        )
        assert m.author.name == "Jane"
        assert m.parent.id == "org.example.foo"


class TestInstalledRecipe:
    def test_construct(self) -> None:
        rec = InstalledRecipe(
            id="org.example.foo",
            version="0.1.0",
            source="https://example.org/foo.tar.gz",
            digest="sha256:" + "b" * 64,
            installed_at="2026-05-27T00:00:00+00:00",
        )
        assert rec.parent is None


class TestRecipeReport:
    def test_construct(self) -> None:
        r = RecipeReport(
            manifest=_manifest(),
            digest="sha256:" + "c" * 64,
            classes=("Foo",),
            slots=("bar",),
        )
        assert r.classes == ("Foo",)


class TestRecipeDiff:
    def test_all_empty_by_default(self) -> None:
        d = RecipeDiff()
        assert d.classes_added == ()
        assert d.classes_removed == ()
        assert d.classes_changed == ()
        assert d.slots_added == ()
        assert d.slots_removed == ()
        assert d.slots_changed == ()


class TestImportPlanAndResult:
    def test_plan_default_order_empty(self) -> None:
        plan = ImportPlan(manifest=_manifest())
        assert plan.order == ()

    def test_result_defaults(self) -> None:
        res = ImportResult()
        assert res.installed == ()
        assert res.classes_added == ()
        assert res.slots_added == ()
        assert res.dry_run is False
