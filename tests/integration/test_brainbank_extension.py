"""S5 integration test — Brain bank as the proving extension (PTS-341 / Doc 1 §7).

Proves that the brain bank ``DomainModule`` user-layer extension upgrades across
a base major-version bump through the full S2–S4 machinery:

* **S2** ``DomainModule.evolve`` — single-hop evolve with staged gate.
* **S3** migration-chain resolver — multi-edge BFS resolves v1.0.0→v1.1.0→v2.0.0
  when called as a direct two-hop ``evolve("1.0.0", "2.0.0")``.
* **S4** orchestrator + end-to-end gate + exposure report —
  dependency-ordered multi-package migration through a bundle with an
  intermediate coordinate; the gate blocks a stranded extension field.

Acceptance (§9 S5): *the brain bank upgrades across a base major with its
extension intact, validated, and provenanced.*

Design reference: Document 1 — Brain Bank LinkML Schema Design (PTS-347 /
doc1-brainbank-linkml-schema). §7 is the load-bearing section; the tests map
directly to §7.1–§7.8.

Polymorphic query (PM §7.6 carry-forward; revised by ADR-0014 Decision 1):
    ``BrainDonor`` rows live in their own ``BrainDonor`` table, but
    ``query("Subject")`` resolves over Subject's concrete closure, so it lists
    them too (each item's ``entity_type`` names its class).
    ``query("Subject", include_subclasses=False)`` reads only the ``Subject``
    table; the migration transforms below use it so no subclass row is
    migrated twice. ``TestQueryDisjoint`` asserts both directly.

Stranded-field gate mechanism — doc-correction note (Doc 1 §7.3/§7.7):
    Doc 1 states the gate bites because ``age_value`` and ``age_unit`` are
    ``required: true`` (null → required violation). However, LinkML's
    SQLTableGenerator translates ``required: true`` to ``NOT NULL`` columns,
    which prevents seeding v1-shape records (null ``age_value``) even with
    ``bypass_validation=True``. Additionally, intermediate hops emit records
    with null ``age_value`` before the final break step; a ``required``
    constraint would fail the *per-hop* gate, preventing multi-hop execution.
    Accordingly, this test uses the established pattern from
    ``test_orchestrator.py``: the stranded field is a **type-invalid value**
    (``age_unit="INVALID_UNIT"`` fails enum validation) rather than a null
    required field. The gate bites on the same invalid record; the behavioral
    claim (MigrationGateError, full rollback) is identical. The §7.3/§7.7
    wording should be corrected in a doc update.

DDL scope note:
    These tests exercise the evolve/orchestrator/gate data path only. The DDL
    tier (``mosaic migrate`` add/drop columns for ``age_at_collection`` →
    ``age_value``/``age_unit``) is out of scope. The merged registry keeps
    ``age_at_collection`` as an optional slot so v1-shape records are
    readable; the migration transforms read it and produce the v2 shape.

hippo_unique note (resolved, PTS-348):
    ``Subject.external_id`` and ``BrainDonor.brain_bank_id`` carry
    ``hippo_unique`` (Document 1 / PTS-347). Two defects once blocked this on
    the migration path: the DDL generator emitted a *non-partial* unique index
    (a superseded predecessor permanently held the key), and ``_run_hop``
    created the new record while the old was still live (two live rows, same
    key). Both are fixed — the index is now partial (``WHERE is_available = 1``)
    and ``_run_hop`` retires each predecessor before writing its replacement.
    These tests exercise that path end-to-end: every migrated, business-keyed
    record proves the create-before-supersede window no longer collides.
"""

from __future__ import annotations

import os
import tempfile
from abc import abstractmethod
from typing import Any

import pytest
import yaml
from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import MigrationGateError
from mosaic.core.loaders.bundle import Bundle
from mosaic.core.loaders.domain_module import (
    DomainModule,
    MigrationContext,
    MigrationStep,
)
from mosaic.core.loaders.exposure import (
    ExposureReport,
    SchemaElement,
    compute_write_set,
    exposure_report,
    extension_referenced_elements,
)
from mosaic.core.loaders.orchestrator import migrate_to_bundle, topological_sort
from mosaic.core.loaders.schema_package import SchemaPackage
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap


