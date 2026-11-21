"""Tests for the ``deprovision`` teardown orchestrator (PTS-339 / sec11 §11.4).

Covers the half of the §9 S3 acceptance that lives in the CLI
orchestrator: *deprovision refuses on packages with dependents* (§11.4.4),
and dispatches the species data-retirement hook correctly —

- ``ReferenceLoader``: the orchestrator prunes its ``reference_write_log``
  rows (hard delete; reconstructible external source) and drops the
  installed-version record.
- Dependents guard: refuses, naming the dependent(s), *before* any
  teardown so a guarded package is never partially torn down.
- ``DomainModule``: the orchestrator threads ``force`` to the species
  hook and, being non-``ExternalData``, never prunes the write log.

The complementary *refuse on live domain data* half (§11.4.3) is proven
at the unit level in ``tests/core/test_domain_module.py::TestDeprovision``
— here we only confirm the orchestrator wiring around it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

# deprovision_reference's internal collaborator calls (find_loader,
# _read_versions, _find_installed_dependents, _load_deployed_registry,
# _build_fragment_spec, _merge_fragment_into, _build_client, _remove_version)
# resolve against the module they're *defined* in
# (mosaic.core.loaders.lifecycle, post-issue-#69 relocation) regardless of
# where the caller imported them from, so patches must target that module.
import mosaic.core.loaders.lifecycle as refmod
from mosaic.cli.commands.reference import (
    META_KEY_VERSIONS,
    _find_installed_dependents,
    deprovision_reference,
    install_reference,
)
from mosaic.cli.main import app
from mosaic.core.exceptions import DeprovisionRefusedError
from mosaic.core.loaders.domain_module import DomainModule
from mosaic.core.loaders.reference import LoadResult, ReferenceLoader
from mosaic.core.meta import get_meta


@pytest.fixture
def hippo_workspace(tmp_path: Path) -> dict[str, Path]:
    db_path = tmp_path / "hippo.db"
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    return {"db": db_path, "schemas": schema_dir}


def _installed_versions(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return get_meta(conn, META_KEY_VERSIONS) or {}
    finally:
        conn.close()


def _count_all_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        cursor.execute(f'SELECT COUNT(*) AS n FROM "{table}"')
        return cursor.fetchone()[0]
    finally:
        conn.close()


class _DependsOnFake(ReferenceLoader):
    """A loader declaring a dependency on ``fake`` — drives the guard."""

    name = "depender"
    description = "depends on fake"

    def versions(self) -> list[str]:
        return ["v1"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": "depender"}

    def depends_on(self) -> list[str]:
        return ["fake"]

    def load(self, client, version, params=None) -> LoadResult:
        return LoadResult()


class TestReferenceLoaderDeprovision:
    def test_prunes_rows_and_removes_version_record(self, hippo_workspace):
        db, sd = hippo_workspace["db"], hippo_workspace["schemas"]
        install_reference("fake", "v1", db_path=db, schema_dir=sd)
        assert _count_all_rows(db, "FakeTerm") == 3
        assert _installed_versions(db) == {"fake": "v1"}

        result = deprovision_reference("fake", db_path=db, schema_dir=sd)

        assert result["status"] == "deprovisioned"
        assert result["version"] == "v1"
        assert len(result["pruned"]) == 3
        # Hard-delete (reconstructible external source; D2.14.J substrate).
        assert _count_all_rows(db, "FakeTerm") == 0
        # The package now reads as uninstalled.
        assert "fake" not in _installed_versions(db)

    def test_not_installed_is_a_clear_error(self, hippo_workspace):
        with pytest.raises(ValueError, match="not installed"):
            deprovision_reference(
                "fake",
                db_path=hippo_workspace["db"],
                schema_dir=hippo_workspace["schemas"],
            )


class TestDependentsGuard:
    def test_find_installed_dependents_unit(self, hippo_workspace, monkeypatch):
        db = hippo_workspace["db"]
        # Install fake (creates mosaic_meta + records fake@v1), then mark a
        # dependent package as installed too.
        install_reference(
            "fake", "v1", db_path=db, schema_dir=hippo_workspace["schemas"]
        )
        refmod._write_versions(db, "depender", "v1")

        real_find = refmod.find_loader
        monkeypatch.setattr(
            refmod,
            "find_loader",
            lambda n: {"instance": _DependsOnFake()}
            if n == "depender"
            else real_find(n),
        )

        assert _find_installed_dependents("fake", db) == ["depender"]
        # `depender` has no dependents of its own.
        assert _find_installed_dependents("depender", db) == []

    def test_deprovision_refuses_with_dependents(
        self, hippo_workspace, monkeypatch
    ):
        db, sd = hippo_workspace["db"], hippo_workspace["schemas"]
        install_reference("fake", "v1", db_path=db, schema_dir=sd)
        refmod._write_versions(db, "depender", "v1")

        real_find = refmod.find_loader
        monkeypatch.setattr(
            refmod,
            "find_loader",
            lambda n: {
                "name": "depender",
                "instance": _DependsOnFake(),
                "package_name": "depender",
                "package_version": "0",
            }
            if n == "depender"
            else real_find(n),
        )

        with pytest.raises(DeprovisionRefusedError) as exc_info:
            deprovision_reference("fake", db_path=db, schema_dir=sd)
        err = exc_info.value
        assert err.reason == "has_dependents"
        assert err.dependents == ["depender"]

        # Guard fired BEFORE teardown: fake's rows and version record are
        # untouched.
        assert _count_all_rows(db, "FakeTerm") == 3
        assert _installed_versions(db)["fake"] == "v1"


class TestDomainModuleOrchestration:
    def test_threads_force_and_skips_prune_for_non_external(
        self, hippo_workspace, monkeypatch
    ):
        # A DomainModule is MigratableData, not ExternalData — the
        # orchestrator must call its hook (threading `force`) and NOT prune
        # the write log. Builders are stubbed so the test stays focused on
        # the dispatch wiring, not schema merging.
        calls: dict[str, object] = {}

        class _SpyModule(DomainModule):
            name = "spy"
            description = "spy domain module"

            def versions(self) -> list[str]:
                return ["v1"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "spy", "classes": {}}

            def migration_steps(self) -> list:
                return []

            def deprovision(self, client, version, *, force=False) -> None:
                calls["force"] = force
                calls["version"] = version

        spy = _SpyModule()
        monkeypatch.setattr(
            refmod,
            "find_loader",
            lambda n: {
                "name": "spy",
                "instance": spy,
                "package_name": "spy",
                "package_version": "0",
            },
        )
        monkeypatch.setattr(refmod, "_read_versions", lambda db: {"spy": "v1"})
        monkeypatch.setattr(
            refmod, "_find_installed_dependents", lambda name, db: []
        )
        monkeypatch.setattr(refmod, "_load_deployed_registry", lambda sd: object())
        monkeypatch.setattr(
            refmod, "_build_fragment_spec", lambda info, loader: object()
        )
        monkeypatch.setattr(
            refmod, "_merge_fragment_into", lambda reg, spec: object()
        )
        monkeypatch.setattr(refmod, "_build_client", lambda reg, db: object())
        removed: dict[str, str] = {}
        monkeypatch.setattr(
            refmod,
            "_remove_version",
            lambda db, name: removed.update(name=name),
        )

        result = deprovision_reference(
            "spy",
            db_path=hippo_workspace["db"],
            schema_dir=hippo_workspace["schemas"],
            force=True,
        )

        assert calls == {"force": True, "version": "v1"}
        assert result["pruned"] == []  # not ExternalData → never pruned
        assert result["forced"] is True
        assert removed["name"] == "spy"


class TestDeprovisionCli:
    def test_cli_deprovision_smoke(self, hippo_workspace):
        db, sd = hippo_workspace["db"], hippo_workspace["schemas"]
        install_reference("fake", "v1", db_path=db, schema_dir=sd)

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "reference",
                "deprovision",
                "fake",
                "--db-path",
                str(db),
                "--schema-dir",
                str(sd),
            ],
        )

        assert result.exit_code == 0
        assert "Deprovisioned fake@v1" in result.stdout
        assert "fake" not in _installed_versions(db)

    def test_cli_deprovision_not_installed_exits_nonzero(self, hippo_workspace):
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "reference",
                "deprovision",
                "fake",
                "--db-path",
                str(hippo_workspace["db"]),
                "--schema-dir",
                str(hippo_workspace["schemas"]),
            ],
        )
        assert result.exit_code != 0
        # Errors are echoed to stderr (Typer ``err=True``). Assert on
        # ``result.output`` rather than ``result.stderr``: click 8.1's
        # ``CliRunner`` merges stderr into stdout by default and only
        # captures it separately when constructed with the now-removed
        # (click 8.2+) ``mix_stderr=False`` flag, so ``.stderr`` raises
        # ``ValueError`` here. ``.output`` is what every other CLI test in
        # this suite already asserts on and is stable across click 8.1/8.2.
        assert "not installed" in result.output
