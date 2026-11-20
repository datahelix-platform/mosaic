"""Tests for the ReferenceLoader ABC, LoadResult, FakeReferenceLoader, and
entry-point discovery in cli.commands.reference."""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from typing import Any

import pytest
from pydantic import BaseModel

from mosaic.core.loaders.reference import EntityRef, LoadResult, ReferenceLoader
from mosaic.core.loaders.schema_package import (
    ExternalData,
    MigratableData,
    SchemaPackage,
)
from mosaic.testing.example_ontology_loader import OboDemoLoader, OboDemoParams


class _StubClient:
    """Tiny stand-in for MosaicClient used by loader unit tests.

    Returns a deterministic synthetic UUID for each ``put`` so
    ``LoadResult.entities`` is populated without spinning up a real
    storage adapter.
    """

    def __init__(self) -> None:
        self._counter = 0
        self.puts: list[tuple[str, dict]] = []

    def put(self, entity_type: str, data: dict) -> dict:
        self._counter += 1
        entity_id = f"stub-{entity_type}-{self._counter:03d}"
        self.puts.append((entity_type, dict(data)))
        return {"id": entity_id, "entity_type": entity_type, "data": data}


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------


class TestReferenceLoaderABC:
    """ReferenceLoader cannot be instantiated directly or via partial subclass."""

    def test_cannot_instantiate_abc_directly(self):
        with pytest.raises(TypeError):
            ReferenceLoader()  # type: ignore[abstract]

    def test_partial_subclass_missing_versions_fails(self):
        class MissingVersions(ReferenceLoader):
            name = "x"
            description = ""

            def populates_types(self) -> list[str]:
                return []

            def schema_fragment(self) -> dict:
                return {}

            def load(self, client, version, params=None):
                return LoadResult()

        with pytest.raises(TypeError):
            MissingVersions()  # type: ignore[abstract]

    def test_partial_subclass_missing_load_fails(self):
        class MissingLoad(ReferenceLoader):
            name = "x"
            description = ""

            def versions(self) -> list[str]:
                return []

            def populates_types(self) -> list[str]:
                return []

            def schema_fragment(self) -> dict:
                return {}

        with pytest.raises(TypeError):
            MissingLoad()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self):
        class Complete(ReferenceLoader):
            name = "complete"
            description = ""

            def versions(self) -> list[str]:
                return ["test"]

            def populates_types(self) -> list[str]:
                return ["Foo"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "complete"}

            def load(self, client, version, params=None) -> LoadResult:
                return LoadResult(created=1)

        loader = Complete()
        assert loader.versions() == ["test"]
        assert loader.populates_types() == ["Foo"]


# ---------------------------------------------------------------------------
# Default method behavior
# ---------------------------------------------------------------------------


