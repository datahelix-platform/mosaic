# Writing a Recipe

A **recipe** is a declarative bundle that packages a LinkML schema fragment for sharing between Mosaic deployments. Recipes are identified by a stable reverse-DNS `id`, verified by a sha256 content digest, and installed atomically — making schema contributions reproducible across peer instances.

This guide walks through authoring a recipe from scratch: laying out the files, writing the manifest, declaring dependencies, and publishing.

> **Who this is for:** researchers and engineers authoring schema extensions to share with collaborators. To *install* an existing recipe, see [Installing Recipes](installing-recipes.md).

---

## Recipe layout

A recipe is two files in a directory:

```
my-recipe/
├── recipe.yaml     # manifest
└── schema.yaml     # LinkML schema fragment
```

Both files must live at the recipe root. Tarballs are also accepted — `tar -czf my-recipe.tar.gz my-recipe/` — but the directory inside the tarball must be the recipe root, not the tarball root itself. Both forms produce identical digests.

---

## 1. Write the manifest (`recipe.yaml`)

`recipe.yaml` is the recipe's identity card. Here is a complete example:

```yaml
id: org.broad.scrnaseq
name: scrnaseq
version: 1.2.0
description: Single-cell RNA-seq metadata schema fragment for Mosaic.
created_at: "2026-05-27T14:00:00Z"
mosaic_version: ">=0.3,<1.0"
license: CC-BY-4.0
source: https://zenodo.org/record/12345

author:
  name: Broad Institute Genomics
  email: genomics@broadinstitute.org
  organization: Broad Institute
```

Required fields are `id`, `name`, `version`, `created_at`, and `mosaic_version`. All others are optional but strongly recommended for any recipe you intend to share.

### Field notes

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Reverse-DNS stable identifier (e.g. `org.broad.scrnaseq`). Globally unique; used as the `provided_by` namespace for every class and slot this recipe installs. |
| `name` | yes | Short, filesystem-safe slug. Becomes the `default_prefix` of the schema fragment — all classes and slots your schema contributes are namespaced under this prefix. Must not collide with any prefix already installed in the target instance. |
| `version` | yes | Opaque version slug. SemVer (e.g. `1.2.0`) is strongly recommended. |
| `created_at` | yes | ISO 8601 UTC timestamp (e.g. `"2026-05-27T14:00:00Z"`). |
| `mosaic_version` | yes | PEP 440 version specifier. Import fails with `RecipeVersionIncompatibleError` if the running Mosaic version does not satisfy this constraint. |
| `description` | no | One paragraph, human-readable. |
| `license` | no | SPDX identifier (e.g. `CC-BY-4.0`, `MIT`). |
| `source` | no | Author-declared canonical origin URI — typically a Zenodo DOI URL. Metadata only; Mosaic never fetches from this field. |
| `author` | no | Sub-object with optional `name`, `email`, and `organization` keys. |

---

## 2. Write the schema fragment (`schema.yaml`)

