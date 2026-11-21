## 10. Recipe Subsystem

**Document status:** Draft v0.1
**Depends on:** sec2_architecture.md, sec3_data_model.md, sec6_provenance.md, sec9_linkml_redesign.md
**Feeds into:** appendix_b_implementation_guide.md

---

### 10.1 Overview & Motivation

#### 10.1.1 What Is a Recipe?

A **recipe** is a declarative, content-addressed, version-pinned bundle that packages a LinkML schema fragment so it can be shared and reproduced between Hippo deployments. A recipe consists of two files in a directory (or tarball):

- **`recipe.yaml`** — the manifest: identity, version, authorship, dependencies, and compatibility constraints.
- **`schema.yaml`** — a LinkML schema fragment containing the classes and slots the recipe contributes.

Recipes are identified by a stable reverse-DNS `id` (e.g., `org.broad.scrnaseq`) and a version slug. At install time the schema fragment is merged into the running deployment's live schema via `SchemaManager`, and the import is recorded in provenance. The result is reproducible from the manifest alone.

#### 10.1.2 How Recipes Generalise Reference Loaders

Hippo's existing **Reference Loader** system ships imperative Python code that runs data ingestion logic and registers itself via the `mosaic.reference_loaders` entry point. Reference Loaders handle bulk reference-data loading and are distributed as Python packages.

Recipes occupy a complementary, lower-level role: they carry **schema**, not code. Where a Reference Loader brings data into a running deployment, a recipe brings the schema fragment that gives that data its shape. The two systems coexist in v1 — Reference Loaders remain unchanged; recipes are not yet their schema-publishing mechanism. That convergence is a future phase.

The distinction matters for reproducibility: a deployment's schema state is not fully described by listing which Reference Loaders are installed. Recipes make the schema-contribution layer explicit, version-pinned, and verifiable.

#### 10.1.3 The Reproducibility Problem

A Hippo deployment's operational schema — the union of `mosaic_core`, user-authored classes, and any imported fragments — can diverge across peer instances when schema fragments are loaded manually or evolved in place. There is no portable artifact that describes "exactly the schema this instance uses and where each fragment came from."

Recipes solve this by introducing two primitives:

1. **Content addressing:** every recipe is identified by a sha256 digest over its canonical content. Installing a recipe by URI plus digest is a reproducible, tamper-evident act.
2. **Lockfile:** the `mosaic_meta.installed_recipes` registry captures the full installation record. `mosaic recipe export-lockfile` serialises it as `recipe.lock.yaml`; `mosaic recipe install-from-lockfile` replays it on a peer instance, verifying each digest. A deployment's exact schema state can be transferred as a single file.

---

### 10.2 Architecture

#### 10.2.1 Service Composition

`RecipeService` is a service facade that follows the same pattern as `IngestionService`, `ProvenanceService`, and `QueryService`. It is instantiated once in `MosaicClient.__init__` and holds references to `SchemaManager` and `ProvenanceService`. It never touches `SchemaView` directly for merging — all schema writes flow through `SchemaManager`.

```
MosaicClient
│
├── SchemaManager          (schema access, merge, validation)
├── ProvenanceService      (audit trail)
├── QueryService
├── IngestionService
└── RecipeService          ◄── new in v1
        ├── delegates schema merging  →  SchemaManager.merge_fragment(...)
        ├── delegates provenance      →  ProvenanceService.record(recipe_imported)
        └── holds resolvers           →  [FileResolver, HttpsResolver]
```

`MosaicClient` exposes one thin delegating wrapper per `RecipeService` public method, named `recipe_<verb>` (e.g., `client.recipe_import(source)`). CLI commands call `MosaicClient` delegators; they never reach `RecipeService` directly.

#### 10.2.2 Install Pipeline

The install pipeline for a single recipe proceeds in the following stages. Bottom-up dependency resolution means the entire pipeline runs recursively for each dependency before the depending recipe is merged.

