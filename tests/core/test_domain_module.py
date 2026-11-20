"""Tests for ``DomainModule`` + single-hop ``evolve`` (PTS-338 / sec11 §11.2.4).

Covers the S2 acceptance criterion verbatim: *a v1→v2 domain migration
runs with full provenance and passes the gate.* The migration model is
read-old → write-new → supersede-old, fronted by a staged dry-run
validation gate that must block every committed write when the transform
output does not validate against the merged schema.
"""

import os
import tempfile

import pytest
import yaml
from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import (
    DeprovisionRefusedError,
    EntityNotFoundError,
    MigrationFloorError,
    MigrationGateError,
    MigrationStepNotFoundError,
)
from mosaic.core.loaders.domain_module import (
    DomainModule,
    MigrationContext,
    MigrationStep,
)
from mosaic.core.loaders.reference import LoadResult, ReferenceLoader
from mosaic.core.loaders.schema_package import MigratableData, SchemaPackage
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap


# ---------------------------------------------------------------------------
# Fixtures: a tiny user-domain schema with a `Widget` class. All domain
# slots are optional so v1-shape records (missing v2 slots) seed cleanly;
# the gate's teeth are exercised via type / unknown-slot violations.
# ---------------------------------------------------------------------------


def _build_widget_registry() -> SchemaRegistry:
    overlay = {
        "id": "https://example.org/mosaic/test_domain_module",
        "name": "test_domain_module",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "mosaic": "https://w3id.org/datahelix/mosaic/",
        },
        "imports": ["linkml:types", "mosaic_core"],
        "default_range": "string",
        "classes": {
            "Widget": {
                "is_a": "Entity",
                "attributes": {
                    "label": {"range": "string"},
                    "kind": {"range": "string"},
                    "count": {"range": "integer"},
                },
            }
        },
    }
    return SchemaRegistry(
        SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
    )


class _WidgetModule(DomainModule):
    """Test DomainModule whose single v1→v2 step is injected at construction."""

    name = "widget"
    description = "Test first-party domain module (PTS-338)"

    def __init__(self, transform, *, duplicate: bool = False) -> None:
        self._transform = transform
        self._duplicate = duplicate

    def versions(self) -> list[str]:
        return ["v1", "v2", "test"]

    def schema_fragment(self) -> dict:
        # Not merged in these tests (the client's registry is built
        # directly); a minimal fragment satisfies the genus contract.
        return {"default_prefix": "widget", "classes": {}}

    def populates_types(self) -> list[str]:
        return ["Widget"]

    def migration_steps(self) -> list[MigrationStep]:
        step = MigrationStep(
            from_version="v1",
            to_version="v2",
            transform=self._transform,
            description="seed kind from label",
        )
        if self._duplicate:
            return [step, step]
        return [step]


def _v1_to_v2_set_kind(ctx: MigrationContext) -> None:
    """Happy transform: each v1 Widget becomes a new v2 Widget with `kind`."""
    for old in ctx.client.query("Widget").items:
        data = old["data"]
        ctx.plan.migrate(
            "Widget",
            old["id"],
            {"label": data.get("label"), "kind": "migrated"},
        )


def _v1_to_v2_bad_type(ctx: MigrationContext) -> None:
    """Invalid transform: stages a record with a non-integer `count`."""
    for old in ctx.client.query("Widget").items:
        ctx.plan.migrate(
            "Widget",
            old["id"],
            {"label": old["data"].get("label"), "count": "not-an-integer"},
        )


def _v1_to_v2_unknown_class(ctx: MigrationContext) -> None:
    """Invalid transform: stages a record for a class absent from the schema."""
    for old in ctx.client.query("Widget").items:
        ctx.plan.migrate("Gadget", old["id"], {"label": "x"})


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_domain_module.db")


@pytest.fixture
def registry() -> SchemaRegistry:
    return _build_widget_registry()


@pytest.fixture
def client(db_path: str, registry: SchemaRegistry) -> MosaicClient:
    storage = SQLiteAdapter(db_path, schema_registry=registry)
    return MosaicClient(storage=storage, registry=registry)