`schema.yaml` is a standard [LinkML](https://linkml.io/) schema document. Keep it focused on what your recipe contributes — do not redeclare classes or slots from an upstream recipe or from Mosaic's own core schema.

```yaml
id: https://example.org/mosaic/scrnaseq
name: scrnaseq
default_prefix: scrnaseq
prefixes:
  linkml: https://w3id.org/linkml/
  scrnaseq: https://example.org/mosaic/scrnaseq/

imports:
  - linkml:types

classes:
  SingleCellExperiment:
    description: A single-cell RNA-seq experiment.
    is_a: Entity
    attributes:
      assay_type:
        range: string
        required: true
      genome_build:
        range: string
      cell_count:
        range: integer
```

**`default_prefix` must match `recipe.yaml` `name`.** This is how Mosaic namespaces your classes as `scrnaseq:SingleCellExperiment` in the merged schema. Mosaic injects a `provided_by: recipe.org.broad.scrnaseq@1.2.0` annotation on every class and slot at import time — do not write this annotation yourself. The manifest identity always wins.

### Overriding upstream classes

In-place modification of a class or slot contributed by another recipe is rejected at import time. To specialize an upstream class, use LinkML's native inheritance:

```yaml
classes:
  AnnotatedExperiment:
    is_a: upstream:BaseExperiment    # extend, not replace
    attributes:
      tissue_type:
        range: string
```

This model is stable across recipe versions: as long as the upstream class exists, the subclass remains valid. No coordination with the upstream recipe author is needed.

---

## 3. Declare lineage with `parent`

If your recipe builds on an existing recipe, declare that lineage in the manifest:

```yaml
parent:
  id: org.broad.base-omics
  version: 2.0.0
  source: https://zenodo.org/record/11111/files/base-omics-2.0.0.tar.gz
  digest: sha256:abc123...
```

The `parent` pointer tells Mosaic to install the parent recipe first, then yours. A child manifest does **not** need to also list its parent in `requires.recipes` — the `parent` pointer is itself a typed dependency.

!!! note
    To scaffold a new recipe that extends an already-installed recipe, use `mosaic recipe extend` (Phase 4). It creates a new directory with `parent` pre-populated from the installed `installed_recipes` registry.

---

## 4. Declare peer dependencies (`requires`)

If your schema references classes or slots from other recipes, list those as peer dependencies:

```yaml
requires:
  recipes:
    - id: org.broad.base-omics
      version: 2.0.0
      source: https://zenodo.org/record/11111/files/base-omics-2.0.0.tar.gz
      digest: sha256:abc123...
```

Peer dependencies are recipes that are siblings or prerequisites, not ancestors. At install time, Mosaic resolves the full dependency graph bottom-up: parent → requires.recipes → self.

### Reference Loader preconditions

If your schema uses classes or data contributed by a Reference Loader, declare the loader as a precondition:

```yaml
requires:
  reference_loaders:
    - ensembl==homo_sapiens.GRCh38.110
```

Mosaic checks that each pinned loader is installed at the declared version before importing the recipe. It does **not** install Reference Loaders automatically. If the loader is absent or at a different version, import fails with `RecipeRequiresUnsatisfiedError`.

---

## 5. Compute and verify the digest

Before publishing, verify the canonical content digest with `mosaic recipe inspect`:

```bash
mosaic recipe inspect ./my-recipe/
```

Example output:

```
id:           org.broad.scrnaseq
name:         scrnaseq
version:      1.2.0
mosaic:       >=0.3,<1.0
created_at:   2026-05-27T14:00:00Z
digest:       sha256:f3a8c9b...
classes:      3
slots:        0
```

Copy the `digest` value and include it in any `RecipeRef` that references your recipe. The digest is computed over sorted file paths and their contents — identical for directory and tarball forms of the same recipe.

To see every class and slot:

```bash
mosaic recipe inspect ./my-recipe/ --show-elements
```

---

## 6. Exporting a live schema as a recipe

If you have been working directly in a live Mosaic instance and want to package your locally-authored schema content as a recipe, use `mosaic recipe export`:

```bash
mosaic recipe export --out ./my-recipe/
```

This writes `recipe.yaml` and `schema.yaml` to `./my-recipe/`. Only classes and slots you authored directly are included — content imported from other recipes or Reference Loaders is excluded automatically. The export also auto-populates `requires.recipes` from any upstream recipes your local classes reference via `is_a:` or slot ranges.

The exported `recipe.yaml` contains stubs you must replace before publishing:

```yaml
id: TODO.set.this       # ← replace with your reverse-DNS id
name: TODO-set-name     # ← replace with a short slug
version: 0.0.0          # ← replace with your version
mosaic_version: ">=0.3"  # ← adjust compatibility range
```

To declare a parent lineage on the export, pass the `id` of an already-installed recipe with `--parent`. Until `mosaic recipe list` ships in Phase 4, find installed recipe ids by reading `mosaic_meta.installed_recipes` directly from the database:

```bash
mosaic recipe export --parent org.broad.base-omics --out ./my-recipe/
```

---

## 7. Publishing

Mosaic has no built-in recipe registry. The recommended distribution pattern for publicly-shared recipes is a **DOI-minted release** (Zenodo, figshare, or similar). DOI-backed releases give stable, citable URIs that consumers can pin in `RecipeRef.source`.

Suggested steps:
1. Tag your recipe directory in a git repository.
2. Archive a release tarball: `tar -czf scrnaseq-1.2.0.tar.gz scrnaseq-1.2.0/`
3. Upload to Zenodo (or similar), mint a DOI, and note the direct download URL.
4. Run `mosaic recipe inspect` on the tarball to obtain the final digest.
5. Include the download URL and digest in your recipe's README so consumers can pin them.

!!! warning "Avoid mutable URLs"
    URLs like `latest.tar.gz` are technically accepted as `source` values but are self-defeating with digest verification. The digest catches byte-level changes, but the lockfile still records a moving target. Use versioned, immutable URLs for published recipes.

For lab-internal recipes, a `file:` URI or bare path is sufficient.