```
   caller
     │
     ▼
 ┌─────────────┐
 │  Resolve    │  FileResolver (file: URIs, bare paths)
 │  source     │  HttpsResolver (https: URIs, cache-hit shortcut)
 └──────┬──────┘
        │  raw bytes / directory
        ▼
 ┌─────────────┐
 │  Cache      │  ~/.mosaic/recipe-cache/<sha256>/
 │  (https     │  Cache hit → skip network on subsequent calls
 │  only)      │
 └──────┬──────┘
        │  recipe directory
        ▼
 ┌─────────────┐
 │  Manifest   │  parse recipe.yaml
 │  validate   │  validate against recipe_manifest.yaml (LinkML)
 └──────┬──────┘
        │  RecipeManifest
        ▼
 ┌─────────────┐
 │  Dep        │  resolve parent + requires.recipes recursively
 │  resolve    │  bottom-up order; cycle detection via visiting set
 └──────┬──────┘
        │  ordered install list
        ▼
 ┌─────────────┐
 │  Schema     │  SchemaManager.merge_fragment(schema.yaml, provided_by=...)
 │  merge      │  prefix-collision check; no-in-place-override check
 └──────┬──────┘
        │  (within same transaction)
        ▼
 ┌─────────────┐
 │  Provenance │  ProvenanceService: emit recipe_imported event
 │  write      │  atomically with the merge
 └─────────────┘
```

All steps from "Schema merge" onward execute within a single storage transaction. A failure at any point leaves no state change in either the schema store or the provenance log.

#### 10.2.3 SDK Interface

```python
class RecipeService:
    def __init__(
        self,
        storage: SQLiteAdapter,
        schema_manager: SchemaManager,
        provenance_service: ProvenanceService,
        cache_dir: Path | None = None,        # default: ~/.mosaic/recipe-cache/
        resolvers: Sequence[RecipeResolver] | None = None,
    ) -> None: ...

    def list_installed(self) -> list[InstalledRecipe]: ...
    def inspect(self, source: str | Path) -> RecipeReport: ...
    def import_(self, source: str | Path, *, dry_run: bool = False) -> ImportResult: ...
    def export(self, *, scope: str = "schema", parent: str | None = None) -> Recipe: ...
    def extend(self, installed_id: str, out_dir: Path) -> Path: ...
    def diff(self, a: str | Path, b: str | Path) -> RecipeDiff: ...
    def export_lockfile(self, out: Path) -> None: ...
    def install_from_lockfile(self, lockfile: Path) -> list[ImportResult]: ...
```

---

### 10.3 Data Model

#### 10.3.1 Recipe Artifact Layout

```
my-recipe/
├── recipe.yaml     # manifest (validated against recipe_manifest.yaml)
└── schema.yaml     # LinkML schema fragment
```

Tarball form: `tar -czf my-recipe.tar.gz my-recipe/` — the contained directory MUST be the recipe root, not the tarball root. Resolvers normalise tarballs by extracting into a temporary directory and operating on the directory form. Both forms produce identical digests (see §10.4.3).

#### 10.3.2 RecipeManifest Fields

Defined in `src/mosaic/schemas/recipe_manifest.yaml`. The LinkML schema is the authoritative field spec.

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Reverse-DNS stable identifier, e.g. `org.broad.scrnaseq`. Globally unique; serves as the namespace for `provided_by` annotation injection. |
| `name` | yes | Short, filesystem-safe. Becomes the `default_prefix` of the schema fragment. Must not collide with any installed prefix. |
| `version` | yes | Opaque slug. SemVer recommended but not enforced. |
| `description` | no | Human-readable, one paragraph. |
| `author` | no | Sub-object: `name`, `email`, `organization`. |
| `license` | no | SPDX identifier. |
| `created_at` | yes | ISO 8601 UTC instant. |
| `mosaic_version` | yes | PEP 440 version specifier (e.g. `">=0.3,<0.5"`). Parsed with `packaging.specifiers.SpecifierSet`. Import fails with `RecipeVersionIncompatibleError` when the running Hippo version is excluded. |
| `source` | no | Author-declared canonical origin URI (e.g. a Zenodo DOI). Metadata only — Hippo never fetches from it. Distinct from per-`RecipeRef` `source` fields. |
| `parent` | no | `RecipeRef`. Present on extended recipes. Auto-resolved as a dependency at install time (see §10.4.4). |
| `requires.recipes` | no | List of `RecipeRef`. Peer dependencies, not ancestors. |
| `requires.reference_loaders` | no | List of pin strings `name==version`. See §10.4.5. |