def _seed_v1(client: MosaicClient, n: int) -> list[str]:
    """Seed `n` v1-shape Widget records (label only); return their ids."""
    ids = []
    for i in range(n):
        wid = f"w{i}"
        client.put(
            "Widget",
            {"id": wid, "label": f"label-{i}", "is_available": True},
            bypass_validation=True,
        )
        ids.append(wid)
    return ids


# ---------------------------------------------------------------------------
# Genus / species contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_is_schema_package_subclass(self) -> None:
        assert issubclass(DomainModule, SchemaPackage)

    def test_instance_satisfies_migratable_data_protocol(self) -> None:
        module = _WidgetModule(_v1_to_v2_set_kind)
        assert isinstance(module, MigratableData)

    def test_reference_loader_does_not_satisfy_migratable_data(self) -> None:
        """The protocol keys on ``migration_steps`` — only the migration
        species matches it, so the orchestrator can dispatch by isinstance."""

        class _Ref(ReferenceLoader):
            name = "ref"
            description = "x"

            def versions(self) -> list[str]:
                return ["1"]

            def schema_fragment(self) -> dict:
                return {"default_prefix": "ref"}

            def load(self, client, version, params=None) -> LoadResult:
                return LoadResult()

        assert not isinstance(_Ref(), MigratableData)

    def test_provision_is_genus_noop(self, client: MosaicClient) -> None:
        module = _WidgetModule(_v1_to_v2_set_kind)
        assert module.provision(client, "v2") is None

    def test_deprovision_noop_when_no_live_data(self, client: MosaicClient) -> None:
        # With no live rows in any populates_types class there is nothing
        # to retire, so deprovision returns quietly (S3 refuse-by-default
        # only fires when the module owns live data — see TestDeprovision).
        module = _WidgetModule(_v1_to_v2_set_kind)
        assert module.deprovision(client, "v2") is None


# ---------------------------------------------------------------------------
# Single-hop evolve — happy path with full provenance
# ---------------------------------------------------------------------------


class TestSingleHopEvolve:
    def test_migrates_v1_to_v2_writing_new_superseding_old(
        self, client: MosaicClient
    ) -> None:
        old_ids = _seed_v1(client, 3)
        module = _WidgetModule(_v1_to_v2_set_kind)

        result = module.evolve(client, "v1", "v2")

        assert isinstance(result, LoadResult)
        assert result.created == 3
        assert len(result.entities) == 3

        # New records exist in v2 shape (kind set, available).
        for ref in result.entities:
            assert ref.type == "Widget"
            new = client.get("Widget", ref.id)
            assert new["data"]["kind"] == "migrated"

        # Old records are superseded: unavailable + superseded_by set.
        for old_id in old_ids:
            with pytest.raises(EntityNotFoundError):
                client.get("Widget", old_id)
            superseded = client.get(
                "Widget", old_id, include_unavailable=True
            )
            assert superseded["superseded_by"] is not None

    def test_writes_supersede_provenance_tagged_as_migration(
        self, client: MosaicClient
    ) -> None:
        _seed_v1(client, 1)
        module = _WidgetModule(_v1_to_v2_set_kind)

        module.evolve(client, "v1", "v2")

        history = client.history("w0")
        supersede_events = [
            e for e in history if e["operation_type"] == "supersede"
        ]
        assert len(supersede_events) == 1
        event = supersede_events[0]
        # actor = module name; reason names the hop ⇒ reads as a migration.
        assert event["user_id"] == "widget"
        assert event["state_snapshot"]["reason"] == "widget migration v1→v2"

    def test_new_record_has_create_event_and_superseded_by_edge(
        self, client: MosaicClient
    ) -> None:
        _seed_v1(client, 1)
        module = _WidgetModule(_v1_to_v2_set_kind)

        result = module.evolve(client, "v1", "v2")
        new_id = result.entities[0].id

        new_history = client.history(new_id)
        assert any(e["operation_type"] == "create" for e in new_history)

        # superseded_by relationship edge old → new.
        storage = client._storage
        with storage._transaction() as conn:
            rel_store = storage._get_relationship_store(conn)
            rels = list(
                rel_store.find(
                    source_id="w0",
                    target_id=new_id,
                    relationship_type="superseded_by",
                )
            )
        assert len(rels) == 1

    def test_migrates_all_records_not_just_first_page(
        self, client: MosaicClient
    ) -> None:
        # client.query applies no default page limit; guard that evolve
        # migrates the whole set, not page one (advisor caution).
        old_ids = _seed_v1(client, 25)
        module = _WidgetModule(_v1_to_v2_set_kind)

        result = module.evolve(client, "v1", "v2")

        assert result.created == 25
        for old_id in old_ids:
            superseded = client.get(
                "Widget", old_id, include_unavailable=True
            )
            assert superseded["superseded_by"] is not None


