# ADR-0014: Polymorphic references resolve over the concrete class closure; abstract classes get GraphQL interface types, are queryable anchors, and the manifest carries the class hierarchy

- **Status:** Accepted
- **Date:** 2026-11-10 (ratified, scope widened; proposed 2026-10-27, shape decided in the
  datahelix#113 decision pass, item M2)
- **Deciders:** labadorf (shape 2026-10-27; scope and ratification 2026-11-10, in the
  [datahelix#126](https://github.com/datahelix-platform/datahelix/issues/126) decision pass)
- **Related:** ADR-0005 (edge-only GraphQL references — the raw `*_id` fallback this ADR retires);
  ADR-0006 (typed `where:` filter contract — the interface-typed filter input this ADR adds);
  ADR-0009 (capability manifest and `QuerySpec` — `predicate` and `UNKNOWN_ANCHOR` semantics
  change here); ADR-0011 / ADR-0012 / ADR-0013 (inverse slots — every reverse ranged on an abstract
  class is usable only through this ADR); issue #224 (`concrete_class_closure`, the inverse-edge
  storage half already shipped); [#241](https://github.com/datahelix-platform/mosaic/issues/241)
  (rows lost through concrete parents and forward abstract edges — fixed by Decision 1);
  reel#10 (the planner anchors on abstract `Sample`); datahelix#113 M2, M5, R3; datahelix#57
  (pending `hippo_*` → `mosaic_*` annotation rename).
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
   them. The storage layer resolves *inverse* edges over an abstract range since #224; no
   transport can use them. Forward references into an abstract range are worse off: see 5 below.
3. **An abstract class cannot be an anchor.** `validate_query_spec` answers `anchor: "Sample"`
   with `UNKNOWN_ANCHOR` (`query_spec.py:469`). A planner grounded on the manifest sees `Blood`,
   `Brain`, `CSF` and 30 more with nothing tying them to the word "sample" the user and the
   schema's own descriptions use (reel#10). The same gap blocks ADR-0012's flagship case: a
   synthesized `Donor.samples → Sample` resolves in storage and is unusable from GraphQL, REST
   or MCP.

4. **A concrete class with subclasses loses rows too** ([#241](https://github.com/datahelix-platform/mosaic/issues/241)).
   A reference is resolved in its range class's own table, so a reference ranged on concrete
   `Process` whose target row lives in `BankFreshBrain`'s table renders `null` in GraphQL. On
   brain-bank data at v0.15.0: `CNSSection.producedBy` null for 13 of 13 rows, `Brain.producedBy`
   11 of 11, `Measurement.person` (→ `Person`, the row is a `Donor`) 1 of 1. Brain bank has 14
   concrete classes with subclasses and 128 exposed forward reference fields ranged on them. This
   also leaves datahelix#113 M5's "Sample → producing process" broken in GraphQL.
5. **Storage handles abstract ranges for inverse edges only** (#241). This ADR as first proposed
   said storage already resolved these edges since #224. That holds for inverse hydration. A
   *forward* `count_relationship` into an abstract range ignores the closure (an `RNASeqAssay` that
   hydrates 3 `inputs` counts 0), and an edge filter into one cannot see a leaf-table field
   (`Unknown filter field 'sample_id'`). The same gaps hit a concrete base with subclasses (an
   inverse listing 1 item while its count says 2).

Abstract classes on this schema: `Activity`, `ArrayAssay`, `ArrayDataset`, `Assessment`,
`Association`, `CultureTissue`, `NamedThing`, `Quantity`, `Sample`, `SequenceDataset`,
`SequencingAssay`. `Person`, `Dataset` and `Assay` are concrete (datahelix#113 listed them as
abstract; the reproduction corrected that), and with `Process` they are the concrete-parent case
of item 4.

## Decision

**Mosaic will resolve every reference over the concrete closure of its range class, and represent
an abstract class as a first-class polymorphic entity.** A class *qualifies* when it is the range
of an exposed reference or has at least one exposed concrete descendant. This covers two kinds of
parent:

- a **concrete parent** — a concrete class with exposed subclasses (`Process` with
  `BankFreshBrain`; `Person` with `Donor`): Decision 1 only;
- an **abstract parent** (`Sample`, `Activity`, `Assessment`, …): Decisions 1–5.

1. **Closure-aware resolution, for every reference.** A reference (forward or inverse) ranged on
   class `C` resolves the referenced id across `concrete_class_closure(C)` — `C` itself when it is
   concrete, plus every exposed concrete descendant — rather than in `C`'s own table only, and
   carries the row's concrete `entity_type`. `count_relationship` and edge predicates (`where:`
   to-one nesting and `some`/`none`, `QuerySpec` edge conditions) are closure-aware in the same
   way, in **both SQLite and Postgres**, forward and inverse alike. A nested `where` on such an
   edge accepts the slots induced on `C`; a slot only some descendants have is `UNKNOWN_SLOT`, as
   on an inverse edge today.
   - **Concrete parent: no change to field types.** The field keeps its type — the base object
     type (`Process`, `Person`) — and a subclass row is rendered as that base type, exposing the
     base's induced slots. `__typename` names the base, not the subclass. This fixes #241
     (`CNSSection.producedBy`, `Brain.producedBy`, `Measurement.person` returning `null`) and
     datahelix#113 M5's "Sample → producing process".
   - **Concrete parent: its own collection resolves over the closure too.** A concrete parent's
     own collection and accessor (the GraphQL list `processes` and its `Page`/count, the SDK
     `query(entity_type="Process")`, and `QuerySpec` with `anchor: "Process"`) resolve over
     `concrete_class_closure(Process)`, so they list `BankFreshBrain`, `SectionFixedBrain`, …
     rows as well, rendered as the base object type the same way a reference is. Filters,
     `order_by`, facets and `field_range` on that collection apply to the base's induced slots.
     Each row carries its concrete type so a caller can narrow: the SDK envelope already carries
     `entity_type` (the leaf class) on every query item; GraphQL has no such field today
     (`__typename` names the base, and the per-type object types expose only `id`,
     `is_available` and the schema's slots), so the implementation **adds a non-null
     `entityType: String!` system field** to the object type of every concrete parent, naming
     the row's concrete class. That field is additive. To narrow, a client filters or re-queries
     the subclass's own collection. **This is a behaviour change for existing clients:** lists,
     page `total`s and counts on a concrete parent grow by the subclass rows. It must be called
     out in the release notes of the version that ships it.
   - **Abstract parent:** rendered through the interface of Decision 2.
2. **GraphQL: an interface type per qualifying abstract class**, carrying the slots induced on the
   abstract class itself, implemented by every concrete descendant's object type. A reference or
   inverse ranged on the abstract class resolves to the interface and returns concrete objects
   (`__typename` names the leaf). The raw-id fallback and its build-time warning are removed for
   this case; a reference whose target has **no** exposed concrete descendant stays a raw id, and
   the warning stays for it. Interface, not union: the shared slots (`Sample.donor`,
   `Sample.sample_id`, …) are the common case and must be selectable without a fragment per
   concrete kind.
3. **A qualifying abstract class is a queryable, read-only anchor.** It gets an accessor
   (`samples`), a `Page` type, a typed filter input built from its induced slots (ADR-0006), and
   `search{Plural}` when any descendant has a searchable slot. The collection resolves over the
   concrete closure, in the shape #224 gave inverse hydration: SQLite `UNION ALL` across
   per-class tables, Postgres `entity_type = ANY(...)`. Rows carry their concrete `entity_type`.
   `order_by`, facet counts and `field_range` apply to induced slots only; a subclass-only slot is
   `UNKNOWN_SLOT` on the abstract anchor.
4. **The capability manifest carries the class hierarchy.** `EntityCapability` gains
   `abstract: bool`, `parent: Optional[str]` (the nearest `is_a`), and `kinds: tuple[str, ...]`
   (the exposed concrete closure, empty for a leaf). `mosaic://capabilities` and
   `MosaicSlotInfo`/`hippoSchema` serialize them. `predicate` for a reference becomes true when
   its target class is in the manifest, which now includes qualifying abstract classes.
   `QuerySpec` follows: `UNKNOWN_ANCHOR` no longer fires for a qualifying abstract anchor;
   `NOT_PREDICATE_FILTERABLE` no longer fires for a reference ranged on one. The
   `construct-query-spec` prompt gains one item: an entity with `kinds` is a family; anchor on it
   when the question is about the family, on a kind when it names one.
5. **Root classes are included by the same rule, with a schema-author opt-out.** A class whose
   closure is the whole exposed set (`NamedThing`) qualifies like any other; there is no
   built-in "skip the root" policy. Instead, two new **class annotations** let a schema author opt
   any class out:
   - `hippo_anchor: false` — the class is not an anchor: no abstract accessor, `Page`, filter input
     or `search{Plural}` is generated for it (Decision 3), and `QuerySpec` answers it with
     `UNKNOWN_ANCHOR`. Its interface type (Decision 2) and closure resolution of references ranged
     on it (Decision 1) are unaffected. On a concrete class it removes only `QuerySpec` anchoring;
     its existing accessor and write surface stay.
   - `hippo_manifest: false` — the class has no `EntityCapability` in the planner-facing manifest
     (Decision 4): it is not offered as an anchor, is not named as any entry's `parent`, and a
     reference ranged on it reports `predicate=false`. GraphQL and SDK resolution are unaffected;
     this hides the class from the planner, it does not remove it.

   Both default to `true`, are declared in `hippo_ext.yaml` in the `class_annotation` subset (so
   the existing annotation validator accepts them on classes only), and follow the `hippo_accessor`
   / `hippo_search` naming. Under datahelix#57's pending `hippo_*` → `mosaic_*` rename they become
   `mosaic_anchor` / `mosaic_manifest`, read under both spellings for the deprecation window like
   every other annotation key.
6. **Writes are unchanged.** No create/update input is generated for an abstract class; the
   abstract accessor is read-only. Ingest already dispatches on `designates_type` (ADR-0003).

### Implementation order

1. **#241's strict-xfail tests** on its minimal fixture (`Person`/`Donor`, `Process` with a
   subclass, abstract `Sample` with two leaves, a to-many `Assay.inputs`), pinning each lost-row
   case on both adapters.
2. **Closure-aware resolution** (Decision 1): references, `count_relationship` and edge
   predicates over the closure in SQLite and Postgres. The xfails flip to passes; concrete parents
   are done at this step.
3. **Interfaces and abstract anchors** (Decisions 2–5), then the manifest hierarchy and the
   `QuerySpec` changes.

## Consequences

- #241's lost rows come back: every reference through a concrete parent (128 exposed forward
  reference fields on brain bank) and every forward count/filter into an abstract range resolves
  over the closure, on both adapters, with no change to field types for the concrete case (the
  only GraphQL addition there is the `entityType` field).
- Every brain-bank reverse ranged on `Sample`, `Activity` or `Assessment` becomes usable from
  every transport, which is the condition ADR-0012 and ADR-0013 both name. Donor → samples works
  end to end with M3 + this.
- reel#10 shrinks to its Reel-side half (the tool schema's hard-coded `"Sample"` example):
  `anchor: "Sample"` is simply valid, and the grounding can render "kinds of Sample".
- The GraphQL schema grows one interface per qualifying abstract class and one accessor each,
  `NamedThing` included unless the schema opts it out (Decision 5). Aperture derives collections
  from introspection and will list abstract collections; its `deriveEdges` and the result table
  need to handle interface-typed references (a follow-up on its side, gated on this landing in a
  certified pin — ADR-0029 honest degradation until then).
- A concrete parent's own collection now includes its subclasses' rows (Decision 1): `processes`
  on brain bank lists every banking process. Clients that read `total` or counts on a concrete
  parent see larger numbers; release notes must say so.
- A subclass row reached through a concrete parent shows only the parent's slots, and
  `__typename` says `Process`, not `BankFreshBrain`. That is the price of keeping field types
  unchanged; `entityType` names the subclass, and a client that needs the subclass's own slots
  must query it directly until the deferred change below.
- Query cost: a closure resolution or abstract anchor is a `UNION ALL` over N tables (Postgres: an
  `ANY` over N types); on brain bank `samples` spans 33, and a root anchor spans every exposed
  class. Pagination applies after the union. Acceptable for paths that returned wrong answers or
  did not exist; measure.
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
- **Abstract classes only (the ADR as first proposed).** Leaves #241's concrete-parent nulls and
  every forward count/filter into an abstract range broken. Rejected 2026-11-10.
- **Skip a class whose closure is the entire exposed set** (the original recommendation for
  `NamedThing`). A hard-coded policy guesses which roots are useless; a schema author knows.
  Replaced by the same rule for every class plus the Decision 5 opt-out annotations.

## Notes / open sub-questions

- **Deferred: a concrete parent as a GraphQL interface.** Making `Process` itself an interface
  (implemented by `Process`'s own object type and each subclass), so `__typename` names the
  subclass and subclass slots are reachable by fragment, would change the type of every field
  ranged on a concrete parent — a breaking change. Not decided here; it needs its own ADR if a
  client needs it.
- Interface naming when a concrete class and an abstract class share a slot name with different
  ranges narrowed by `slot_usage` (e.g. `DissectFixedBrain.inputs: CNSSection` under
  `Process.inputs: Sample`): the interface field carries the abstract range; the implementing
  object type may narrow it (GraphQL allows covariant implementations). Confirm Strawberry emits it.
- Mixins stay excluded (`concrete_class_closure` already skips them).
- **Amended 2026-11-10 (labadorf, #267 review):** the question this section left open, whether a
  concrete parent's own collection (`processes`) lists subclass rows, is decided **yes** and moved
  into Decision 1.
