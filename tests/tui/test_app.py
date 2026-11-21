"""Smoke tests for MosaicTUIApp — requires textual[dev] installed."""

from __future__ import annotations

import asyncio

import pytest

# Skip entire module if textual is not installed
textual = pytest.importorskip(
    "textual", reason="textual not installed; run: pip install datahelix-mosaic[tui]"
)

from mosaic.tui.backend.protocol import (
    EntityDetail,
    EntityTypeSummary,
    PagedResult,
    ProvenanceEvent,
    SchemaView,
)


# ---------------------------------------------------------------------------
# Minimal MockBackend for TUI tests
# ---------------------------------------------------------------------------


class MockBackend:
    def __init__(self, entity_types=None):
        self._entity_types = entity_types or [
            EntityTypeSummary(name="Sample", count=5),
            EntityTypeSummary(name="Donor", count=3),
        ]

    async def list_entity_types(self):
        return self._entity_types

    async def list_entities(self, entity_type, page=1, filter_text=""):
        return PagedResult(items=[], page=1, total_pages=1, total_items=0)

    async def get_entity(self, entity_type, entity_id):
        return EntityDetail(
            id=entity_id, entity_type=entity_type, fields={}, relationships=[]
        )

    async def get_schema(self):
        return SchemaView()

    async def get_provenance(self, entity_type, entity_id):
        return []


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_app_mounts_with_mock_backend():
    """MosaicTUIApp can be created with a MockBackend without error."""
    from mosaic.tui.app import MosaicTUIApp

    backend = MockBackend()
    app = MosaicTUIApp(backend=backend)
    assert app is not None


def test_schema_cache_starts_empty():
    """Schema cache is None before on_mount is called."""
    from mosaic.tui.app import MosaicTUIApp

    backend = MockBackend()
    app = MosaicTUIApp(backend=backend)
    assert app.schema_cache is None


def test_get_or_fetch_schema_populates_cache():
    """get_or_fetch_schema populates the schema cache."""
    from mosaic.tui.app import MosaicTUIApp

    backend = MockBackend()
    app = MosaicTUIApp(backend=backend)

    schema = asyncio.run(app.get_or_fetch_schema())
    assert app.schema_cache is schema


def test_invalidate_schema_cache_refetches():
    """invalidate_schema_cache clears and re-fetches."""
    from mosaic.tui.app import MosaicTUIApp

    backend = MockBackend()
    app = MosaicTUIApp(backend=backend)

    schema1 = asyncio.run(app.get_or_fetch_schema())
    schema2 = asyncio.run(app.invalidate_schema_cache())
    # After invalidation, a new schema object is returned
    assert schema2 is not schema1


def test_app_pilot_smoke():
    """Textual pilot: app starts and exits cleanly."""
    from mosaic.tui.app import MosaicTUIApp

    backend = MockBackend()
    app = MosaicTUIApp(backend=backend)

    async def run():
        async with app.run_test(headless=True, size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Command palette → app dispatch (#285)
# ---------------------------------------------------------------------------


def _pick_from_palette(app, query: str):
    """Open the palette, filter to ``query``, select the first match.

    Returns the screen stack (as class names) after the selection settles.
    """

    async def run():
        async with app.run_test(headless=True, size=(100, 30)) as pilot:
            await pilot.pause()
            await app.action_command_palette()
            await pilot.pause()
            await pilot.press(*query)
            await pilot.pause()
            # Focus the filtered list and select its first entry.
            from textual.widgets import ListView

            lv = app.screen.query_one(ListView)
            lv.focus()
            lv.index = 0
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            return [type(s).__name__ for s in app.screen_stack]

    return asyncio.run(run())


def test_palette_entity_type_opens_entity_browser():
    """Picking an entity type in the palette opens its entity browser."""
    from mosaic.tui.app import MosaicTUIApp
    from mosaic.tui.backend.protocol import EntityTypeSchema

    class SchemaBackend(MockBackend):
        async def get_schema(self):
            return SchemaView(entity_types=[EntityTypeSchema(name="Donor")])

    app = MosaicTUIApp(backend=SchemaBackend())
    opened: list[str] = []

    async def fake_open_entity_browser(entity_type, count=0):
        opened.append(entity_type)

    app.open_entity_browser = fake_open_entity_browser
    _pick_from_palette(app, "Donor")
    assert opened == ["Donor"]


def test_palette_command_runs():
    """Picking a built-in command in the palette runs it."""
    from mosaic.tui.app import MosaicTUIApp

    app = MosaicTUIApp(backend=MockBackend())
    ran: list[str] = []

    async def fake_open_query_view():
        ran.append("query")

    app.open_query_view = fake_open_query_view
    _pick_from_palette(app, "Query")
    assert ran == ["query"]
