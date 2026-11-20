# Recipe Reference

Complete field and command reference for the Mosaic recipe system. For authoring walkthroughs see [Writing a Recipe](writing-a-recipe.md); for install and lockfile walkthroughs see [Installing Recipes](installing-recipes.md).

---

## `RecipeManifest` fields

`recipe.yaml` at the recipe root. Validated against the bundled `recipe_manifest.yaml` LinkML schema at `inspect` and `import` time.

| Field | Required | Type | Notes |
|---|---|---|---|
| `id` | yes | string | Reverse-DNS stable identifier (`org.broad.scrnaseq`). Globally unique; used as the `provided_by` namespace (`recipe.<id>@<version>`) injected on every class and slot at merge time. |
| `name` | yes | string | Short, filesystem-safe slug. Becomes the `default_prefix` of the schema fragment. Must not collide with any installed prefix. |
| `version` | yes | string | Opaque version slug. SemVer recommended, not enforced. |
| `created_at` | yes | datetime | ISO 8601 UTC instant (`"2026-05-27T14:00:00Z"`). |
| `mosaic_version` | yes | string | PEP 440 version specifier (e.g. `">=0.3,<1.0"`). Import fails with `RecipeVersionIncompatibleError` when the running Mosaic version is excluded by this specifier. |
| `description` | no | string | Human-readable, one paragraph. |
| `license` | no | string | SPDX identifier (e.g. `CC-BY-4.0`). |
| `source` | no | string | Author-declared canonical origin URI (e.g. a Zenodo DOI URL). Metadata only — Mosaic never fetches from this field. |
| `author` | no | `RecipeAuthor` | Contact metadata sub-object (see below). |
| `parent` | no | `RecipeRef` | Ancestor recipe for explicit lineage. Auto-resolved as a dependency at install time; does not need to be repeated in `requires.recipes`. |
| `requires` | no | `RecipeRequires` | Peer dependencies block (see below). |

### `RecipeAuthor`

| Field | Notes |
|---|---|
| `name` | Author's full name. |
| `email` | Author's email address. |
| `organization` | Author's institution or organization. |

All fields are optional.

### `RecipeRequires`

| Field | Notes |
|---|---|
| `recipes` | List of `RecipeRef`. Peer recipe dependencies merged bottom-up before the depending recipe. |
| `reference_loaders` | List of `name==version` pin strings. Treated as preconditions only — Mosaic checks each is installed at the declared version; it does not install them automatically (see §10.4.5). |

---

## `RecipeRef` fields