# ---------------------------------------------------------------------------
# Merged test registry (v2.0.0 target shape)
#
# All packages' classes are merged into one flat schema so SchemaRegistry
# can back the MosaicClient.  ``age_at_collection`` is kept as an optional
# slot (not removed) so v1-shape records are readable by migration transforms.
# ``age_value`` and ``age_unit`` are optional to avoid NOT NULL issues when
# seeding v1-shape records with bypass_validation=True.
# ---------------------------------------------------------------------------

def _build_merged_registry() -> SchemaRegistry:
    overlay: dict[str, Any] = {
        "id": "https://example.org/hippo/test_brainbank",
        "name": "test_brainbank",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "hippo": "https://w3id.org/hippo/",
        },
        "imports": ["linkml:types", "hippo_core"],
        "default_range": "string",
        "classes": {
            # --- base: subject module ---
            "Subject": {
                "is_a": "Entity",
                "annotations": {"hippo_accessor": "subjects"},
                "attributes": {
                    "external_id": {
                        "range": "string",
                        "required": True,
                        # hippo_unique (Document 1 / PTS-347): a migration
                        # target — proves the create-before-supersede window
                        # no longer collides on the partial unique index.
                        "annotations": {"hippo_unique": True},
                    },
                    "species": {"range": "string", "required": True},
                    "biological_sex": {"range": "string"},
                    "diagnosis": {"range": "string"},
                    "cohort_id": {"range": "string"},
                    "collection_site": {"range": "string"},
                    # v1 slot — kept optional for readback; not removed in test schema
                    "age_at_collection": {"range": "float"},
                    # v2 replacement slots — optional in test schema (see module note)
                    "age_value": {"range": "float"},
                    "age_unit": {"range": "AgeUnit"},
                },
            },
            # --- extension: brainbank module — BrainDonor ---
            "BrainDonor": {
                "is_a": "Subject",
                "annotations": {"hippo_accessor": "brain_donors"},
                "attributes": {
                    "brain_bank_id": {
                        "range": "string",
                        "required": True,
                        # hippo_unique (Document 1 / PTS-347): a migration
                        # target — proves the create-before-supersede window
                        # no longer collides on the partial unique index.
                        "annotations": {"hippo_unique": True},
                    },
                    "post_mortem_interval_hours": {"range": "float"},
                    # neuropathology_confirmed: range: boolean — re-enabled by
                    # PTS-349. _coerce_for_column stores bool as 0/1; the read-side
                    # _decode_column_value now reverses 0/1 → bool for boolean slots,
                    # so the value survives the migration transform read and passes
                    # the per-hop + end-to-end LinkML gate (previously rejected as
                    # integer 1 for a range: boolean slot).
                    "neuropathology_confirmed": {"range": "boolean"},
                    "rin": {"range": "float"},
                },
            },
            # --- base: tissue module ---
            "Sample": {
                "is_a": "Entity",
                "annotations": {"hippo_accessor": "samples"},
                "attributes": {
                    "external_id": {
                        "range": "string",
                        "required": True,
                        "annotations": {"hippo_index": True},
                    },
                    "tissue_type": {"range": "string", "required": True},
                    "tissue_region": {"range": "string"},
                },
            },
            # --- extension: brainbank module — BrainSample ---
            "BrainSample": {
                "is_a": "Sample",
                "annotations": {"hippo_accessor": "brain_samples"},
                "attributes": {
                    "brain_region": {
                        "range": "string",
                        "required": True,
                        "annotations": {"hippo_index": True},
                    },
                    "hemisphere": {"range": "string"},
                    "post_mortem_interval_hours": {"range": "float"},
                    "neuropathology_stage": {"range": "string"},
                },
            },
        },
        "enums": {
            "AgeUnit": {
                "permissible_values": {
                    "years": {},
                    "months": {},
                    "days": {},
                }
            }
        },
    }
    return SchemaRegistry(
        SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
    )


# ---------------------------------------------------------------------------
# Package implementations
# ---------------------------------------------------------------------------