# ---------------------------------------------------------------------------
# Hard validation gate — must block committed writes on invalid output
# ---------------------------------------------------------------------------


class TestValidationGate:
    def test_gate_blocks_commit_and_leaves_db_untouched(
        self, client: MosaicClient
    ) -> None:
        old_ids = _seed_v1(client, 2)
        module = _WidgetModule(_v1_to_v2_bad_type)

        with pytest.raises(MigrationGateError):
            module.evolve(client, "v1", "v2")

        # Nothing committed: old records still available, none superseded,
        # no supersede provenance events, and no new Widget rows.
        for old_id in old_ids:
            entity = client.get("Widget", old_id)
            assert entity["superseded_by"] is None
            history = client.history(old_id)
            assert not any(
                e["operation_type"] == "supersede" for e in history
            )
        assert len(client.query("Widget").items) == 2

    def test_gate_error_carries_validation_messages(
        self, client: MosaicClient
    ) -> None:
        _seed_v1(client, 1)
        module = _WidgetModule(_v1_to_v2_bad_type)

        with pytest.raises(MigrationGateError) as exc_info:
            module.evolve(client, "v1", "v2")

        err = exc_info.value
        assert err.package == "widget"
        assert err.from_version == "v1"
        assert err.to_version == "v2"
        assert err.errors
        assert any("integer" in m for m in err.errors)

    def test_gate_blocks_record_for_undefined_class(
        self, client: MosaicClient
    ) -> None:
        _seed_v1(client, 1)
        module = _WidgetModule(_v1_to_v2_unknown_class)

        with pytest.raises(MigrationGateError):
            module.evolve(client, "v1", "v2")

        assert client.get("Widget", "w0")["superseded_by"] is None

    def test_gate_blocks_missing_required_v2_field(self, db_path: str) -> None:
        # Canonical "v2 adds a required field" story: a transform that
        # stages a record omitting the now-required slot fails the gate
        # before any write. Staged via plan.put (no seeding) so the
        # required-field NOT NULL column is never inserted into.
        overlay = {
            "id": "https://example.org/mosaic/test_required",
            "name": "test_required",
            "prefixes": {
                "linkml": "https://w3id.org/linkml/",
                "mosaic": "https://w3id.org/datahelix/mosaic/",
            },
            "imports": ["linkml:types", "mosaic_core"],
            "default_range": "string",
            "classes": {
                "Gizmo": {
                    "is_a": "Entity",
                    "attributes": {
                        "label": {"range": "string"},
                        "kind": {"range": "string", "required": True},
                    },
                }
            },
        }
        reg = SchemaRegistry(
            SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
        )
        client = MosaicClient(
            storage=SQLiteAdapter(db_path, schema_registry=reg), registry=reg
        )

        def _stage_missing_required(ctx: MigrationContext) -> None:
            ctx.plan.put("Gizmo", {"label": "no kind here"})

        module = _WidgetModule(_stage_missing_required)
        with pytest.raises(MigrationGateError) as exc_info:
            module.evolve(client, "v1", "v2")
        assert any("kind" in m for m in exc_info.value.errors)
        # Nothing committed.
        assert len(client.query("Gizmo").items) == 0

    def test_gate_requires_a_schema_backed_client(self) -> None:
        # No registry ⇒ the gate cannot run; evolve refuses rather than
        # committing unvalidated writes.
        client = MosaicClient()

        def _stage(ctx: MigrationContext) -> None:
            ctx.plan.put("Widget", {"label": "x"})

        module = _WidgetModule(_stage)
        with pytest.raises(MigrationGateError):
            module.evolve(client, "v1", "v2")


# ---------------------------------------------------------------------------
# Step resolution (single-hop; multi-hop is S3)
# ---------------------------------------------------------------------------


