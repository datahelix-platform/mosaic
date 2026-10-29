# Releasing Mosaic (formerly Hippo)

Releases are cut by pushing a `vX.Y.Z` git tag. The `Release` workflow
(`.github/workflows/release.yml`) does the rest — see "What automation does"
below. This pipeline is DataHelix 1.0 epic P1.1 (issue #97): every release
must produce a **digest-addressed image** so the DataHelix certified-frontier
ledger (`certification/composition.lock.json`) has evidence to pin.

## Version conventions

- `pyproject.toml` `project.version` is the single source of truth, with no
  `v` prefix (`0.10.2`).
- Git tags and CHANGELOG headings carry the `v` prefix (`v0.10.2`).
- The `validate` job fails the release when the tag and `pyproject.toml`
  disagree.

## Cutting a release

1. Bump `version` in `pyproject.toml`.
2. Retitle the `## [Unreleased]` section of `CHANGELOG.md` to
   `## vX.Y.Z — YYYY-MM-DD (short title)` and start a fresh `## [Unreleased]`
   stub above it. The release notes are extracted from this section
   (`scripts/extract_release_notes.py`) and the workflow fails if it is
   missing.
3. Land those changes on `main` via the normal PR flow.
4. Tag and push:

   ```bash
   git tag vX.Y.Z main
   git push origin vX.Y.Z
   ```

   Equivalently, dispatch the **Release** workflow (Actions → Release →
   Run workflow) on `main` with the tag name as input — the workflow
   creates the tag at the dispatched ref. Useful when tag pushes aren't
   available (e.g. sandboxed sessions).

## What automation does on tag push

| Job | Output |
|---|---|
| `validate` | tag ↔ `pyproject.toml` version check |
| `build` | wheel + sdist (`uv build`) |
| `image` | `ghcr.io/bu-neuromics/mosaic:{X.Y.Z,latest}`, digest-addressed |
| `release` | GitHub Release with CHANGELOG notes, `dist/*`, and `image-digest.json` |
| `notify-datahelix` | `repository_dispatch` (`component-release`, payload = `image-digest.json`) to DataHelix, which opens the lock-bump PR. **Best-effort** (`continue-on-error`): a failed dispatch never fails the release; dispatch by hand from the release asset. Needs the `DATAHELIX_DISPATCH_TOKEN` secret (fine-grained PAT or App token, **Contents: read & write** on `datahelix-platform/datahelix` only) — adding it is a maintainer act. |
| `pypi` | PyPI publish — **gated**, see below |
| `notify-datahelix` | `repository_dispatch` of `image-digest.json` to `datahelix-platform/datahelix` as `component-release`; DataHelix's bump-lock workflow opens the one-pin lock PR (platform ADR-0001). Best-effort: a failed dispatch never fails the release. Needs the `DATAHELIX_DISPATCH_TOKEN` org secret |

`image-digest.json` is the machine-readable asset the DataHelix bump bot
reads to move the `composition.lock.json` pin to
`ghcr.io/bu-neuromics/mosaic@sha256:…`:

```json
{
  "component": "mosaic",
  "repo": "datahelix-platform/mosaic",
  "version": "X.Y.Z",
  "image": "ghcr.io/bu-neuromics/mosaic",
  "digest": "sha256:…",
  "ref": "ghcr.io/bu-neuromics/mosaic@sha256:…"
}
```

## PyPI publish gate ([HUMAN] decision pending)

The distribution-name question is settled: the package ships as
**`datahelix-mosaic`** (platform ADR-0002 prefixed-dist convention +
ADR-0004 rename; the import package is `mosaic`, with a deprecated `hippo`
alias). The `pypi` job stays disabled until:

1. A [trusted publisher](https://docs.pypi.org/trusted-publishers/) is
   configured on PyPI for this repository (workflow `release.yml`,
   environment `pypi`).

Then set the repository variable `PYPI_PUBLISH` to `true`. Until then, wheels
and sdists are still attached to every GitHub Release.

## Retro-tags

The v0.10.x releases predate this pipeline. Their tags (`v0.10.0`,
`v0.10.1`) point at the historical release commits so artifact provenance is
verifiable. Retro-tags do **not** trigger the release workflow (the workflow
file does not exist at those commits) — they are provenance markers only; no
artifacts are published for them retroactively.

## LinkML pin coupling

Mosaic's public behavior tracks its LinkML pins (`linkml`, `linkml-runtime`,
`linkml-store` in `pyproject.toml`): a LinkML patch bump can change validation
or generation behavior, so **any pin change forces at least a patch release**,
and the CHANGELOG entry must name the old and new pins (see the Key Decisions
log in `design/INDEX.md`).