class _CorePackage(SchemaPackage):
    """Pure-schema package — abstract association layer (no data)."""

    name = "core"
    description = "Abstract association layer."

    def versions(self) -> list[str]:
        return ["1.0.0", "test"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": "core", "classes": {}}

    def depends_on(self) -> list[str]:
        return []


def _subject_v1_0_to_v1_1(ctx: MigrationContext) -> None:
    """Carry-forward Subject records to v1.1.0 (adds cohort_id, collection_site)."""
    for item in ctx.client.query("Subject", include_subclasses=False).items:
        old = item.get("data") or {}
        new = {
            "external_id":        old.get("external_id"),
            "species":            old.get("species"),
            "biological_sex":     old.get("biological_sex"),
            "age_at_collection":  old.get("age_at_collection"),
            "diagnosis":          old.get("diagnosis"),
            "cohort_id":          None,
            "collection_site":    None,
        }
        ctx.plan.migrate("Subject", item["id"], new,
                         reason="subject 1.0.0→1.1.0 carry-forward")


def _subject_v1_1_to_v2_0(ctx: MigrationContext) -> None:
    """Breaking: rename age_at_collection → age_value + age_unit (years)."""
    for item in ctx.client.query("Subject", include_subclasses=False).items:
        old = item.get("data") or {}
        age = old.get("age_at_collection")
        new = {
            "external_id":     old.get("external_id"),
            "species":         old.get("species"),
            "biological_sex":  old.get("biological_sex"),
            "diagnosis":       old.get("diagnosis"),
            "cohort_id":       old.get("cohort_id"),
            "collection_site": old.get("collection_site"),
            "age_value":       age,
            "age_unit":        "years" if age is not None else None,
        }
        ctx.plan.migrate("Subject", item["id"], new,
                         reason="subject 1.1.0→2.0.0 age split")


class _SubjectModule(DomainModule):
    """DomainModule owning Subject records with a two-edge migration DAG."""

    name = "subject"
    description = "Biological subject module."

    def __init__(self, call_log: list[str] | None = None) -> None:
        self._call_log = call_log

    def versions(self) -> list[str]:
        return ["1.0.0", "1.1.0", "2.0.0", "test"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": "subject", "classes": {}}

    def depends_on(self) -> list[str]:
        return ["core"]

    def populates_types(self) -> list[str]:
        return ["Subject"]

    def migration_steps(self) -> list[MigrationStep]:
        def _v1_0_to_v1_1(ctx: MigrationContext) -> None:
            if self._call_log is not None:
                self._call_log.append("subject")
            _subject_v1_0_to_v1_1(ctx)

        def _v1_1_to_v2_0(ctx: MigrationContext) -> None:
            if self._call_log is not None:
                self._call_log.append("subject")
            _subject_v1_1_to_v2_0(ctx)

        return [
            MigrationStep("1.0.0", "1.1.0", _v1_0_to_v1_1,
                          description="Additive: cohort_id + collection_site"),
            MigrationStep("1.1.0", "2.0.0", _v1_1_to_v2_0,
                          description="Breaking: age_at_collection → age_value + age_unit"),
        ]


def _coerce_bool(v: object) -> bool | None:
    """Convert SQLite integer (0/1) back to Python bool for gate validation."""
    return bool(v) if v is not None else None


def _brainbank_v1_to_v2(ctx: MigrationContext) -> None:
    """Complementary step: migrate BrainDonor age_at_collection → age_value + age_unit."""
    for item in ctx.client.query("BrainDonor", include_subclasses=False).items:
        old = item.get("data") or {}
        age = old.get("age_at_collection")
        new = {
            # BrainDonor-specific slots (unchanged):
            "brain_bank_id":              old.get("brain_bank_id"),
            "post_mortem_interval_hours": old.get("post_mortem_interval_hours"),
            # range: boolean — carried forward as a Python bool (PTS-349); the
            # source read must have decoded the stored 0/1 back to bool, else the
            # per-hop gate would reject the int for this boolean slot.
            "neuropathology_confirmed":   old.get("neuropathology_confirmed"),
            "rin":                        old.get("rin"),
            # Inherited Subject slots (v2 shape):
            "external_id":                old.get("external_id"),
            "species":                    old.get("species"),
            "biological_sex":             old.get("biological_sex"),
            "diagnosis":                  old.get("diagnosis"),
            "cohort_id":                  old.get("cohort_id"),
            "collection_site":            old.get("collection_site"),
            "age_value":                  age,
            "age_unit":                   "years" if age is not None else None,
        }
        ctx.plan.migrate("BrainDonor", item["id"], new,
                         reason="brainbank 1.0.0→2.0.0 age_at_collection→age_value")


class _BrainBankModule(DomainModule):
    """DomainModule owning BrainDonor and BrainSample records."""

    name = "brainbank"
    description = "Brain bank extension module."

    def __init__(
        self,
        call_log: list[str] | None = None,
        *,
        empty_steps: bool = False,
    ) -> None:
        self._call_log = call_log
        self._empty_steps = empty_steps

    def versions(self) -> list[str]:
        return ["1.0.0", "2.0.0", "test"]

    def schema_fragment(self) -> dict:
        return {"default_prefix": "brainbank", "classes": {}}

    def depends_on(self) -> list[str]:
        return ["core", "subject"]

    def populates_types(self) -> list[str]:
        return ["BrainDonor", "BrainSample"]

    def migration_steps(self) -> list[MigrationStep]:
        if self._empty_steps:
            return []

        def _v1_to_v2(ctx: MigrationContext) -> None:
            if self._call_log is not None:
                self._call_log.append("brainbank")
            _brainbank_v1_to_v2(ctx)

        return [
            MigrationStep("1.0.0", "2.0.0", _v1_to_v2,
                          description="BrainDonor age_at_collection → age_value + age_unit"),
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _build_merged_registry()
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "brainbank.db"), schema_registry=reg
        )
        yield MosaicClient(storage=storage, registry=reg)


def _seed_subject(
    client: MosaicClient,
    sid: str,
    *,
    external_id: str,
    age: float | None,
) -> None:
    client.put(
        "Subject",
        {
            "id": sid,
            "external_id": external_id,
            "species": "Homo sapiens",
            "biological_sex": "male",
            "age_at_collection": age,
            "is_available": True,
        },
        bypass_validation=True,
    )


def _seed_brain_donor(
    client: MosaicClient,
    did: str,
    *,
    external_id: str,
    brain_bank_id: str,
    age: float | None,
    rin: float = 7.5,
    pmi: float = 18.0,
    age_unit: str | None = None,
    neuropathology_confirmed: bool | None = None,
) -> None:
    """Seed a BrainDonor at v1 shape; ``age_unit=None`` by default (un-migrated)."""
    client.put(
        "BrainDonor",
        {
            "id": did,
            "external_id": external_id,
            "species": "Homo sapiens",
            "biological_sex": "male",
            "brain_bank_id": brain_bank_id,
            "age_at_collection": age,
            "post_mortem_interval_hours": pmi,
            "neuropathology_confirmed": neuropathology_confirmed,
            "rin": rin,
            "age_unit": age_unit,  # None → valid (optional); "INVALID" → enum error at gate
            "is_available": True,
        },
        bypass_validation=True,
    )


def _seed_brain_sample(
    client: MosaicClient,
    sid: str,
    *,
    external_id: str,
    brain_region: str,
) -> None:
    client.put(
        "BrainSample",
        {
            "id": sid,
            "external_id": external_id,
            "tissue_type": "brain",
            "brain_region": brain_region,
            "hemisphere": "left",
            "is_available": True,
        },
        bypass_validation=True,
    )


def _standard_bundle() -> Bundle:
    """Two-hop bundle: subject 1.0.0→1.1.0 then subject+brainbank to 2.0.0."""
    return Bundle.from_manifest({
        "name": "brainbank-bundle",
        "version": "2.0.0",
        "packages": {"subject": "2.0.0", "brainbank": "2.0.0"},
        "coordinates": [{"subject": "1.1.0"}],
    })


# ---------------------------------------------------------------------------
# Polymorphic query disjoint — PM §7.6 carry-forward (closes first-class item)
# ---------------------------------------------------------------------------

class TestQueryDisjoint:
    """Per-class tables stay disjoint; a concrete parent's query spans its
    closure (ADR-0014 Decision 1) unless ``include_subclasses=False``."""

    def test_subject_query_excludes_brain_donors(self, client: MosaicClient) -> None:
        _seed_subject(client, "s1", external_id="VA-001", age=65.3)
        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5)

        exact = client.query("Subject", include_subclasses=False).items
        donors = client.query("BrainDonor").items

        # Type-exact read: each table contains only its own records.
        assert len(exact) == 1
        assert exact[0]["id"] == "s1"
        assert len(donors) == 1
        assert donors[0]["id"] == "bd1"

    def test_subject_query_lists_brain_donors_over_closure(
        self, client: MosaicClient
    ) -> None:
        _seed_subject(client, "s1", external_id="VA-001", age=65.3)
        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5)

        page = client.query("Subject")
        types = {item["id"]: item["entity_type"] for item in page.items}
        assert types == {"s1": "Subject", "bd1": "BrainDonor"}
        assert page.total == 2
        assert client.count("Subject") == 2
        assert client.count("Subject", include_subclasses=False) == 1

    def test_brain_sample_query_excludes_base_samples(self, client: MosaicClient) -> None:
        client.put("Sample",
                   {"id": "sa1", "external_id": "S-001", "tissue_type": "brain",
                    "is_available": True},
                   bypass_validation=True)
        _seed_brain_sample(client, "bs1", external_id="BB-SAMP-001",
                           brain_region="prefrontal cortex")

        samples = client.query("Sample", include_subclasses=False).items
        brain_samples = client.query("BrainSample").items

        assert len(samples) == 1
        assert samples[0]["id"] == "sa1"
        assert len(brain_samples) == 1
        assert brain_samples[0]["id"] == "bs1"