Reused for `parent`, each `requires.recipes[i]` entry, and entries stored in `mosaic_meta.installed_recipes`.

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Matches `RecipeManifest.id` of the referenced recipe. |
| `version` | yes | Matches `RecipeManifest.version`. |
| `source` | yes | `file:` URI, bare path, or `https:` URI. Relative paths in a manifest resolve against the importing recipe's directory; relative paths in a lockfile resolve against the lockfile's directory. |
| `digest` | required for `https:`; optional for `file:` | sha256 canonical content hash (see [Digest algorithm](#digest-algorithm)), lowercase hex, optionally prefixed `sha256:`. Always recorded in `installed_recipes` after install. |

---

## Installed recipe registry

`mosaic_meta.installed_recipes` is a JSON object keyed by recipe `id`. Each entry extends `RecipeRef` with fields recorded at install time.

| Field | Notes |
|---|---|
| `id` | Matches `RecipeManifest.id`. |
| `version` | Matches `RecipeManifest.version`. |
| `source` | The exact URI Mosaic fetched from (post-resolution). |
| `digest` | Verified or computed sha256, always present. |
| `installed_at` | ISO 8601 UTC instant recorded at install time. |
| `parent` | Embedded `RecipeRef` sub-object (carried from the manifest), or null. |

This registry is the source of truth for `mosaic recipe list` and the input to `mosaic recipe export-lockfile`.

---

## Digest algorithm

The canonical content hash is computed over file contents, not tarball bytes, so directory and tarball forms of the same recipe produce identical digests.

Algorithm:

1. Enumerate all files in the recipe directory recursively, relative to the recipe root.
2. Sort the file list lexicographically by relative path.
3. For each file in order, concatenate: `<relative-path>\n<lowercase-hex-sha256-of-file-bytes>\n`.
4. The recipe digest is `sha256(buffer)`, lowercase hex. No files are excluded.

`mosaic recipe inspect` prints the computed digest so authors can verify it before publishing.

---

## CLI commands

### `mosaic recipe inspect`

Parse, validate, and digest a recipe without writing any state.

```bash
mosaic recipe inspect <source> [--show-elements]
```

| Argument / Option | Description |
|---|---|
| `<source>` | Recipe source: directory path, tarball path, `file:` URI, or `https:` URI. |
| `--show-elements` | Also print every class and slot declared in the schema. |

Accepts both directory and tarball forms. No DB writes, no cache writes, no provenance records.

---

### `mosaic recipe import`

Install a recipe and all its dependencies into the current Mosaic instance.

```bash
mosaic recipe import <source> [--digest <hash>] [--dry-run] [--db-path PATH] [--schema-dir DIR]
```

| Argument / Option | Description |
|---|---|
| `<source>` | Recipe source: directory path, tarball, `file:` URI, or `https:` URI. |
| `--digest <hash>` | Declared sha256 content hash (hex, optionally prefixed `sha256:`). Required for `https:` sources. |
| `--dry-run` | Resolve and validate all dependencies without writing any state. |
| `--db-path PATH` | SQLite database path (default: `data/mosaic.db`). |
| `--schema-dir DIR` | Schema directory (default: `schemas/`). |

All writes (schema merge, `installed_recipes` entry, `recipe_imported` provenance event) execute in a single storage transaction. A failure at any step rolls back all writes.

---

### `mosaic recipe export`

Package the locally-authored schema content as a redistributable recipe.

```bash
mosaic recipe export --out PATH [--parent <installed-id>] [--db-path PATH] [--schema-dir DIR]
```

| Option | Description |
|---|---|
| `--out PATH` | Output directory. Writes `recipe.yaml` and `schema.yaml` here; fails if either file already exists. |
| `--parent <installed-id>` | `id` of an installed recipe to declare as the lineage parent. Until `mosaic recipe list` ships (Phase 4), find installed ids in `mosaic_meta.installed_recipes`. |
| `--db-path PATH` | SQLite database path (default: `data/mosaic.db`). |
| `--schema-dir DIR` | Schema directory (default: `schemas/`). |

**Selectivity:** only classes and slots whose `provided_by` annotation is absent or does not start with `recipe.` or `loader.`, and whose containing schema is not a Mosaic framework schema (`mosaic_core`, `recipe_manifest`), are exported. Content contributed by installed recipes or Reference Loaders is excluded.

`requires.recipes` is auto-populated from `is_a:` and slot range references to installed recipe content.

The exported `recipe.yaml` contains stubs (`TODO.set.this`, `TODO-set-name`, `0.0.0`) that the author must replace before publishing.

---

### `mosaic recipe list` *(Phase 4)*

!!! note "Coming in Phase 4"
    This command is not yet available.

List all installed recipes.

```bash
mosaic recipe list
```

Prints `id`, `version`, `source`, and `digest` for every entry in `mosaic_meta.installed_recipes`.

---

### `mosaic recipe extend` *(Phase 4)*

!!! note "Coming in Phase 4"
    This command is not yet available.

Scaffold a new recipe directory that extends an already-installed recipe.

```bash
mosaic recipe extend <installed-id> --out PATH
```

| Argument / Option | Description |
|---|---|
| `<installed-id>` | `id` of the installed recipe to extend (from `mosaic recipe list`). |
| `--out PATH` | Output directory for the new recipe scaffold. |

Writes `recipe.yaml` with `parent` pre-populated from `installed_recipes`, and an empty `schema.yaml`.

---

### `mosaic recipe diff` *(Phase 4)*

!!! note "Coming in Phase 4"
    This command is not yet available.

Show the structural difference between two recipes.

```bash
mosaic recipe diff <a> <b>
```

| Argument | Description |
|---|---|
| `<a>` | First recipe source (directory path, tarball, or URI). |
| `<b>` | Second recipe source. |

---

### `mosaic recipe export-lockfile` *(Phase 4)*

!!! note "Coming in Phase 4"
    This command is not yet available.

Serialize `mosaic_meta.installed_recipes` as a portable lockfile.

```bash
mosaic recipe export-lockfile [--out PATH] [--db-path PATH]
```

| Option | Description |
|---|---|
| `--out PATH` | Output path (default: `recipe.lock.yaml`). |
| `--db-path PATH` | SQLite database path (default: `data/mosaic.db`). |

The lockfile carries `lockfile_version: 1` for forward-compatible parsing. See [Installing Recipes — lockfile flow](installing-recipes.md#reproducing-a-deployment-with-a-lockfile).

---

### `mosaic recipe install-from-lockfile` *(Phase 4)*

!!! note "Coming in Phase 4"
    This command is not yet available.

Reproduce a deployment from a lockfile.

```bash
mosaic recipe install-from-lockfile <file> [--db-path PATH]
```

| Argument / Option | Description |
|---|---|
| `<file>` | Path to `recipe.lock.yaml`. |
| `--db-path PATH` | SQLite database path (default: `data/mosaic.db`). |

Re-fetches each entry via its `source`, verifies each `digest`, and installs in dependency order. Round-trip guarantee: the restored instance will have identical `id`, `version`, and `digest` for every entry.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MOSAIC_RECIPE_CACHE` | `~/.mosaic/recipe-cache/` | Override the cache directory used by `HttpsResolver` for fetched recipes. Useful for shared NFS mounts or CI cache volumes. |

---

## Error reference

| Error | Cause |
|---|---|
| `RecipeManifestError` | `recipe.yaml` fails validation against the `recipe_manifest.yaml` LinkML schema. |
| `RecipePrefixCollisionError` | The recipe's `name`/`default_prefix` collides with an already-installed prefix from any source (recipe, loader, or hand-authored). |
| `RecipeRequiresUnsatisfiedError` | A `requires.recipes` or `requires.reference_loaders` dependency cannot be satisfied. The error message includes the unresolved `RecipeRef.source`. |
| `RecipeVersionIncompatibleError` | The running Mosaic version is excluded by the recipe's `mosaic_version` specifier. |
| `RecipeLineageCycleError` | The `parent` / `requires.recipes` dependency graph contains a cycle. |
| `RecipeSchemaError` | `schema.yaml` fails LinkML validation, or the merge layer rejects the fragment — including attempts to modify an upstream class in-place. |
| `RecipeFetchError` | Resolver could not retrieve the artifact (HTTP error, corrupt tarball, unsupported URI scheme). |
| `RecipeDigestMismatchError` | The fetched recipe's content hash does not match the declared digest. |

Every error includes the recipe `id`, `version`, and the failing `source` URI in its message where those are known.