#### 10.3.3 RecipeRef Shape

`RecipeRef` is a single LinkML class reused for `parent`, `requires.recipes[i]`, and entries in `installed_recipes`.

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Matches `RecipeManifest.id` of the referenced recipe. |
| `version` | yes | Matches `RecipeManifest.version`. |
| `source` | yes (v1) | `file:` URI, bare path, or `https:` URI. Relative paths in a manifest resolve against the importing recipe's directory. Relative paths in a lockfile resolve against the lockfile's directory. |
| `digest` | required for `https:`; optional, always recorded for `file:` | sha256 canonical-content-hash (see §10.4.3). |

#### 10.3.4 Installed Recipes Registry

`mosaic_meta.installed_recipes` is a JSON object keyed by recipe `id`. Each entry extends `RecipeRef` with additional fields captured at install time:

| Field | Notes |
|---|---|
| `id` | Matches `RecipeManifest.id`. |
| `version` | Matches `RecipeManifest.version`. |
| `source` | The exact URI fetched (post-resolution). |
| `digest` | Verified or computed sha256. Always present after install. |
| `installed_at` | ISO 8601 UTC instant. |
| `parent` | Embedded `RecipeRef` sub-object, or null. Carried from the manifest for audit. |

This registry is the input to `mosaic recipe export-lockfile` and the source of truth for `mosaic recipe list`.

#### 10.3.5 Lineage via Parent

The `parent` field establishes explicit recipe lineage. Only `mosaic recipe extend` creates lineage; editing the live schema after a recipe import does NOT retroactively attach those edits to any recipe (invariant 5). The `parent` pointer in an installed recipe's manifest is preserved in `installed_recipes` for audit.

---

### 10.4 Install Semantics

#### 10.4.1 Resolvers

Two resolvers are provided in v1. Both implement the `RecipeResolver` interface:

| Scheme | Resolver | Behaviour |
|---|---|---|
| `file:` URI or bare path | `FileResolver` | Reads from local filesystem. Relative paths resolve against the importing recipe's directory (manifest context) or lockfile directory (lockfile context). |
| `https:` URI | `HttpsResolver` | Fetches over HTTPS. Checks the content-addressable cache before hitting the network. `digest` is mandatory for `https:` sources. |

No other URI schemes are supported in v1. Attempts to resolve a `git:` or `oci:` URI produce `RecipeFetchError`.

#### 10.4.2 Content-Addressable Cache

The cache lives at `~/.mosaic/recipe-cache/<sha256>/`. The cache key is the recipe's sha256 canonical-content-hash digest. A cache hit skips network fetch entirely; the cached directory is used directly. Cache eviction is out of scope for v1.

The cache directory can be overridden with the `MOSAIC_RECIPE_CACHE` environment variable (resolution: open question 3 — adopted; see §10.4.6). This is parallel to Hippo's existing cache-override conventions.

#### 10.4.3 Digest Algorithm

The digest is computed over the recipe's **canonical content hash**, not over tarball bytes (which are repack-sensitive). This yields identical digests for directory and tarball forms of the same recipe.

Algorithm:

1. Enumerate all files in the recipe directory recursively, relative to the recipe root.
2. Sort the file list lexicographically by relative path.
3. Build a buffer by concatenating, for each file in sorted order: `<relative-path-as-utf8>\n<lowercase-hex-sha256-of-file-bytes>\n`.
4. The recipe digest is `sha256(buffer)`, lowercase hex. No files are excluded.

The reference implementation lives in `core/recipe/digest.py` as a single ~20-line function. `mosaic recipe inspect` prints the computed digest so recipe authors can verify it before publishing.

#### 10.4.4 Dependency Resolution

Dependencies are resolved bottom-up: `parent` and all `requires.recipes` entries are installed before the depending recipe is merged. The merge order is:

