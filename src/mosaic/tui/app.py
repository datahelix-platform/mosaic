"""MosaicTUIApp — Textual application root for the Mosaic TUI.

The app owns the backend, the schema cache, and the main-panel routing;
individual views talk to the backend through the :class:`TUIBackend`
protocol and report failures via :meth:`MosaicTUIApp.report_error`.
"""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from mosaic.tui.backend.protocol import BackendError, SchemaView, TUIBackend
from mosaic.tui.widgets.command_palette import MosaicCommandPalette
from mosaic.tui.widgets.help_overlay import HelpOverlay
from mosaic.tui.widgets.sidebar import EntityTypeSidebar
from mosaic.tui.widgets.status_bar import StatusBar

_DARK_THEME = "textual-dark"
_LIGHT_THEME = "textual-light"


class MosaicTUIApp(App):
    """Mosaic Terminal User Interface.

    A keyboard-driven browser for entities, schemas, queries, and
    provenance, backed by either the Python SDK (local SQLite) or the
    REST API (remote ``mosaic serve``).

    Args:
        backend: The TUIBackend implementation (SDK or REST).
    """

    TITLE = "Mosaic"
    SUB_TITLE = "Metadata Tracking Service"

    CSS = """
    #sidebar {
        width: 28;
        min-width: 20;
        border-right: solid $panel;
    }
    #main-panel {
        width: 1fr;
    }
    #status-bar {
        height: 1;
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("question_mark", "help_overlay", "Help", show=True, key_display="?"),
        Binding("slash", "command_palette", "Palette", show=True, key_display="/"),
        Binding("ctrl+p", "command_palette", "Palette", show=False),
        Binding("tab", "focus_next", "Focus next", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("ctrl+t", "toggle_theme", "Theme", show=True),
        Binding("ctrl+q", "open_query", "Query", show=True),
        Binding("escape", "escape", "Back", show=False),
    ]

    def __init__(self, backend: TUIBackend, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._backend = backend
        self._schema_cache: SchemaView | None = None

    # ------------------------------------------------------------------
    # Backend / schema cache
    # ------------------------------------------------------------------

    @property
    def backend(self) -> TUIBackend:
        return self._backend

    @property
    def schema_cache(self) -> SchemaView | None:
        return self._schema_cache

    @schema_cache.setter
    def schema_cache(self, value: SchemaView | None) -> None:
        self._schema_cache = value

    async def get_or_fetch_schema(self) -> SchemaView:
        """Return cached schema or fetch from backend and cache the result."""
        if self._schema_cache is None:
            self._schema_cache = await self._backend.get_schema()
        return self._schema_cache

    async def invalidate_schema_cache(self) -> SchemaView:
        """Invalidate the schema cache and re-fetch from backend."""
        self._schema_cache = None
        return await self.get_or_fetch_schema()

    # ------------------------------------------------------------------
    # Error reporting
    # ------------------------------------------------------------------

    def _status_bar(self) -> StatusBar | None:
        """The StatusBar on the base screen, or None when not mounted yet.

        The status bar lives on the app's default screen; ``self.query_one``
        only searches the *active* screen, which would miss it whenever a
        detail/provenance/modal screen is pushed on top.
        """
        try:
            return self.screen_stack[0].query_one(StatusBar)
        except Exception:  # noqa: BLE001 — not mounted yet
            return None

    def report_error(self, message: str) -> None:
        """Show *message* as an error toast and in the status bar."""
        status_bar = self._status_bar()
        if status_bar is not None:
            status_bar.set_error(message)
        self.notify(message, title="Error", severity="error", timeout=8)

    def report_success(self, message: str) -> None:
        """Show *message* as a success toast and clear status-bar errors."""
        status_bar = self._status_bar()
        if status_bar is not None:
            status_bar.clear_error()
        self.notify(message, timeout=4)

    # ------------------------------------------------------------------
    # Compose / lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield EntityTypeSidebar(id="sidebar")
            with Vertical(id="main-panel"):
                pass
        yield StatusBar(id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        """Probe the connection, fetch schema, and populate the sidebar."""
        status_bar = self.query_one(StatusBar)
        if hasattr(self._backend, "_db_path"):
            status_bar.set_backend("sdk", str(self._backend._db_path))
        elif hasattr(self._backend, "_url"):
            status_bar.set_backend("rest", self._backend._url)
        self._startup()

    def _startup(self) -> None:
        """Kick off the initial connection probe + sidebar load worker."""
        self.run_worker(self._startup_async(), exclusive=True, group="startup")

    async def _startup_async(self) -> None:
        status_bar = self.query_one(StatusBar)
        try:
            info = await self._backend.connection_info()
            status_bar.set_connection(info)
            if not info.ok:
                self.report_error(f"Cannot reach backend: {info.detail}")
        except BackendError as exc:
            self.report_error(exc.message)
        except Exception:  # noqa: BLE001 — backend may not implement probe
            pass

        try:
            await self.get_or_fetch_schema()
            sidebar = self.query_one(EntityTypeSidebar)
            await sidebar.load_entity_types(self._backend)
        except BackendError as exc:
            self.report_error(exc.message)

    # ------------------------------------------------------------------
    # Main panel routing
    # ------------------------------------------------------------------

    async def switch_main_panel(self, widget: Any) -> None:
        """Replace the main panel contents with *widget*."""
        main_panel = self.query_one("#main-panel")
        await main_panel.remove_children()
        await main_panel.mount(widget)

    async def open_entity_browser(self, entity_type: str, count: int = 0) -> None:
        """Show the entity browser for *entity_type* in the main panel."""
        from mosaic.tui.views.entity_browser import EntityBrowserView

        await self.switch_main_panel(
            EntityBrowserView(entity_type=entity_type, backend=self._backend)
        )
        status_bar = self.query_one(StatusBar)
        status_bar.entity_count = count

    async def open_schema_explorer(self) -> None:
        """Show the schema explorer in the main panel."""
        from mosaic.tui.views.schema_explorer import SchemaExplorerView

        try:
            schema = await self.get_or_fetch_schema()
        except BackendError as exc:
            self.report_error(exc.message)
            return
        await self.switch_main_panel(SchemaExplorerView(schema=schema, app_ref=self))

    async def open_query_view(self) -> None:
        """Show the query screen in the main panel."""
        from mosaic.tui.views.query import QueryView

        try:
            schema = await self.get_or_fetch_schema()
        except BackendError as exc:
            self.report_error(exc.message)
            return
        await self.switch_main_panel(QueryView(backend=self._backend, schema=schema))

    def open_entity_detail(self, entity_type: str, entity_id: str) -> None:
        """Push a detail screen for one entity onto the screen stack."""
        from mosaic.tui.views.entity_detail import EntityDetailScreen

        self.push_screen(
            EntityDetailScreen(
                entity_type=entity_type, entity_id=entity_id, backend=self._backend
            )
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_help_overlay(self) -> None:
        """Show the help/keybindings overlay."""
        await self.push_screen(HelpOverlay())

    async def action_command_palette(self) -> None:
        """Open the command palette."""
        schema = await self.get_or_fetch_schema()
        entity_type_names = [et.name for et in schema.entity_types]
        await self.push_screen(
            MosaicCommandPalette(entity_type_names=entity_type_names)
        )

    async def action_refresh(self) -> None:
        """Invalidate schema cache and refresh sidebar + active view."""
        try:
            await self.invalidate_schema_cache()
            sidebar = self.query_one(EntityTypeSidebar)
            await sidebar.load_entity_types(self._backend)
        except BackendError as exc:
            self.report_error(exc.message)
            return

        active = self.screen
        if hasattr(active, "action_refresh"):
            await active.action_refresh()

    def action_toggle_theme(self) -> None:
        """Toggle between the dark and light themes."""
        self.theme = _LIGHT_THEME if self.theme == _DARK_THEME else _DARK_THEME

    async def action_open_query(self) -> None:
        """Open the query view."""
        await self.open_query_view()

    async def action_escape(self) -> None:
        """Go back / dismiss overlays."""
        if len(self.screen_stack) > 1:
            self.pop_screen()

    # ------------------------------------------------------------------
    # Message handlers — sidebar
    # ------------------------------------------------------------------

    async def on_entity_type_sidebar_entity_type_selected(
        self, message: EntityTypeSidebar.EntityTypeSelected
    ) -> None:
        await self.open_entity_browser(message.entity_type, message.entity_count)

    async def on_entity_type_sidebar_schema_explorer_selected(
        self, message: EntityTypeSidebar.SchemaExplorerSelected
    ) -> None:
        await self.open_schema_explorer()

    async def on_entity_type_sidebar_query_selected(
        self, message: EntityTypeSidebar.QuerySelected
    ) -> None:
        await self.open_query_view()

    # ------------------------------------------------------------------
    # Message handlers — command palette
    # ------------------------------------------------------------------

    # Bound with @on rather than the on_<class>_<message> naming convention so
    # a rename of the palette class can't silently unbind them (#285).

    @on(MosaicCommandPalette.EntityTypeNavigated)
    async def _palette_entity_type_navigated(
        self, message: MosaicCommandPalette.EntityTypeNavigated
    ) -> None:
        await self.open_entity_browser(message.entity_type)

    @on(MosaicCommandPalette.CommandExecuted)
    async def _palette_command_executed(
        self, message: MosaicCommandPalette.CommandExecuted
    ) -> None:
        command = message.command
        if command == "Go to schema":
            await self.open_schema_explorer()
        elif command == "Query":
            await self.open_query_view()
        elif command == "Refresh":
            await self.action_refresh()
        elif command == "Toggle theme":
            self.action_toggle_theme()
        elif command == "Help":
            await self.action_help_overlay()
        elif command == "Quit":
            self.exit()
