"""Tests for ``RecipeService.diff`` (sec10 §10.2.3).

PR 10 of PTS-292. Verifies structural diff between two recipes:

- Identical recipes produce an empty diff.
- Classes/slots present only in ``b`` appear under ``classes_added`` /
  ``slots_added``.
- Classes/slots present only in ``a`` appear under ``classes_removed`` /
  ``slots_removed``.
- Classes/slots present in both but with different bodies appear under
  ``classes_changed`` / ``slots_changed``.
- ``a`` and ``b`` may be paths or ``file:`` URIs.
- Missing ``schema.yaml`` on either side is treated as "no elements".

``diff`` is read-only — no DB writes, no schema merge — so all
assertions check the returned :class:`RecipeDiff` shape.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from mosaic.core.recipe import RecipeDiff
from mosaic.core.recipe_service import RecipeService


def _write_recipe(
    root: Path,
    *,
    recipe_id: str,
    name: str,
    classes: dict | None = None,
    slots: dict | None = None,
    omit_schema: bool = False,
) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "recipe.yaml").write_text(
        dedent(
            f"""\
            id: {recipe_id}
            name: {name}
            version: 0.1.0
            created_at: '2026-05-27T00:00:00+00:00'
            mosaic_version: '>=0.0'
            """
        )
    )
    if omit_schema:
        return d
    parts = [
        f"id: https://example.org/{name}",
        f"name: {name}",
        f"default_prefix: {name}",
        "prefixes:",
        f"  {name}: https://example.org/{name}/",
        "default_range: string",
    ]
    if classes:
        parts.append("classes:")
        for cls_name, body in classes.items():
            parts.append(f"  {cls_name}:")
            attrs = body.get("attributes") or {}
            if attrs:
                parts.append("    attributes:")
                for attr_name, attr_body in attrs.items():
                    parts.append(f"      {attr_name}:")
                    for k, v in attr_body.items():
                        parts.append(f"        {k}: {v}")
            elif body:
                for k, v in body.items():
                    parts.append(f"    {k}: {v}")
    if slots:
        parts.append("slots:")
        for slot_name, body in slots.items():
            parts.append(f"  {slot_name}:")
            for k, v in body.items():
                parts.append(f"    {k}: {v}")
    (d / "schema.yaml").write_text("\n".join(parts) + "\n")
    return d


@pytest.fixture
def service():
    return RecipeService()


class TestDiffEmpty:
    def test_identical_recipes_produce_empty_diff(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        a = _write_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        diff = service.diff(a, b)
        assert diff == RecipeDiff()

    def test_returns_recipediff(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        a = _write_recipe(tmp_path, recipe_id="org.example.a", name="a")
        b = _write_recipe(tmp_path, recipe_id="org.example.b", name="b")
        diff = service.diff(a, b)
        assert isinstance(diff, RecipeDiff)


class TestDiffClasses:
    def test_added_class(self, service: RecipeService, tmp_path: Path) -> None:
        a = _write_recipe(tmp_path, recipe_id="org.example.a", name="a")
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        diff = service.diff(a, b)
        assert diff.classes_added == ("Sample",)
        assert diff.classes_removed == ()
        assert diff.classes_changed == ()

    def test_removed_class(self, service: RecipeService, tmp_path: Path) -> None:
        a = _write_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        b = _write_recipe(tmp_path, recipe_id="org.example.b", name="b")
        diff = service.diff(a, b)
        assert diff.classes_removed == ("Sample",)
        assert diff.classes_added == ()

    def test_changed_class(self, service: RecipeService, tmp_path: Path) -> None:
        a = _write_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={"Sample": {"attributes": {"label": {"range": "integer"}}}},
        )
        diff = service.diff(a, b)
        assert diff.classes_changed == ("Sample",)
        assert diff.classes_added == ()
        assert diff.classes_removed == ()


class TestDiffSlots:
    def test_added_slot(self, service: RecipeService, tmp_path: Path) -> None:
        a = _write_recipe(tmp_path, recipe_id="org.example.a", name="a")
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            slots={"label": {"range": "string"}},
        )
        diff = service.diff(a, b)
        assert diff.slots_added == ("label",)

    def test_removed_slot(self, service: RecipeService, tmp_path: Path) -> None:
        a = _write_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            slots={"label": {"range": "string"}},
        )
        b = _write_recipe(tmp_path, recipe_id="org.example.b", name="b")
        diff = service.diff(a, b)
        assert diff.slots_removed == ("label",)

    def test_changed_slot(self, service: RecipeService, tmp_path: Path) -> None:
        a = _write_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            slots={"label": {"range": "string"}},
        )
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            slots={"label": {"range": "integer"}},
        )
        diff = service.diff(a, b)
        assert diff.slots_changed == ("label",)


class TestDiffSorting:
    def test_added_classes_sorted_lexicographically(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        a = _write_recipe(tmp_path, recipe_id="org.example.a", name="a")
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={
                "Zeta": {"attributes": {"x": {"range": "string"}}},
                "Alpha": {"attributes": {"x": {"range": "string"}}},
                "Mu": {"attributes": {"x": {"range": "string"}}},
            },
        )
        diff = service.diff(a, b)
        assert diff.classes_added == ("Alpha", "Mu", "Zeta")


class TestDiffMissingSchema:
    def test_missing_schema_treated_as_empty(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        """Mirrors :meth:`inspect` semantics: no schema.yaml → no elements."""
        a = _write_recipe(
            tmp_path, recipe_id="org.example.a", name="a", omit_schema=True
        )
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        diff = service.diff(a, b)
        assert diff.classes_added == ("Sample",)
        assert diff.classes_removed == ()


class TestDiffFileUri:
    def test_file_uri_accepted(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        a = _write_recipe(tmp_path, recipe_id="org.example.a", name="a")
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        diff = service.diff(f"file://{a}", f"file://{b}")
        assert diff.classes_added == ("Sample",)

    def test_paths_and_uri_mixable(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        a = _write_recipe(tmp_path, recipe_id="org.example.a", name="a")
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={"Sample": {"attributes": {"label": {"range": "string"}}}},
        )
        diff = service.diff(a, f"file://{b}")
        assert diff.classes_added == ("Sample",)


class TestDiffCombined:
    def test_mixed_classes_and_slots(
        self, service: RecipeService, tmp_path: Path
    ) -> None:
        a = _write_recipe(
            tmp_path,
            recipe_id="org.example.a",
            name="a",
            classes={
                "Removed": {"attributes": {"x": {"range": "string"}}},
                "Common": {"attributes": {"x": {"range": "string"}}},
            },
            slots={
                "removed_slot": {"range": "string"},
                "common_slot": {"range": "string"},
            },
        )
        b = _write_recipe(
            tmp_path,
            recipe_id="org.example.b",
            name="b",
            classes={
                "Added": {"attributes": {"y": {"range": "string"}}},
                "Common": {"attributes": {"x": {"range": "integer"}}},
            },
            slots={
                "added_slot": {"range": "string"},
                "common_slot": {"range": "string"},
            },
        )
        diff = service.diff(a, b)
        assert diff.classes_added == ("Added",)
        assert diff.classes_removed == ("Removed",)
        assert diff.classes_changed == ("Common",)
        assert diff.slots_added == ("added_slot",)
        assert diff.slots_removed == ("removed_slot",)
        assert diff.slots_changed == ()