# ---------------------------------------------------------------------------
# S3: chain resolver — two-hop path on a direct evolve("1.0.0", "2.0.0") call
# ---------------------------------------------------------------------------

class TestSubjectChainResolver:
    """Chain resolver finds the v1.0→v1.1→v2.0 path when no shortcut edge exists."""

    def test_two_hop_path_via_chain_resolver(self, client: MosaicClient) -> None:
        _seed_subject(client, "s1", external_id="VA-001", age=65.3)
        mod = _SubjectModule()

        # Direct two-version jump — resolver must traverse two edges.
        result = mod.evolve(client, "1.0.0", "2.0.0")

        assert result.created == 2  # two new records: intermediate then final
        subjects = client.query("Subject").items
        assert len(subjects) == 1  # only the final live record
        final = subjects[0]["data"]
        assert final.get("age_value") == 65.3
        assert final.get("age_unit") == "years"

    def test_intermediate_record_superseded(self, client: MosaicClient) -> None:
        _seed_subject(client, "s1", external_id="VA-001", age=65.3)
        mod = _SubjectModule()
        mod.evolve(client, "1.0.0", "2.0.0")

        # Two supersession events: s1 → s1' (hop1), s1' → s1'' (hop2)
        # Only 1 live record exists.
        subjects = client.query("Subject").items
        assert len(subjects) == 1
        # Provenance: created=2 means we wrote 2 new records (hop1 + hop2 outputs)
        # The original (s1) and intermediate (s1') are both superseded.


