# TUI — Terminal User Interface

`mosaic tui` launches an interactive, keyboard-driven terminal browser for a
Mosaic instance. It can drive the **Python SDK in-process against a local
database**, or call a **remote `mosaic serve` instance over the REST API** —
the interface is identical in both modes. In SDK mode the TUI is a pure
consumer of the public `MosaicClient` API (queries, writes, provenance, and
validation all go through the SDK); it never touches the storage backend
directly.

```bash
pip install 'datahelix-mosaic[tui]'
mosaic tui
```

## Try it now with the example dataset

The repository ships a complete worked deployment under
[`examples/bibliography/`](https://github.com/BU-Neuromics/hippo/tree/main/examples/bibliography) —
a citation-graph schema (Author, the Publication hierarchy, Venue, Authorship,
Citation) plus a seed script. Two commands give you something to browse:

```bash
cd examples/bibliography
python sdk_example.py     # seeds ./data/mosaic.db with the demo citation graph
mosaic tui                 # auto-detects config.json — no flags needed
```

`mosaic tui` reads `config.json` in the current directory, so it picks up the
example's database and schema automatically. You land in the entity browser
with three Authors, a JournalArticle and Preprint of *"Attention Is All You
Need"*, and a Citation between them. From there you can follow the Authorship
relationships, inspect the preprint's supersession in its provenance history,
and run a full-text search for `transformer` on the Query screen.

## Launching

### SDK mode (local database, default)

```bash
# Use config.json / data/mosaic.db / mosaic.db from the current directory
mosaic tui

# Point at an explicit database and schema
mosaic tui --db data/mosaic.db --schema schemas/
```

| Option | Default | Description |
|--------|---------|-------------|
| `--backend`, `-b` | `sdk` | Backend mode: `sdk` or `rest` |
| `--db` | resolved | SQLite database path. Falls back to `config.json` in the cwd, then `data/mosaic.db`, then `mosaic.db` |
| `--schema` | resolved | LinkML schema file or directory. Falls back to `schemas/` in the cwd, then the bundled `mosaic_core` schema |

### REST mode (remote `mosaic serve`)

```bash
mosaic serve &                       # on the host
mosaic tui -b rest --url http://localhost:8000 --token $TOKEN
```

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | `http://127.0.0.1:8000` | Base URL of the `mosaic serve` instance |
| `--token` | env / `dev-token` | Bearer token. Falls back to the `MOSAIC_TUI_TOKEN` environment variable, then `dev-token` |

The status bar at the bottom always shows the backend mode, the connection
target, and a live connection indicator (green `●` when the backend is
reachable). Connection failures surface as error toasts and in the status bar.

## Screens

### Sidebar

Lists every entity type in the schema with live entity counts, plus entries
for the **Schema** explorer and the **Query** screen. `Enter` opens the
selection in the main panel.

### Entity browser

The main listing for one entity type:

- **Filter** (`f`): incremental text filter over the visible columns.
- **Pagination** (`←` / `→`): 20 entities per page.
- **`Enter`**: open the entity detail screen.
- **`n`** new entity, **`e`** edit, **`a`** availability, **`p`** provenance.

### Entity detail

Shows every field of one entity — system fields (`id`, `is_available`,
`version`) and computed temporal fields (`created_at`, `updated_at`,
`schema_version`, derived from the provenance log at read time) are rendered
dimmed above the user slots. The right panel lists outbound relationships
(`Enter` follows a link and chains detail screens; `Esc` unwinds) and a
preview of the most recent provenance events.

### Create / edit forms

`n` (create) and `e` (edit) open a modal form generated from the entity
type's LinkML schema:

- Enums and booleans render as dropdowns; everything else as typed inputs.
- Values are coerced to the slot's range (`integer`, `float`, `boolean`,
  comma-separated lists for multivalued slots) with inline error messages.
- Required slots are marked `*` and block saving when empty.
- Server-side validation failures are shown inline with the backend's detail
  message.
- `Ctrl+S` saves, `Esc` cancels.

### Availability transitions

Mosaic has **no hard deletes**. `a` opens the availability dialog, which maps
the entity lifecycle statuses onto the `is_available` flag:

| Status | `is_available` |
|--------|----------------|
| `active` | `true` |
| `archived`, `superseded`, `deleted`, `distributed`, `removed` | `false` |

The chosen status (plus an optional free-text note) is recorded as the reason
in the provenance log.

### Provenance history

`p` opens the full append-only history for an entity, newest first, with a
payload inspector showing the diff recorded for the highlighted event.

### Schema explorer

Browses the LinkML schema: entity types, slot ranges, required/multivalued
flags, enum values, and relationships. `Enter` on a relationship jumps to the
target type.

### Query screen

`Ctrl+Q` opens the query screen:

- **Field filters** — `field=value, field2=value2` with AND/OR composition,
  backed by the SDK `QueryEngine`. Shown only when the backend supports
  structured filters (SDK mode); the REST API points you at full-text search
  instead.
- **Full-text search** — backed by the FTS index in both modes.

Results land in a table; `Enter` opens the detail screen.

## Keyboard shortcuts

Press `?` inside the TUI for this list.

| Key | Action |
|-----|--------|
| `q` / `Ctrl+C` | Quit |
| `?` | Help overlay |
| `/` or `Ctrl+P` | Command palette (jump to any entity type or command) |
| `Ctrl+Q` | Query screen |
| `Ctrl+T` | Toggle dark/light theme |
| `Tab` | Cycle focus between panels |
| `↑` / `↓` | Navigate lists |
| `Enter` | Select / drill in |
| `Esc` | Go back / dismiss |
| `r` | Refresh current view (also invalidates the schema cache) |
| `f` | Focus filter bar (entity browser) |
| `←` / `→` | Previous / next page |
| `n` | New entity |
| `e` | Edit entity |
| `a` | Availability transition |
| `p` | Provenance history |

## Architecture notes

The TUI is a pure consumer of the SDK ([design principles](design-principles.md)):
all views talk to a small `TUIBackend` protocol with two implementations —
`SDKBackend` (wraps `MosaicClient`, dispatching sync calls off the event loop)
and `RESTBackend` (an `httpx` async client for `mosaic serve`). Backends
declare capabilities (structured filters, FTS) and the UI gates features
accordingly, so both modes stay first-class. All data loading runs in
Textual workers; the UI never blocks.
