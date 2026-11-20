# Installation

Mosaic is a LinkML runtime. This guide covers installing Mosaic in production and development environments.

## Upgrading from Hippo

Mosaic is the new name of the component previously distributed as **Hippo**
(ADR-0004). Since v0.16.0 the `hippo` spellings are gone entirely
(ADR-0015, platform ADR-0007), so an upgrade from a pre-0.16 release is a
rename plus a rebuild:

- **Distribution and imports:** install `datahelix-mosaic` and import
  `mosaic`. `import hippo`, `HippoClient`, `HippoError`, `HippoConfig` and
  `load_hippo_config` were removed; use `MosaicClient`, `MosaicError`,
  `MosaicConfig` and `load_mosaic_config`.
- **CLI:** the `hippo` console script was removed; run `mosaic`.
- **Config:** only `config.json`, `mosaic.yaml` and `mosaic.yml` are
  auto-detected; rename a `hippo.yaml`. The default SQLite database is
  `data/mosaic.db` (a `data/hippo.db` is no longer picked up).
- **Environment variables:** only the `MOSAIC_*` spellings are read
  (`MOSAIC_CACHE_DIR`, `MOSAIC_RECIPE_CACHE`, `MOSAIC_TUI_TOKEN`,
  `MOSAIC_DATABASE_URL`, `MOSAIC_CONFIG`, …). Cache defaults moved to
  `~/.cache/mosaic/references/` and `~/.mosaic/recipe-cache/`.
- **Plugins:** only the `mosaic.*` entry-point groups are read; a plugin
  registered only under `hippo.*` is no longer discovered. Name loader
  distributions `mosaic-reference-<name>`.
- **Schemas and data:** schemas import `mosaic_core` (not `hippo_core`) and
  use `mosaic_*` annotation keys; the internal tables are `mosaic_meta` and
  `mosaic_xref_index`. There is no migration: rebuild the database from
  source data. The full old → new table is in the CHANGELOG's v0.16.0
  `### Upgrading` section.

## Requirements

- Python 3.11 or later
- pip or uv package manager (uv recommended for faster installs)

## Install Methods

### Using pip

```bash
pip install datahelix-mosaic
```

### Using uv

```bash
uv add datahelix-mosaic
```

## Development Install

To set up Mosaic for local development:

1. Clone the repository:
   ```bash
   git clone https://github.com/your-org/mosaic.git
   cd mosaic
   ```

2. Install with development dependencies:
   ```bash
   uv sync --extra dev
   ```

3. Verify the installation by running the test suite:
   ```bash
   uv run pytest
   ```

## Verify Installation

After installation, verify the CLI is working:

```bash
mosaic --help
```

This should display the Mosaic CLI help message with available commands.
