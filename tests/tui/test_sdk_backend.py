"""Tests for SDKBackend — db_path resolution and asyncio.to_thread usage."""

from __future__ import annotations

import asyncio
import json
import os
import unittest.mock
from pathlib import Path

import pytest

from mosaic.tui.backend.sdk import (
    SDKBackend,
    _resolve_db_path,
    _resolve_validators_path,
)


# ---------------------------------------------------------------------------
# Tests: db_path resolution
# ---------------------------------------------------------------------------


def test_explicit_db_path_takes_precedence(tmp_path):
    """Explicit db_path argument overrides config.json."""
    explicit = tmp_path / "explicit.db"
    resolved = _resolve_db_path(explicit)
    assert resolved == explicit


def test_config_json_db_path_used_when_no_explicit(tmp_path, monkeypatch):
    """Falls back to config.json in cwd when no explicit path given."""
    config = {"db_path": str(tmp_path / "from_config.db")}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.chdir(tmp_path)

    resolved = _resolve_db_path(None)
    assert resolved == Path(str(tmp_path / "from_config.db"))


def test_default_mosaic_db_when_no_config(tmp_path, monkeypatch):
    """Falls back to mosaic.db when config.json is absent."""
    monkeypatch.chdir(tmp_path)
    resolved = _resolve_db_path(None)
    assert resolved == Path("mosaic.db")


def test_legacy_hippo_db_still_found(tmp_path, monkeypatch):
    """An existing legacy hippo.db is still picked up (ADR-0004)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hippo.db").touch()
    resolved = _resolve_db_path(None)
    assert resolved == Path("hippo.db")


def test_explicit_string_path_is_resolved():
    """String paths are converted to Path objects."""
    resolved = _resolve_db_path("/tmp/my.db")
    assert resolved == Path("/tmp/my.db")


# ---------------------------------------------------------------------------
# Tests: asyncio.to_thread wrapping
# ---------------------------------------------------------------------------


def test_list_entity_types_uses_to_thread(monkeypatch):
    """list_entity_types dispatches via asyncio.to_thread."""
    backend = SDKBackend(db_path=":memory:")

    calls: list[str] = []

    original_to_thread = asyncio.to_thread

    async def mock_to_thread(func, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", mock_to_thread)

    async def run():
        try:
            await backend.list_entity_types()
        except Exception:
            pass

    asyncio.run(run())
    assert "_list_entity_types_sync" in calls


def test_list_entities_uses_to_thread(monkeypatch):
    """list_entities dispatches via asyncio.to_thread."""
    backend = SDKBackend(db_path=":memory:")

    calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def mock_to_thread(func, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", mock_to_thread)

    async def run():
        try:
            await backend.list_entities("Sample")
        except Exception:
            pass

    asyncio.run(run())
    assert "_list_entities_sync" in calls


def test_get_schema_uses_to_thread(monkeypatch):
    """get_schema dispatches via asyncio.to_thread."""
    backend = SDKBackend(db_path=":memory:")

    calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def mock_to_thread(func, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", mock_to_thread)

    async def run():
        try:
            await backend.get_schema()
        except Exception:
            pass

    asyncio.run(run())
    assert "_get_schema_sync" in calls


# ---------------------------------------------------------------------------
# Integration tests: SDKBackend against a real temp SQLite instance
# ---------------------------------------------------------------------------

_FIXTURE_SCHEMA = (
    Path(__file__).parents[1] / "fixtures" / "schemas" / "sample_schema.yaml"
)


@pytest.fixture
def sdk_backend(tmp_path):
    """SDKBackend over a fresh SQLite db with the sample fixture schema."""
    return SDKBackend(db_path=tmp_path / "tui.db", schema_path=_FIXTURE_SCHEMA)


@pytest.fixture
def seeded_backend(sdk_backend):
    """Backend with one Project and three Samples created through the SDK."""

    async def seed():
        project_id = await sdk_backend.create_entity(
            "Project", {"name": "Alpha", "description": "first project"}
        )
        sample_ids = []
        for i in range(3):
            sample_ids.append(
                await sdk_backend.create_entity(
                    "Sample",
                    {
                        "name": f"S{i}",
                        "project_id": project_id,
                        "status": "active",
                        "volume_ml": 1.0 + i,
                    },
                )
            )
        return project_id, sample_ids

    project_id, sample_ids = asyncio.run(seed())
    return sdk_backend, project_id, sample_ids


def test_sdk_connection_info_ok(sdk_backend):
    info = asyncio.run(sdk_backend.connection_info())
    assert info.mode == "sdk"
    assert info.ok is True
    assert "2 entity types" in info.detail


def test_sdk_connection_info_bad_schema(tmp_path):
    backend = SDKBackend(
        db_path=tmp_path / "x.db", schema_path=tmp_path / "missing.yaml"
    )
    info = asyncio.run(backend.connection_info())
    assert info.ok is False


def test_sdk_capabilities(sdk_backend):
    caps = sdk_backend.capabilities()
    assert caps.supports_filters is True
    assert caps.supports_fts is True


def test_sdk_list_entity_types_with_counts(seeded_backend):
    backend, _project_id, _sample_ids = seeded_backend
    summaries = asyncio.run(backend.list_entity_types())
    by_name = {s.name: s.count for s in summaries}
    assert by_name == {"Project": 1, "Sample": 3}


_PARENT_SCHEMA = """\
id: https://example.org/tui/parent
name: tui_parent
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Procedure:
    is_a: Entity
    attributes:
      name:
  BankProcedure:
    is_a: Procedure
    attributes:
      site:
