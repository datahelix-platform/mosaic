"""Tests for ``mosaic ingest`` — LinkML-native instance YAML ingest.

PR 3.3 retires the ``entities: [{type, data, external_id}]`` wrapper in
favor of LinkML-native tree-root bundles. Each top-level key in the
bundle is a tree-root accessor slot (``samples:`` etc.); each value is a
list of instance dicts. Identity is by the ``id`` slot — re-ingesting an
instance with an existing id updates it in place.
"""

import tempfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mosaic.cli.main import app


SCHEMA_YAML = """\
id: https://example.org/mosaic/test/ingest_cli
name: ingest_cli_schema
description: Minimal LinkML schema for ingest CLI tests.

prefixes:
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types
  - mosaic_core

default_range: string

classes:
  Project:
    is_a: Entity
    attributes:
      name:
        required: true

  Sample:
    is_a: Entity
    attributes:
      name:
        required: true
      project_id:
        range: Project
"""


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def tmp_hippo(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "schema.yaml"
    p.write_text(SCHEMA_YAML)
    return p


@pytest.fixture()
def schema_registry(schema_file: Path):
    from mosaic.linkml_bridge import SchemaRegistry

    return SchemaRegistry.from_path(schema_file)


@pytest.fixture()
def client(tmp_hippo: Path, schema_registry):
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

    return MosaicClient(
        storage=SQLiteAdapter(
            str(tmp_hippo / "test.db"), schema_registry=schema_registry
        )
    )


def _write_bundle(tmp_path: Path, bundle: dict, name: str = "bundle.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.dump(bundle))
    return p


# ---------------------------------------------------------------------------
# LinkML-native instance YAML ingest
# ---------------------------------------------------------------------------


class TestIngestLinkMLYAML:
    """ingest_linkml_yaml consumes tree-root bundles and writes via client.put."""

    def test_ingest_creates_entities(self, tmp_hippo, client, schema_registry):
        bundle = _write_bundle(
            tmp_hippo,
            {
                "projects": [
                    {"id": "p1", "name": "Project One", "is_available": True},
                    {"id": "p2", "name": "Project Two", "is_available": True},
                ]
            },
        )

        from mosaic.cli.commands.ingest import ingest_linkml_yaml

        result = ingest_linkml_yaml(bundle, client, schema_registry)

        assert result.created == 2
        assert result.updated == 0
        assert result.errors == 0
        items = list(client.query("Project").items)
        assert len(items) == 2

    def test_ingest_updates_existing_id(self, tmp_hippo, client, schema_registry):
        """Re-ingest with the same `id` updates the existing entity."""
        from mosaic.cli.commands.ingest import ingest_linkml_yaml

        v1 = _write_bundle(
            tmp_hippo,
            {"projects": [{"id": "p1", "name": "Project One", "is_available": True}]},
            name="v1.yaml",
        )
        ingest_linkml_yaml(v1, client, schema_registry)

        v2 = _write_bundle(
            tmp_hippo,
            {"projects": [{"id": "p1", "name": "Project One Renamed", "is_available": True}]},
            name="v2.yaml",
        )
        r2 = ingest_linkml_yaml(v2, client, schema_registry)

        assert r2.updated == 1
        assert r2.created == 0
        items = list(client.query("Project").items)
        assert len(items) == 1
        assert items[0]["data"]["name"] == "Project One Renamed"

    def test_ingest_multiple_classes(self, tmp_hippo, client, schema_registry):
        """A single bundle can carry instances of more than one class."""
        from mosaic.cli.commands.ingest import ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_hippo,
            {
                "projects": [{"id": "p1", "name": "Project One", "is_available": True}],
                "samples": [
                    {"id": "s1", "name": "Tissue A", "project_id": "p1", "is_available": True},
                    {"id": "s2", "name": "Tissue B", "project_id": "p1", "is_available": True},
                ],
            },
        )

        result = ingest_linkml_yaml(bundle, client, schema_registry)

        assert result.created == 3
        assert list(client.query("Project").items)
        assert len(list(client.query("Sample").items)) == 2

    def test_ingest_missing_required_field_raises(
        self, tmp_hippo, client, schema_registry
    ):
        """A bundle with a missing required field fails LinkML validation."""
        from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_hippo,
            {"samples": [{"id": "s1", "project_id": "p1", "is_available": True}]},  # no `name`
        )

        with pytest.raises(IngestError, match="name"):
            ingest_linkml_yaml(bundle, client, schema_registry)

    def test_ingest_unknown_top_level_slot_raises(
        self, tmp_hippo, client, schema_registry
    ):
        from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_hippo,
            {"not_a_class": [{"id": "x", "name": "y", "is_available": True}]},
        )

        with pytest.raises(IngestError):
            ingest_linkml_yaml(bundle, client, schema_registry)

    def test_ingest_not_a_mapping_raises(
        self, tmp_hippo, client, schema_registry
    ):
        from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml

        bad = tmp_hippo / "list.yaml"
        bad.write_text(yaml.dump(["item1", "item2"]))

        with pytest.raises(IngestError, match="mapping"):
            ingest_linkml_yaml(bad, client, schema_registry)

    def test_ingest_file_not_found_raises(self, tmp_hippo, client, schema_registry):
        from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml

        with pytest.raises(IngestError, match="not found"):
            ingest_linkml_yaml(
                tmp_hippo / "nonexistent.yaml", client, schema_registry
            )

    def test_ingest_csv_file_rejected(self, tmp_hippo, client, schema_registry):
        """``ingest_linkml_yaml`` rejects CSV/JSON data files — they belong
        to Cappella."""
        from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml

        csv_file = tmp_hippo / "data.csv"
        csv_file.write_text("external_id,name\nBU0001,Alice\n")

        with pytest.raises(IngestError):
            ingest_linkml_yaml(csv_file, client, schema_registry)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestIngestCLI:
    """End-to-end ``mosaic ingest`` CLI tests with ``--validate-schema``."""

    def test_cli_file_with_validate_schema(
        self, runner, tmp_hippo, schema_file, monkeypatch
    ):
        bundle = _write_bundle(
            tmp_hippo,
            {"projects": [{"id": "p1", "name": "Project One", "is_available": True}]},
        )
        # `_get_client` writes to `data/hippo.db` relative to CWD; chdir
        # into tmp_path so the on-disk side effect is contained.
        (tmp_hippo / "data").mkdir()
        monkeypatch.chdir(tmp_hippo)
        result = runner.invoke(
            app,
            [
                "ingest",
                "--file",
                str(bundle),
                "--validate-schema",
                str(schema_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "created=1" in result.output

    def test_cli_dry_run_validates_bundle(
        self, runner, tmp_hippo, schema_file
    ):
        bundle = _write_bundle(
            tmp_hippo,
            {"projects": [{"id": "p1", "name": "Project One", "is_available": True}]},
        )
        result = runner.invoke(
            app,
            [
                "ingest",
                "--file",
                str(bundle),
                "--validate-schema",
                str(schema_file),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "validates" in result.output

    def test_cli_dry_run_rejects_missing_required(
        self, runner, tmp_hippo, schema_file
    ):
        bundle = _write_bundle(
            tmp_hippo,
            {"samples": [{"id": "s1", "project_id": "p1"}]},  # no name
        )
        result = runner.invoke(
            app,
            [
                "ingest",
                "--file",
                str(bundle),
                "--validate-schema",
                str(schema_file),
                "--dry-run",
            ],
        )
        assert result.exit_code == 1
        assert "validation error" in result.output

    def test_cli_missing_schema_file(self, runner, tmp_hippo):
        bundle = _write_bundle(
            tmp_hippo,
            {"projects": [{"id": "p1", "name": "Project One", "is_available": True}]},
        )
        result = runner.invoke(
            app,
            [
                "ingest",
                "--file",
                str(bundle),
                "--validate-schema",
                str(tmp_hippo / "missing.yaml"),
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_cli_file_not_found(self, runner, tmp_hippo, schema_file):
        result = runner.invoke(
            app,
            [
                "ingest",
                "--file",
                str(tmp_hippo / "no-such-file.yaml"),
                "--validate-schema",
                str(schema_file),
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_cli_db_path_targets_named_store(
        self, runner, tmp_hippo, schema_file, monkeypatch
    ):
        """``--db-path`` writes to the named store, not ``data/mosaic.db`` (issue #89)."""
        bundle = _write_bundle(
            tmp_hippo,
            {"projects": [{"id": "p1", "name": "Project One", "is_available": True}]},
        )
        store_dir = tmp_hippo / "store-0"
        store_dir.mkdir()
        db_path = store_dir / "mosaic.db"

        # A default-path store also exists in the CWD, so a regression back
        # to the old CWD-relative default would silently "succeed" against
        # the wrong database instead of erroring.
        (tmp_hippo / "data").mkdir()
        monkeypatch.chdir(tmp_hippo)

        result = runner.invoke(
            app,
            [
                "ingest",
                "--file",
                str(bundle),
                "--validate-schema",
                str(schema_file),
                "--db-path",
                str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "created=1" in result.output
        assert db_path.exists()
        assert not (tmp_hippo / "data" / "mosaic.db").exists()


# ---------------------------------------------------------------------------
# IngestResult
# ---------------------------------------------------------------------------


class TestIngestResult:
    """IngestResult carries counts and errors."""

    def test_result_defaults(self):
        from mosaic.cli.commands.ingest import IngestResult

        r = IngestResult(source_file="bundle.yaml")
        assert r.created == 0
        assert r.updated == 0
        assert r.errors == 0
        assert r.error_messages == []

    def test_result_to_dict(self):
        from mosaic.cli.commands.ingest import IngestResult

        r = IngestResult(source_file="bundle.yaml", created=2, errors=1)
        d = r.to_dict()
        assert d["created"] == 2
        assert d["errors"] == 1
        assert "source_file" in d


# ---------------------------------------------------------------------------
# Cyclic / self-referential references (issue #95)
# ---------------------------------------------------------------------------

#: A schema with a self-referential reference slot (``Node.part_of -> Node``).
#: Schema-valid data can form reference cycles (``A -> B -> A``) or self-loops
#: (``A -> A``) that no per-row insertion order can satisfy under immediately
#: enforced foreign keys.
CYCLIC_SCHEMA_YAML = """\
id: https://example.org/mosaic/test/cyclic
name: cyclic_schema
description: Schema with a self-referential reference slot.

prefixes:
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types
  - mosaic_core

default_range: string

classes:
  Node:
    is_a: Entity
    attributes:
      name:
        required: true
      part_of:
        range: Node
"""


class TestCyclicSelfReferentialIngest:
    """A bundle whose instances reference each other cyclically ingests
    atomically: foreign-key checks are deferred to the single commit that
    wraps the whole bundle (issue #95)."""

    @pytest.fixture()
    def cyclic_registry(self, tmp_path: Path):
        from mosaic.linkml_bridge import SchemaRegistry

        schema = tmp_path / "cyclic.yaml"
        schema.write_text(CYCLIC_SCHEMA_YAML)
        return SchemaRegistry.from_path(schema)

    @pytest.fixture()
    def cyclic_client(self, tmp_path: Path, cyclic_registry):
        from mosaic.core.client import MosaicClient
        from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

        return MosaicClient(
            storage=SQLiteAdapter(
                str(tmp_path / "cyclic.db"), schema_registry=cyclic_registry
            )
        )

    def test_two_node_cycle_ingests(
        self, tmp_path, cyclic_client, cyclic_registry
    ):
        """``A -> B -> A`` — the reproduction from issue #95."""
        from mosaic.cli.commands.ingest import ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_path,
            {
                "nodes": [
                    {"id": "N-1", "name": "one", "part_of": "N-2", "is_available": True},
                    {"id": "N-2", "name": "two", "part_of": "N-1", "is_available": True},
                ]
            },
        )

        result = ingest_linkml_yaml(bundle, cyclic_client, cyclic_registry)

        assert result.errors == 0, result.error_messages
        assert result.created == 2
        assert cyclic_client.get("Node", "N-1")["data"]["part_of"] == "N-2"
        assert cyclic_client.get("Node", "N-2")["data"]["part_of"] == "N-1"

    def test_self_loop_ingests(self, tmp_path, cyclic_client, cyclic_registry):
        """A single node referencing itself (``A -> A``)."""
        from mosaic.cli.commands.ingest import ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_path,
            {"nodes": [{"id": "N-1", "name": "self", "part_of": "N-1", "is_available": True}]},
        )

        result = ingest_linkml_yaml(bundle, cyclic_client, cyclic_registry)

        assert result.errors == 0, result.error_messages
        assert result.created == 1
        assert cyclic_client.get("Node", "N-1")["data"]["part_of"] == "N-1"

    def test_forward_reference_is_order_independent(
        self, tmp_path, cyclic_client, cyclic_registry
    ):
        """An instance may reference another declared *later* in the bundle;
        deferral removes any need to hand-order the file."""
        from mosaic.cli.commands.ingest import ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_path,
            {
                "nodes": [
                    {"id": "N-1", "name": "child", "part_of": "N-2", "is_available": True},
                    {"id": "N-2", "name": "parent", "is_available": True},
                ]
            },
        )

        result = ingest_linkml_yaml(bundle, cyclic_client, cyclic_registry)

        assert result.errors == 0, result.error_messages
        assert result.created == 2

    def test_dangling_reference_rolls_back_whole_bundle(
        self, tmp_path, cyclic_client, cyclic_registry
    ):
        """A reference to an id present in neither the bundle nor the database
        fails the deferred check at commit; the whole bundle rolls back and
        nothing is persisted (atomic ingest)."""
        from mosaic.cli.commands.ingest import IngestError, ingest_linkml_yaml

        bundle = _write_bundle(
            tmp_path,
            {
                "nodes": [
                    {"id": "N-1", "name": "one", "part_of": "GHOST", "is_available": True},
                    {"id": "N-2", "name": "two", "is_available": True},
                ]
            },
        )

        with pytest.raises(IngestError, match="rolled back"):
            ingest_linkml_yaml(bundle, cyclic_client, cyclic_registry)

        # Nothing from the bundle survived the rollback.
        from mosaic.core.exceptions import EntityNotFoundError

        for node_id in ("N-1", "N-2"):
            with pytest.raises(EntityNotFoundError):
                cyclic_client.get("Node", node_id)
