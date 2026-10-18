# Handoff: Hippo → Mosaic rename (ADR-0004 execution plan)

**Status:** 🟠 Ready for implementation
**Date:** 2026-07-08
**Implements:** [ADR-0004](./decisions/ADR-0004-rename-hippo-to-mosaic.md) (Accepted) — tracked in
[hippo#113](https://github.com/BU-Neuromics/hippo/issues/113)
**Coordinates with:** platform ADR-0002 (`datahelix` metapackage) and the cross-repo master plan
[`proposals/mosaic-rename-and-metapackage.md`](https://github.com/datahelix-platform/datahelix/blob/claude/hippo-package-rename-gq1cne/proposals/mosaic-rename-and-metapackage.md)
in the datahelix repo. **This document is self-contained for the hippo-repo work**; the master
plan governs what happens in datahelix/aperture afterwards.
**Branch:** `claude/hippo-package-rename-gq1cne` — one PR, commits structured WP-H1 → WP-H6.

---

## 0. Baseline (verified 2026-07-08)

- v0.10.6; `src/hippo/` with subpackages `api cli config core graphql linkml_bridge models
  requires schemas serve testing tui validators`; 295 `.py` files in src+tests; 274 files
  mention `hippo`.
- `uv sync --extra dev --extra all` works; suite runs in ~10–20 min.
- **Known-red baseline — exactly one pre-existing failure:**
  `tests/cli/test_reference_deprovision.py::TestDeprovisionCli::test_cli_deprovision_not_installed_exits_nonzero`.
  Do not fix it in this PR (unrelated scope); do not let the failure count grow beyond it.
- Entry-point group resolution is centralized in exactly four places:
  `core/factory.py` (`STORAGE_ADAPTERS_GROUP`), `core/validation/validators.py`
  (`ENTRY_POINT_GROUP`), `core/loaders/discovery.py` (already unions
  `hippo.schema_packages` + `hippo.reference_loaders` via `_resolve_group_eps`, dedups by
  name), `cli/commands/reference.py` (`hippo.reference_loader_cli`).

## 1. The carve-out (read before any sed)

A blanket `hippo→mosaic` substitution **will corrupt data contracts**. ADR-0004 declares
"no data-model impact"; the following stay `hippo` verbatim and must be excluded from every
mechanical replacement:

| Category | Identifiers | Why untouchable |
|---|---|---|
| LinkML schema-layer names | `hippo_core`, `hippo_ext` (schema ids, imports in user schemas, `reference_hippo_core.md` / `reference_hippo_ext.md` vocabulary) | Appear inside **user-authored schemas**; renaming = schema migration, separate future ADR |
| LinkML annotation keys | `hippo_index_partial`, `hippo_search`, and any other `hippo_*` annotation read by `core/storage/ddl_generator.py` | Same — user-schema surface |
| SQL identifiers | `hippo_meta` table and any `hippo_`-prefixed DDL emitted into existing databases | On-disk contract of deployed DBs |
| Provenance/log content | any recorded strings in existing provenance rows | Append-only history |
| `design/` documents | ADRs, sec1–sec11, sec9 handoffs/progress notes, reference docs | Historical record (forward-only convention). This handoff + an INDEX row are the only design/ changes |

Practical rule: rename the **Python namespace, public API, dist/CLI names, entry-point
groups, env vars, config filename**; leave every string that ends up **inside a schema, a
database, or history** alone. When in doubt, check what reads the string: `importlib` /
`os.environ` / CLI → rename with fallback; `yaml.safe_load(user_schema)` / SQL → keep.

---

## 2. Work packages

### WP-H1 — Mechanical rename (Python namespace + dist + CLI)

1. `git mv src/hippo src/mosaic`
2. Rewrite module references across `src/mosaic/`, `tests/`, `docs/` code blocks:
   `from hippo` → `from mosaic`, `import hippo` → `import mosaic`, `hippo.<submodule>`
   dotted paths → `mosaic.<...>` — **as import/module references only**, honoring §1.
   Do it in reviewed passes (e.g. `grep -rn 'hippo' src/mosaic | grep -v -e hippo_core
   -e hippo_ext -e hippo_meta -e hippo_index -e hippo_search -e hippo.yaml -e HIPPO_`
   until the residue is only §1 + WP-H3/H4 strings).
3. `HippoClient` → `MosaicClient` (class def in `core/client.py` + all references
   including docstrings). Other `Hippo*` class/exception names follow the same pattern.
4. `pyproject.toml`: `name = "datahelix-mosaic"`, `version = "0.11.0"`, description
   updated; `[project.scripts] mosaic = "mosaic.cli.main:app"`;
   `[tool.hatch.build.targets.wheel] packages = ["src/mosaic", "src/hippo"]`
   (the `src/hippo` shim package arrives in WP-H2).
5. `src/mosaic/__init__.py`: `_pkg_version("datahelix-mosaic")`; docstring; `__all__`.
6. Rename test dirs/files that encode the name only where cheap (`tests/` module paths);
   test *content* rename is mandatory, file renames cosmetic.

### WP-H2 — Compatibility shims (`import hippo`, `hippo` CLI, `HippoClient`)

1. **Import shim** — new `src/hippo/__init__.py` (the only file in the package): emit one
   `DeprecationWarning` on import, re-export mosaic's public surface, and install a
   meta-path finder so **submodule imports return the same module objects** (isinstance
   identity across old/new spellings is a hard requirement):

   ```python
   """Deprecated alias: the 'hippo' package is now 'mosaic' (ADR-0004)."""
   import importlib, importlib.abc, importlib.util, sys, warnings

   warnings.warn(
       "'hippo' has been renamed to 'mosaic' (ADR-0004); the 'hippo' alias will be "
       "removed in a future release. Use 'import mosaic'.",
       DeprecationWarning, stacklevel=2,
   )
   _mosaic = importlib.import_module("mosaic")

   class _AliasLoader(importlib.abc.Loader):
       def __init__(self, target): self._target = target
       def create_module(self, spec):
           mod = importlib.import_module(self._target)
           sys.modules[spec.name] = mod
           return mod
       def exec_module(self, module): pass

   class _AliasFinder(importlib.abc.MetaPathFinder):
       def find_spec(self, fullname, path=None, target=None):
           if fullname.startswith("hippo."):
               return importlib.util.spec_from_loader(
                   fullname, _AliasLoader("mosaic" + fullname[5:]))
           return None

   sys.meta_path.append(_AliasFinder())

   def __getattr__(name):  # hippo.HippoClient, hippo.__version__, ...
       return getattr(_mosaic, name)
   ```

2. **CLI alias** — `[project.scripts] hippo = "mosaic.cli.compat:legacy_main"`; new
   `src/mosaic/cli/compat.py` prints a one-line deprecation notice to **stderr** (stdout
   stays script-safe) and delegates to `mosaic.cli.main:app`.
3. **API alias** — in `src/mosaic/__init__.py` and `core/client.py`:
   `HippoClient = MosaicClient` (assignment alias → `isinstance`/`issubclass` safe).
   Keep it in `__all__` with a `# deprecated` comment. A warning on *access* is optional
   (module `__getattr__`); do not warn on the assignment itself.
4. Wheel must ship both packages (H1.4). `import hippo; import mosaic` in one process
   must yield `hippo.core.client is mosaic.core.client → True` via the finder.

### WP-H3 — Entry-point groups: canonical `mosaic.*`, legacy `hippo.*`

1. `pyproject.toml`: for each of the five groups, register built-ins under **both**
   spellings (same members):
   `mosaic.storage_adapters` + `hippo.storage_adapters`, `mosaic.write_validators` +
   `hippo.write_validators`, `mosaic.schema_packages` + `hippo.schema_packages`,
   `mosaic.reference_loaders` + `hippo.reference_loaders`,
   `mosaic.reference_loader_cli` + `hippo.reference_loader_cli`.
2. The four resolution sites read **both groups, mosaic canonical, dedup by entry-point
   name** (mosaic wins on collision — dual-registered plugins load once):
   - `core/factory.py`: `STORAGE_ADAPTERS_GROUP` → `STORAGE_ADAPTERS_GROUPS =
     ("mosaic.storage_adapters", "hippo.storage_adapters")`; iterate + dedup.
   - `core/validation/validators.py`: same treatment for `ENTRY_POINT_GROUP`.
   - `core/loaders/discovery.py`: extend the existing two-group union (`_resolve_group_eps`
     + name-dedup machinery is already there) to the 2×2 spellings.
   - `cli/commands/reference.py`: both `*_reference_loader_cli` groups.
3. Third-party plugin naming convention in docs: `mosaic-reference-<name>` /
   `mosaic-adapter-<name>` going forward; `hippo-reference-<name>` remains discoverable
   through the legacy group for the whole deprecation window.

### WP-H4 — Config file + environment variables (prefer new, honor old, warn)

1. **Config:** everywhere `hippo.yaml` is resolved (`config/loader.py`, `cli/main.py`,
   `cli/templates.py`, `cli/commands/init.py`, `cli/commands/recipe.py`, `core/factory.py`,
   `tui/backend/sdk.py`, `graphql/router.py`, `config/{__init__,models}.py`): look for
   `mosaic.yaml` first, fall back to `hippo.yaml` with a single `DeprecationWarning`
   naming the found path. `mosaic init` writes `mosaic.yaml`. Centralize the lookup in
   `config/loader.py` (one function; callers stop hardcoding the filename). Default *new*
   DB filename becomes `mosaic.db`; existing configs keep whatever path they declare.
2. **Env vars:** add one helper (suggested: `mosaic.config.env.get_env(name)`) that reads
   `MOSAIC_<name>` then `HIPPO_<name>` (warn once per var on legacy hit). Convert the
   env *reads* found at: `core/client.py` (`HIPPO_CACHE_DIR`), `core/schema_typing.py`,
   `core/recipe/resolver.py`, `core/loaders/discovery.py:367`,
   `core/storage/postgres_adapter.py` (`HIPPO_DATABASE_URL`), `cli/main.py`,
   `cli/commands/{init,reference}.py`. **Audit each `HIPPO_` hit before converting** —
   `core/storage/ddl_generator.py`'s `HIPPO_*` constants hold §1 annotation keys /
   SQL names: those stay.
3. Docs for both fallbacks land in WP-H5 (`docs/configuration.md`,
   `design/reference_hippo_yaml.md` gets **no edit** — historical; the *user-facing*
   config doc is the one that changes).

### WP-H5 — Docs, CI, metadata

1. `README.md`, `CLAUDE.md` (component overview wording), all 22 files under `docs/`
   (prose + code samples → `mosaic`, with one "formerly Hippo" note in `docs/index.md`
   and an upgrade note in `docs/installation.md` covering the shims and fallbacks).
2. `.github/workflows/`: `tests.yml`, `release.yml`, `quickstart.yml`,
   `schema-closure.yml`, `docs.yml` — install specs, `HIPPO_DATABASE_URL` →
   `MOSAIC_DATABASE_URL` (fallback makes this safe), job/artifact names. Postgres
   service creds (`hippo_test`) may stay — they are opaque strings; renaming is cosmetic.
3. `CHANGELOG.md`: 0.11.0 entry — rename, shims, deprecation window (≥ 2 minor releases,
   removal via future ADR).
4. `design/INDEX.md`: add the Document Map row for this handoff (already done when this
   doc landed); **no other design/ edits** (§1).

### WP-H6 — Verification

1. New `tests/compat/test_hippo_alias.py`:
   - `import hippo` emits `DeprecationWarning`; `hippo.HippoClient is mosaic.MosaicClient`.
   - `from hippo.core.types import Filter` works and `hippo.core.types is
     mosaic.core.types` (module identity through the finder).
   - `hippo.__version__ == mosaic.__version__`.
   - CLI: `hippo --help` exits 0 and stderr carries the deprecation notice; `mosaic
     --help` is clean.
   - Entry points: a test-registered plugin under the **legacy** group is discovered;
     one registered under **both** groups loads exactly once.
   - Config: a dir with only `hippo.yaml` loads with a warning; with both files,
     `mosaic.yaml` wins silently. Env: `HIPPO_CACHE_DIR` honored with warning,
     `MOSAIC_CACHE_DIR` wins when both set.
2. Full suite: `uv run pytest -q` — green **except** the §0 known-red baseline test
   (unchanged failure, count = 1).
3. Grep gates (all must return only §1 carve-out identifiers, shim files, legacy-group
   strings, and fallback literals):
   `grep -rn '\bhippo\b' src/ --include='*.py'` and
   `grep -rn 'HippoClient' src/ tests/` (only alias definitions + compat tests).
4. Packaging: `uv build` succeeds; wheel contains `mosaic/` and the one-file `hippo/`;
   `uv run python -c "import hippo, mosaic"` from a clean venv install works.

---

## 3. Commit / PR structure

Single PR on `claude/hippo-package-rename-gq1cne` (base: `main`), one commit per WP in
order H1→H6, PR body linking ADR-0004 + hippo#113 + the datahelix master plan. Do **not**
merge until review; datahelix WP-D4 (submodule bump) is blocked on this PR merging.

## 4. Rollback

Revert the PR. The shims mean no external consumer can have depended on the new names
before datahelix WP-D4 lands, so a pre-D4 revert is zero-impact. On-disk data is
untouched by design (§1), so rollback has no migration component.
