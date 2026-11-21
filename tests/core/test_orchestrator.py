"""Tests for the dependency-ordered lifecycle orchestrator (PTS-340 / sec11 §11.5).

Covers the S4 acceptance criterion verbatim: *one command migrates a
multi-package deployment to a target bundle, dependency-ordered, validating
end-to-end before commit; an extension with a stranded field is flagged
pre-migration and blocked at the gate.* (The pre-migration *flagging* is the
exposure report — :mod:`test_exposure`; here we prove the gate *blocks* it
and rolls the whole chain back.)

Modelling note: the SQLite adapter is columnar (unknown slots are dropped)
and required slots are NOT NULL, so a "stranded extension field" is modelled
as a persisted *type-invalid* value — a value that was coherent under the
old base structure but no longer types against the v2 merged schema because
the lab supplied no complementary step. The end-to-end gate reads it back
and fails it as a record-level error.
"""

import os
import tempfile

import pytest
import yaml
from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import MigrationGateError, OrchestrationError
from mosaic.core.loaders.bundle import Bundle
from mosaic.core.loaders.domain_module import (
    DomainModule,
    MigrationContext,
    MigrationStep,
)
from mosaic.core.loaders.orchestrator import (
    migrate_to_bundle,
    topological_sort,
)
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap


# ---------------------------------------------------------------------------
# Merged registry: a base `Sample` and an extension `SampleTag`. All slots
# optional so v1-shape rows seed cleanly; the gate's teeth come from a
# type-invalid persisted value (stranded field) or the migration output.
# ---------------------------------------------------------------------------


