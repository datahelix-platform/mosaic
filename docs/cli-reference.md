# Mosaic CLI Reference

Complete reference for all `mosaic` CLI commands.

## Global Options

| Option | Description |
|--------|-------------|
| `--help` | Show help message and exit |
| `--version` | Show Mosaic version (if available) |

## init

Initialize a new Mosaic project with the specified template.

### Usage

```bash
mosaic init [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--path`, `-p` | `string` | `None` | Project directory path |
| `--template`, `-t` | `string` | `"basic"` | Template to use: `basic`, `minimal`, or `full` |
| `--force`, `-f` | `boolean` | `false` | Force initialization even if directory exists |

### Example

```bash
# Initialize with default (basic) template
mosaic init

# Initialize in a specific directory
mosaic init --path /path/to/project

# Initialize with full template, overwriting existing
mosaic init --template full --force
```

---

## serve

Start the Mosaic REST API server.

### Usage

```bash
mosaic serve [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--host`, `-h` | `string` | `"127.0.0.1"` | Host address to bind the server to |
| `--port`, `-p` | `integer` | `8000` | Port number to listen on |
| `--reload`, `-r` | `boolean` | `false` | Enable auto-reload during development |
| `--workers`, `-w` | `integer` | `None` | Number of worker processes to use |
| `--log-level`, `-l` | `string` | `"info"` | Logging level: `debug`, `info`, `warning`, `error` |

### Example

```bash
# Start with default configuration
mosaic serve

# Start on custom port with debug logging
mosaic serve --port 9000 --log-level debug

# Enable auto-reload for development
mosaic serve --reload

# Run with multiple workers
mosaic serve --workers 4
```

---

## tui

Launch the interactive terminal browser. Requires the `tui` extra
(`pip install 'datahelix-mosaic[tui]'`). See the **[TUI guide](tui.md)** for screens
and keyboard shortcuts.

### Usage

```bash
mosaic tui [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--backend`, `-b` | `string` | `"sdk"` | Backend mode: `sdk` (local database) or `rest` (remote `mosaic serve`) |
| `--db` | `string` | resolved | SQLite database path (sdk mode). Falls back to `config.json`, then `data/mosaic.db`, then `mosaic.db` |
| `--schema` | `string` | resolved | LinkML schema file or directory (sdk mode). Falls back to `schemas/`, then the bundled `mosaic_core` schema |
| `--url` | `string` | `"http://127.0.0.1:8000"` | Base URL (rest mode) |
| `--token` | `string` | env / `dev-token` | Bearer token (rest mode). Falls back to `MOSAIC_TUI_TOKEN` |

### Example

```bash
# Browse the local database in the current project
mosaic tui

# Explicit database + schema
mosaic tui --db data/mosaic.db --schema schemas/

# Connect to a remote mosaic serve instance
mosaic tui -b rest --url http://mosaic.example.org:8000 --token "$MOSAIC_TUI_TOKEN"
```

---

## migrate

Run schema migrations based on YAML schema definitions.

### Usage

```bash
mosaic migrate [TARGET] [OPTIONS]
```

### Arguments

| Argument | Type | Description |
|-----------|------|-------------|
| `target` | `string` | Target version (not used in v0.1) |

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--dry-run`, `--preview` | `boolean` | `false` | Preview migrations without applying |
| `--schema-dir` | `string` | `"schemas/"` | Path to schema directory |
| `--db-path` | `string` | `"data/mosaic.db"` | Path to SQLite database |

### Example

```bash
# Run migrations with defaults
mosaic migrate

# Preview migrations without applying
mosaic migrate --dry-run