```
parent (if present) → requires.recipes (in declaration order) → self
```

`parent` is **auto-resolved** as a dependency. A child manifest does NOT need to also list its parent in `requires.recipes`; the parent pointer is itself a typed dependency.

Cycle detection is mandatory across both `parent` and `requires.recipes`. Implementation: maintain a visiting set during the resolution recursion; emit `RecipeLineageCycleError` on revisit.

#### 10.4.5 Reference Loader Preconditions

**Open question 1 (resolved):** `requires.reference_loaders` pins (`name==version`) are treated as **preconditions only** in v1. Hippo checks that each named Reference Loader is installed at the declared version before proceeding with the recipe import. Hippo does NOT install Reference Loaders as a side effect of recipe import. If a required loader is absent or at the wrong version, the import fails with `RecipeRequiresUnsatisfiedError`. This is the handoff recommendation and preserves a clear separation between the schema-only recipe system and the data-loading Reference Loader system.

#### 10.4.6 Cache Directory Override

**Open question 3 (resolved):** The `MOSAIC_RECIPE_CACHE` environment variable overrides the default cache directory `~/.mosaic/recipe-cache/`. This is adopted from the handoff recommendation and is consistent with Hippo's existing conventions for cache path overrides. When set, the env var value is used as the cache root for all `HttpsResolver` fetches in that process.

#### 10.4.7 Atomicity

The schema merge and provenance write execute in a single storage transaction. A failure at any stage — digest mismatch, prefix collision, schema validation rejection, provenance write error — rolls back both. A failed import leaves the database in the state it was before the call. `dry_run=True` skips all state writes and returns an `ImportResult` describing what would have happened.

---

### 10.5 Export Semantics

`mosaic recipe export` packages the locally-authored content of the live schema into a new recipe directory or tarball.

#### 10.5.1 Selectivity

Only classes and slots whose `provided_by` annotation is **absent** or does not begin with `recipe.` or `loader.` are included in the export. Content contributed by installed recipes or Reference Loaders is excluded. This rule:

- Prevents accidental redistribution of upstream schema content.
- Protects attribution by keeping upstream `provided_by` annotations in their source recipes.
- Ensures the exported recipe installs cleanly on a peer instance that already has the same upstream recipes installed, without duplicate-class errors.

#### 10.5.2 Parent and Requires Inference

The exported manifest's `parent` field is left empty by default. If the user passes `--parent <installed-id>`, Hippo populates `parent` from the matching `installed_recipes` entry (carrying `id`, `version`, `source`, `digest`).

The `requires.recipes` list is populated automatically from the set of installed recipes whose content is referenced by `is_a:` or slot ranges in the exported schema fragment. Hippo inspects the schema graph and infers the minimal dependency set.

#### 10.5.3 What Is Not Packaged

- Classes and slots with `provided_by` starting `recipe.` or `loader.` (upstream content).
- Reference data (seed rows). Controlled vocabularies that live as LinkML `permissible_values:` inside `schema.yaml` ARE included; bulk reference data continues to ship via Reference Loaders.
- Runtime state (`mosaic_meta`, entity data, provenance records).

---

### 10.6 Reproducibility Model

#### 10.6.1 Two Primitives

**Content addressing.** A recipe is identified by its sha256 canonical-content-hash digest (§10.4.3). The declared digest in a `RecipeRef` is verified against freshly-fetched bytes at install time; mismatch aborts with `RecipeDigestMismatchError`. For `file:` sources where no digest is declared, the digest is computed and recorded in `installed_recipes`. Tag drift, link rot of mutable URLs, and substitution attacks are all caught loudly.

**Lockfile.** `mosaic recipe export-lockfile` serialises `mosaic_meta.installed_recipes` as `recipe.lock.yaml`. `mosaic recipe install-from-lockfile` re-fetches each entry by its `source`, verifies each `digest`, and installs in dependency order. Together these provide a full reproducibility round-trip: a deployment's exact schema state can be recreated on a peer instance from a single file, provided each entry's `source` URI is still resolvable.

#### 10.6.2 Lockfile Format