"""


def test_sdk_sidebar_counts_concrete_parent_own_rows(tmp_path):
    """A concrete parent's sidebar count is its own rows (ADR-0014 Decision 1):
    its subclass rows are counted once, under the subclass, and the
    sidebar agrees with ``status()["entity_counts"]``."""
    schema = tmp_path / "parent.yaml"
    schema.write_text(_PARENT_SCHEMA)
    backend = SDKBackend(db_path=tmp_path / "p.db", schema_path=schema)

    async def seed():
        await backend.create_entity("Procedure", {"name": "plain"})
        await backend.create_entity("BankProcedure", {"name": "b1", "site": "A"})
        await backend.create_entity("BankProcedure", {"name": "b2", "site": "B"})

    asyncio.run(seed())
    by_name = {s.name: s.count for s in asyncio.run(backend.list_entity_types())}
    assert by_name == {"Procedure": 1, "BankProcedure": 2}
    stats = backend._get_client().status()["entity_counts"]
    assert {k: stats.get(k, 0) for k in by_name} == by_name


def test_sdk_list_entities_pagination(seeded_backend):
    backend, _project_id, sample_ids = seeded_backend
    result = asyncio.run(backend.list_entities("Sample"))
    assert result.total_items == 3
    assert result.total_pages == 1
    assert {item["id"] for item in result.items} == set(sample_ids)
    # System + temporal fields present on listed records
    first = result.items[0]
    assert first["is_available"] is True
    assert first["created_at"]


def test_sdk_list_entities_field_filter(seeded_backend):
    backend, _project_id, _sample_ids = seeded_backend
    result = asyncio.run(backend.list_entities("Sample", filter_text="name=S1"))
    assert result.total_items == 1
    assert result.items[0]["data"]["name"] == "S1"


def test_sdk_list_entities_substring_filter(seeded_backend):
    backend, _project_id, _sample_ids = seeded_backend
    result = asyncio.run(backend.list_entities("Sample", filter_text="s2"))
    assert result.total_items == 1
    assert result.items[0]["data"]["name"] == "S2"


def test_sdk_query_entities_or_mode(seeded_backend):
    backend, _project_id, _sample_ids = seeded_backend
    result = asyncio.run(
        backend.query_entities(
            "Sample",
            filters=[
                {"field": "name", "value": "S0"},
                {"field": "name", "value": "S2"},
            ],
            filter_mode="or",
        )
    )
    assert result.total_items == 2


def test_sdk_search_entities_fts(seeded_backend):
    backend, _project_id, _sample_ids = seeded_backend
    results = asyncio.run(backend.search_entities("Sample", "S1"))
    assert len(results) == 1
    assert results[0]["data"]["name"] == "S1"


def test_sdk_get_entity_detail(seeded_backend):
    backend, project_id, sample_ids = seeded_backend
    detail = asyncio.run(backend.get_entity("Sample", sample_ids[0]))
    assert detail.id == sample_ids[0]
    assert detail.entity_type == "Sample"
    # System fields first, then user data
    assert detail.fields["is_available"] is True
    assert detail.fields["version"] == 1
    assert detail.fields["created_at"]
    assert detail.fields["updated_at"]
    assert detail.fields["schema_version"]
    assert detail.fields["name"] == "S0"
    assert detail.data["name"] == "S0"
    # Relationship derived from the class-ranged project_id slot
    assert len(detail.relationships) == 1
    rel = detail.relationships[0]
    assert rel.relationship_name == "project_id"
    assert rel.target_type == "Project"
    assert rel.target_id == project_id


def test_sdk_get_entity_not_found(sdk_backend):
    from mosaic.tui.backend.protocol import BackendError

    with pytest.raises(BackendError):
        asyncio.run(sdk_backend.get_entity("Sample", "nonexistent-id"))


def test_sdk_get_schema_fields(sdk_backend):
    schema = asyncio.run(sdk_backend.get_schema())
    names = [et.name for et in schema.entity_types]
    assert names == ["Project", "Sample"]

    sample = schema.get_entity_type("Sample")
    fields = {f.name: f for f in sample.fields}
    assert fields["name"].required is True
    assert fields["project_id"].ref_target == "Project"
    assert fields["project_id"].indexed is True
    assert fields["status"].enum_values == ["active", "archived", "distributed"]
    assert fields["volume_ml"].field_type == "float"
    # Inherited hippo_core Entity system slots are present
    assert "id" in fields
    assert "is_available" in fields

    # Sample.project_id (the stored FK) and Project.samples — its declared
    # LinkML ``inverse`` (ADR-0011), a virtual reverse edge — both surface.
    rels = {(r.source_type, r.relationship_name): r.target_type for r in schema.relationships}
    assert rels == {
        ("Sample", "project_id"): "Project",
        ("Project", "samples"): "Sample",
    }


def test_sdk_provenance_newest_first(seeded_backend):
    backend, project_id, sample_ids = seeded_backend
    sid = sample_ids[0]

    async def mutate_and_fetch():
        await backend.update_entity(
            "Sample", sid, {"name": "S0-renamed", "project_id": project_id}
        )
        return await backend.get_provenance("Sample", sid)

    events = asyncio.run(mutate_and_fetch())
    assert len(events) == 2
    assert events[0].event_type == "update"
    assert events[-1].event_type == "create"
    assert events[0].timestamp >= events[-1].timestamp


def test_sdk_create_validation_failure(sdk_backend):
    from mosaic.tui.backend.protocol import BackendError

    with pytest.raises(BackendError):
        # 'name' is required on Sample
        asyncio.run(sdk_backend.create_entity("Sample", {"volume_ml": 2.0}))


def test_sdk_set_availability_round_trip(seeded_backend):
    backend, _project_id, sample_ids = seeded_backend
    sid = sample_ids[0]

    async def transition():
        await backend.set_availability("Sample", sid, False, reason="archived")
        detail = await backend.get_entity("Sample", sid)
        listing = await backend.list_entities("Sample")
        return detail, listing

    detail, listing = asyncio.run(transition())
    assert detail.fields["is_available"] is False
    # Unavailable entities drop out of the default listing
    assert listing.total_items == 2

    async def restore():
        await backend.set_availability("Sample", sid, True, reason="restored")
        return await backend.get_entity("Sample", sid)

    detail = asyncio.run(restore())
    assert detail.fields["is_available"] is True


def test_sdk_set_availability_unknown_entity(seeded_backend):
    from mosaic.tui.backend.protocol import BackendError

    backend, _project_id, _sample_ids = seeded_backend
    with pytest.raises(BackendError):
        asyncio.run(backend.set_availability("Sample", "no-such-id", False))


# ---------------------------------------------------------------------------
# Tests: validators_path resolution + write pipeline wiring
# ---------------------------------------------------------------------------


def test_explicit_validators_path_takes_precedence(tmp_path):
    explicit = tmp_path / "v.yaml"
    assert _resolve_validators_path(explicit) == explicit


def test_validators_path_from_config_json(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"validators_path": "./validators.yaml"})
    )
    monkeypatch.chdir(tmp_path)
    assert _resolve_validators_path(None) == Path("./validators.yaml")


def test_validators_path_skipped_when_validation_disabled(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"validators_path": "./validators.yaml", "validation_enabled": False}
        )
    )
    monkeypatch.chdir(tmp_path)
    assert _resolve_validators_path(None) is None


def test_validators_path_none_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _resolve_validators_path(None) is None


_VALIDATORS_YAML = """\
validators:
  - name: volume_positive
    entity_types:
      - Sample
    on:
      - create
      - update
    condition: 'entity.volume_ml > 0'
    error: "volume_ml must be positive"
