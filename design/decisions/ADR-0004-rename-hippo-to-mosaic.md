# ADR-0004: Rename the Hippo component to Mosaic

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** labadorf, design session (packaging & naming, 2026-07-08)
- **Related:** **platform ADR-0002** (the `datahelix` metapackage + prefixed dists — removes the PyPI-saturation forcing function this rename would otherwise depend on); platform `sec2_components.md`; **Canon** and **Cappella** as consumers of the `hippo.*` entry-point namespace; this repo's `design/INDEX.md` Key Decisions Log

## Context

The component is named **Hippo**. Two problems motivate a rename:

1. **PyPI saturation.** `hippo` is heavily squatted; the bare name is effectively unavailable.
2. **Convention misfit.** The other component names — Canon, Cappella, Aperture, Bridge — are real words in a music/art register that evoke each component's role. "Hippo" is an animal mascot (originally from *hippocampus*) that reads as a codename, not an intentional platform name. The platform expects the remaining in-tree components to follow the established convention.

**The reframe that makes this a clean decision:** platform ADR-0002 introduces the `datahelix` metapackage with prefixed dists (`datahelix-<component>`) and bare imports. Behind that surface, bare-name PyPI collisions no longer matter — so **problem (1) is dissolved by ADR-0002 regardless of the name chosen**. This rename therefore proceeds on **naming-convention/fit grounds alone**, not necessity. Keeping "Hippo" would be technically fine; it would simply remain the lone mascot among intentional real-word names.

The convention distilled from the existing names: a **real music/art word, ≤3 syllables** (Cappella = 3, Canon = 2), that evokes the component's role.

The question: *what should Hippo be renamed to, and is the rename worth its cross-package cost?*

## Decision

**The Hippo component will be renamed to Mosaic.**

*Why "Mosaic":* a knowledge graph **is** a mosaic — many small typed entities (tesserae) tiling into one coherent picture, where the defining property of a tiling (no gaps, no overlaps) echoes referential integrity / a well-formed schema. It is 3 syllables (within budget), and it belongs to the platform's music/art register on three counts: *mosaic*, *music*, and *museum* share a root (the **Muses**, via *opus musivum*); "audio **musaicing**" (concatenative synthesis) is a live music-technology term; and **tiling ("mosaic") rhythmic canons** are cousins of the existing **Canon** component. Unlike more contrived candidates, the graph-as-tiles metaphor needs no footnote.

Scope of the rename:

- **Distribution:** publish as `datahelix-mosaic` (per ADR-0002).
- **Import package & API:** `hippo` → `mosaic`; `HippoClient` → `MosaicClient`; CLI `hippo` → `mosaic`.
- **Entry-point namespace (the load-bearing part):** the `hippo.storage_adapters`, `hippo.write_validators`, `hippo.reference_loaders`, and `hippo.schema_packages` group names → `mosaic.*`. These are **cross-package string contracts** — e.g. Canon registers `[project.entry-points."hippo.reference_loaders"]` — and the `hippo-reference-<name>` / `hippo-adapter-<name>` plugin-naming conventions become `mosaic-reference-<name>` / `mosaic-adapter-<name>`.
- **Repository:** `BU-Neuromics/hippo` → `datahelix-platform/mosaic`, with the datahelix submodule pointer updated.
- **Docs/specs:** the component name in prose across platform and component docs.

A **compatibility strategy** (import shim, CLI alias, and dual-registered entry-point groups with a deprecation window) will be defined so consumers migrate without a flag-day break.

## Consequences

- The component name reads as intentional alongside Canon / Cappella / Aperture / Bridge, and the platform gains a reusable convention for future names (e.g. Bridge is not yet built).
- **The real cost is the entry-point namespace.** Renaming `hippo.*` → `mosaic.*` is a breaking change to the plugin contract: **Canon and Cappella** must migrate their registrations and any `import hippo`; third-party `hippo-reference-*` / `hippo-adapter-*` packages are affected. This needs dual-registration + a deprecation window, not a hard cut.
- Dependency updates: Aperture's `local = ["hippo"]` extra and Cappella's `hippo` dependency (including `[tool.uv.sources]`) point to `datahelix-mosaic` / `mosaic`.
- **No data-model impact.** This is a naming change; on-disk provenance and entity data are untouched.
- Cross-reference: platform ADR-0002 names this rename as the first beneficiary of prefixed dist names.

## Alternatives considered

- **Keep "Hippo."** Zero migration cost, and ADR-0002 already neutralizes the PyPI problem — so this was genuinely viable. Rejected on fit: it remains the sole mascot among intentional real-word names, and the convention is meant to extend to future components.
- **Other music/art finalists.** *Ripieno* (the full orchestral ensemble — conceptually apt, but 4 syllables, over budget); *Leger* / *Register* (the "authoritative record" reading — strong, but shift the metaphor from ensemble to ledger); *Consort* / *Tutti* (ensemble words). Rejected in favor of Mosaic: it is within the syllable budget, needs no gloss, and its Muse-root earns a place in the musical family without being a contrived tempo marking.
- **Rename only the distribution** (ship `datahelix-mosaic`, keep `import hippo` and the `hippo.*` entry points). Cheapest, but leaves a split-brain identity — docs say Mosaic, code says hippo — the half-measure that ages worst. Rejected.

## Notes / open sub-questions

- **Sequence the entry-point migration with aliases first:** register under both `hippo.*` and `mosaic.*`, migrate Canon/Cappella, then drop the old groups — no flag day. Confirm the deprecation window with both consumers.
- Repository rename + submodule-pointer update is a coordinated step across `BU-Neuromics/hippo` and the datahelix integration repo.
- **Out of scope here:** the "BASS platform" wording in this repo's `CLAUDE.md`, and any deeper rebrand beyond the component name.
