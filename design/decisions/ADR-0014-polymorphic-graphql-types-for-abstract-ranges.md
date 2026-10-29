# ADR-0014: Abstract classes get GraphQL interface types, are queryable anchors, and the manifest carries the class hierarchy

- **Status:** Proposed
- **Date:** 2026-10-27 (proposed; shape decided by labadorf in the datahelix#113 decision pass, item M2)
- **Deciders:** labadorf (shape); ratification pending
- **Related:** ADR-0005 (edge-only GraphQL references — the raw `*_id` fallback this ADR retires);
  ADR-0006 (typed `where:` filter contract — the interface-typed filter input this ADR adds);
  ADR-0009 (capability manifest and `QuerySpec` — `predicate` and `UNKNOWN_ANCHOR` semantics
  change here); ADR-0011 / ADR-0012 / ADR-0013 (inverse slots — every reverse ranged on an abstract
  class is usable only through this ADR); issue #224 (`concrete_class_closure`, the storage half
  already shipped); reel#10 (the planner anchors on abstract `Sample`); datahelix#113 M2, R3.
- **Tracking issue:** [#228](https://github.com/datahelix-platform/mosaic/issues/228)

## Context

`exposed_class_names` (`core/schema_typing.py:155`) skips every abstract class, so an abstract
class has no GraphQL object type, no collection, no manifest entry and no accessor. Three things
follow, each measured on brainbank-hippo-schema `05c58aa` against mosaic `main`:

1. **References ranged on an abstract class fall back to raw ids.** `GraphQLTypeBuilder`
   (`graphql/schema_builder.py:467`, `resolvable = slot.target_class in self.entities`) keeps a
   raw `*_id` / `*_ids` field "as an interim" and warns at build time
   (`schema_builder.py:612`). On brain bank that is **56** fields: `Diagnosis.sample`,
   `Measurement.sample`, `Assay.inputs`, every banking process's `inputs`/`outputs`,
   `Dataset.produced_by` (→ `Activity`), `BrainBank.assessments` (→ `Assessment`). ADR-0005's
   edge-only rule is therefore violated on exactly the schema's most connected slots.
2. **The manifest marks them non-filterable.** `build_capability_manifest`
   (`schema_typing.py:384`, `predicate = slot.target_class in exposed`) sets `predicate=False`, so
   `QuerySpec` rejects them with `NOT_PREDICATE_FILTERABLE` and the typed `where:` input omits
   them. The storage layer resolves these edges fine since #224; no transport can use them.
3. **An abstract class cannot be an anchor.** `validate_query_spec` answers `anchor: "Sample"`
   with `UNKNOWN_ANCHOR` (`query_spec.py:469`). A planner grounded on the manifest sees `Blood`,
   `Brain`, `CSF` and 30 more with nothing tying them to the word "sample" the user and the
   schema's own descriptions use (reel#10). The same gap blocks ADR-0012's flagship case: a
   synthesized `Donor.samples → Sample` resolves in storage and is unusable from GraphQL, REST
   or MCP.

Abstract classes on this schema: `Activity`, `ArrayAssay`, `ArrayDataset`, `Assessment`,
`Association`, `CultureTissue`, `NamedThing`, `Quantity`, `Sample`, `SequenceDataset`,
`SequencingAssay`. `Person`, `Dataset` and `Assay` are concrete (datahelix#113 listed them as
abstract; the reproduction corrected that).

## Decision

**Mosaic will represent an abstract class that is the range of an exposed reference, or that has
at least one exposed concrete descendant, as a first-class polymorphic entity:**

1. **GraphQL: an interface type per such abstract class**, carrying the slots induced on the
   abstract class itself, implemented by every concrete descendant's object type
   (`concrete_class_closure`, #224). A reference or inverse ranged on the abstract class resolves
   to the interface and returns concrete objects (`__typename` names the leaf). The raw-id
   fallback and its build-time warning are removed for this case; a reference whose target has
   **no** exposed concrete descendant stays a raw id, and the warning stays for it.
   Interface, not union: the shared slots (`Sample.donor`, `Sample.sample_id`, …) are the common
   case and must be selectable without a fragment per concrete kind.
2. **An abstract class is a queryable anchor.** It gets an accessor (`samples`), a `Page` type, a
   typed filter input built from its induced slots (ADR-0006), and `search{Plural}` when any
   descendant has a searchable slot. The collection resolves over the concrete closure, in the
   same shape #224 gave inverse hydration: SQLite `UNION ALL` across per-class tables, Postgres
   `entity_type = ANY(...)`. Rows carry their concrete `entity_type`. `order_by`, facet counts
   and `field_range` apply to induced slots only; a subclass-only slot is `UNKNOWN_SLOT` on the
   abstract anchor, as it is on an inverse edge's nested `where` today.
3. **The capability manifest carries the class hierarchy.** `EntityCapability` gains
   `abstract: bool`, `parent: Optional[str]` (the nearest `is_a`), and `kinds: tuple[str, ...]`
   (the exposed concrete closure, empty for a leaf). `mosaic://capabilities` and
   `MosaicSlotInfo`/`hippoSchema` serialize them. `predicate` for a reference becomes true when
   its target class is in the manifest, which now includes abstract classes with a non-empty
   closure.
4. **`QuerySpec` follows the manifest.** `UNKNOWN_ANCHOR` no longer fires for an abstract anchor
   with a closure; `NOT_PREDICATE_FILTERABLE` no longer fires for a reference ranged on one. The
   `construct-query-spec` prompt gains one item: an entity with `kinds` is a family; anchor on it
   when the question is about the family, on a kind when it names one.
5. **Writes are unchanged.** No create/update input is generated for an abstract class; the
   abstract accessor is read-only. Ingest already dispatches on `designates_type` (ADR-0003).

## Consequences

- Every brain-bank reverse ranged on `Sample`, `Activity` or `Assessment` becomes usable from
  every transport, which is the condition ADR-0012 and ADR-0013 both name. Donor → samples works
  end to end with M3 + this.
- reel#10 shrinks to its Reel-side half (the tool schema's hard-coded `"Sample"` example):
  `anchor: "Sample"` is simply valid, and the grounding can render "kinds of Sample".
- The GraphQL schema grows one interface per qualifying abstract class and one accessor each.
  Aperture derives collections from introspection and will list abstract collections; its
  `deriveEdges` and the result table need to handle interface-typed references (a follow-up on its
  side, gated on this landing in a certified pin — ADR-0029 honest degradation until then).
- Query cost: an abstract anchor is a `UNION ALL` over N tables; on brain bank `samples` spans 33.
  Pagination applies after the union. Acceptable for a read path that did not exist; measure.
- The 56 build-time warnings on brain bank drop to zero.

## Alternatives considered

- **Union types.** Simpler to generate, but a union has no fields of its own, so every selection
  of a shared slot needs a fragment per concrete kind (`... on Blood { donor }`, ×33). Rejected.
- **Interfaces but no abstract anchors.** Fixes the references and the `predicate` flag but leaves
  "show me samples" unsayable and pushes the family/kind grounding entirely onto the planner.
  Rejected: the hierarchy is the server's fact to state.
- **Expose abstract classes as ordinary object types backed by no table.** Every resolver would
  need the same closure logic ad hoc; an interface is the GraphQL construct for exactly this.
  Rejected.
- **Rename abstract classes concrete in the schema.** Pushes a Mosaic limitation onto schema
  authors and changes the meaning of `Sample`. Rejected.

## Notes / open sub-questions

- Interface naming when a concrete class and an abstract class share a slot name with different
  ranges narrowed by `slot_usage` (e.g. `DissectFixedBrain.inputs: CNSSection` under
  `Process.inputs: Sample`): the interface field carries the abstract range; the implementing
  object type may narrow it (GraphQL allows covariant implementations). Confirm Strawberry emits it.
- Whether `NamedThing` (the root, abstract, every class descends from it) should be excluded by
  policy: it qualifies by rule 2 but an anchor over the whole graph is not useful. Recommended:
  skip a class whose closure is the entire exposed set.
- Mixins stay excluded (`concrete_class_closure` already skips them).