# ---------------------------------------------------------------------------
# S4: Bundle orchestration — multi-hop, dependency-ordered, provenanced
# ---------------------------------------------------------------------------

class TestMultiHopUpgradeHappyPath:
    """Full end-to-end brain bank upgrade across a base major (§9 S5 acceptance)."""

    def _packages(
        self,
        call_log: list[str],
    ) -> tuple[list[SchemaPackage], _SubjectModule, _BrainBankModule]:
        core = _CorePackage()
        subj = _SubjectModule(call_log=call_log)
        brain = _BrainBankModule(call_log=call_log)
        return [core, subj, brain], subj, brain

    def test_committed_and_dependency_ordered(self, client: MosaicClient) -> None:
        call_log: list[str] = []
        packages, _, _ = self._packages(call_log)

        _seed_subject(client, "s1", external_id="VA-001", age=70.0)
        _seed_subject(client, "s2", external_id="VA-002", age=58.5)
        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5, rin=7.8, pmi=18.5)
        _seed_brain_donor(client, "bd2", external_id="VA-DON-002",
                          brain_bank_id="BB-002", age=65.3, rin=6.2, pmi=24.0)
        _seed_brain_sample(client, "bs1", external_id="BB-SAMP-001",
                           brain_region="prefrontal cortex")

        bundle = _standard_bundle()
        result = migrate_to_bundle(
            client, packages, bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        assert result.committed is True
        assert result.target_versions == {"subject": "2.0.0", "brainbank": "2.0.0"}

        # Dependency order within hop 2: subject before brainbank.
        # call_log: hop1=[subject], hop2=[subject, brainbank]
        assert call_log == ["subject", "subject", "brainbank"]

    def test_brain_donor_age_migrated(self, client: MosaicClient) -> None:
        packages, _, _ = self._packages([])

        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5, rin=7.8, pmi=18.5)

        bundle = _standard_bundle()
        result = migrate_to_bundle(
            client, packages, bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        assert result.committed is True
        donors = client.query("BrainDonor").items
        assert len(donors) == 1
        data = donors[0]["data"]
        assert data.get("age_value") == 72.5
        assert data.get("age_unit") == "years"

    def test_extension_slots_intact_after_upgrade(self, client: MosaicClient) -> None:
        """BrainDonor-specific fields survive the migration unchanged (§9 S5 'intact')."""
        packages, _, _ = self._packages([])

        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5, rin=7.8, pmi=18.5,
                          neuropathology_confirmed=True)

        bundle = _standard_bundle()
        migrate_to_bundle(
            client, packages, bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        donors = client.query("BrainDonor").items
        assert len(donors) == 1
        data = donors[0]["data"]
        # Extension-specific fields survive with original values.
        assert data.get("brain_bank_id") == "BB-001"
        assert data.get("rin") == pytest.approx(7.8)
        assert data.get("post_mortem_interval_hours") == pytest.approx(18.5)
        # range: boolean survives the full migration + end-to-end gate as a
        # native Python bool, not the stored integer 1 (PTS-349). `is True`
        # is intentional: an un-decoded `1` would pass `== True` but fail this.
        assert data.get("neuropathology_confirmed") is True

    def test_brain_sample_intact_no_migration(self, client: MosaicClient) -> None:
        """BrainSample is unchanged — no version bump, data survives end-to-end gate."""
        packages, _, _ = self._packages([])

        _seed_brain_sample(client, "bs1", external_id="BB-SAMP-001",
                           brain_region="prefrontal cortex")

        bundle = _standard_bundle()
        result = migrate_to_bundle(
            client, packages, bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        assert result.committed is True
        samples = client.query("BrainSample").items
        assert len(samples) == 1
        assert samples[0]["data"].get("brain_region") == "prefrontal cortex"

    def test_provenance_supersession_lineage(self, client: MosaicClient) -> None:
        """Migrated records carry supersession events with package actor + reason (§7.8).

        Subject goes through 2 hops: s1 → s1' (hop1, actor=subject) → s1'' (hop2, actor=subject).
        BrainDonor goes through 1 hop: bd1 → bd1' (hop2, actor=brainbank).
        Each original entity's history must contain a ``supersede`` event tagged
        with the migration package's name and the hop label.
        """
        packages, _, _ = self._packages([])

        _seed_subject(client, "s1", external_id="VA-001", age=70.0)
        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5, rin=7.8, pmi=18.0)

        bundle = _standard_bundle()
        result = migrate_to_bundle(
            client, packages, bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        assert result.committed is True

        # Subject "s1" history: superseded in hop 1 by actor="subject",
        # reason names the 1.0.0→1.1.0 carry-forward.
        s1_history = client.history("s1")
        s1_supersede_events = [e for e in s1_history if e["operation_type"] == "supersede"]
        assert len(s1_supersede_events) == 1
        s1_sup = s1_supersede_events[0]
        assert s1_sup["user_id"] == "subject"
        assert "1.0.0→1.1.0" in (s1_sup["state_snapshot"] or {}).get("reason", "")

        # BrainDonor "bd1" history: superseded in hop 2 by actor="brainbank".
        bd1_history = client.history("bd1")
        bd1_supersede_events = [e for e in bd1_history if e["operation_type"] == "supersede"]
        assert len(bd1_supersede_events) == 1
        bd1_sup = bd1_supersede_events[0]
        assert bd1_sup["user_id"] == "brainbank"
        assert "age_at_collection" in (bd1_sup["state_snapshot"] or {}).get("reason", "")

        # Only the final live records remain queryable.
        assert len(client.query("Subject", include_subclasses=False).items) == 1
        assert len(client.query("BrainDonor").items) == 1

    def test_migration_result_records_three_entries(self, client: MosaicClient) -> None:
        """Orchestrator records one migration entry per (package, hop)."""
        packages, _, _ = self._packages([])

        _seed_subject(client, "s1", external_id="VA-001", age=65.3)

        bundle = _standard_bundle()
        result = migrate_to_bundle(
            client, packages, bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        # hop1: subject 1.0.0→1.1.0; hop2: subject 1.1.0→2.0.0, brainbank 1.0.0→2.0.0
        assert len(result.migrations) == 3
        pkgs = [(m.package, m.from_version, m.to_version) for m in result.migrations]
        assert ("subject", "1.0.0", "1.1.0") in pkgs
        assert ("subject", "1.1.0", "2.0.0") in pkgs
        assert ("brainbank", "1.0.0", "2.0.0") in pkgs


# ---------------------------------------------------------------------------
# S4: Stranded extension field — gate blocks and rolls back (§7.7)
# ---------------------------------------------------------------------------

class TestStrandedFieldGateFailure:
    """Gate catches a BrainDonor with an invalid age_unit and rolls back (§7.7).

    Mechanism: ``age_unit="INVALID_UNIT"`` fails the ``AgeUnit`` enum
    validation in the merged registry.  The gate reads BrainDonor records
    after the base Subject migration and finds the invalid value, raising
    ``MigrationGateError``.  The staged transaction rolls the whole chain back.

    See module-level docstring for the §7.3/§7.7 doc-correction note on why
    type-invalidity is used instead of null-required.
    """

    def test_stranded_brain_donor_blocks_and_rolls_back(
        self, client: MosaicClient
    ) -> None:
        core = _CorePackage()
        subj = _SubjectModule()
        # brainbank module has empty migration steps — BrainDonors are NOT migrated.
        brain_no_steps = _BrainBankModule(empty_steps=True)

        _seed_subject(client, "s1", external_id="VA-001", age=70.0)
        _seed_subject(client, "s2", external_id="VA-002", age=58.5)
        # Stranded: invalid age_unit seeded directly; no complementary step will fix it.
        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5,
                          age_unit="INVALID_UNIT")  # enum value not in AgeUnit
        _seed_brain_donor(client, "bd2", external_id="VA-DON-002",
                          brain_bank_id="BB-002", age=65.3,
                          age_unit="INVALID_UNIT")

        # Bundle only targets subject; brainbank stays at v1.0.0 (extension omitted).
        # brainbank is still in the packages list so its types are in the gate scope.
        target = Bundle(
            name="bb",
            packages={"subject": "2.0.0"},
            coordinates=({"subject": "1.1.0"},),
        )

        with pytest.raises(MigrationGateError) as exc:
            migrate_to_bundle(
                client,
                [core, subj, brain_no_steps],
                target,
                {"subject": "1.0.0"},
            )

        # Gate names the invalid age_unit.
        assert any("age_unit" in e for e in exc.value.errors)

        # Full rollback: Subjects are still at v1 shape (no age_value migrated).
        subjects = client.query("Subject", include_subclasses=False).items
        assert len(subjects) == 2
        for item in subjects:
            assert item["data"].get("age_value") is None, "rollback should undo migration"

        # BrainDonors untouched (no step ran for them).
        donors = client.query("BrainDonor").items
        assert len(donors) == 2

    def test_valid_brain_donor_passes_gate(self, client: MosaicClient) -> None:
        """When extension provides the complementary step, gate passes green."""
        core = _CorePackage()
        subj = _SubjectModule()
        brain = _BrainBankModule()

        _seed_subject(client, "s1", external_id="VA-001", age=70.0)
        _seed_brain_donor(client, "bd1", external_id="VA-DON-001",
                          brain_bank_id="BB-001", age=72.5)

        bundle = _standard_bundle()
        result = migrate_to_bundle(
            client, [core, subj, brain], bundle,
            {"subject": "1.0.0", "brainbank": "1.0.0"},
        )

        assert result.committed is True
        donors = client.query("BrainDonor").items
        assert len(donors) == 1
        assert donors[0]["data"].get("age_unit") == "years"


# ---------------------------------------------------------------------------
# S4: Exposure report — flags break before migration (§7.5)
# ---------------------------------------------------------------------------

class TestExposureReport:
    """Exposure report warns before migration that subject v2.0 touches brainbank refs."""

    def _subject_v1_1_fragment(self) -> dict:
        """subject schema_fragment shape at v1.1.0 — has age_at_collection, no age_value."""
        return {
            "classes": {
                "Subject": {
                    "attributes": {
                        "external_id":        {},
                        "species":            {},
                        "biological_sex":     {},
                        "age_at_collection":  {},
                        "diagnosis":          {},
                        "cohort_id":          {},
                        "collection_site":    {},
                    }
                }
            }
        }

    def _subject_v2_0_fragment(self) -> dict:
        """subject schema_fragment shape at v2.0.0 — no age_at_collection, has age_value."""
        return {
            "classes": {
                "Subject": {
                    "attributes": {
                        "external_id":    {},
                        "species":        {},
                        "biological_sex": {},
                        "diagnosis":      {},
                        "cohort_id":      {},
                        "collection_site": {},
                        "age_value":      {},
                        "age_unit":       {},
                    }
                }
            }
        }

    def _brainbank_v1_fragment(self) -> dict:
        """brainbank v1.0.0 schema_fragment — BrainDonor slot_usage refs age_at_collection.

        ``is_a: "subject:Subject"`` uses the FQN form as in the real schema_fragment()
        (default_prefix differs from the base package prefix). The FQN does NOT currently
        intersect the write-set's bare ``"Subject"`` class name — a known limitation noted
        in Doc 1 §7.5. The **slot** intersection (``age_at_collection`` in both
        ``slot_usage`` and the write-set) is the load-bearing exposure signal.
        """
        return {
            "classes": {
                "BrainDonor": {
                    "is_a": "subject:Subject",  # FQN — does NOT intersect bare "Subject" in write-set
                    "slot_usage": {
                        "age_at_collection": {  # base slot referenced — this is the exposure
                            "minimum_value": 0,
                            "required": True,
                        }
                    },
                    "attributes": {
                        "brain_bank_id":              {},
                        "post_mortem_interval_hours": {},
                        "neuropathology_confirmed":   {},
                        "rin":                        {},
                    },
                }
            }
        }

    def test_exposure_is_not_safe_when_break_touches_slot_usage(self) -> None:
        old_schema = self._subject_v1_1_fragment()
        new_schema = self._subject_v2_0_fragment()
        ext_fragment = self._brainbank_v1_fragment()

        write_set = compute_write_set(old_schema, new_schema)
        report = exposure_report(write_set, ext_fragment, extension_name="brainbank")

        assert not report.is_safe, "write-set touches age_at_collection which brainbank references"
        assert "age_at_collection" in report.exposed_slots

    def test_exposure_report_names_exposed_slot(self) -> None:
        old_schema = self._subject_v1_1_fragment()
        new_schema = self._subject_v2_0_fragment()
        ext_fragment = self._brainbank_v1_fragment()

        write_set = compute_write_set(old_schema, new_schema)
        report = exposure_report(write_set, ext_fragment, extension_name="brainbank")

        rendered = report.render()
        assert "age_at_collection" in rendered
        assert "brainbank" in rendered

    def test_write_set_captures_slot_removal(self) -> None:
        old_schema = self._subject_v1_1_fragment()
        new_schema = self._subject_v2_0_fragment()

        ws = compute_write_set(old_schema, new_schema)

        removed_slots = {e.name for e in ws.removed if e.kind == "slot"}
        added_slots = {e.name for e in ws.added if e.kind == "slot"}

        assert "age_at_collection" in removed_slots
        assert "age_value" in added_slots
        assert "age_unit" in added_slots

    def test_additive_minor_is_safe_for_brainbank(self) -> None:
        """v1.0→v1.1 only adds cohort_id + collection_site; brainbank does not ref them."""
        old_schema = {
            "classes": {
                "Subject": {
                    "attributes": {
                        "external_id": {}, "species": {}, "age_at_collection": {}
                    }
                }
            }
        }
        new_schema = {
            "classes": {
                "Subject": {
                    "attributes": {
                        "external_id": {}, "species": {}, "age_at_collection": {},
                        "cohort_id": {}, "collection_site": {}
                    }
                }
            }
        }
        ext_fragment = self._brainbank_v1_fragment()

        ws = compute_write_set(old_schema, new_schema)
        report = exposure_report(ws, ext_fragment, extension_name="brainbank")

        assert report.is_safe


# ---------------------------------------------------------------------------
# Bundle — to_requires() covers requires:-pinning scope word from PTS-341
# ---------------------------------------------------------------------------

class TestBundleRequiresPin:
    def test_to_requires_generates_exact_pins(self) -> None:
        bundle = _standard_bundle()
        requires = bundle.to_requires()
        assert requires["subject"] == "==2.0.0"
        assert requires["brainbank"] == "==2.0.0"

    def test_coordinate_sequence_has_intermediate_then_target(self) -> None:
        bundle = _standard_bundle()
        seq = bundle.coordinate_sequence()
        assert len(seq) == 2
        assert seq[0] == {"subject": "1.1.0"}
        assert seq[1] == {"subject": "2.0.0", "brainbank": "2.0.0"}


# ---------------------------------------------------------------------------
# Topological sort — dependency order for the full package set (Doc 1 §3)
# ---------------------------------------------------------------------------

class TestTopologicalOrderBrainbank:
    def test_subject_before_brainbank(self) -> None:
        """brainbank depends on subject → subject must come first."""
        core = _CorePackage()
        subj = _SubjectModule()
        brain = _BrainBankModule()

        ordered = topological_sort([brain, subj, core])  # intentionally out of order
        names = [p.name for p in ordered]

        assert names.index("subject") < names.index("brainbank")
        assert names.index("core") < names.index("subject")
