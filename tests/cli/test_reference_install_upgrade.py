"""End-to-end tests for the reference loader install/upgrade lifecycle.

Exercises every acceptance criterion from PTS-229 and the v2 substrate
swap from PTS-256:

- ``install --version v1`` writes ``mosaic_meta[reference_versions]``
  AND populates ``reference_write_log`` for every ``client.put()`` made
  inside the ``load_context`` block (sec2 §2.14.9, D2.14.J).
- Upgrade additive: both versions queryable, mosaic_meta reflects target.
- Upgrade ``--prune-old`` success: prior rows removed (hard delete) and
  matching ``reference_write_log`` rows deleted in the same transaction.
- Upgrade ``--prune-old`` failure mid-load: prior rows intact.
- ``--prune-old`` works when the loader returns an empty
  ``LoadResult.entities`` list (write log is the authoritative substrate).
- ``--prune-old`` removes the entity row on stable-id upgrade overlap
  (documented v2 constraint, sec2 §2.14.9).
- ``--version test`` install works against the fake loader's bundled
  fixture with no network.
- Re-installing the same version is a clear, idempotent no-op.

All cases drive the public ``install_reference`` / ``upgrade_reference``
helpers against the real SQLite adapter and ``FakeReferenceLoader``.
The Typer surface is exercised in one smoke test via ``CliRunner``.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mosaic.cli.commands import reference as ref_cmd
from mosaic.cli.commands.reference import (
    META_KEY_VERSIONS,
    _resolve_breakdown_counts,
    install_reference,
    list_reference_loaders,
    render_breakdown,
    upgrade_reference,
)
from mosaic.cli.main import app
from mosaic.core.loaders.reference import EntityRef
from mosaic.core.meta import get_meta
from mosaic.testing.example_ontology_loader import OboDemoLoader
from mosaic.testing.fake_reference_loader import FakeLoadParams


@pytest.fixture
def hippo_workspace(tmp_path: Path) -> dict[str, Path]:
    """Return paths to an empty Mosaic workspace (db + schemas dir).

    The schemas dir is intentionally left empty so the install lifecycle
    falls back to the bundled ``mosaic_core`` schema as the base. The
    fake loader's fragment is then merged on top.
    """
    db_path = tmp_path / "hippo.db"
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    return {"db": db_path, "schemas": schema_dir, "root": tmp_path}


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _count_rows(db_path: Path, table: str) -> int:
    """Count *available* rows. Mosaic uses soft-delete (sec3) for the
    standard user/REST path — every ``client.delete()`` flips
    ``is_available=0`` instead of removing the row. ``--prune-old``
    bypasses that path and hard-deletes from the entity tables, so a
    post-prune count is also the total row count for the loader.
    """
    conn = _open(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f'SELECT COUNT(*) AS n FROM "{table}" WHERE is_available = 1'
        )
        return cursor.fetchone()["n"]
    finally:
        conn.close()


def _count_all_rows(db_path: Path, table: str) -> int:
    """Count every row, including soft-deleted. Used to assert that
    ``--prune-old`` actually drops rows from disk (D2.14.J hard delete).
    """
    conn = _open(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(f'SELECT COUNT(*) AS n FROM "{table}"')
        return cursor.fetchone()["n"]
    finally:
        conn.close()


def _select_write_log(
    db_path: Path, loader: str, version: str
) -> list[tuple[str, str]]:
    """Return ``(entity_id, entity_type)`` rows from the write log."""
    conn = _open(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT entity_id, entity_type FROM reference_write_log "
            "WHERE loader_name = ? AND version = ? "
            "ORDER BY entity_id",
            (loader, version),
        )
        return [(row["entity_id"], row["entity_type"]) for row in cursor.fetchall()]
    finally:
        conn.close()


class TestInstall:
    def test_install_records_version_in_mosaic_meta(self, hippo_workspace):
        result = install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )

        assert result["status"] == "installed"
        assert result["version"] == "v1"
        assert result["created"] == 3
        # v2 LoadResult shape: result["entities"] is the advisory list of
        # EntityRef handles populated by the loader.
        assert len(result["entities"]) == 3
        assert all(isinstance(e, EntityRef) for e in result["entities"])
        assert all(e.type == "FakeTerm" for e in result["entities"])

        conn = _open(hippo_workspace["db"])
        try:
            versions = get_meta(conn, META_KEY_VERSIONS)
        finally:
            conn.close()
        assert versions == {"fake": "v1"}
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 3

        # Every put() inside ``load_context`` recorded a write-log row.
        log = _select_write_log(hippo_workspace["db"], "fake", "v1")
        assert len(log) == 3
        assert {entity_type for _, entity_type in log} == {"FakeTerm"}
        assert {entity_id for entity_id, _ in log} == {
            e.id for e in result["entities"]
        }

    def test_reinstall_same_version_is_noop(self, hippo_workspace):
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        again = install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        assert again["status"] == "already_installed"
        # No duplicate rows were written on the second invocation.
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 3

    def test_install_test_slug_works_without_network(self, hippo_workspace):
        # No HTTP traffic is mocked here — the fake loader is in-memory.
        # If the test_slug code path ever introduces a network call, this
        # test will hang or fail outright.
        result = install_reference(
            "fake",
            "test",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        assert result["version"] == "test"
        assert result["created"] == 2
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 2

    def test_install_unknown_loader_raises(self, hippo_workspace):
        with pytest.raises(KeyError):
            install_reference(
                "no-such-loader",
                "v1",
                db_path=hippo_workspace["db"],
                schema_dir=hippo_workspace["schemas"],
            )

    def test_install_default_version_skips_reserved_test_slug(
        self, hippo_workspace
    ):
        # Confirm D2.14.I — internal default-version resolution never
        # picks "test" even when the loader exposes it.
        result = install_reference(
            "fake",
            None,
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        assert result["version"] != "test"


class TestUpgradeAdditive:
    def test_upgrade_keeps_old_rows_and_adds_new(self, hippo_workspace):
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        before = _count_rows(hippo_workspace["db"], "FakeTerm")
        assert before == 3

        upgrade = upgrade_reference(
            "fake",
            "v2",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        assert upgrade["status"] == "upgraded"
        assert upgrade["from_version"] == "v1"
        assert upgrade["to_version"] == "v2"
        assert upgrade["created"] == 4
        assert upgrade["pruned"] == []

        # v1 (3) + v2 (4) ingested side-by-side per D2.14.F.
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == before + 4

        conn = _open(hippo_workspace["db"])
        try:
            versions = get_meta(conn, META_KEY_VERSIONS)
        finally:
            conn.close()
        assert versions == {"fake": "v2"}

        # Both versions appear in the write log (additive — no prune).
        assert len(_select_write_log(hippo_workspace["db"], "fake", "v1")) == 3
        assert len(_select_write_log(hippo_workspace["db"], "fake", "v2")) == 4


class TestUpgradePruneOld:
    def test_prune_old_success_removes_prior_rows(self, hippo_workspace):
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 3

        upgrade = upgrade_reference(
            "fake",
            "v2",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            prune_old=True,
        )
        assert upgrade["status"] == "upgraded"
        assert len(upgrade["pruned"]) == 3
        assert all(isinstance(p, EntityRef) for p in upgrade["pruned"])
        assert all(p.type == "FakeTerm" for p in upgrade["pruned"])
        # Hard delete — only v2 rows survive in the entity table.
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 4
        assert _count_all_rows(hippo_workspace["db"], "FakeTerm") == 4

        # The matching write-log rows for the pruned version went with
        # the entity rows; the new version's log rows are intact.
        assert _select_write_log(hippo_workspace["db"], "fake", "v1") == []
        assert len(_select_write_log(hippo_workspace["db"], "fake", "v2")) == 4

    def test_prune_old_uses_write_log_when_entities_empty(
        self, hippo_workspace
    ):
        # Simulates a large-scale loader that leaves
        # ``LoadResult.entities`` empty and relies on the write log
        # (sec2 §2.14.8 advisory contract).
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            params=FakeLoadParams(omit_entity_refs=True),
        )
        # Loader returned an empty advisory list, but writes still
        # happened — assert the substrate is decoupled.
        installed_log = _select_write_log(hippo_workspace["db"], "fake", "v1")
        assert len(installed_log) == 3

        upgrade = upgrade_reference(
            "fake",
            "v2",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            prune_old=True,
            params=FakeLoadParams(omit_entity_refs=True),
        )
        assert upgrade["entities"] == []
        # Prune still removed all 3 v1 rows — the write log was the
        # source of truth, not ``LoadResult.entities``.
        assert len(upgrade["pruned"]) == 3
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 4
        assert _select_write_log(hippo_workspace["db"], "fake", "v1") == []

    def test_prune_old_removes_overlapping_stable_ids(self, hippo_workspace):
        # Stable-id overlap (sec2 §2.14.9): v1 and v2 share IDs for
        # "alpha"/"beta"/"gamma". The default ``upgrade()`` re-writes
        # those rows under v2 so the entity table holds 4 rows
        # (alpha/beta/gamma updated + delta new). Prune of v1 then
        # removes the overlapping entity rows even though v2 still
        # references the same IDs — documented v2 behavior; loaders
        # that need overlap survival must override ``upgrade()``.
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            params=FakeLoadParams(stable_ids=True),
        )
        assert _count_rows(hippo_workspace["db"], "FakeTerm") == 3
        v1_log = _select_write_log(hippo_workspace["db"], "fake", "v1")
        v1_ids = {entity_id for entity_id, _ in v1_log}
        assert v1_ids == {"fake-alpha", "fake-beta", "fake-gamma"}

        upgrade = upgrade_reference(
            "fake",
            "v2",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            prune_old=True,
            params=FakeLoadParams(stable_ids=True),
        )
        assert upgrade["status"] == "upgraded"
        assert len(upgrade["pruned"]) == 3

        # All three overlapping entity rows are gone — the v2 "delta"
        # row is the only thing left.
        assert _count_all_rows(hippo_workspace["db"], "FakeTerm") == 1
        conn = _open(hippo_workspace["db"])
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM "FakeTerm"')
            remaining = {row["id"] for row in cursor.fetchall()}
        finally:
            conn.close()
        assert remaining == {"fake-delta"}

        # v1 log rows are gone; v2 still has its 4 rows (3 overlapping
        # ids + delta).
        assert _select_write_log(hippo_workspace["db"], "fake", "v1") == []
        v2_log = _select_write_log(hippo_workspace["db"], "fake", "v2")
        assert len(v2_log) == 4

    def test_prune_old_failure_mid_load_keeps_prior_rows(self, hippo_workspace):
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        baseline = _count_rows(hippo_workspace["db"], "FakeTerm")
        assert baseline == 3

        # Force loader.load() (called via .upgrade default) to raise
        # after persisting 2 rows of the v2 dataset.
        with pytest.raises(RuntimeError, match="simulated failure"):
            upgrade_reference(
                "fake",
                "v2",
                db_path=hippo_workspace["db"],
                schema_dir=hippo_workspace["schemas"],
                prune_old=True,
                params=FakeLoadParams(fail_after=2),
            )

        # The prior v1 rows MUST still be intact — prune runs only after
        # a clean LoadResult. The partial v2 writes are not rolled back
        # (the loader contract isn't transactional). The test asserts on
        # the v1 invariant, which is the load-bearing one here.
        conn = _open(hippo_workspace["db"])
        try:
            versions = get_meta(conn, META_KEY_VERSIONS) or {}
        finally:
            conn.close()
        # mosaic_meta still points at v1 — upgrade was aborted before
        # the version pointer rotated.
        assert versions == {"fake": "v1"}

        # The write log still has all 3 v1 rows (the prune path never
        # ran). The 2 committed v2 puts also appear — that mirrors the
        # "log rows correspond exactly to committed entity writes"
        # invariant from sec2 §2.14.9.
        v1_log = _select_write_log(hippo_workspace["db"], "fake", "v1")
        assert len(v1_log) == 3
        v2_log = _select_write_log(hippo_workspace["db"], "fake", "v2")
        assert len(v2_log) == 2

        # All three prior IDs still queryable in FakeTerm with
        # is_available=1.
        prior_ids = [entity_id for entity_id, _ in v1_log]
        conn = _open(hippo_workspace["db"])
        try:
            cursor = conn.cursor()
            placeholders = ",".join("?" * len(prior_ids))
            cursor.execute(
                f'SELECT id, is_available FROM "FakeTerm" '
                f'WHERE id IN ({placeholders})',
                prior_ids,
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        ids_still_there = {row["id"] for row in rows}
        assert ids_still_there == set(prior_ids)
        assert all(row["is_available"] == 1 for row in rows)


class TestUpgradeGuards:
    def test_upgrade_without_prior_install_errors(self, hippo_workspace):
        with pytest.raises(ValueError, match="not installed"):
            upgrade_reference(
                "fake",
                "v2",
                db_path=hippo_workspace["db"],
                schema_dir=hippo_workspace["schemas"],
            )

    def test_upgrade_to_same_version_is_noop(self, hippo_workspace):
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        result = upgrade_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        assert result["status"] == "already_at_version"


class TestListing:
    def test_list_reports_installed_version(self, hippo_workspace):
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
        )
        loaders = list_reference_loaders(db_path=hippo_workspace["db"])
        fake = next(loader for loader in loaders if loader["name"] == "fake")
        assert fake["installed"] is True
        assert fake["installed_version"] == "v1"

    def test_list_reports_not_installed(self, hippo_workspace):
        loaders = list_reference_loaders(db_path=hippo_workspace["db"])
        fake = next(loader for loader in loaders if loader["name"] == "fake")
        assert fake["installed"] is False
        assert fake["installed_version"] is None


class TestCliSmoke:
    """Drive one happy path through the Typer surface for coverage."""

    def test_install_then_upgrade_via_typer(self, hippo_workspace):
        runner = CliRunner()
        db = str(hippo_workspace["db"])
        sd = str(hippo_workspace["schemas"])

        install = runner.invoke(
            app,
            ["reference", "install", "fake", "--version", "v1",
             "--db-path", db, "--schema-dir", sd],
        )
        assert install.exit_code == 0, install.output
        assert "Installed fake@v1" in install.output

        upgrade = runner.invoke(
            app,
            ["reference", "upgrade", "fake", "--version", "v2",
             "--db-path", db, "--schema-dir", sd, "--prune-old"],
        )
        assert upgrade.exit_code == 0, upgrade.output
        assert "Upgraded fake v1 → v2" in upgrade.output
        assert "3 prior row(s) pruned" in upgrade.output

        ls = runner.invoke(app, ["reference", "list", "--db-path", db])
        assert ls.exit_code == 0, ls.output
        assert "fake" in ls.output
        assert "v2" in ls.output


# ---------------------------------------------------------------------------
# Per-entity_type breakdown — sec2 §2.14.8 advisory contract / D2.14.K.
# ---------------------------------------------------------------------------


class TestBreakdownRendering:
    """Format-level checks for the rendering function itself."""

    def test_render_orders_by_count_desc_then_type_asc(self):
        # Mixed counts including a tie ensure deterministic ordering:
        # higher counts first, alphabetical to break ties.
        counts = Counter({"Gene": 78_334, "GeneVersion": 78_334, "GeneOrtholog": 77_909})
        out = render_breakdown("Installed ensembl@v1", counts, total=234_577)
        lines = out.splitlines()
        assert lines[0] == "Installed ensembl@v1:"
        # Tie-break by name: Gene before GeneVersion (both 78,334), then GeneOrtholog.
        assert "Gene" in lines[1] and "78,334" in lines[1]
        assert "GeneVersion" in lines[2] and "78,334" in lines[2]
        assert "GeneOrtholog" in lines[3] and "77,909" in lines[3]
        assert lines[-1].strip().startswith("total")
        assert "234,577" in lines[-1]

    def test_render_emits_total_when_counts_empty(self):
        # Empty breakdown still shows a total line (e.g. zero-write install).
        out = render_breakdown("Installed fake@v1", Counter(), total=0)
        assert out.splitlines() == ["Installed fake@v1:", "  total  0"]

    def test_render_preserves_load_result_created_total(self):
        # The "total" line is sourced from the scalar, not summed from
        # the breakdown — advisory drift must not change the bottom line.
        counts = Counter({"FakeTerm": 2})
        out = render_breakdown("Installed fake@v1", counts, total=999)
        assert "999" in out.splitlines()[-1]


class TestBreakdownSourceResolution:
    """``_resolve_breakdown_counts`` picks the right source."""

    def test_entities_path_uses_counter_when_populated(self, hippo_workspace):
        # No db query needed — a populated advisory list short-circuits.
        entities = [
            EntityRef(id="a", type="FakeTerm"),
            EntityRef(id="b", type="FakeTerm"),
            EntityRef(id="c", type="Other"),
        ]
        counts = _resolve_breakdown_counts(
            entities, hippo_workspace["db"], "fake", "v1"
        )
        assert counts == Counter({"FakeTerm": 2, "Other": 1})

    def test_write_log_path_used_when_entities_empty(self, hippo_workspace):
        # Run a real install so the write log gets populated, then ask
        # the resolver to read from it (empty advisory list).
        install_reference(
            "fake",
            "v1",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            params=FakeLoadParams(omit_entity_refs=True),
        )
        counts = _resolve_breakdown_counts(
            [], hippo_workspace["db"], "fake", "v1"
        )
        assert counts == Counter({"FakeTerm": 3})

    def test_missing_db_yields_empty_counter(self, tmp_path):
        # The CLI may render before a db is created (loader produced no
        # writes). The fallback degrades to an empty Counter rather than
        # crashing on a missing file.
        counts = _resolve_breakdown_counts(
            [], tmp_path / "does-not-exist.db", "fake", "v1"
        )
        assert counts == Counter()


class TestCliBreakdownOutput:
    """End-to-end CLI assertions on the rendered breakdown."""

    def test_install_renders_breakdown_from_entities(self, hippo_workspace):
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "reference", "install", "fake", "--version", "v1",
                "--db-path", str(hippo_workspace["db"]),
                "--schema-dir", str(hippo_workspace["schemas"]),
            ],
        )
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert lines[0] == "Installed fake@v1:"
        assert any("FakeTerm" in line and "3" in line for line in lines[1:])
        assert lines[-1].strip().startswith("total")
        assert "3" in lines[-1]

    def test_install_renders_breakdown_from_write_log(self, hippo_workspace):
        # Same loader, same version, but the advisory list comes back
        # empty — the write log MUST supply the same breakdown.
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "reference", "install", "fake", "--version", "v1",
                "--db-path", str(hippo_workspace["db"]),
                "--schema-dir", str(hippo_workspace["schemas"]),
                "--omit-entity-refs",
            ],
        )
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert lines[0] == "Installed fake@v1:"
        assert any("FakeTerm" in line and "3" in line for line in lines[1:])
        assert lines[-1].strip().startswith("total")
        assert "3" in lines[-1]

    def test_install_both_source_paths_produce_identical_output(
        self, tmp_path
    ):
        # Acceptance criterion: "Both paths render the same totals /
        # breakdown structure". Run the same install twice into two
        # isolated workspaces — once via advisory entities, once via the
        # write log — and assert byte-identical multi-line output.
        runner = CliRunner()

        def _install(workspace_root: Path, *extra: str) -> str:
            db = workspace_root / "hippo.db"
            schemas = workspace_root / "schemas"
            schemas.mkdir(parents=True)
            result = runner.invoke(
                app,
                [
                    "reference", "install", "fake", "--version", "v1",
                    "--db-path", str(db), "--schema-dir", str(schemas),
                    *extra,
                ],
            )
            assert result.exit_code == 0, result.output
            return result.output

        entities_output = _install(tmp_path / "ents")
        write_log_output = _install(tmp_path / "wlog", "--omit-entity-refs")
        assert entities_output == write_log_output

    def test_upgrade_renders_breakdown_and_pruned_tail(self, hippo_workspace):
        # Install v1 then upgrade --prune-old to v2. The new printer
        # MUST keep the trailing "N prior row(s) pruned." line so the
        # operator still sees prune impact alongside the breakdown.
        runner = CliRunner()
        install = runner.invoke(
            app,
            [
                "reference", "install", "fake", "--version", "v1",
                "--db-path", str(hippo_workspace["db"]),
                "--schema-dir", str(hippo_workspace["schemas"]),
            ],
        )
        assert install.exit_code == 0, install.output

        upgrade = runner.invoke(
            app,
            [
                "reference", "upgrade", "fake", "--version", "v2",
                "--db-path", str(hippo_workspace["db"]),
                "--schema-dir", str(hippo_workspace["schemas"]),
                "--prune-old",
            ],
        )
        assert upgrade.exit_code == 0, upgrade.output
        lines = upgrade.output.splitlines()
        assert lines[0] == "Upgraded fake v1 → v2:"
        assert any("FakeTerm" in line and "4" in line for line in lines)
        total_line = next(line for line in lines if line.lstrip().startswith("total"))
        assert "4" in total_line
        assert "3 prior row(s) pruned." in lines


# ---------------------------------------------------------------------------
# OboDemoLoader — realistic ontology species, full external-data path.
# PTS-337 S1 acceptance (verbatim §9 S1): "an ontology release upgrade
# re-ingests via diff with prune and a clean dry-run." Unlike the in-memory
# fake, this loader drives cached_fetch (sha256-verified) against bundled
# fixtures and overrides upgrade() with a diff-based reconstruction.
# ---------------------------------------------------------------------------


class _GroupedEP:
    """Minimal group-aware entry point stand-in for the obodemo tests."""

    def __init__(self, name: str, value: str, target: object):
        self.name = name
        self.value = value
        self._target = target

    def load(self) -> object:
        return self._target


class _GroupedEPs:
    def __init__(self, by_group: dict[str, list["_GroupedEP"]]):
        self._by_group = by_group

    def select(self, *, group: str) -> list["_GroupedEP"]:
        return list(self._by_group.get(group, []))


@pytest.fixture
def obodemo_workspace(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Workspace with ``obodemo`` registered via entry points and an
    isolated reference cache.

    Locally the editable install's ``.dist-info`` predates ``obodemo``, so
    discovery is monkeypatched (mirrors the S0 schema-package tests); CI's
    clean install carries the real entry point. ``MOSAIC_CACHE_DIR`` is
    pinned to ``tmp_path`` so ``cached_fetch`` is hermetic and never
    touches ``~/.cache/mosaic``.
    """
    import importlib.metadata as md

    ep = _GroupedEP(
        "obodemo",
        "mosaic.testing.example_ontology_loader:OboDemoLoader",
        OboDemoLoader,
    )
    monkeypatch.setattr(
        md,
        "entry_points",
        lambda: _GroupedEPs(
            {
                ref_cmd.SCHEMA_PACKAGES_GROUP: [ep],
                ref_cmd.REFERENCE_LOADERS_GROUP: [ep],
            }
        ),
    )
    monkeypatch.setenv("MOSAIC_CACHE_DIR", str(tmp_path / "cache"))
    db_path = tmp_path / "hippo.db"
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    return {"db": db_path, "schemas": schema_dir, "root": tmp_path}


