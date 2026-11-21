# Reference Loaders

Reference loaders install community-standard ontology and annotation data (Ensembl genes, FMA anatomy terms, Gene Ontology, etc.) as regular Mosaic entities. Once installed, your schema can reference loader-provided entity types directly — no manual ETL required.

Reference loaders are distributed as `mosaic-reference-<name>` packages on PyPI, discovered automatically when installed alongside Mosaic.

## Installing a loader package

```bash
pip install datahelix-mosaic-reference-ensembl   # example: Ensembl gene annotations
```

After installation, the loader appears in `mosaic reference list`. No further registration steps are needed.

## Commands

### `mosaic reference list`

List all discoverable loaders and show which version (if any) is installed in the current Mosaic database.

```bash
mosaic reference list
```

Example output:

```
NAME       PACKAGE                   PKG VERSION  INSTALLED VERSION
ensembl    mosaic-reference-ensembl   0.3.1        homo_sapiens.GRCh38.110
fma        mosaic-reference-fma       1.0.0        —
go         mosaic-reference-go        2.1.0        2024-01-15
```

---

### `mosaic reference install`

Install a reference dataset. This merges the loader's schema fragment, runs a schema migration, and ingests the data.

```bash
mosaic reference install <name> [--version <v>] [--flag ...]
```

| Argument / Option | Description |
|---|---|
| `<name>` | Loader name (matches the entry point key, e.g. `ensembl`) |
| `--version <v>` | Version slug to install. Defaults to the latest non-test version the loader reports. |
| `--<param> <val>` | Loader-specific flags auto-rendered from the loader's parameter schema (see below). |

**Examples:**

```bash
# Install the latest Ensembl version
mosaic reference install ensembl

# Install a specific organism + release
mosaic reference install ensembl --version mus_musculus.GRCm39.115

# Pass loader-specific parameters
mosaic reference install ensembl --organism homo_sapiens --gene-biotypes protein_coding,lncRNA
```

Re-installing the same name + version is a silent no-op (`status: already_installed`).

---

### `mosaic reference upgrade`

Upgrade an already-installed loader to a newer version.

```bash
mosaic reference upgrade <name> [--version <v>] [--prune-old] [--flag ...]
```

| Option | Description |
|---|---|
| `--version <v>` | Target version slug. Defaults to the latest non-test version. |
| `--prune-old` | Deactivates the prior version's rows (sets `is_available = false`) so they no longer appear in default queries. The rows remain in the database and are recoverable via the provenance log. **Opt-in — changes the default queryable surface.** Without this flag the old rows remain queryable alongside the new ones. |
| `--<param> <val>` | Loader-specific flags (same as `install`). |

**Default (additive) behavior:** new entities are ingested alongside existing ones. Prior-version rows remain queryable — foreign-key references in your data that point at the old version continue to resolve. Use `--prune-old` only when you are certain no user data references the prior version's entities; it deactivates those rows (marks them unavailable and writes an `availability_change` provenance record) rather than removing them, so they remain auditable and recoverable.

!!! note "Atomic gating guarantee"
    `--prune-old` only runs after a fully clean load completes. If the new version fails mid-ingestion, the prior rows stay queryable — no deactivation occurs. This makes the flag safe to use: partial upgrades cannot leave you with both the old and new versions partially deactivated.

```bash
# Additive upgrade — old rows stay
mosaic reference upgrade ensembl --version homo_sapiens.GRCh38.111

# Opt-in: deactivate prior version's entities after successful install
mosaic reference upgrade ensembl --version homo_sapiens.GRCh38.111 --prune-old
```

---

### `mosaic reference clean-cache`

Remove locally cached download files used by reference loaders.

```bash
# Clear cache for a single loader
mosaic reference clean-cache ensembl

# Clear the entire reference cache
mosaic reference clean-cache
```