def _build_registry() -> SchemaRegistry:
    overlay = {
        "id": "https://example.org/mosaic/test_orchestrator",
        "name": "test_orchestrator",
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


class _TestModule(DomainModule):
    """A DomainModule whose v1→v2 step re-shapes its one entity type.

    The transform reads each live record and writes a new-shape record
    (slots carried forward; ``Sample`` also gains ``kind``), superseding the
    old — the canonical read-old → write-new → supersede migration. Records
    its name in ``call_log`` so dependency ordering is observable.
    """

    def __init__(
        self,
        name: str,
        entity_type: str,
        *,
        depends: list[str],
        call_log: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = f"test module {name}"
        self._entity_type = entity_type
        self._depends = list(depends)
        self._call_log = call_log

    def versions(self) -> list[str]:
        return ["v1", "v2", "test"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": self.name, "classes": {}}

    def populates_types(self) -> list[str]:
        return [self._entity_type]

    def depends_on(self) -> list[str]:
        return list(self._depends)

    def _transform(self, ctx: MigrationContext) -> None:
        if self._call_log is not None:
            self._call_log.append(self.name)
        for old in ctx.client.query(self._entity_type).items:
            new = dict(old["data"])
            if self._entity_type == "Sample":
                new["kind"] = "migrated"
            ctx.plan.migrate(self._entity_type, old["id"], new)

    def migration_steps(self) -> list[MigrationStep]:
        return [
            MigrationStep(
                from_version="v1",
                to_version="v2",
                transform=self._transform,
                description=f"{self.name} v1->v2",
            )
        ]


def _make_module(
    name: str,
    entity_type: str,
    *,
    depends: list[str],
    call_log: list[str] | None = None,
) -> _TestModule:
    return _TestModule(name, entity_type, depends=depends, call_log=call_log)


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _build_registry()
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "orchestrator.db"), schema_registry=reg
        )
        yield MosaicClient(storage=storage, registry=reg)


def _seed_sample(client: MosaicClient, n: int) -> None:
    for i in range(n):
        client.put(
            "Sample",
            {"id": f"s{i}", "label": f"label-{i}", "is_available": True},
            bypass_validation=True,
        )


def _seed_tag(client: MosaicClient, wid: str, rank) -> None:
    client.put(
        "SampleTag",
        {"id": wid, "tag": "t", "rank": rank, "is_available": True},
        bypass_validation=True,
    )


def _sample_kinds(client: MosaicClient) -> list:
    return [it["data"].get("kind") for it in client.query("Sample").items]


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_base_before_dependents(self) -> None:
        base = _make_module("sample", "Sample", depends=[])
        ext = _make_module("sampletag", "SampleTag", depends=["sample"])
        ordered = topological_sort([ext, base])  # input order reversed
        assert [p.name for p in ordered] == ["sample", "sampletag"]

    def test_independent_packages_are_alphabetical(self) -> None:
        a = _make_module("zebra", "Sample", depends=[])
        b = _make_module("alpha", "SampleTag", depends=[])
        ordered = topological_sort([a, b])
        assert [p.name for p in ordered] == ["alpha", "zebra"]

    def test_out_of_set_dependency_ignored(self) -> None:
        # depends on a package not in the operation set → ignored for order.
        ext = _make_module("sampletag", "SampleTag", depends=["not-installed"])
        ordered = topological_sort([ext])
        assert [p.name for p in ordered] == ["sampletag"]

    def test_cycle_raises(self) -> None:
        a = _make_module("a", "Sample", depends=["b"])
        b = _make_module("b", "SampleTag", depends=["a"])
        with pytest.raises(OrchestrationError) as exc:
            topological_sort([a, b])
        assert set(exc.value.cycle) == {"a", "b"}


# ---------------------------------------------------------------------------
# Happy path: dependency-ordered multi-package migration commits
# ---------------------------------------------------------------------------


class TestMigrateHappyPath:
    def test_dependency_ordered_and_committed(self, client: MosaicClient) -> None:
        call_log: list[str] = []
        base = _make_module("sample", "Sample", depends=[], call_log=call_log)
        ext = _make_module(
            "sampletag", "SampleTag", depends=["sample"], call_log=call_log
        )
        _seed_sample(client, 3)
        _seed_tag(client, "tg1", rank=1)

        target = Bundle(name="bb", packages={"sample": "v2", "sampletag": "v2"})
        result = migrate_to_bundle(
            client,
            [ext, base],  # unordered input
            target,
            {"sample": "v1", "sampletag": "v1"},
        )

        # Base evolved before the extension (dependency order).
        assert call_log == ["sample", "sampletag"]
        assert result.committed is True
        assert result.target_versions == {"sample": "v2", "sampletag": "v2"}
        # Migration persisted past the staged scope (committed): every Sample
        # carries the migrated `kind`.
        assert _sample_kinds(client) == ["migrated", "migrated", "migrated"]

    def test_no_op_when_already_at_target(self, client: MosaicClient) -> None:
        base = _make_module("sample", "Sample", depends=[])
        _seed_sample(client, 1)
        target = Bundle(name="bb", packages={"sample": "v2"})
        # current already v2 → nothing to evolve, gate still validates state.
        result = migrate_to_bundle(client, [base], target, {"sample": "v2"})
        assert result.committed is True
        assert result.migrations == []


# ---------------------------------------------------------------------------
# The end-to-end gate blocks a stranded extension field and rolls back
# ---------------------------------------------------------------------------


class TestStrandedFieldBlockedAtGate:
    def test_stranded_extension_field_blocks_and_rolls_back(
        self, client: MosaicClient
    ) -> None:
        base = _make_module("sample", "Sample", depends=[])
        ext = _make_module("sampletag", "SampleTag", depends=["sample"])
        _seed_sample(client, 2)
        # Stranded: a SampleTag whose `rank` no longer types under v2 (the lab
        # supplied no complementary step). It persists but fails the gate.
        _seed_tag(client, "stranded", rank="not-an-int")

        # Only the base is upgraded; the extension stays put (no step shipped).
        target = Bundle(name="bb", packages={"sample": "v2"})
        with pytest.raises(MigrationGateError) as exc:
            migrate_to_bundle(
                client,
                [ext, base],
                target,
                {"sample": "v1", "sampletag": "v1"},
            )
        # The gate names a record-level failure on the stranded rank.
        assert any("rank" in e for e in exc.value.errors)

        # Rolled back: the base migration's reshape was undone — every Sample
        # is still its v1 shape (no `kind`), nothing half-committed.
        assert _sample_kinds(client) == [None, None]
        assert len(client.query("Sample").items) == 2

    def test_clean_extension_state_commits(self, client: MosaicClient) -> None:
        base = _make_module("sample", "Sample", depends=[])
        ext = _make_module("sampletag", "SampleTag", depends=["sample"])
        _seed_sample(client, 2)
        _seed_tag(client, "ok", rank=5)  # valid → gate passes
        target = Bundle(name="bb", packages={"sample": "v2"})
        result = migrate_to_bundle(
            client, [ext, base], target, {"sample": "v1", "sampletag": "v1"}
        )
        assert result.committed is True
        assert _sample_kinds(client) == ["migrated", "migrated"]


# ---------------------------------------------------------------------------
# Structural errors
# ---------------------------------------------------------------------------


class TestOrchestrationErrors:
    def test_target_pins_uninstalled_package(self, client: MosaicClient) -> None:
        base = _make_module("sample", "Sample", depends=[])
        target = Bundle(name="bb", packages={"sample": "v2", "ghost": "v2"})
        with pytest.raises(OrchestrationError) as exc:
            migrate_to_bundle(client, [base], target, {"sample": "v1"})
        assert "ghost" in exc.value.missing