def _build_merged_client(ws: dict[str, Path]) -> tuple[OboDemoLoader, object]:
    """Build a schema-backed client over ``ws`` (merged obodemo fragment).

    Reuses the same private helpers the install/upgrade lifecycle uses, so
    the resulting client exposes the merged ``registry`` the dry-run gate
    validates against — the SDK equivalent of ``mosaic ingest
    --validate-schema --dry-run`` (sec11 §11.5.2).
    """
    info = ref_cmd.find_loader("obodemo")
    loader = info["instance"]
    deployed = ref_cmd._load_deployed_registry(ws["schemas"])
    spec = ref_cmd._build_fragment_spec(info, loader)
    merged = ref_cmd._merge_fragment_into(deployed, spec)
    client = ref_cmd._build_client(merged, ws["db"])
    return loader, client


def _curies(db_path: Path) -> set[str]:
    conn = _open(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT curie FROM "OntologyTerm" WHERE is_available = 1')
        return {row["curie"] for row in cursor.fetchall()}
    finally:
        conn.close()


class TestOboDemoInstall:
    def test_install_v1_full_release_via_cached_fetch(self, obodemo_workspace):
        result = install_reference(
            "obodemo",
            "v1",
            db_path=obodemo_workspace["db"],
            schema_dir=obodemo_workspace["schemas"],
        )
        assert result["status"] == "installed"
        assert result["created"] == 4
        assert _count_rows(obodemo_workspace["db"], "OntologyTerm") == 4
        assert _curies(obodemo_workspace["db"]) == {
            "OBO:0000001",
            "OBO:0000002",
            "OBO:0000003",
            "OBO:0000004",
        }
        # Every put landed in the write log — the prune substrate.
        assert (
            len(_select_write_log(obodemo_workspace["db"], "obodemo", "v1")) == 4
        )

    def test_install_test_version_is_network_free(self, obodemo_workspace):
        # No HTTP server, no cache priming: the "test" slug reads the tiny
        # bundled fixture directly. Any network reach would fail here.
        result = install_reference(
            "obodemo",
            "test",
            db_path=obodemo_workspace["db"],
            schema_dir=obodemo_workspace["schemas"],
        )
        assert result["version"] == "test"
        assert result["created"] == 2
        assert _count_rows(obodemo_workspace["db"], "OntologyTerm") == 2


class TestOboDemoDryRun:
    def test_clean_dry_run_validates_without_writing(self, obodemo_workspace):
        install_reference(
            "obodemo",
            "v1",
            db_path=obodemo_workspace["db"],
            schema_dir=obodemo_workspace["schemas"],
        )
        before = _count_rows(obodemo_workspace["db"], "OntologyTerm")

        loader, client = _build_merged_client(obodemo_workspace)
        result = loader.dry_run_upgrade(client, "v1", "v2")

        # A clean dry-run: zero errors, reports what it WOULD write, and
        # leaves both the entity table and the write log untouched.
        assert result.errors == 0
        assert result.created == 4
        assert result.entities == []
        assert _count_rows(obodemo_workspace["db"], "OntologyTerm") == before
        assert _select_write_log(obodemo_workspace["db"], "obodemo", "v2") == []

    def test_dry_run_gate_catches_schema_violation(self, obodemo_workspace):
        # The gate is real, not vacuous: a term missing the required
        # ``label`` slot fails validation against the merged schema.
        install_reference(
            "obodemo",
            "v1",
            db_path=obodemo_workspace["db"],
            schema_dir=obodemo_workspace["schemas"],
        )
        loader, client = _build_merged_client(obodemo_workspace)
        errors = loader._dry_run_validate(client, [{"curie": "OBO:0000099"}])
        assert errors  # non-empty → the missing required slot was caught

    def test_dry_run_is_not_a_state_corrupting_cli_flag(self, obodemo_workspace):
        # Regression guard: the dry-run is an SDK-only method, deliberately
        # NOT a load param. If it leaked into ``load_params_schema`` it would
        # render as ``mosaic reference upgrade --dry-run`` — and the lifecycle
        # would still prune + rotate the version pointer after a write-free
        # LoadResult, leaving an installed-but-empty (data-loss) state. So
        # the flag must NOT exist on the loader's params, and the CLI must
        # reject it.
        assert "dry_run" not in OboDemoLoader.load_params_schema.model_fields

        install_reference(
            "obodemo",
            "v1",
            db_path=obodemo_workspace["db"],
            schema_dir=obodemo_workspace["schemas"],
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["reference", "upgrade", "obodemo", "--version", "v2",
             "--db-path", str(obodemo_workspace["db"]),
             "--schema-dir", str(obodemo_workspace["schemas"]),
             "--prune-old", "--dry-run"],
        )
        # Unknown flag → non-zero exit, and crucially the prior release is
        # untouched (no prune, no version rotation).
        assert result.exit_code != 0
        assert _count_rows(obodemo_workspace["db"], "OntologyTerm") == 4
        conn = _open(obodemo_workspace["db"])
        try:
            versions = get_meta(conn, META_KEY_VERSIONS)
        finally:
            conn.close()
        assert versions == {"obodemo": "v1"}


class TestOboDemoDiffUpgradeAcceptance:
    """The verbatim §9 S1 acceptance, end to end."""

    def test_release_upgrade_reingests_via_diff_with_prune_and_clean_dry_run(
        self, obodemo_workspace
    ):
        db = obodemo_workspace["db"]
        schemas = obodemo_workspace["schemas"]

        # Install the v1 release (full load through cached_fetch).
        install_reference("obodemo", "v1", db_path=db, schema_dir=schemas)
        assert _count_rows(db, "OntologyTerm") == 4

        # 1) A clean dry-run gate BEFORE committing anything.
        loader, client = _build_merged_client(obodemo_workspace)
        dry = loader.dry_run_upgrade(client, "v1", "v2")
        assert dry.errors == 0
        assert _count_rows(db, "OntologyTerm") == 4  # still untouched

        # 2) The live diff-based upgrade with --prune-old.
        upgrade = upgrade_reference(
            "obodemo", "v2", db_path=db, schema_dir=schemas, prune_old=True
        )
        assert upgrade["status"] == "upgraded"
        assert upgrade["from_version"] == "v1"
        assert upgrade["to_version"] == "v2"
        assert upgrade["created"] == 4
        assert len(upgrade["pruned"]) == 4

        # The prior release is gone (fresh ids ⇒ disjoint ⇒ a clean prune);
        # only the reconstructed v2 term set survives. The obsoleted term
        # (OBO:0000003) is absent; the added term (OBO:0000005) is present.
        assert _count_all_rows(db, "OntologyTerm") == 4
        assert _curies(db) == {
            "OBO:0000001",
            "OBO:0000002",
            "OBO:0000004",
            "OBO:0000005",
        }

        # The write log rotated: v1 pruned, v2 now authoritative.
        assert _select_write_log(db, "obodemo", "v1") == []
        assert len(_select_write_log(db, "obodemo", "v2")) == 4

        # mosaic_meta now pins v2.
        conn = _open(db)
        try:
            versions = get_meta(conn, META_KEY_VERSIONS)
        finally:
            conn.close()
        assert versions == {"obodemo": "v2"}

    def test_diff_upgrade_via_typer_smoke(self, obodemo_workspace):
        runner = CliRunner()
        db = str(obodemo_workspace["db"])
        sd = str(obodemo_workspace["schemas"])

        install = runner.invoke(
            app,
            ["reference", "install", "obodemo", "--version", "v1",
             "--db-path", db, "--schema-dir", sd],
        )
        assert install.exit_code == 0, install.output
        assert "Installed obodemo@v1" in install.output

        upgrade = runner.invoke(
            app,
            ["reference", "upgrade", "obodemo", "--version", "v2",
             "--db-path", db, "--schema-dir", sd, "--prune-old"],
        )
        assert upgrade.exit_code == 0, upgrade.output
        assert "Upgraded obodemo v1 → v2" in upgrade.output
        assert "4 prior row(s) pruned." in upgrade.output
