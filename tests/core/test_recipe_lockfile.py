"""Tests for ``RecipeService.export_lockfile`` and ``install_from_lockfile`` (sec10 §10.6).

PR 11 of PTS-292. Verifies:

- ``export_lockfile`` dumps every ``installed_recipes`` entry into a
  ``recipe.lock.yaml`` document carrying ``lockfile_version: 1``.
- Digests in the lockfile are sha256-prefixed for portability.
- The parent field is preserved (or null) per entry.
- ``install_from_lockfile`` replays the document on a fresh instance,
  fetching each entry via its ``source`` and verifying digests.
- **Round-trip:** export, wipe instance, install-from-lockfile
  reproduces identical ``installed_recipes`` digests. This is the
  PHASE-4 acceptance test from the issue.
- A bad ``lockfile_version`` raises ``ValueError``.
- A lockfile with a digest mismatch fails on install.
- The same-version skip inside ``import_`` means repeated installs are
  idempotent (re-running ``install_from_lockfile`` is a no-op once
  installed).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import (
    RecipeDigestMismatchError,
)
from mosaic.core.meta import get_meta
from mosaic.core.recipe_service import (
    META_KEY_INSTALLED_RECIPES,
    RecipeService,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.conftest import _build_minimal_schema_registry


def _make_recipe(
    root: Path,
    *,
    recipe_id: str,
    name: str,
    parent: dict | None = None,
) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        f"id: {recipe_id}",
        f"name: {name}",
        "version: 0.1.0",
        "created_at: '2026-05-27T00:00:00+00:00'",
        "mosaic_version: '>=0.0'",
    ]
    if parent is not None:
        lines.append("parent:")
        for k, v in parent.items():
            if v is not None:
                lines.append(f"  {k}: {v}")
    (d / "recipe.yaml").write_text("\n".join(lines) + "\n")
    cls_pascal = name.capitalize()
    (d / "schema.yaml").write_text(
        dedent(
            f"""\
            id: https://example.org/{name}
            name: {name}
            default_prefix: {name}
            prefixes:
              {name}: https://example.org/{name}/
            default_range: string
            classes:
              {cls_pascal}:
                attributes:
                  label:
                    range: string
            """
        )
    )
    return d


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "lock.db")


@pytest.fixture
def storage(db_path):
    return SQLiteAdapter(
        db_path, schema_registry=_build_minimal_schema_registry()
    )


@pytest.fixture
def client(storage):
    return MosaicClient(
        storage=storage,
        registry=_build_minimal_schema_registry(),
        bypass_validation=True,
    )


def _fresh_client_at(db_path: str) -> MosaicClient:
    """Open a fresh MosaicClient over a brand-new SQLite DB at ``db_path``."""
    storage = SQLiteAdapter(
        db_path, schema_registry=_build_minimal_schema_registry()
    )
    return MosaicClient(
        storage=storage,
        registry=_build_minimal_schema_registry(),
        bypass_validation=True,
    )


class TestExportLockfileShape:
    def test_writes_yaml_with_lockfile_version_1(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        recipe = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe))

        out = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(out)
        data = yaml.safe_load(out.read_text())
        assert data["lockfile_version"] == 1
        assert "installed_recipes" in data
        assert "org.example.solo" in data["installed_recipes"]

    def test_empty_instance_writes_empty_entries(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        out = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(out)
        data = yaml.safe_load(out.read_text())
        assert data["lockfile_version"] == 1
        assert data["installed_recipes"] == {}

    def test_digest_is_sha256_prefixed(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        """Lockfile digests carry the ``sha256:`` prefix for portability."""
        recipe = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe))

        out = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(out)
        data = yaml.safe_load(out.read_text())
        entry = data["installed_recipes"]["org.example.solo"]
        assert entry["digest"].startswith("sha256:")
        assert len(entry["digest"]) == len("sha256:") + 64

    def test_parent_preserved(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        parent_recipe = _make_recipe(
            tmp_path, recipe_id="org.example.parent", name="parent"
        )
        # Extend produces a child recipe.yaml carrying parent lineage.
        child_dir = tmp_path / "child"
        client.recipe_import(str(parent_recipe))
        client.recipe_extend("org.example.parent", child_dir)
        # Author edits stubs so it can be imported.
        child_manifest = yaml.safe_load((child_dir / "recipe.yaml").read_text())
        child_manifest["id"] = "org.example.child"
        child_manifest["name"] = "child"
        child_manifest["version"] = "0.1.0"
        (child_dir / "recipe.yaml").write_text(yaml.safe_dump(child_manifest))
        (child_dir / "schema.yaml").write_text(
            dedent(
                """\
                id: https://example.org/child
                name: child
                default_prefix: child
                prefixes:
                  child: https://example.org/child/
                default_range: string
                classes:
                  ChildClass:
                    attributes:
                      label:
                        range: string
                """
            )
        )
        client.recipe_import(str(child_dir))

        out = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(out)
        data = yaml.safe_load(out.read_text())
        child_entry = data["installed_recipes"]["org.example.child"]
        assert child_entry["parent"] is not None
        assert child_entry["parent"]["id"] == "org.example.parent"
        parent_entry = data["installed_recipes"]["org.example.parent"]
        assert parent_entry["parent"] is None


class TestInstallFromLockfileVersionGate:
    def test_rejects_missing_version(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.safe_dump({"installed_recipes": {}}))
        with pytest.raises(ValueError, match="lockfile_version"):
            client.recipe_install_from_lockfile(bad)

    def test_rejects_unknown_version(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.safe_dump({"lockfile_version": 99, "installed_recipes": {}})
        )
        with pytest.raises(ValueError, match="Unsupported lockfile_version"):
            client.recipe_install_from_lockfile(bad)

    def test_rejects_non_mapping_top_level(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- one\n- two\n")
        with pytest.raises(ValueError, match="mapping at the top level"):
            client.recipe_install_from_lockfile(bad)


class TestRoundTrip:
    """The PHASE-4 acceptance test from PTS-292:

    `mosaic recipe import` (a real recipe) + add a local extension class +
    `mosaic recipe export-lockfile` + wipe instance +
    `mosaic recipe install-from-lockfile` reproduces digests.
    """

    def test_roundtrip_single_recipe(
        self, client: MosaicClient, storage: SQLiteAdapter, tmp_path: Path
    ) -> None:
        recipe = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe))

        with storage._transaction() as conn:
            before = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}

        lockfile = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(lockfile)

        # Fresh instance, replay the lockfile.
        fresh_db = tmp_path / "fresh.db"
        fresh = _fresh_client_at(str(fresh_db))
        fresh.recipe_install_from_lockfile(lockfile)

        fresh_storage = fresh._storage
        with fresh_storage._transaction() as conn:
            after = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}

        assert set(before) == set(after)
        for rid in before:
            assert before[rid]["digest"] == after[rid]["digest"]
            assert before[rid]["version"] == after[rid]["version"]

    def test_roundtrip_parent_child(
        self, client: MosaicClient, storage: SQLiteAdapter, tmp_path: Path
    ) -> None:
        """Parent + extended child round-trip preserves both digests."""
        parent_recipe = _make_recipe(
            tmp_path, recipe_id="org.example.parent", name="parent"
        )
        client.recipe_import(str(parent_recipe))

        child_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", child_dir)
        child_manifest = yaml.safe_load((child_dir / "recipe.yaml").read_text())
        child_manifest["id"] = "org.example.child"
        child_manifest["name"] = "child"
        child_manifest["version"] = "0.1.0"
        (child_dir / "recipe.yaml").write_text(yaml.safe_dump(child_manifest))
        (child_dir / "schema.yaml").write_text(
            dedent(
                """\
                id: https://example.org/child
                name: child
                default_prefix: child
                prefixes:
                  child: https://example.org/child/
                default_range: string
                classes:
                  ChildClass:
                    attributes:
                      extra:
                        range: string
                """
            )
        )
        client.recipe_import(str(child_dir))

        with storage._transaction() as conn:
            before = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}

        lockfile = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(lockfile)

        fresh_db = tmp_path / "fresh.db"
        fresh = _fresh_client_at(str(fresh_db))
        fresh.recipe_install_from_lockfile(lockfile)

        with fresh._storage._transaction() as conn:
            after = get_meta(conn, META_KEY_INSTALLED_RECIPES) or {}

        assert set(before) == set(after)
        for rid in before:
            assert before[rid]["digest"] == after[rid]["digest"]


class TestInstallOrderParentFirst:
    def test_parent_installs_before_child(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        """Topological sort by parent: parents land first regardless of
        lockfile key order."""
        parent_recipe = _make_recipe(
            tmp_path, recipe_id="org.example.parent", name="parent"
        )
        client.recipe_import(str(parent_recipe))

        child_dir = tmp_path / "child"
        client.recipe_extend("org.example.parent", child_dir)
        child_manifest = yaml.safe_load((child_dir / "recipe.yaml").read_text())
        child_manifest["id"] = "org.example.child"
        child_manifest["name"] = "child"
        child_manifest["version"] = "0.1.0"
        (child_dir / "recipe.yaml").write_text(yaml.safe_dump(child_manifest))
        (child_dir / "schema.yaml").write_text(
            dedent(
                """\
                id: https://example.org/child
                name: child
                default_prefix: child
                prefixes:
                  child: https://example.org/child/
                default_range: string
                """
            )
        )
        client.recipe_import(str(child_dir))

        lockfile = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(lockfile)

        # Replay on a fresh instance.
        fresh_db = tmp_path / "fresh.db"
        fresh = _fresh_client_at(str(fresh_db))
        results = fresh.recipe_install_from_lockfile(lockfile)

        first_top_level = results[0].installed[-1].id
        assert first_top_level == "org.example.parent"


class TestInstallFromLockfileDigestVerification:
    def test_digest_mismatch_raises(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        recipe = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe))

        lockfile = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(lockfile)

        # Corrupt the lockfile's digest so install verification fails.
        data = yaml.safe_load(lockfile.read_text())
        data["installed_recipes"]["org.example.solo"]["digest"] = (
            "sha256:" + "0" * 64
        )
        lockfile.write_text(yaml.safe_dump(data))

        fresh_db = tmp_path / "fresh.db"
        fresh = _fresh_client_at(str(fresh_db))
        with pytest.raises(RecipeDigestMismatchError):
            fresh.recipe_install_from_lockfile(lockfile)


class TestInstallFromLockfileIdempotence:
    def test_replay_on_existing_instance_is_noop(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        recipe = _make_recipe(
            tmp_path, recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe))

        lockfile = tmp_path / "recipe.lock.yaml"
        client.recipe_export_lockfile(lockfile)

        # Second install on the same instance — same-version skip kicks in.
        before = client.recipe_list()
        client.recipe_install_from_lockfile(lockfile)
        after = client.recipe_list()
        assert {r.id for r in before} == {r.id for r in after}
        for b, a in zip(
            sorted(before, key=lambda r: r.id),
            sorted(after, key=lambda r: r.id),
        ):
            # Digests survive replay.
            assert b.digest == a.digest


class TestRelativeSourceResolvesAgainstLockfile:
    """Sec10 §10.3.3: relative ``source`` paths in a lockfile resolve
    against the lockfile's directory."""

    def test_relative_source_path(
        self, client: MosaicClient, tmp_path: Path
    ) -> None:
        recipe = _make_recipe(
            tmp_path / "lockdir", recipe_id="org.example.solo", name="solo"
        )
        client.recipe_import(str(recipe))

        lockfile = tmp_path / "lockdir" / "recipe.lock.yaml"
        client.recipe_export_lockfile(lockfile)

        # Rewrite the source to be relative to the lockfile.
        data = yaml.safe_load(lockfile.read_text())
        data["installed_recipes"]["org.example.solo"]["source"] = "solo"
        lockfile.write_text(yaml.safe_dump(data))

        fresh_db = tmp_path / "fresh.db"
        fresh = _fresh_client_at(str(fresh_db))
        results = fresh.recipe_install_from_lockfile(lockfile)
        assert results[0].installed[-1].id == "org.example.solo"