`recipe.lock.yaml` is a YAML document. It carries `lockfile_version: 1` at the top level for forward-compatibility parsing. Each entry under `installed_recipes` corresponds to one entry in the `mosaic_meta.installed_recipes` registry and includes `id`, `version`, `source`, `digest`, `installed_at`, and `parent` (if set).

**Open question 2 (resolved):** `recipe.lock.yaml` carries `lockfile_version: 1`. This is adopted from the handoff recommendation. The version key allows future parsers to branch on lockfile format without breaking existing lockfiles.

Example:

```yaml
lockfile_version: 1
installed_recipes:
  org.broad.scrnaseq:
    id: org.broad.scrnaseq
    version: 1.2.0
    source: https://zenodo.org/record/12345/files/scrnaseq-recipe-1.2.0.tar.gz
    digest: sha256:abc123...
    installed_at: "2026-05-27T14:30:00Z"
    parent: null
```

#### 10.6.3 Round-Trip Guarantee

The round-trip guarantee is: export-lockfile on instance A, install-from-lockfile on instance B → B's `installed_recipes` registry has identical `id`, `version`, and `digest` values for every entry. Schema shape is therefore identical (modulo user-authored local classes).

#### 10.6.4 Documented Limits

The following limits apply in v1 and are documented in user-facing CLI reference pages:

| Limit | Note |
|---|---|
| URL availability | Hippo cannot guarantee that `source` URIs remain resolvable. Canonical recipes should use DOI-minted releases (Zenodo, figshare) to maximise link stability. |
| Mutable URLs | Mutable tags (e.g. `latest.tar.gz`) are technically permitted but self-defeating with the mandatory `digest` check. The lockfile will prevent silent version drift, but the URI still points at a moving target. Spec discourages but does not forbid. |
| No signing | v1 verifies byte integrity (digest) but not authorship. A compromised `source` URI serving bytes that hash to the declared digest cannot be detected. Signing is a v2 concern. |

---

### 10.7 Override & Extension Model

#### 10.7.1 LinkML-Native Inheritance

Schema overrides in v1 are achieved via **LinkML's native inheritance**:

- To specialise an upstream class, authors declare a new class with `is_a: upstream:UpstreamClass` and add slots via `attributes:` or `slot_usage:`.
- To constrain an upstream slot, authors use `slot_usage:` on the subclass.
- To add a slot to an upstream class across the deployment (not across instances), authors use a locally-authored slot and reference it from a subclass.

This model is safe across recipe versions: as long as the upstream class continues to exist, the subclass remains valid. No coordination with the upstream recipe author is required to add specialisations.

#### 10.7.2 In-Place Override Is Rejected

`SchemaManager.merge_fragment` enforces the following check: if a recipe's schema fragment attempts to modify a class or slot whose `provided_by` annotation names a different recipe or loader, the merge is rejected with `RecipeSchemaError`. This is invariant 6.

The rationale: in-place override of upstream classes creates hidden dependencies between the overriding deployment and the upstream recipe author's future changes. It also makes the `provided_by` annotation semantics ambiguous. Users who need to change upstream behaviour must subclass.

#### 10.7.3 The `extend` Workflow

`mosaic recipe extend <installed-id> --out <dir>` scaffolds a new recipe directory pre-populated with:

- A `recipe.yaml` manifest with `parent` set to a `RecipeRef` for `<installed-id>` (resolved from `installed_recipes`).
- An empty `schema.yaml` importing the parent's prefix, ready for local additions.

The extended recipe's manifest carries explicit `parent` lineage. When installed, the parent is auto-resolved and installed first (§10.4.4). Lineage is explicit and traceable: `mosaic recipe list` shows the `parent` field for each installed recipe.

No implicit lineage is created by any other operation (invariant 5). Editing the live schema after a recipe import does not retroactively attach those edits to the recipe.

---

### 10.8 Error Model & Provenance

#### 10.8.1 Error Hierarchy

All recipe errors are defined in `core/exceptions.py` and inherit from Hippo's existing base error class. Every error includes the recipe `id` + `version` and the failing `RecipeRef.source` in its message when those are known.

