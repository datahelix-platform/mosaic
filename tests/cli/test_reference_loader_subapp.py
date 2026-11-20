"""Tests for loader-provided Typer sub-app mounting (PTS-228 / D2.14.A).

Covers the four acceptance criteria from the issue:

1. ``mosaic reference <name> --help`` shows the registered subcommands.
2. Invoking a subcommand reads ``client.cache_dir_for(<name>)`` and
   gets the same path as ``load()`` would.
3. A loader that registers only ``mosaic.reference_loaders`` (no sub-app)
   does not break the CLI; the parent install/upgrade/list verbs still
   work on it.
4. Two loaders each with their own sub-app coexist (mounted under
   different names).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mosaic.cli.commands.reference import (
    ReferenceLoaderRegistrationError,
    discover_reference_loader_subapps,
    mount_reference_loader_subapps,
)
from mosaic.cli.main import app, reference_app


class TestFakeLoaderSubapp:
    """Acceptance criteria (1) and (2) against the real ``fake`` loader."""

    def test_fake_subapp_help_lists_registered_subcommands(self):
        runner = CliRunner()
        result = runner.invoke(app, ["reference", "fake", "--help"])
        assert result.exit_code == 0, result.output
        # Subcommands declared on ``fake_cli_app`` must appear in --help.
        assert "echo" in result.output
        assert "cache-path" in result.output

    def test_fake_subapp_cache_path_matches_load_cache_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Drive both surfaces against the same MOSAIC_CACHE_DIR override and
        # verify the sub-app prints the path the loader's load() would use.
        monkeypatch.setenv("MOSAIC_CACHE_DIR", str(tmp_path / "cache"))

        runner = CliRunner()
        result = runner.invoke(app, ["reference", "fake", "cache-path"])
        assert result.exit_code == 0, result.output

        # Compute the canonical path the same way load() would (it calls
        # the same MosaicClient.cache_dir_for helper under the hood).
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
        from mosaic.linkml_bridge import SchemaRegistry
        from linkml_runtime.utils.schemaview import SchemaView
        import importlib.resources

        mosaic_core_path = importlib.resources.files("mosaic.schemas").joinpath(
            "mosaic_core.yaml"
        )
        registry = SchemaRegistry(SchemaView(str(mosaic_core_path)))
        client = MosaicClient(
            storage=SQLiteAdapter(":memory:", schema_registry=registry),
            registry=registry,
        )
        expected = client.cache_dir_for("fake")

        assert str(expected) in result.output
        # Same prefix as $MOSAIC_CACHE_DIR proves the env override is honored
        # in both the sub-app path and the load() path.
        assert str(tmp_path / "cache" / "fake") == str(expected)

    def test_fake_subapp_echo_runs(self):
        runner = CliRunner()
        result = runner.invoke(app, ["reference", "fake", "echo", "boom"])
        assert result.exit_code == 0, result.output
        assert "boom" in result.output


class TestMountSemantics:
    """Acceptance criteria (3) and (4) via the public mount helper."""

    def test_no_subapps_is_quiet_noop(self, monkeypatch: pytest.MonkeyPatch):
        """A loader with no sub-app entry leaves the reference group
        untouched — install/upgrade/list still serve the parent group."""
        monkeypatch.setattr(
            "mosaic.cli.commands.reference.discover_reference_loader_subapps",
            lambda: [],
        )
        fresh = typer.Typer(name="reference")

        @fresh.command()
        def install(name: str) -> None:
            typer.echo(f"install {name}")

        @fresh.command()
        def lst() -> None:  # second command forces Typer to dispatch by name
            typer.echo("list")

        mount_reference_loader_subapps(fresh)
        # The mount step must not have added any subgroups.
        assert fresh.registered_groups == []

        # The parent verb on a loader without a sub-app keeps working.
        runner = CliRunner()
        out = runner.invoke(fresh, ["install", "no-subapp-loader"])
        assert out.exit_code == 0, out.output
        assert "install no-subapp-loader" in out.output

    def test_two_subapps_coexist_under_distinct_names(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Two registered sub-apps mount side-by-side without collision."""
        sub_a = typer.Typer()

        @sub_a.command()
        def ping() -> None:
            typer.echo("pong-a")

        sub_b = typer.Typer()

        @sub_b.command()
        def ping() -> None:  # noqa: F811 — distinct Typer command
            typer.echo("pong-b")

        monkeypatch.setattr(
            "mosaic.cli.commands.reference.discover_reference_loader_subapps",
            lambda: [("loader-a", sub_a), ("loader-b", sub_b)],
        )

        fresh = typer.Typer(name="reference")
        mount_reference_loader_subapps(fresh)

        mounted = {grp.name for grp in fresh.registered_groups}
        assert mounted == {"loader-a", "loader-b"}

        runner = CliRunner()
        out_a = runner.invoke(fresh, ["loader-a", "ping"])
        assert out_a.exit_code == 0, out_a.output
        assert "pong-a" in out_a.output

        out_b = runner.invoke(fresh, ["loader-b", "ping"])
        assert out_b.exit_code == 0, out_b.output
        assert "pong-b" in out_b.output


class TestDiscoveryStrictness:
    """An entry point pointing at a non-Typer object must fail loud,
    matching the reference_loaders strictness from PTS-224."""

    def test_non_typer_entry_point_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        class _StubEP:
            def __init__(self, name: str, value: str, payload):
                self.name = name
                self.value = value
                self._payload = payload

            def load(self):
                return self._payload

        class _StubEPS:
            def __init__(self, eps):
                self._eps = eps

            def select(self, group: str):
                if group == "mosaic.reference_loader_cli":
                    return self._eps
                return []

            def __getitem__(self, group: str):
                if group == "mosaic.reference_loader_cli":
                    return self._eps
                raise KeyError(group)

        broken = _StubEP("broken", "pkg:not_a_typer_app", object())
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda: _StubEPS([broken]),
        )

        with pytest.raises(ReferenceLoaderRegistrationError, match="not a typer.Typer"):
            discover_reference_loader_subapps()


class TestRealRegistryWiring:
    """Sanity check the live entry-point wiring loaded at import time.

    The mounting happens in ``mosaic.cli.main`` at module import — this
    test asserts the ``fake`` sub-app actually landed under the group.
    """

    def test_fake_sub_app_is_mounted_on_reference_app(self):
        mounted = {grp.name for grp in reference_app.registered_groups}
        assert "fake" in mounted