Clearing the cache does not uninstall any data from the Mosaic database — it only removes the on-disk download cache so the next `install` or `upgrade` will re-fetch source files.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MOSAIC_CACHE_DIR` | `~/.cache/mosaic/references/` | Root directory for all reference loader download caches. Set this to a shared path (e.g., an NFS mount or CI cache volume) to avoid redundant downloads across machines. |

Each loader gets its own subdirectory under the cache root: `$MOSAIC_CACHE_DIR/<loader_name>/`.

---

## Declaring required loaders in your schema

Add a `requires:` block to `schema.yaml` to declare which reference loaders your schema depends on:

```yaml
# schema.yaml
requires:
  - mosaic-reference-fma==3.3
  - mosaic-reference-ensembl==0.1.0
```

Only exact-match pins (`==`) are supported in v1, and the pin's right-hand side is matched against the **installed pip package version** of the loader (`importlib.metadata.version`) — not the data-version slug you pass to `mosaic reference install --version` (see [Installing a loader package](#installing-a-loader-package) above). A future v2 surface will support pinning against the data-version slug directly via `mosaic_meta.reference_versions`. If you need a minimum version, pin the lowest acceptable release and upgrade explicitly with `mosaic reference upgrade`.

`mosaic validate` and `mosaic migrate` both check `requires:` before doing anything else — each fails fast with a clear install suggestion if a required loader is missing or its installed version disagrees with the pin.

### Getting a client that spans your schema + its loaders

When your schema declares `requires:`, Mosaic automatically merges every pinned loader's classes into the registry, so a single client knows both your own entity types **and** the reference loaders' types — with no registry-assembly code. Every transport (the CLI, `mosaic serve`, the TUI) does this for you.

From the SDK, build a spanning client in one call:

```python
import mosaic

# Resolves `requires:`, merges the installed loaders' fragments, returns a client.
client = mosaic.client_for_schema("schema.yaml", database_url="data/de.db")

# Look up a reference entity and a consumer entity through the same client:
gene = client.get("Gene", gene_id)                       # loader-provided type
ann  = client.put("DEResult", {"gene": gene_id, ...})    # your own type, linking to it
```

`mosaic.registry_for_schema("schema.yaml")` returns just the spanning `SchemaRegistry` if you only need schema introspection. Both raise a `SchemaError` (the same gate as `mosaic validate`) when a declared loader is not installed.

### Referencing loader-provided entity types

Reference a loader-provided class from your own schema using the loader-prefixed form `<loader_name>:<TypeName>` — or the bare class name. Both resolve against the loader classes merged in via `requires:`:

```yaml
# schema.yaml
requires:
  - mosaic-reference-ensembl==0.1.0
classes:
  SampleAnnotation:
    attributes:
      gene:
        range: ensembl:Gene     # class provided by mosaic-reference-ensembl
        # equivalently: range: Gene
```

A slot ranged on a merged loader class is recognized as a cross-loader reference: it participates in joins and expansion and is validated against the loader class, rather than treated as an opaque value. The loader-prefixed form resolves only when the named class was actually provided by that loader. References *between* installed loaders remain advisory in v1.

---

## The `"test"` version slug

Well-behaved loaders expose a `"test"` pseudo-version that installs a small, deterministic, network-free fixture dataset bundled with the package:

```bash
mosaic reference install ensembl --version test
```

Use `--version test` in CI pipelines to avoid network dependencies and keep test runs hermetic and fast. The `"test"` slug is reserved — Mosaic will never use it for a real release version.

---

## Tracking installed loaders

Mosaic records installed loader versions in `mosaic_meta` under the key `reference_versions`. `mosaic status` surfaces this alongside other system information:

```
$ mosaic status
...
Reference loaders:
  ensembl    homo_sapiens.GRCh38.110
  go         2024-01-15
```

The recorded version is what Mosaic uses as the `from_version` baseline when you later run `mosaic reference upgrade`.

Mosaic's **recipe system** is a complementary, lower-level mechanism for sharing schema fragments — where Reference Loaders bring data into a deployment, recipes bring the schema that gives that data its shape. In v1, Reference Loaders and recipes coexist unchanged: loaders continue on their existing install path and are not yet recipe producers. Recipes can declare a loader as a precondition (via `requires.reference_loaders`) to ensure the loader is installed before schema import proceeds. For details, see [Installing Recipes](installing-recipes.md) and [Writing a Recipe](writing-a-recipe.md).
