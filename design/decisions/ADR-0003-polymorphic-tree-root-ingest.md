# ADR-0003: Polymorphic tree-root ingest via `designates_type` dispatch

- **Status:** Accepted
- **Date:** 2026-11-10 (ratified as built; proposed 2026-06-26)
- **Deciders:** labadorf; design session
- **Related:** sec9 §9.8 (typed-client accessor surface / synthesized tree-root), sec3b (per-class table storage), [issue #80](https://github.com/BU-Neuromics/hippo/issues/80)

## Context

`hippo ingest` consumes a **tree-root instance bundle**: a YAML mapping whose keys are
class **accessors** (`samples:`, `assays:` …, the same names the typed client exposes) and
whose values are lists of instances. Hippo synthesizes a hidden `_HippoInstanceBundle`
class — one multivalued/inlined slot per **concrete** class — validates the whole bundle
against it, then for each key dispatches `accessor → declared range` and `put`s every
instance under that range class. Storage is **one table per concrete class** (sec3b); an
instance keeps its subclass-specific fields only if `put` under the subclass.

A schema that models polymorphism the standard LinkML way — an abstract or concrete base
with a `designates_type` discriminator (e.g. `category`) and concrete subclasses — breaks
in two ways (issue #80, brainbank-hippo-schema):

- **(a) Abstract-base collection has no accessor.** `BrainBank.samples` ranges on abstract
  `Sample`; the synthesized bundle skips abstract classes, so there is no `samples` slot
  and the bundle hard-fails validation (`Additional properties … ('samples')`). The
  schema's own published example does not ingest.
- **(b) Concrete-base accessor silently downcasts.** An `RNASeqAssay` under `assays:`
  (range concrete `Assay`) is stored as `Assay`; `category` is kept as a string but never
  used to dispatch, so subclass fields (`platform`, `read_length`) are dropped and the
  instance is not queryable as `RNASeqAssay`. No error.

The question: **how does `hippo ingest` consume polymorphic inlined collections so the
published example ingests and each instance lands in its correct concrete subclass?**

## Decision

**Hippo will (1) give an explicit polymorphic base — an abstract class declaring a
`designates_type` slot — a base-ranged tree-root accessor, and (2) at ingest time dispatch
each instance under a base accessor to the concrete subclass named by its `designates_type`
discriminator, storing it as that subclass.** The synthesized `_HippoInstanceBundle`
remains the bundle contract; the user-declared `tree_root` class is *not* reinterpreted as
the bundle root (see Alternatives).

Concretely:

1. **Accessor for polymorphic bases.** `_build_tree_root_class` keeps emitting one slot per
   concrete class, and additionally emits a base-ranged slot for any **abstract** class
   that declares a `designates_type` slot (`SchemaRegistry`-level `_has_type_designator`).
   Plain abstract roots like `Entity` carry no designator and stay excluded (preserving the
   existing "no `entities` accessor" invariant). LinkML's JSON-Schema generation already
   renders a `designates_type` range as a discriminated `anyOf` over the concrete
   subclasses, so the bundle validates polymorphically.
2. **Dispatch at ingest.** New registry helpers `type_designator_slot(class)` and
   `resolve_designated_class(base, value)` (match the value against subclass name, then
   `class_uri`, then its CURIE short form). `ingest._dispatch_class` resolves each
   instance's concrete target: if the declared range has a designator and the instance
   carries a value, route to the resolved concrete subclass; otherwise fall back to the
   declared range, which must itself be concrete. It raises (per-instance, write-nothing on
   that row) if the value resolves to nothing, names an abstract class, or the declared
   range is abstract with no designator.

Both concrete-subclass accessors (`solid_samples:`) and base accessors (`samples:`,
`assays:`) are valid simultaneously; the documented concrete-accessor workaround keeps
working.

## Consequences

- The schema's published `examples/brainbank-example.yaml` (`donors:`/`samples:`/`assays:`/
  `datasets:`) ingests as-authored — those keys are exactly the accessor-convention names
  for the base classes.
- Subtype data is preserved: an instance under a base accessor is stored as its concrete
  subclass and is queryable as its real type; subclass fields survive.
- Out-of-family or abstract discriminator values are caught — by up-front bundle validation
  (the designator enum) in the normal path, and by the dispatch guard as defense-in-depth
  if validation is bypassed.
- **No silent downcast even without a designator.** A polymorphic base that declares *no*
  `designates_type` slot still validates a subtype instance under its accessor (LinkML
  permits a subclass under a base-ranged inlined slot via `anyOf`), and Hippo would store it
  as the base and drop the subtype's fields. The dispatch guard refuses this: when the
  fallback target is a polymorphic base (abstract, or concrete carrying fields the base does
  not define) it raises an **actionable** error naming the dropped fields, the valid concrete
  subclasses, and the two fixes (add a `designates_type` discriminator, or use the concrete
  accessor), pointing at `docs/polymorphic-ingest.md`. This is a behavior change: bundles
  that previously "succeeded" by silently discarding subtype fields now error.
- The wire contract is unchanged (still accessor-keyed, `_HippoInstanceBundle` still hidden:
  no table, absent from `class_names`/typed client/schema-diff). A user-declared
  `tree_root: true` remains **ignored** by ingest (status quo) — see Alternatives.
- New obligation on schemas: a polymorphic base must declare its discriminator with
  `designates_type: true` for both the accessor and the dispatch to engage. A base without
  one is treated as an ordinary (concrete) class.

## Alternatives considered

- **Honor the user-declared `tree_root` class as the bundle root.** Validate/ingest against
  that class's own slots verbatim, honoring arbitrary (non-convention) collection names.
  **Rejected** for this increment: it switches the bundle root for such schemas (the
  synthesized concrete-accessor bundles — the current workaround — would stop validating),
  makes `tree_root_class_name()`/`tree_root_slots()` return a real domain class that *also*
  has its own table and accessor (a container masquerading as an entity), and is a larger,
  higher-surprise change across the registry surface. The chosen approach makes the
  reported example ingest with no change to the wire contract, because the author named
  their collections with the accessor convention. True arbitrary-`tree_root` honoring can
  layer on top later if a schema needs non-convention names.
- **Polymorphic sweep at query/storage instead of dispatch at ingest.** Store under the
  base and resolve subtype on read. **Rejected:** conflicts with the per-class-table model
  (sec3b; `test_brainbank_extension` documents "no polymorphic sweep") and still loses
  subclass fields, since the base table has no columns for them.
- **Validate-and-reject base-accessor instances (force concrete accessors only).** Keep the
  workaround as the only path. **Rejected:** the published example uses base accessors and
  should ingest; forcing concrete-only is a worse developer experience and doesn't honor
  the standard LinkML polymorphism pattern.

## Implementation

- `SchemaRegistry.type_designator_slot`, `resolve_designated_class`; module helper
  `_has_type_designator`; `_build_tree_root_class` polymorphic-base inclusion
  (`src/hippo/linkml_bridge.py`).
- `SchemaRegistry.has_subclasses` / `concrete_subclasses` back the downcast guard.
- `_dispatch_class` + `_downcast_message` + per-instance dispatch in `ingest_linkml_yaml`
  (`src/hippo/cli/commands/ingest.py`).
- Author-facing guide: `docs/polymorphic-ingest.md` (linked from every dispatch error).
- Tests: `tests/cli/test_ingest_polymorphic.py`.

## Ratification (2026-11-10)

**Accepted as built** by labadorf in the 2026-11-10 decision pass over stale Proposed ADRs.
Verified at mosaic `v0.15.0`: `_dispatch_class` / `_downcast_message` in
`src/mosaic/cli/commands/ingest.py` implement the dispatch and downcast guard exactly as
decided (resolve via `SchemaRegistry.type_designator_slot` / `resolve_designated_class`;
raise on an unresolvable or abstract designator value, and on a fallback to a polymorphic
base that would drop subtype fields); `_has_type_designator` / `_build_tree_root_class` in
`src/mosaic/linkml_bridge.py` emit the base-ranged accessor. Covered by
`tests/cli/test_ingest_polymorphic.py` against the SQLite backend. Dispatch happens in the
ingest command before `put`, so it is backend-agnostic in design; it is not separately
exercised on Postgres.

Ratified separately from [#99](https://github.com/datahelix-platform/mosaic/issues/99) (the
1.0 Postgres parity epic that listed ADR-0002/0003 ratification in its scope).

## Notes / open sub-questions

- `designates_type` value matching is name → `class_uri` → CURIE short form. Confirm no
  schema in scope relies on a designator value that is *only* expressible as a full URI not
  reducible by the short-form rule.
- The downcast guard keys "is this a polymorphic base?" off having any proper descendant
  (`class_descendants`). A concrete leaf class with no subclasses is never guarded (and
  closed-schema validation already rejects stray fields on it), so the guard is scoped to
  genuine polymorphic bases.