| Error | Cause |
|---|---|
| `RecipeManifestError` | Manifest fails LinkML validation against `recipe_manifest.yaml`. |
| `RecipePrefixCollisionError` | Incoming recipe's `name`/`default_prefix` collides with an installed prefix from any source (recipe, loader, or hand-authored). |
| `RecipeRequiresUnsatisfiedError` | A declared `requires.recipes` or `requires.reference_loaders` dep cannot be resolved. Message includes the unresolved `RecipeRef.source` URI. |
| `RecipeVersionIncompatibleError` | `mosaic_version` SpecifierSet excludes the running Hippo version. |
| `RecipeLineageCycleError` | Parent / requires graph contains a cycle. |
| `RecipeSchemaError` | Embedded `schema.yaml` fails LinkML validation, or `SchemaManager.merge_fragment` rejects the fragment (including the no-in-place-override check, invariant 6). |
| `RecipeFetchError` | Resolver could not retrieve the artifact (404, network error, unreadable tarball, unsupported URI scheme). |
| `RecipeDigestMismatchError` | Fetched bytes' canonical-content-hash digest does not match the digest declared in the `RecipeRef`. |

#### 10.8.2 Provenance Event: `recipe_imported`

One new provenance event kind is introduced. Its schema lives in `mosaic_core.yaml` alongside existing event kinds. Exactly one `recipe_imported` event is emitted per top-level `import_()` call; bottom-up dependency installs each emit their own event in the same transaction.

| Field | Notes |
|---|---|
| `recipe_id` | |
| `recipe_version` | |
| `recipe_digest` | The verified-or-computed sha256. Always present. |
| `recipe_source` | The exact URI Hippo fetched from (post-resolution). |
| `parent` | `RecipeRef` or null. |
| `classes_added` | List of qualified class names introduced by this import. |
| `slots_added` | List of qualified slot names introduced by this import. |

The provenance write is part of the same storage transaction as the schema merge (invariant 3). A failed merge means no `recipe_imported` event. A failed provenance write rolls back the schema merge.

---

### 10.9 Out of Scope (v1)

The following capabilities are explicitly deferred. Each entry names the rationale and the expected v2 surface.

| Feature | Rationale | v2 pointer |
|---|---|---|
| **Federation / HTTP `/recipe` endpoint** | Serving recipes over HTTP from a Hippo instance requires auth, versioning, and discovery design that is premature before adoption patterns are clear. | `mosaic serve` will expose a `/recipes` read endpoint in a future phase. |
| **Overlay / harmonize import modes** | Bootstrap-only install (schema must be additive) keeps the merge logic simple and safe. Overlay (modifying existing schema elements via import) requires a 3-way merge and conflict-resolution semantics not yet designed. | `mosaic recipe import --mode overlay` in v2. |
| **Data sidecar / seed rows** | Separating schema from data keeps recipes portable and avoids conflating schema install with data migration. Controlled vocabularies expressible as `permissible_values:` are included in `schema.yaml`; bulk reference data ships via Reference Loaders. | Recipe-bundled seed rows (`data.yaml`) in a future phase. |
| **Signing / authorship trust** | v1 verifies byte integrity via sha256. Signing requires a trust model (sigstore, minisign) and key distribution not yet designed. | Recipe signing in v2, likely via sigstore. |
| **Recipe uninstall** | Consistent with Hippo's no-hard-deletes principle. Schema elements contributed by a recipe cannot be removed without potentially breaking entities that reference those types. Workaround: rebuild the instance from a lockfile that omits the unwanted recipe. | A future `mosaic recipe uninstall --force` with explicit cascade warnings may be scoped in v2. |
| **Discovery / search** | No `mosaic recipe search`. Users find recipes via READMEs, lab wikis, and DOI-minted releases. A public registry is a community governance problem, not a core Hippo feature. | Community registry in a future phase, if adoption warrants it. |
| **`git:` / `oci:` URI schemes** | `file:` + `https:` cover the primary distribution patterns. Git and OCI add significant resolver complexity. | `git:` and `oci:` resolvers in v2. |
| **Reference Loader internals using RecipeService** | Reference Loaders continue on their existing code path. Convergence (RL as recipe producer) is a design task for a later phase once both systems are stable. | Convergence phase in a future sprint. |
| **`recipe rebase` / 3-way schema merge** | Rebasing a recipe against an updated parent requires semantic understanding of schema-level diffs that is deferred until `mosaic recipe diff` and `linkml-diff` integration are proven at scale. | `mosaic recipe rebase` in v2. |

