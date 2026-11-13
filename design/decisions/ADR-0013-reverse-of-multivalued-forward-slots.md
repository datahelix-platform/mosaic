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

## Amendment (2026-11-10): which side of an `inverse` pair is stored

Recorded by labadorf in the [datahelix#126](https://github.com/datahelix-platform/datahelix/issues/126)
decision pass, for the M4 implementation ([#231](https://github.com/datahelix-platform/mosaic/issues/231)).
The Decision above is unchanged; this pins a rule it left implicit.

Once ADR-0012's forward-side declaration and this ADR's multivalued forward slots are both
allowed, "multivalued and carries `inverse`" no longer identifies the virtual side:
`Assay.inputs: {range: Sample, multivalued: true, inverse: assays}` is multivalued, carries
`inverse`, and is the **stored** slot. The stored side is decided by the pair instead. For a slot
`A.x` declaring `inverse: y`, ranged on `B`:

| `B.y` | Stored | Virtual | Precedent |
|---|---|---|---|
| a real slot that does **not** declare `inverse` back | `B.y` | `A.x`, the reverse | ADR-0011; this ADR's derived-side declaration |
| does **not exist** | `A.x`, the forward slot | `B.y`, synthesized | ADR-0012; this ADR's forward-side declaration (`Assay.inputs … inverse: assays`) |
| a real slot that declares `inverse: x` back | the **single-valued** side | the multivalued side | |
| …and both sides are multivalued | — | — | `SchemaError`: "declare `inverse` on one side only" |

**This replaces the "multivalued + `inverse` ⇒ virtual" test** used today by
`SchemaRegistry.inverse_reference_slots` (`src/mosaic/linkml_bridge.py`, ~line 1374) and by
`inverse_of` in `_classify_slot` (`src/mosaic/core/schema_typing.py`, ~line 215). Left in place,
that test would classify `Assay.inputs` as a virtual reverse: its relationship rows would stop
being written on ingest (virtual slots are write-ignored), silently, and its column-less shape
would hide the loss. The M4 implementation must swap both call sites to the pair rule above and
test the forward-multivalued case explicitly (a write to `Assay.inputs` persists and hydrates).

Open PR [#240](https://github.com/datahelix-platform/mosaic/pull/240) (ADR-0012's implementation)
keeps the old test. That is correct for ADR-0012 alone, where every forward slot carrying `inverse`
is single-valued; it becomes wrong the moment M4 allows a multivalued forward slot, so the switch
belongs in M4 (or in #240, if it lands after this note is read).

## Implementation note (#231)

- The pair rule lives in `linkml_bridge.is_virtual_inverse`; `inverse_reference_slots`,
  `multivalued_reference_slots` and `_classify_slot`'s `inverse_of` all use it.
  `InverseSlot.forward_multivalued` tells adapters which storage the forward slot uses.
- SQLite and Postgres resolve the reverse as `relationships` rows with `target_id` = this
  entity and `relationship_type` = the forward slot, joined to the forward class's concrete
  closure (so `Run.inputs` and `Assay.inputs`, sharing a slot name, never mix). Hydration,
  `some`/`none` and `count_relationship` count available source rows with an available edge.
  Postgres runs on the current JSONB `entities` layout; it moves with ADR-0008.
- The composite `(target_id, relationship_type)` index was not added — still *measure, don't
  assume*.
- A forward count or filter through `Assay.inputs` into abstract `Sample` still ignores the
  concrete closure; that is #241 under ADR-0014.