class _MinimalLoader(ReferenceLoader):
    name = "minimal"
    description = "minimal loader for testing defaults"

    def __init__(self):
        self.load_calls: list[tuple[Any, str, Any]] = []

    def versions(self) -> list[str]:
        return ["v1", "v2", "test"]

    def populates_types(self) -> list[str]:
        return ["Thing"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": "minimal"}

    def load(self, client, version, params=None) -> LoadResult:
        self.load_calls.append((client, version, params))
        return LoadResult(created=1)


class TestDefaultMethods:
    def test_default_validate_raises_with_loader_name_in_message(self):
        loader = _MinimalLoader()
        with pytest.raises(NotImplementedError) as exc:
            loader.validate(user_artifact=object())
        assert "minimal" in str(exc.value)
        assert "validate" in str(exc.value)

    def test_default_upgrade_delegates_to_load_with_to_version_and_params(self):
        loader = _MinimalLoader()
        sentinel_client = object()
        sentinel_params = object()

        result = loader.upgrade(
            sentinel_client, "v1", "v2", params=sentinel_params  # type: ignore[arg-type]
        )

        assert isinstance(result, LoadResult)
        assert loader.load_calls == [(sentinel_client, "v2", sentinel_params)]


# ---------------------------------------------------------------------------
# LoadResult shape
# ---------------------------------------------------------------------------


class TestEntityRef:
    """v2 ``EntityRef`` handle (spec §2.14.8)."""

    def test_is_frozen(self):
        ref = EntityRef(id="abc", type="FakeTerm")
        with pytest.raises(Exception):
            ref.id = "xyz"  # type: ignore[misc]

    def test_equality_and_hashable(self):
        a = EntityRef(id="x", type="T")
        b = EntityRef(id="x", type="T")
        c = EntityRef(id="y", type="T")
        assert a == b
        assert hash(a) == hash(b)
        assert a != c

    def test_from_put_result_uses_id_and_entity_type(self):
        put_dict = {
            "id": "stub-FakeTerm-001",
            "entity_type": "FakeTerm",
            "data": {"label": "alpha"},
        }
        ref = EntityRef.from_put_result(put_dict)
        assert ref == EntityRef(id="stub-FakeTerm-001", type="FakeTerm")

    def test_from_put_result_missing_keys_raises(self):
        with pytest.raises(KeyError):
            EntityRef.from_put_result({"id": "x"})
        with pytest.raises(KeyError):
            EntityRef.from_put_result({"entity_type": "T"})


class TestLoadResult:
    def test_defaults_are_zero_and_empty(self):
        r = LoadResult()
        assert r.created == 0
        assert r.updated == 0
        assert r.unchanged == 0
        assert r.errors == 0
        assert r.error_messages == []
        assert r.entities == []

    def test_constructable_with_entities_and_counters(self):
        refs = [
            EntityRef(id="a", type="FakeTerm"),
            EntityRef(id="b", type="FakeTerm"),
        ]
        r = LoadResult(created=2, entities=refs)
        assert r.created == 2
        assert r.entities == refs
        # Mixed-class loaders use a heterogeneous list — D2.14.K-3.
        mixed = LoadResult(
            created=2,
            entities=[
                EntityRef(id="g1", type="Gene"),
                EntityRef(id="t1", type="Transcript"),
            ],
        )
        assert {e.type for e in mixed.entities} == {"Gene", "Transcript"}

    def test_error_messages_are_isolated_per_instance(self):
        a = LoadResult()
        b = LoadResult()
        a.error_messages.append("boom")
        assert b.error_messages == []

    def test_entities_are_isolated_per_instance(self):
        a = LoadResult()
        b = LoadResult()
        a.entities.append(EntityRef(id="x", type="T"))
        assert b.entities == []


# ---------------------------------------------------------------------------
# FakeReferenceLoader (test fixture)
# ---------------------------------------------------------------------------


class TestFakeReferenceLoader:
    def test_fake_loader_is_a_reference_loader(self):
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        loader = FakeReferenceLoader()
        assert isinstance(loader, ReferenceLoader)
        assert loader.name == "fake"
        assert "test" in loader.versions()
        assert loader.populates_types() == ["FakeTerm"]
        assert loader.schema_fragment()["default_prefix"] == "fake"

    def test_fake_loader_load_returns_loadresult(self):
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        loader = FakeReferenceLoader()
        # ``load()`` calls ``client.put()`` on each row (PTS-229 made the
        # fake actually persist so install/upgrade tests have rows to
        # query/prune). A small stub is sufficient for this unit-level
        # contract check — the integration test in
        # tests/cli/test_reference_install_upgrade.py covers the real
        # client path end-to-end.
        client = _StubClient()
        result = loader.load(client=client, version="test")
        assert isinstance(result, LoadResult)
        assert result.created == 2
        assert len(result.entities) == 2
        assert all(isinstance(e, EntityRef) for e in result.entities)
        assert all(e.type == "FakeTerm" for e in result.entities)

    def test_fake_loader_unknown_version_returns_error(self):
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        loader = FakeReferenceLoader()
        result = loader.load(client=None, version="nonexistent")
        assert result.errors == 1
        assert result.error_messages == ["unknown version: nonexistent"]

    def test_fake_loader_declares_pydantic_params_schema(self):
        from mosaic.testing.fake_reference_loader import (
            FakeLoadParams,
            FakeReferenceLoader,
        )

        assert FakeReferenceLoader.load_params_schema is FakeLoadParams
        assert issubclass(FakeLoadParams, BaseModel)


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint used in tests."""

    def __init__(self, name: str, value: str, target: Any):
        self.name = name
        self.value = value
        self._target = target

    def load(self) -> Any:
        return self._target


class _FakeEntryPoints:
    def __init__(self, eps: list[_FakeEntryPoint]):
        self._eps = eps

    def select(self, *, group: str) -> list[_FakeEntryPoint]:
        return list(self._eps)


class TestDiscoverReferenceLoaders:
    def _patch_entry_points(self, monkeypatch, eps: list[_FakeEntryPoint]) -> None:
        import importlib.metadata as md

        monkeypatch.setattr(md, "entry_points", lambda: _FakeEntryPoints(eps))

    def test_discovery_returns_instance_for_valid_loader(self, monkeypatch):
        from mosaic.cli.commands import reference as ref_cmd
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        self._patch_entry_points(
            monkeypatch,
            [
                _FakeEntryPoint(
                    "fake",
                    "mosaic.testing.fake_reference_loader:FakeReferenceLoader",
                    FakeReferenceLoader,
                )
            ],
        )

        loaders = ref_cmd.discover_reference_loaders()

        assert len(loaders) == 1
        entry = loaders[0]
        assert entry["name"] == "fake"
        assert entry["class"] == "FakeReferenceLoader"
        assert isinstance(entry["instance"], FakeReferenceLoader)
        assert isinstance(entry["instance"], ReferenceLoader)

    def test_discovery_raises_clear_error_for_non_subclass(self, monkeypatch):
        from mosaic.cli.commands import reference as ref_cmd

        class NotALoader:
            pass

        self._patch_entry_points(
            monkeypatch,
            [
                _FakeEntryPoint(
                    "bogus",
                    "some.module:NotALoader",
                    NotALoader,
                )
            ],
        )

        with pytest.raises(ref_cmd.ReferenceLoaderRegistrationError) as exc:
            ref_cmd.discover_reference_loaders()

        msg = str(exc.value)
        assert "bogus" in msg
        assert "ReferenceLoader" in msg

    def test_discovery_raises_clear_error_for_non_class_object(self, monkeypatch):
        from mosaic.cli.commands import reference as ref_cmd

        self._patch_entry_points(
            monkeypatch,
            [
                _FakeEntryPoint("instance_ep", "some.module:instance", object())
            ],
        )

        with pytest.raises(ref_cmd.ReferenceLoaderRegistrationError):
            ref_cmd.discover_reference_loaders()


# ---------------------------------------------------------------------------
# SchemaPackage genus (Doc 2 §2A / PTS-335 S0)
# ---------------------------------------------------------------------------


class TestSchemaPackageGenus:
    """The genus ABC: abstract ``versions``/``schema_fragment``; concrete
    ``depends_on``/``validate`` + no-op lifecycle hooks."""

    def test_cannot_instantiate_genus_directly(self):
        with pytest.raises(TypeError):
            SchemaPackage()  # type: ignore[abstract]

    def test_missing_versions_fails(self):
        class P(SchemaPackage):
            name = "p"
            description = ""

            def schema_fragment(self) -> dict:
                return {"default_prefix": "p"}

        with pytest.raises(TypeError):
            P()  # type: ignore[abstract]

    def test_missing_schema_fragment_fails(self):
        class P(SchemaPackage):
            name = "p"
            description = ""

            def versions(self) -> list[str]:
                return ["v1"]

        with pytest.raises(TypeError):
            P()  # type: ignore[abstract]

    def test_minimal_pure_schema_instantiates_with_defaults(self):
        class P(SchemaPackage):
            name = "p"
            description = ""

            def versions(self) -> list[str]:
                return ["v1"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "p"}

        p = P()
        # depends_on default: no dependencies.
        assert p.depends_on() == []
        # All three lifecycle hooks default to a no-op (return None).
        assert p.provision(client=None, version="v1") is None
        assert (
            p.evolve(client=None, from_version="v1", to_version="v2") is None
        )
        assert p.deprovision(client=None, version="v1") is None
        # validate() is optional; the default raises with the package name.
        with pytest.raises(NotImplementedError) as exc:
            p.validate(object())
        assert "p" in str(exc.value)

    def test_load_params_schema_defaults_none(self):
        class P(SchemaPackage):
            name = "p"
            description = ""

            def versions(self) -> list[str]:
                return ["v1"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "p"}

        assert P.load_params_schema is None


class TestPureSchemaPackage:
    """``FakeSchemaPackage`` — a pure-schema package with no data hooks."""

    def test_is_schema_package_not_reference_loader(self):
        from mosaic.testing.fake_reference_loader import FakeSchemaPackage

        pkg = FakeSchemaPackage()
        assert isinstance(pkg, SchemaPackage)
        assert not isinstance(pkg, ReferenceLoader)
        assert pkg.name == "fake_schema"
        assert pkg.schema_fragment()["default_prefix"] == "fake_schema"

    def test_lifecycle_hooks_are_noop(self):
        from mosaic.testing.fake_reference_loader import FakeSchemaPackage

        pkg = FakeSchemaPackage()
        assert pkg.provision(client=None, version="v1") is None
        assert (
            pkg.evolve(client=None, from_version="v1", to_version="v2")
            is None
        )
        assert pkg.deprovision(client=None, version="v1") is None

    def test_omits_populates_types(self):
        # ``populates_types`` is a *species* concern; a pure-schema package
        # does not declare it (Doc 2 §2A).
        from mosaic.testing.fake_reference_loader import FakeSchemaPackage

        assert not hasattr(FakeSchemaPackage(), "populates_types")

    def test_fragment_merges_into_deployed_schema(self):
        # Acceptance (§9 S0): a pure-schema SchemaPackage with no hooks
        # *merges*. Prove it at the merge engine directly — no install,
        # no entry-point reinstall dependency.
        import importlib.resources

        from linkml_runtime.utils.schemaview import SchemaView

        from mosaic.linkml_bridge import (
            LoaderFragmentSpec,
            merge_loader_fragment,
        )
        from mosaic.testing.fake_reference_loader import FakeSchemaPackage

        mosaic_core = importlib.resources.files("mosaic.schemas").joinpath(
            "mosaic_core.yaml"
        )
        deployed_sv = SchemaView(str(mosaic_core))

        pkg = FakeSchemaPackage()
        spec = LoaderFragmentSpec(
            loader_name=pkg.name,
            package_name="mosaic-schema-fake",
            package_version="1.0.0",
            fragment=pkg.schema_fragment(),
        )
        merged = merge_loader_fragment(deployed_sv, spec)
        assert "FakeSchemaTerm" in merged.all_classes()


class TestReferenceLoaderReparenting:
    """``ReferenceLoader`` re-parented on ``SchemaPackage`` with hooks
    mapped onto the historical method names."""

    def test_reference_loader_is_schema_package(self):
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        assert issubclass(ReferenceLoader, SchemaPackage)
        assert isinstance(FakeReferenceLoader(), SchemaPackage)

    def test_provision_delegates_to_load(self):
        loader = _MinimalLoader()
        client = object()
        result = loader.provision(client, "v1")  # type: ignore[arg-type]
        assert isinstance(result, LoadResult)
        assert loader.load_calls == [(client, "v1", None)]

    def test_evolve_delegates_through_upgrade_to_load(self):
        loader = _MinimalLoader()
        client = object()
        params = object()
        result = loader.evolve(client, "v1", "v2", params)  # type: ignore[arg-type]
        assert isinstance(result, LoadResult)
        # evolve → upgrade (default) → load(to_version, params).
        assert loader.load_calls == [(client, "v2", params)]


class TestCapabilityProtocols:
    """``ExternalData`` / ``MigratableData`` runtime-checkable dispatch."""

    def test_reference_loader_satisfies_external_data(self):
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        assert isinstance(FakeReferenceLoader(), ExternalData)

    def test_pure_schema_is_not_external_data(self):
        from mosaic.testing.fake_reference_loader import FakeSchemaPackage

        assert not isinstance(FakeSchemaPackage(), ExternalData)

    def test_no_package_is_migratable_in_s0(self):
        # MigratableData keys on ``migration_steps()`` — which neither the
        # genus nor ReferenceLoader define. Critically, the genus's no-op
        # ``evolve`` must NOT make every package look Migratable.
        from mosaic.testing.fake_reference_loader import (
            FakeReferenceLoader,
            FakeSchemaPackage,
        )

        assert not isinstance(FakeReferenceLoader(), MigratableData)
        assert not isinstance(FakeSchemaPackage(), MigratableData)

    def test_migration_steps_shape_satisfies_migratable_data(self):
        # A package exposing ``migration_steps`` (the shape DomainModule
        # will provide in S2/S3) satisfies the protocol — the forward
        # contract is real, not vacuous.
        class _Migratable(SchemaPackage):
            name = "m"
            description = ""

            def versions(self) -> list[str]:
                return ["v1"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "m"}

            def migration_steps(self) -> list:
                return []

        assert isinstance(_Migratable(), MigratableData)


class TestEntityTypesBackCompat:
    """``entity_types()`` lives on as a deprecated alias so loaders written
    against the pre-SchemaPackage ABC install unchanged."""

    def test_legacy_entity_types_flows_through_populates_types(self):
        class LegacyLoader(ReferenceLoader):
            name = "legacy"
            description = ""

            def versions(self) -> list[str]:
                return ["v1"]

            def entity_types(self) -> list[str]:
                return ["LegacyTerm"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "legacy"}

            def load(self, client, version, params=None) -> LoadResult:
                return LoadResult()

        loader = LegacyLoader()
        assert loader.entity_types() == ["LegacyTerm"]
        # The new name reports the legacy-declared types unchanged.
        assert loader.populates_types() == ["LegacyTerm"]

    def test_new_loader_overrides_populates_types_directly(self):
        class NewLoader(ReferenceLoader):
            name = "new"
            description = ""

            def versions(self) -> list[str]:
                return ["v1"]

            def populates_types(self) -> list[str]:
                return ["NewTerm"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "new"}

            def load(self, client, version, params=None) -> LoadResult:
                return LoadResult()

        loader = NewLoader()
        assert loader.populates_types() == ["NewTerm"]
        # The deprecated alias returns its empty default when unused.
        assert loader.entity_types() == []


# ---------------------------------------------------------------------------
# discover_schema_packages — both groups resolve + dedup + error contract
# ---------------------------------------------------------------------------


class _GroupedFakeEntryPoints:
    """Group-aware stand-in: returns only the EPs registered for the asked
    group (unlike ``_FakeEntryPoints``, which ignores ``group``)."""

    def __init__(self, by_group: dict[str, list[_FakeEntryPoint]]):
        self._by_group = by_group

    def select(self, *, group: str) -> list[_FakeEntryPoint]:
        return list(self._by_group.get(group, []))


class TestDiscoverSchemaPackages:
    def _patch(self, monkeypatch, by_group: dict[str, list[_FakeEntryPoint]]) -> None:
        import importlib.metadata as md

        monkeypatch.setattr(
            md, "entry_points", lambda: _GroupedFakeEntryPoints(by_group)
        )

    def test_resolves_both_groups_and_dedups_by_name(self, monkeypatch):
        from mosaic.cli.commands import reference as ref_cmd
        from mosaic.testing.fake_reference_loader import (
            FakeReferenceLoader,
            FakeSchemaPackage,
        )

        fake_ep = _FakeEntryPoint(
            "fake",
            "mosaic.testing.fake_reference_loader:FakeReferenceLoader",
            FakeReferenceLoader,
        )
        schema_ep = _FakeEntryPoint(
            "fake_schema",
            "mosaic.testing.fake_reference_loader:FakeSchemaPackage",
            FakeSchemaPackage,
        )
        # ``fake`` is registered under reference_loaders AND aliased into
        # schema_packages — discovery must collapse it to one entry.
        self._patch(
            monkeypatch,
            {
                ref_cmd.SCHEMA_PACKAGES_GROUP: [schema_ep, fake_ep],
                ref_cmd.REFERENCE_LOADERS_GROUP: [fake_ep],
            },
        )

        pkgs = ref_cmd.discover_schema_packages()
        names = [p["name"] for p in pkgs]
        assert names.count("fake") == 1
        assert set(names) == {"fake_schema", "fake"}

    def test_reference_loaders_only_package_resolves(self, monkeypatch):
        # A package present only in the reference_loaders subset/alias is
        # still discovered by the genus-level discovery.
        from mosaic.cli.commands import reference as ref_cmd
        from mosaic.testing.fake_reference_loader import FakeReferenceLoader

        fake_ep = _FakeEntryPoint(
            "fake",
            "mosaic.testing.fake_reference_loader:FakeReferenceLoader",
            FakeReferenceLoader,
        )
        self._patch(
            monkeypatch,
            {
                ref_cmd.SCHEMA_PACKAGES_GROUP: [],
                ref_cmd.REFERENCE_LOADERS_GROUP: [fake_ep],
            },
        )
        names = [p["name"] for p in ref_cmd.discover_schema_packages()]
        assert names == ["fake"]

    def test_non_subclass_raises_schema_package_error(self, monkeypatch):
        from mosaic.cli.commands import reference as ref_cmd

        class NotAPackage:
            pass

        self._patch(
            monkeypatch,
            {
                ref_cmd.SCHEMA_PACKAGES_GROUP: [
                    _FakeEntryPoint("bogus", "m:NotAPackage", NotAPackage)
                ],
                ref_cmd.REFERENCE_LOADERS_GROUP: [],
            },
        )
        with pytest.raises(ref_cmd.SchemaPackageRegistrationError) as exc:
            ref_cmd.discover_schema_packages()
        msg = str(exc.value)
        assert "bogus" in msg
        assert "SchemaPackage" in msg

    def test_reference_error_is_schema_package_error_subclass(self):
        from mosaic.cli.commands import reference as ref_cmd

        assert issubclass(
            ref_cmd.ReferenceLoaderRegistrationError,
            ref_cmd.SchemaPackageRegistrationError,
        )


class TestPureSchemaPackageInstall:
    """End-to-end acceptance (§9 S0): a pure-schema ``SchemaPackage`` with
    **no hooks** installs through the same lifecycle as a reference loader —
    its fragment merges (the class table is created) and its version is
    recorded — without any hand-written ``load()``/``provision()``.

    Entry points are monkeypatched so the test does not depend on a fresh
    editable reinstall picking up the ``mosaic.schema_packages`` group.
    """

    def test_install_pure_schema_package_merges_and_records_version(
        self, monkeypatch, tmp_path
    ):
        import sqlite3

        from mosaic.cli.commands import reference as ref_cmd
        from mosaic.core.meta import get_meta
        from mosaic.testing.fake_reference_loader import FakeSchemaPackage

        import importlib.metadata as md

        monkeypatch.setattr(
            md,
            "entry_points",
            lambda: _FakeEntryPoints(
                [
                    _FakeEntryPoint(
                        "fake_schema",
                        "mosaic.testing.fake_reference_loader:FakeSchemaPackage",
                        FakeSchemaPackage,
                    )
                ]
            ),
        )

        db_path = tmp_path / "hippo.db"
        schema_dir = tmp_path / "schemas"
        schema_dir.mkdir()

        result = ref_cmd.install_reference(
            "fake_schema", "v1", db_path=db_path, schema_dir=schema_dir
        )

        # No data hooks ran: nothing created, advisory list empty.
        assert result["status"] == "installed"
        assert result["version"] == "v1"
        assert result["created"] == 0
        assert result["entities"] == []

        # The fragment merged: the package's class table now exists.
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='FakeSchemaTerm'"
            )
            assert cursor.fetchone() is not None
            versions = get_meta(conn, ref_cmd.META_KEY_VERSIONS)
        finally:
            conn.close()

        # The version is pinned in mosaic_meta (the requires: source of truth).
        assert versions == {"fake_schema": "v1"}


# ---------------------------------------------------------------------------
# OboDemoLoader — a realistic (non-Fake) reference-data species exercising the
# full external-data path: cached_fetch + sha256, diff-based upgrade, the
# network-free "test" fixture (Doc 2 §1/§2A/§4, sec2 §2.14; PTS-337 S1).
# The full install/dry-run/upgrade/prune acceptance lives in
# tests/cli/test_reference_install_upgrade.py (needs a merged client); these
# are the SDK-level unit checks that need no storage.
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Pin ``$MOSAIC_CACHE_DIR`` to a tmp path so ``cached_fetch`` never
    touches the developer's real ``~/.cache/mosaic``."""
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("MOSAIC_CACHE_DIR", str(cache_root))
    return cache_root


class _FetchForbiddenStubClient(_StubClient):
    """Stub client whose ``cached_fetch`` fails loudly.

    Proves the ``"test"`` pseudo-version is genuinely network-free: if its
    code path ever reaches ``cached_fetch`` this raises instead of hanging
    on (or silently mocking) a download.
    """

    def cached_fetch(self, *args: Any, **kwargs: Any):  # noqa: D401
        raise AssertionError(
            "cached_fetch must not be called for the 'test' pseudo-version"
        )


class TestOboDemoLoaderSurface:
    """Identity, ABC conformance, and declared schema surface."""

    def test_is_reference_loader_with_expected_surface(self):
        loader = OboDemoLoader()
        assert isinstance(loader, ReferenceLoader)
        assert isinstance(loader, SchemaPackage)
        assert loader.name == "obodemo"
        # "test" is the reserved network-free pseudo-version (sec2 §2.14.7).
        assert "test" in loader.versions()
        assert loader.populates_types() == ["OntologyTerm"]
        frag = loader.schema_fragment()
        # default_prefix MUST equal name (sec2 §2.14.5 Rule 1).
        assert frag["default_prefix"] == "obodemo"
        attrs = frag["classes"]["OntologyTerm"]["attributes"]
        assert set(attrs) == {"curie", "label", "definition"}
        # The source CURIE and label are required; definition is optional.
        assert attrs["curie"]["required"] is True
        assert attrs["label"]["required"] is True
        assert "required" not in attrs["definition"]

    def test_declares_pydantic_params_schema(self):
        assert OboDemoLoader.load_params_schema is OboDemoParams
        assert issubclass(OboDemoParams, BaseModel)
        # Both fields must be CLI-renderable (str | None and bool are
        # supported); discovery validates this in CI, asserted here too.
        from mosaic.cli.commands.reference import _validate_load_params_schema

        _validate_load_params_schema("obodemo", OboDemoParams)

    def test_external_data_but_not_migratable(self):
        # A ReferenceLoader is external/reconstructible (ExternalData); it is
        # NOT a MigratableData package — that capability belongs to
        # DomainModule (Doc 2 §2A capability protocols).
        loader = OboDemoLoader()
        assert isinstance(loader, ExternalData)
        assert not isinstance(loader, MigratableData)


class TestOboDemoParsing:
    """Pure parse / diff units — no client, no network."""

    def test_parse_obo_extracts_curie_label_definition(self):
        loader = OboDemoLoader()
        terms = loader._parse_obo(loader._fixtures_dir() / "obodemo-v1.obo")
        assert [t["curie"] for t in terms] == [
            "OBO:0000001",
            "OBO:0000002",
            "OBO:0000003",
            "OBO:0000004",
        ]
        assert all("label" in t and "definition" in t for t in terms)
        # def: "text" [xrefs] — only the quoted text survives.
        assert "[" not in terms[0]["definition"]

    def test_apply_diff_adds_changes_and_obsoletes(self):
        loader = OboDemoLoader()
        base = loader._parse_obo(loader._fixtures_dir() / "obodemo-v1.obo")
        diff = json.loads(
            (loader._fixtures_dir() / "obodemo-v2.diff.json").read_text()
        )
        v2 = loader._apply_diff(base, diff)
        curies = [t["curie"] for t in v2]
        assert "OBO:0000005" in curies  # added
        assert "OBO:0000003" not in curies  # obsoleted
        changed = next(t for t in v2 if t["curie"] == "OBO:0000002")
        assert "revised" in changed["definition"]  # changed
        # Carried-forward terms keep their v1 content untouched.
        carried = next(t for t in v2 if t["curie"] == "OBO:0000001")
        assert carried["label"] == "cellular process"


class TestOboDemoFetch:
    """``cached_fetch`` integration: content-addressing + sha256 gate."""

    def test_fetch_is_content_addressed_and_cache_hits(self, isolated_cache):
        from mosaic.core.client import MosaicClient

        loader = OboDemoLoader()
        client = MosaicClient()
        first = loader._fetch(client, OboDemoParams(), "v1")
        second = loader._fetch(client, OboDemoParams(), "v1")
        # Stable, content-addressed path; bytes match the bundled release.
        assert first == second
        assert (
            first.read_bytes()
            == (loader._fixtures_dir() / "obodemo-v1.obo").read_bytes()
        )

    def test_fetch_sha256_mismatch_raises_cache_integrity_error(
        self, isolated_cache, tmp_path
    ):
        from mosaic.core.client import MosaicClient
        from mosaic.core.exceptions import CacheIntegrityError

        loader = OboDemoLoader()
        # A tampered origin whose bytes do not match the pinned manifest
        # sha256 — the integrity gate must fire (sec2 §2.14.3).
        bad_origin = tmp_path / "origin"
        bad_origin.mkdir()
        (bad_origin / "obodemo-v1.obo").write_text("tampered\n", encoding="utf-8")

        client = MosaicClient()
        with pytest.raises(CacheIntegrityError):
            loader._fetch(
                client, OboDemoParams(base_url=bad_origin.as_uri()), "v1"
            )


class TestOboDemoLoadUnit:
    """``load()`` behaviour exercised with a put-recording stub client."""

    def test_load_test_version_is_network_free(self):
        loader = OboDemoLoader()
        client = _FetchForbiddenStubClient()
        result = loader.load(client, "test")
        assert result.errors == 0
        assert result.created == 2
        assert all(e.type == "OntologyTerm" for e in result.entities)
        # Two terms persisted; cached_fetch was never reached.
        assert len(client.puts) == 2
        assert {data["curie"] for _, data in client.puts} == {
            "OBO:0000001",
            "OBO:0000002",
        }

    def test_load_unknown_version_returns_error(self):
        result = OboDemoLoader().load(_StubClient(), "v99")
        assert result.errors == 1
        assert "unknown version" in result.error_messages[0]
