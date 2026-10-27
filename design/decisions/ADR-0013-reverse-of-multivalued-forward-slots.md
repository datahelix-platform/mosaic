# ADR-0013: Reverse edges over multivalued (relationship-row) forward slots

- **Status:** Accepted
- **Date:** 2026-10-27 (ratified; proposed 2026-10-23)
- **Deciders:** labadorf; clandaverde (proposal, reproduction)
- **Related:** ADR-0011 (inverse-declared slots as virtual reverse edges; this ADR lifts its
  deferral of the multivalued case); ADR-0002 (multivalued reference slots persist as
  relationships — the storage this reverse reads); ADR-0012 (forward-side declaration);
  datahelix#113 item M4.
- **Tracking issue:** [#231](https://github.com/datahelix-platform/mosaic/issues/231)

## Context

ADR-0011 rejects at load an `inverse` whose forward slot is multivalued (decision 2), and its
Alternatives defer support: "no consumer needs it … the link-table reverse is a different SQL
shape." The brain-bank schema (upstream `05c58aa`) now needs it — several of its most useful
forward edges are multivalued:

| Forward slot (multivalued) | Reverse wanted |
|---|---|
| `Assay.inputs → Sample` (also `Process.inputs`) | Sample → assays that used it — the Sample ↔ Dataset path |
| `Person.cohort → Cohort` | Cohort → members |
| `Sample.anatomical_site → AnatomicalStructure` | AnatomicalStructure → samples |
| `Dataset.files → File` | File → datasets containing it |
| `Analysis.input_datasets → Dataset` | Dataset → analyses that consumed it |

Today (mosaic `main`, `e4d93e6`) declaring one fails with ADR-0011's error, e.g.
`Cohort.members declares inverse: cohort but Person.cohort is multivalued (stored as
relationship rows, ADR-0002) …`.

## Decision

Mosaic will accept `inverse` over a multivalued forward slot, as a virtual reverse resolved
through the ADR-0002 `relationships` table — joining on `target_id` and scoped by
`relationship_type` (which ADR-0002 storage sets to the forward slot's name) — with ADR-0011's
contract otherwise unchanged: virtual (no storage of its own), write-ignored, batch-hydrated,
edge predicate compiled to a correlated `EXISTS`, available targets only.

## Consequences

- Many-to-many reverses (Sample ↔ assays/datasets, Cohort → members, …) become queryable.
- Both adapters need the new join shape; `schema_diff` / DDL walks keep excluding the virtual
  slot. Both already index `relationships.target_id` and `relationship_type` separately
  (`idx_relationships_target`, `idx_relationships_type`); a composite
  `(target_id, relationship_type)` index is to be measured, not assumed.
- Reverses ranged on abstract classes (`Assay.inputs → Sample` reversed lands on `Sample`)
  combine with #224's concrete closure and still need polymorphic GraphQL types (#228).
- The ADR-0011 load-time rejection is removed (or narrowed if a sub-case stays unsupported).

## Alternatives considered

- **Keep the deferral.** Blocks the Sample ↔ Dataset provenance path entirely; rejected now
  that a consumer exists.
- **Remodel the schema to single-valued forward slots** (e.g. `Person.cohort`). Possible for
  some (datahelix#113 S3 asks this of `Person.cohort`), impossible for genuinely many-to-many
  facts like assay inputs; not a general answer.
- **Materialize reverse relationship rows.** Duplicates every edge and invites drift — the
  same reason ADR-0011 rejected storing reverses; rejected.

## Notes / open sub-questions

- Counts and `some`/`none` semantics when both sides are lists — confirm they mirror the
  forward edge exactly.
- Should ADR-0012's forward-side declaration apply here too (declare on `Assay.inputs`)?
  Recommended yes, for the same layering reason.

## Ratification note (2026-10-27)

Ratified by labadorf in the [datahelix#113](https://github.com/datahelix-platform/datahelix/issues/113)
decision pass (item M4), together with ADR-0012. **Accepted as written**: a reverse join on
`relationships.target_id` scoped by `relationship_type`, ADR-0011's contract otherwise unchanged
(virtual, write-ignored, batch-hydrated, `EXISTS` predicate, available targets only). ADR-0011
Decision item 2's load-time rejection of a multivalued forward slot is removed.

Sub-questions:

- **ADR-0012's forward-side declaration applies here too.** `Assay.inputs … inverse: assays` on the
  multivalued forward slot synthesizes `Sample.assays`, for the same layering reason.
- **Counts and `some`/`none` mirror the forward edge exactly** (ADR-0006 M5b quantifier semantics).
  This is a test obligation on the implementing PR, not a choice left open.
- The composite `(target_id, relationship_type)` index stays *measure, don't assume*.

**Sequencing.** After ADR-0012's implementation (#230). The abstract-range cases
(`Assay.inputs → Sample` reversed lands on `Sample`) also wait on #228. The brain-bank schema
decisions taken the same day (datahelix#113 S3) keep `Person.cohort` multivalued and make
`DissectFixedBrain.new_fixed_samples` multivalued, so both lean on this ADR rather than a remodel.
