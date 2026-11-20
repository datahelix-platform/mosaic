"""Integration tests for ``migrate_bundle`` — the S4 *one command* (PTS-340).

Exercises the orchestration glue end to end: discover installed packages,
read current versions from ``mosaic_meta``, fold the merged registry, run the
dependency-ordered staged migration, and persist version pointers only on a
clean commit. The orchestrator internals are unit-tested in
``test_orchestrator``; here we prove the CLI-level SDK entrypoint wires them
to real ``reference_versions`` state and rolls pointers forward atomically
with the data (and leaves them untouched on a gate failure).
"""

import os
import tempfile

import pytest
import yaml
from linkml_runtime.utils.schemaview import SchemaView

# migrate_bundle's internal collaborator calls (discover_schema_packages,
# _build_merged_registry) resolve against the module they're *defined* in
# (mosaic.core.loaders.lifecycle, post-issue-#69 relocation) regardless of
# where the caller imported them from, so patches must target that module.
from mosaic.core.loaders import lifecycle as refmod
from mosaic.cli.commands.reference import _read_versions, _write_versions, migrate_bundle
from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import MigrationGateError
from mosaic.core.loaders.domain_module import (
    DomainModule,
    MigrationContext,
    MigrationStep,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap


def _registry() -> SchemaRegistry:
    overlay = {
        "id": "https://example.org/mosaic/test_migrate_bundle",
        "name": "test_migrate_bundle",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "mosaic": "https://w3id.org/datahelix/mosaic/",
        },
        "imports": ["linkml:types", "mosaic_core"],
        "default_range": "string",
        "classes": {
            "Sample": {
                "is_a": "Entity",
                "attributes": {
                    "label": {"range": "string"},
                    "kind": {"range": "string"},
                },
            },
            "SampleTag": {
                "is_a": "Entity",
                "attributes": {
                    "tag": {"range": "string"},
                    "rank": {"range": "integer"},
                },
            },
        },
    }
    return SchemaRegistry(
        SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
    )


class _Mod(DomainModule):
    def __init__(self, name: str, entity_type: str, depends: list[str]) -> None:
        self.name = name
        self.description = name
        self._et = entity_type
        self._depends = depends

    def versions(self) -> list[str]:
        return ["v1", "v2", "test"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": self.name, "classes": {}}

    def populates_types(self) -> list[str]:
        return [self._et]

    def depends_on(self) -> list[str]:
        return list(self._depends)

    def _transform(self, ctx: MigrationContext) -> None:
        for old in ctx.client.query(self._et).items:
            new = dict(old["data"])
            if self._et == "Sample":
                new["kind"] = "migrated"
            ctx.plan.migrate(self._et, old["id"], new)

    def migration_steps(self) -> list[MigrationStep]:
        return [MigrationStep("v1", "v2", self._transform)]


@pytest.fixture
def deployment(monkeypatch):
    """A db pre-seeded with Sample v1 + a SampleTag, versions at v1, and the
    two modules wired into discovery + the merged registry."""
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "hippo.db")
    reg = _registry()
    storage = SQLiteAdapter(db, schema_registry=reg)
    client = MosaicClient(storage=storage, registry=reg)
    for i in range(2):
        client.put(
            "Sample",
            {"id": f"s{i}", "label": f"l{i}", "is_available": True},
            bypass_validation=True,
        )
    _write_versions(db, "sample", "v1")
    _write_versions(db, "sampletag", "v1")

    sample = _Mod("sample", "Sample", [])
    tag = _Mod("sampletag", "SampleTag", ["sample"])
    infos = [
        {"name": "sample", "instance": sample},
        {"name": "sampletag", "instance": tag},
    ]
    monkeypatch.setattr(refmod, "discover_schema_packages", lambda: infos)
    # Isolate the orchestration glue from fragment-merge mechanics (covered by
    # install/upgrade tests): the merged registry already carries both classes.
    monkeypatch.setattr(refmod, "_build_merged_registry", lambda sd, infos: reg)

    return {"db": db, "client": client, "reg": reg}


def _seed_tag(client: MosaicClient, wid: str, rank) -> None:
    client.put(
        "SampleTag",
        {"id": wid, "tag": "t", "rank": rank, "is_available": True},
        bypass_validation=True,
    )


def _kinds(db: str, reg: SchemaRegistry) -> list:
    c = MosaicClient(storage=SQLiteAdapter(db, schema_registry=reg), registry=reg)
    return [it["data"].get("kind") for it in c.query("Sample").items]


def test_one_command_migrates_and_records_versions(deployment) -> None:
    _seed_tag(deployment["client"], "ok", rank=3)
    result = migrate_bundle(
        {"name": "bb", "packages": {"sample": "v2"}},
        db_path=deployment["db"],
        schema_dir=None,
    )
    assert result["committed"] is True
    assert result["target_versions"] == {"sample": "v2"}
    # Version pointer advanced and data committed.
    assert _read_versions(__import__("pathlib").Path(deployment["db"]))["sample"] == "v2"
    assert _kinds(deployment["db"], deployment["reg"]) == ["migrated", "migrated"]


def test_gate_failure_rolls_back_and_leaves_versions(deployment) -> None:
    # Stranded extension field: SampleTag.rank no longer types under v2.
    _seed_tag(deployment["client"], "stranded", rank="not-an-int")
    with pytest.raises(MigrationGateError):
        migrate_bundle(
            {"name": "bb", "packages": {"sample": "v2"}},
            db_path=deployment["db"],
            schema_dir=None,
        )
    # Pointer NOT advanced; data rolled back (no `kind`).
    from pathlib import Path

    assert _read_versions(Path(deployment["db"]))["sample"] == "v1"
    assert _kinds(deployment["db"], deployment["reg"]) == [None, None]


def test_bundle_pins_uninstalled_package_fails_loud(deployment) -> None:
    with pytest.raises(ValueError, match="not installed"):
        migrate_bundle(
            {"name": "bb", "packages": {"ghost": "v2"}},
            db_path=deployment["db"],
            schema_dir=None,
        )