class TestStepResolution:
    def test_unknown_hop_fails_loud(self, client: MosaicClient) -> None:
        module = _WidgetModule(_v1_to_v2_set_kind)
        with pytest.raises(MigrationStepNotFoundError) as exc_info:
            module.evolve(client, "v2", "v3")
        assert exc_info.value.available_steps == [("v1", "v2")]

    def test_duplicate_step_fails_loud(self, client: MosaicClient) -> None:
        module = _WidgetModule(_v1_to_v2_set_kind, duplicate=True)
        with pytest.raises(MigrationStepNotFoundError):
            module.evolve(client, "v1", "v2")


# ---------------------------------------------------------------------------
# Multi-hop migration chain (S3) — the verbatim §9 S3 acceptance:
# "a 3-hop upgrade composes intermediate steps correctly".
# ---------------------------------------------------------------------------


def _make_set_kind(kind: str, reads: dict[str, int]):
    """Build a transform that migrates each live Widget, stamping ``kind``.

    Records how many records it *read* under ``reads[kind]`` so a test can
    prove a hop ran exactly once over the previous hop's live output (and
    never re-processed superseded predecessors — the double-processing
    failure the resolver must avoid).
    """

    def _transform(ctx: MigrationContext) -> None:
        items = ctx.client.query("Widget").items
        reads[kind] = reads.get(kind, 0) + len(items)
        for old in items:
            ctx.plan.migrate(
                "Widget",
                old["id"],
                {"label": old["data"].get("label"), "kind": kind},
            )

    return _transform


