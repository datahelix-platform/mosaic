# Installing Recipes

Recipes are declarative LinkML schema bundles that extend a Mosaic deployment's schema. This guide covers how to inspect, install, and reproduce recipe deployments.

---

## Recipe sources

A recipe author distributes their recipe as one of:

- A directory containing `recipe.yaml` and `schema.yaml`
- A tarball (`*.tar.gz`) with that directory as its root
- An `https:` URL pointing at a tarball, with a published sha256 digest

---

## Inspecting a recipe

Before installing, use `mosaic recipe inspect` to examine what a recipe declares — without writing any state:

```bash
mosaic recipe inspect ./scrnaseq-1.2.0.tar.gz
```

Example output:

```
id:           org.broad.scrnaseq
name:         scrnaseq
version:      1.2.0
mosaic:       >=0.3,<1.0
created_at:   2026-05-27T14:00:00Z
description:  Single-cell RNA-seq metadata schema fragment
license:      CC-BY-4.0
digest:       sha256:f3a8c9b...
classes:      3
slots:        0
```

To list every class and slot the recipe declares, add `--show-elements`:

```bash
mosaic recipe inspect ./scrnaseq-1.2.0.tar.gz --show-elements
```

`inspect` accepts a directory path, tarball path, `file:` URI, or `https:` URI. No DB writes, no cache writes, no provenance records are created.

---

## Installing a recipe

```bash
mosaic recipe import <source> [--digest <hash>] [--dry-run]
```

**From a local directory or tarball:**

```bash
mosaic recipe import ./scrnaseq-1.2.0/
mosaic recipe import /path/to/scrnaseq-1.2.0.tar.gz
```

**From an HTTPS URL:**

`--digest` is required for `https:` sources. Obtain the digest from the recipe author's release notes or by running `mosaic recipe inspect` on a local copy:

```bash
mosaic recipe import https://zenodo.org/record/12345/files/scrnaseq-1.2.0.tar.gz \
    --digest sha256:f3a8c9b...
```

**Dry run:**

A dry run resolves and validates all dependencies, checks the `mosaic_version` specifier, and checks Reference Loader preconditions — but writes nothing to the database:

```bash
mosaic recipe import ./scrnaseq-1.2.0/ --dry-run
```

### What happens during import

1. Mosaic resolves every `parent` and `requires.recipes` dependency bottom-up, recursively.
2. `mosaic_version` is checked against the running Mosaic version; import fails if the specifier excludes it.
3. Every `requires.reference_loaders` pin is checked against installed loader versions; import fails if any pin is unmet (Mosaic does not install loaders automatically).
4. Each schema fragment is merged through `SchemaManager`, verifying no prefix collisions and no in-place redefinition of upstream classes.
5. An `installed_recipes` entry and a `recipe_imported` provenance event are written for each recipe — all in a single storage transaction.

If anything fails, the entire transaction rolls back — no partial state is left.

### Prefix collision

If a recipe's `name`/`default_prefix` matches a prefix already installed by another recipe, a Reference Loader, or a hand-authored class, import fails immediately with `RecipePrefixCollisionError`. The error message identifies the conflicting prefix and its source. No state is changed.

### Digest mismatch

For `https:` sources, Mosaic verifies the fetched bytes' canonical content hash against the declared `--digest`. A mismatch aborts with `RecipeDigestMismatchError`. The cache is not polluted — the fetch result is discarded.

---

## Content-addressable cache

`HttpsResolver` caches fetched recipes at `~/.mosaic/recipe-cache/<sha256>/`. A cache hit skips the network entirely on subsequent installs of the same digest. Cache eviction is not implemented in v1.

To use a different cache directory, set `MOSAIC_RECIPE_CACHE`:

```bash
export MOSAIC_RECIPE_CACHE=/shared/mosaic-recipe-cache
mosaic recipe import https://zenodo.org/record/12345/files/scrnaseq-1.2.0.tar.gz \
    --digest sha256:f3a8c9b...
```

---

## Reproducing a deployment with a lockfile

!!! note "Phase 4 — coming soon"
    `export-lockfile` and `install-from-lockfile` are not yet available. They ship in Phase 4 of the recipe system.

A **lockfile** captures the exact set of installed recipes — id, version, source URI, and verified digest — so a peer instance can reproduce the same schema state.

**Export the lockfile from an existing instance:**

```bash
mosaic recipe export-lockfile --out recipe.lock.yaml
```

This serialises `mosaic_meta.installed_recipes` as a portable YAML document:

```yaml
lockfile_version: 1
installed_recipes:
  org.broad.scrnaseq:
    id: org.broad.scrnaseq
    version: 1.2.0
    source: https://zenodo.org/record/12345/files/scrnaseq-1.2.0.tar.gz
    digest: sha256:f3a8c9b...
    installed_at: "2026-05-27T14:30:00Z"
    parent: null
```

**Reproduce on a fresh instance:**

```bash
mosaic recipe install-from-lockfile recipe.lock.yaml
```

Mosaic re-fetches each entry via its `source`, verifies each `digest`, and installs in dependency order. The round-trip guarantee: the restored instance will have identical `id`, `version`, and `digest` values for every entry.

Relative paths in a lockfile resolve against the lockfile's directory, so a lockfile alongside local tarballs is fully portable.

---

## v1 limitations

The following limitations apply in v1. Future versions may lift some of them.

| Limitation | Notes |
|---|---|
| **URL availability** | Mosaic cannot guarantee that `source` URIs remain resolvable over time. Use DOI-minted releases (Zenodo, figshare) for maximum link stability. |
| **No signing** | v1 verifies byte integrity (digest) but not authorship. Signing is deferred to v2. |
| **No uninstall** | Recipes cannot be removed from an instance, consistent with Mosaic's no-hard-deletes principle. Schema elements contributed by a recipe may be referenced by existing entities. **Workaround:** rebuild the instance from a lockfile that omits the unwanted recipe. |
| **Bootstrap-only install** | v1 only supports additive imports — the recipe's fragment must not overlap with any already-installed prefix. In-place modification of upstream classes is rejected. Extend upstream classes via LinkML inheritance (`is_a:` + `slot_usage:`). |
| **No registry or discovery** | There is no `mosaic recipe search` command. Find recipes via READMEs, lab wikis, or DOI-minted releases. |