"""


@pytest.fixture
def validated_backend(tmp_path):
    """SDKBackend whose write pipeline carries a CEL validator (volume > 0)."""
    validators = tmp_path / "validators.yaml"
    validators.write_text(_VALIDATORS_YAML)
    return SDKBackend(
        db_path=tmp_path / "tui.db",
        schema_path=_FIXTURE_SCHEMA,
        validators_path=validators,
    )


def test_configured_validator_rejects_bad_write(validated_backend):
    """A write violating the configured CEL rule is rejected via the SDK.

    Without the pipeline wired into MosaicClient this would succeed — the
    schema permits any float for volume_ml — so this proves the TUI honours
    deployment-configured validators rather than bypassing them.
    """
    from mosaic.tui.backend.protocol import BackendError

    with pytest.raises(BackendError):
        asyncio.run(
            validated_backend.create_entity(
                "Sample", {"name": "bad", "volume_ml": -1.0}
            )
        )


def test_configured_validator_allows_valid_write(validated_backend):
    """A write satisfying the CEL rule succeeds through the same pipeline."""
    new_id = asyncio.run(
        validated_backend.create_entity(
            "Sample", {"name": "good", "volume_ml": 2.5}
        )
    )
    assert new_id

    detail = asyncio.run(validated_backend.get_entity("Sample", new_id))
    assert detail.data["volume_ml"] == 2.5