class _ChainModule(DomainModule):
    """DomainModule whose migration DAG is injected at construction.

    ``edges`` is a list of ``(from, to)`` pairs; each becomes a step whose
    transform stamps ``kind=<to>``. ``reads`` accumulates per-``to`` read
    counts so tests can assert each hop processed exactly the live set.
    """

    name = "chain"
    description = "Test multi-hop domain module (PTS-339)"

    def __init__(self, edges: list[tuple[str, str]], reads: dict[str, int]) -> None:
        self._edges = edges
        self._reads = reads

    def versions(self) -> list[str]:
        return ["v1", "v2", "v3", "v4", "test"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": "chain", "classes": {}}

    def populates_types(self) -> list[str]:
        return ["Widget"]

    def migration_steps(self) -> list[MigrationStep]:
        return [
            MigrationStep(
                from_version=fr,
                to_version=to,
                transform=_make_set_kind(to, self._reads),
                description=f"{fr}->{to}",
            )
            for fr, to in self._edges
        ]


class TestMultiHopChain:
    def test_three_hop_composes_intermediate_steps(
        self, client: MosaicClient
    ) -> None:
        # v1 ─► v2 ─► v3 ─► v4, evolved in one call. Each hop must run
        # exactly once over the 3 live records — never re-touching the
        # superseded predecessors (query() excludes them).
        old_ids = _seed_v1(client, 3)
        reads: dict[str, int] = {}
        module = _ChainModule(
            [("v1", "v2"), ("v2", "v3"), ("v3", "v4")], reads
        )

        result = module.evolve(client, "v1", "v4")

        # Three hops × 3 records each = 9 new records written.
        assert result.created == 9
        # Each hop read EXACTLY the 3 live records (no double-processing).
        assert reads == {"v2": 3, "v3": 3, "v4": 3}

        # Final live set: exactly 3 records, all at the terminal v4 shape.
        live = client.query("Widget").items
        assert len(live) == 3
        assert {w["data"]["kind"] for w in live} == {"v4"}

        # Each original record sits at the head of a depth-3 supersession
        # chain ending in an available v4 record.
        for old_id in old_ids:
            chain_len = 0
            cursor = old_id
            seen_available = False
            for _ in range(10):  # generous bound; real depth is 3
                entity = client.get(
                    "Widget", cursor, include_unavailable=True
                )
                nxt = entity["superseded_by"]
                if nxt is None:
                    # Terminal node must be the live v4 record.
                    assert entity["data"]["kind"] == "v4"
                    seen_available = True
                    break
                chain_len += 1
                cursor = nxt
            assert seen_available
            assert chain_len == 3

    def test_single_hop_is_a_one_edge_path(self, client: MosaicClient) -> None:
        # The resolver has no special-case for single-hop: v1→v2 on the
        # same chain runs exactly one hop.
        _seed_v1(client, 2)
        reads: dict[str, int] = {}
        module = _ChainModule(
            [("v1", "v2"), ("v2", "v3"), ("v3", "v4")], reads
        )

        result = module.evolve(client, "v1", "v2")

        assert result.created == 2
        assert reads == {"v2": 2}  # only the first hop ran
        assert {w["data"]["kind"] for w in client.query("Widget").items} == {"v2"}

    def test_shortcut_edge_is_preferred_over_chain(
        self, client: MosaicClient
    ) -> None:
        # A direct v1→v4 shortcut alongside the step chain: the BFS path
        # finder picks the one-edge shortcut, so only its transform runs.
        _seed_v1(client, 2)
        reads: dict[str, int] = {}
        module = _ChainModule(
            [("v1", "v2"), ("v2", "v3"), ("v3", "v4"), ("v1", "v4")], reads
        )

        result = module.evolve(client, "v1", "v4")

        assert result.created == 2  # one hop, not three
        # Only the shortcut (to=v4) ran; the intermediate hops did not.
        assert reads == {"v4": 2}
        assert {w["data"]["kind"] for w in client.query("Widget").items} == {"v4"}

    def test_no_op_when_already_at_target(self, client: MosaicClient) -> None:
        _seed_v1(client, 2)
        reads: dict[str, int] = {}
        module = _ChainModule([("v1", "v2"), ("v2", "v3")], reads)

        result = module.evolve(client, "v2", "v2")

        assert result.created == 0
        assert reads == {}  # no transform ran

    def test_below_floor_fails_loud(self, client: MosaicClient) -> None:
        # Current version is older than the oldest shipped step (not a node
        # anywhere in the DAG) ⇒ MigrationFloorError naming the floor.
        reads: dict[str, int] = {}
        module = _ChainModule(
            [("v1", "v2"), ("v2", "v3"), ("v3", "v4")], reads
        )

        with pytest.raises(MigrationFloorError) as exc_info:
            module.evolve(client, "v0", "v4")
        err = exc_info.value
        assert err.floor == "v1"
        assert err.package == "chain"
        assert "v1" in str(err)

    def test_unreachable_target_fails_loud(self, client: MosaicClient) -> None:
        # Target is not reachable from a known current node ⇒ not-found,
        # carrying the declared edges for diagnosis.
        reads: dict[str, int] = {}
        module = _ChainModule([("v1", "v2"), ("v2", "v3")], reads)

        with pytest.raises(MigrationStepNotFoundError) as exc_info:
            module.evolve(client, "v1", "v9")
        assert ("v1", "v2") in exc_info.value.available_steps


# ---------------------------------------------------------------------------
# deprovision — refuse-by-default on live domain data (S3, sec11 §11.4.3)
# ---------------------------------------------------------------------------


class TestDeprovision:
    def test_refuses_when_module_owns_live_data(
        self, client: MosaicClient
    ) -> None:
        _seed_v1(client, 3)
        module = _WidgetModule(_v1_to_v2_set_kind)

        with pytest.raises(DeprovisionRefusedError) as exc_info:
            module.deprovision(client, "v2")
        err = exc_info.value
        assert err.reason == "live_domain_data"
        assert err.live_types == ["Widget"]
        assert err.package == "widget"

        # Refused ⇒ nothing retired: the live rows are untouched.
        assert len(client.query("Widget").items) == 3

    def test_force_soft_deletes_live_rows(self, client: MosaicClient) -> None:
        ids = _seed_v1(client, 3)
        module = _WidgetModule(_v1_to_v2_set_kind)

        module.deprovision(client, "v2", force=True)

        # Soft delete: no longer live, but the rows (and provenance) remain
        # — this is the availability transition, not a hard delete (sec3).
        assert client.query("Widget").items == []
        for wid in ids:
            with pytest.raises(EntityNotFoundError):
                client.get("Widget", wid)
            archived = client.get("Widget", wid, include_unavailable=True)
            assert archived is not None

    def test_noop_when_no_live_data_even_without_force(
        self, client: MosaicClient
    ) -> None:
        module = _WidgetModule(_v1_to_v2_set_kind)
        # No rows seeded ⇒ nothing to refuse, returns quietly.
        assert module.deprovision(client, "v2") is None