# Use custom schema and database paths
mosaic migrate --schema-dir custom/schemas --db-path custom/data/mosaic.db
```

---

## validate

Validate a LinkML schema file and/or an instance YAML bundle.

### Usage

```bash
mosaic validate [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--schema`, `-s` | `string` | `None` | Path to a LinkML schema file or directory |
| `--data`, `-d` | `string` | `None` | Path to an instance YAML bundle to validate against `--schema` |
| `--config`, `-c` | `string` | `None` | Path to a Mosaic config file to validate |
| `--verbose`, `-v` | `boolean` | `false` | Show detailed validation output |

`--data` requires `--schema`. `--config` is independent.

### Example

```bash
# Validate a LinkML schema — exits non-zero on any LinkML error
mosaic validate --schema schemas/brain_study.yaml

# Validate a data bundle against a schema
mosaic validate --schema schemas/brain_study.yaml --data data/samples.yaml

# Validate a Mosaic config file
mosaic validate --config config/production.yaml
```

---

## ingest

Ingest a LinkML-native instance YAML bundle into Mosaic, or load data via a generic loader.

The default path (`--file`) accepts a **tree-root bundle**: a YAML mapping whose top-level keys are pluralized class names (`samples:`, `projects:`, …) and whose values are lists of instance dicts. Identity is by the `id` slot on each instance — re-ingesting an existing id updates it in place.

CSV/JSON/SQL operational data files are not accepted here; pass `--type` + `--config` for those.

### Usage

```bash
mosaic ingest [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--file`, `-f` | `string` | `None` | Path to a LinkML-native instance YAML bundle |
| `--validate-schema` | `string` | `None` | Path to a LinkML schema file or directory; bundle is validated before any writes |
| `--dry-run` | `boolean` | `false` | Validate and preview without writing |
| `--type`, `-t` | `string` | `None` | Loader type for generic loaders: `csv`, `json`, or `sql` |
| `--config`, `-c` | `string` | `None` | Path to loader config file (for `--type csv/json/sql`) |

### Example

```bash
# Ingest a bundle, validating it against the schema first
mosaic ingest --file data/samples.yaml --validate-schema schemas/brain_study.yaml

# Dry run — validate and show what would be written
mosaic ingest --file data/samples.yaml --validate-schema schemas/brain_study.yaml --dry-run

# Generic CSV loader (separate path)
mosaic ingest --type csv --file donors.csv --config donors_mapping.yaml
```

---

## entity

Read-only inspection of stored entities. All subcommands default to YAML
output; pass `--json` for machine-readable JSON. A missing database is an
error — these verbs never create one.

### Usage

```bash
mosaic entity <subcommand> [OPTIONS]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `get TYPE ID` | Fetch a single entity by type and id (`--expand` for related entities) |
| `query TYPE` | List entities; `--filter field=value` (repeatable, AND), `--limit`/`--offset` |
| `search TYPE QUERY` | Full-text search over fields declared with `mosaic_search` |
| `history ID` | Provenance trail for an entity, oldest first |

### Common Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--db-path` | `string` | `data/mosaic.db` | Path to the SQLite database |
| `--schema` | `string` | `None` | LinkML schema file or directory (required to recognize user-domain classes) |
| `--json` | `boolean` | `false` | Emit JSON instead of YAML |

### Example

```bash
# Fetch one entity
mosaic entity get Sample 6f1a... --schema schemas/ --json

# All brain samples
mosaic entity query Sample --filter tissue_type=brain --schema schemas/

# Who touched this entity, and when?
mosaic entity history 6f1a... --schema schemas/
```

---

## status

Show deployment status: Mosaic version, storage adapter, schema version,
per-type entity counts, and adapter capability declarations. Mirrors the
REST `GET /status` endpoint and the SDK's `client.status()`.

### Usage

```bash
mosaic status [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--db-path` | `string` | `data/mosaic.db` | Path to the SQLite database |
| `--schema` | `string` | `None` | LinkML schema file or directory |
| `--json` | `boolean` | `false` | Emit JSON instead of YAML |

### Example

```bash
mosaic status --schema schemas/ --json
```

---

## reference

Manage reference data loader packages.

### Usage

```bash
mosaic reference <subcommand> [OPTIONS]
```

### Subcommands

#### reference install

Install a reference loader package.

```bash
mosaic reference install <PACKAGE> [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `package` | `string` | **required** | Package name to install |
| `--source`, `-s` | `string` | `None` | Package source (URL or local path) |

**Example:**

```bash
mosaic reference install my-loader
mosaic reference install my-loader --source https://example.com/loader.tar.gz
```

#### reference list

List installed reference loader packages.

```bash
mosaic reference list
```

**Example:**

```bash
mosaic reference list
```

---

## install-ref

Install reference data from a source.

### Usage

```bash
mosaic install-ref <SOURCE> [OPTIONS]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `source` | `string` | **required** | Source path or URL |

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--name`, `-n` | `string` | `None` | Name for the reference |
| `--force`, `-f` | `boolean` | `false` | Force installation if already exists |

### Example

```bash
# Install from a local path
mosaic install-ref /path/to/reference/data

# Install with a specific name
mosaic install-ref /path/to/reference/data --name my_reference

# Force reinstall
mosaic install-ref /path/to/reference/data --force
```

---

## update-ref

Update an existing reference.

### Usage

```bash
mosaic update-ref <NAME> [OPTIONS]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `name` | `string` | **required** | Name of the reference to update |

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--source`, `-s` | `string` | `None` | New source path or URL |

### Example

```bash
# Update a reference
mosaic update-ref my_reference

# Update with new source
mosaic update-ref my_reference --source /new/path/to/data
```

---

## list-ref

List installed reference data.

### Usage

```bash
mosaic list-ref [NAME] [OPTIONS]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `name` | `string` | `None` | Filter by reference name |

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--format`, `-f` | `string` | `"table"` | Output format: `table`, `json`, or `yaml` |

### Example

```bash
# List all references in table format
mosaic list-ref

# List in JSON format
mosaic list-ref --format json

# List in YAML format
mosaic list-ref --format yaml

# Filter by name
mosaic list-ref sample_ref
```

---

## validate-schema

Validate a LinkML schema file.

### Usage

```bash
mosaic validate-schema <INPUT> [OPTIONS]
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `input` | `string` | **required** | Input LinkML schema file path |

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--format`, `-f` | `string` | `"yaml"` | Input format: `yaml` or `json` |

### Example

```bash
# Validate a LinkML schema
mosaic validate-schema schema.yaml

# Validate a JSON-format schema
mosaic validate-schema schema.json --format json
```

---

## schema-diff

Compare two schema files and show differences.

### Usage

```bash
mosaic schema-diff <FILE1> <FILE2>
```

### Arguments

| Argument | Type | Description |
|----------|------|-------------|
| `file1` | `string` | **required** | First schema file path |
| `file2` | `string` | **required** | Second schema file path |

### Example

```bash
# Compare two schema versions
mosaic schema-diff schemas/v1.yaml schemas/v2.yaml
```