---

### 10.10 Architecture Invariants

These invariants must remain true after every PR in the recipe work. An implementing agent must reject its own changes that violate any of them.

1. **`SchemaManager` owns schema merging.** `RecipeService` never touches `SchemaView` directly to merge — it calls `SchemaManager` and lets that class enforce prefix collision, closed-schema validation, and `provided_by` injection.
2. **All recipe schema writes flow through `SchemaManager.merge_fragment(...)`** (or whichever exact method the implementer chooses to add/use) so future invariants added to `SchemaManager` apply to recipes automatically.
3. **Provenance is unconditional.** Every successful recipe import writes exactly one `recipe_imported` provenance entry in the same transaction as the schema merge. Failed imports leave no state change in either store.
4. **Digest is the source of truth.** When a `RecipeRef` declares a `digest`, the install path verifies it against the freshly fetched bytes and aborts on mismatch with `RecipeDigestMismatchError`. When no digest is declared (only legal for `file:` sources in v1), the install path computes one and records it in `installed_recipes`.
5. **No implicit lineage.** Editing the live schema after a recipe import does not retroactively attach those edits to the recipe. Lineage is created only by `mosaic recipe extend` and propagated only by an explicit subsequent `import` of the extended recipe.
6. **No in-place override of upstream classes.** The merge layer rejects a recipe whose schema modifies a class or slot whose `provided_by` annotation names a different recipe or loader. Users override by subclassing (`is_a:`). The implementing agent must add a check for this in the `SchemaManager` seam.
7. **`provided_by` is set, not inferred.** `recipe.<id>@<version>` is injected by the merge layer. `loader.<name>@<version>` continues to be injected by the Reference Loader path. `provided_by` values a recipe author hand-writes are overwritten — the manifest identity wins.

---

### 10.11 CLI Surface

```
mosaic recipe list                              # list installed recipes with id, version, source
mosaic recipe inspect <path-or-uri>             # parse + validate manifest; print computed digest; no DB writes
mosaic recipe import <path-or-uri> [--dry-run]  # bootstrap-only install
mosaic recipe export [--out PATH] [--parent <installed-id>]
mosaic recipe extend <installed-id> --out PATH  # scaffold derivative recipe directory
mosaic recipe diff <a> <b>                      # structural diff between two recipes (paths or URIs)
mosaic recipe export-lockfile [--out PATH]      # write recipe.lock.yaml from installed_recipes
mosaic recipe install-from-lockfile <file>      # re-fetch + verify + install from lockfile
```

CLI is implemented in `hippo/cli/recipe.py`, mirroring the structure of other subcommand modules. CLI calls `MosaicClient` delegators; it never reaches `RecipeService` directly.

---

### 10.12 Open Questions Resolved

This section records the three open questions flagged in the handoff document and their resolutions.

| # | Question | Resolution |
|---|---|---|
| 1 | `requires.reference_loaders` resolution | **Precondition only.** v1 does not install Reference Loaders as a side effect of recipe import. Hippo checks that each pinned RL is installed at the declared version before proceeding; failure raises `RecipeRequiresUnsatisfiedError`. Adopted from handoff recommendation. See §10.4.5. |
| 2 | Lockfile schema versioning | **Yes — `lockfile_version: 1`.** `recipe.lock.yaml` carries `lockfile_version: 1` at the top level for forward-compatible parsing. Adopted from handoff recommendation. See §10.6.2. |
| 3 | Cache-dir override | **Yes — `MOSAIC_RECIPE_CACHE` env var.** Overrides the default `~/.mosaic/recipe-cache/` directory, parallel to existing Hippo cache conventions. Adopted from handoff recommendation. See §10.4.2 and §10.4.6. |
