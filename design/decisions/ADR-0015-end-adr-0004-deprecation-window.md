# ADR-0015: End ADR-0004's deprecation window: remove the `hippo` aliases in v0.16.0

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** labadorf (ruled as platform ADR-0007 Q4, 2026-11-17)
- **Related:** ADR-0004 (the Hippo → Mosaic rename, which opened the window); platform
  [ADR-0007](https://github.com/datahelix-platform/datahelix/blob/main/platform/design/decisions/ADR-0007-retire-hippo-data-contract-identifiers.md)
  (retires the `hippo` data-contract identifiers, Q4 rules on this window); ADR-0014 Decision 5
  (`mosaic_anchor` / `mosaic_manifest`); issue #124; datahelix#57

## Context

ADR-0004 renamed Hippo to Mosaic and kept the old spellings working as deprecated aliases:
`import hippo` (a shim package), the `hippo` console script, the `hippo.*` entry-point groups,
the `HIPPO_*` environment-variable fallbacks, the `hippo.yaml` / `hippo.yml` config fallback,
the pre-rename on-disk defaults (`data/hippo.db`, `~/.cache/hippo/references/`,
`~/.hippo/recipe-cache/`, `~/.hippo/data/`), the `hippo-reference-<name>` package prefix, and
the Python aliases `HippoClient`, `HippoError`, `HippoConfig` and `load_hippo_config`. The
v0.11.0 CHANGELOG promised to keep them for at least two minor releases and left their removal
to "a future ADR".

Platform ADR-0007 retires the `hippo` data-contract identifiers in one breaking change, before
1.0, with no compatibility window: there are no users to migrate, and every deployment is
rebuilt from source data. Its Q4 asked whether ADR-0004's window should end in the same
release, and ruled that it should, recorded as a short Mosaic ADR, because the window is
Mosaic's decision. This is that ADR. The aliases shipped in v0.11.0 and four more minor
releases followed (v0.12.0 through v0.15.0), so the "at least two minor releases" promise is
kept.

## Decision

**Mosaic v0.16.0 removes every ADR-0004 alias, in the same release as platform ADR-0007's
rename.** After it, Mosaic reads and registers only the `mosaic` spellings:

- the `hippo` shim package (`src/hippo/`) and the `hippo` console script are deleted;
- the `hippo.storage_adapters`, `hippo.write_validators`, `hippo.schema_packages`,
  `hippo.reference_loaders` and `hippo.reference_loader_cli` entry-point groups are neither
  registered nor read;
- `get_env` reads only `MOSAIC_<name>`, and `${MOSAIC_X}` in a config no longer falls back to
  `HIPPO_X`;
- config auto-detection looks only for `config.json`, `mosaic.yaml` and `mosaic.yml`;
- the default paths are `data/mosaic.db`, `~/.cache/mosaic/references/`,
  `~/.mosaic/recipe-cache/` and `~/.mosaic/data/`, and the legacy `data/hippo.db` / `hippo.db`
  are no longer picked up;
- `hippo-reference-<name>` is no longer stripped to a loader short name;
- `HippoClient`, `HippoError`, `HippoConfig` and `load_hippo_config` are removed.

A legacy spelling is ignored rather than rejected, except where platform ADR-0007 asks for a
loud failure: a `hippo_*` schema annotation key, an `imports:` of `hippo_core` / `hippo_ext`,
and a `recipe.yaml` `hippo_version` key each fail validation with an error naming the
replacement.

## Consequences

- One spelling of everything, so "which spelling won?" bugs (`mosaic.yaml` next to
  `hippo.yaml`, `MOSAIC_X` next to `HIPPO_X`) can no longer happen.
- Consumers must have migrated. Inside the platform, Canon's `pyproject.toml` still registers
  under `hippo.reference_loaders` next to `mosaic.reference_loaders`. The `mosaic.*` block keeps
  it discovered; the datahelix PR (datahelix#57) drops the `hippo.*` block. Two datahelix
  scripts (`deploy/recipes/demo/remap_accessors.py`, `certification/scripts/seed_via_config.py`)
  fall back to `import hippo` only when `import mosaic` fails, so they keep working.
- An installation that still relied on a fallback path (a `hippo.yaml`, a `data/hippo.db`, a
  `HIPPO_*` variable, a cache under `~/.cache/hippo/`) silently starts from the new default
  instead. That is acceptable here because every deployment is rebuilt from source data
  (platform ADR-0007). The CHANGELOG's v0.16.0 `### Upgrading` section lists every rename.
- `tests/compat/` and the dual-registration entry-point tests are removed; a test now asserts
  that nothing is registered under `hippo.*`.

## Alternatives considered

- **Keep the aliases until 1.0.** Rejected by platform ADR-0007 Q4: there is no one to protect,
  and keeping code that only runs against fixtures means removing it later anyway, in a
  release that would otherwise not be breaking.
- **Turn every legacy spelling into a hard error** (for example, refuse to start when a
  `hippo.yaml` is present). Rejected: it would keep the alias code paths alive only to reject
  them. Loud failures are kept only where a stale artifact would otherwise be silently
  misread: schema annotations, schema imports and recipe manifests.
