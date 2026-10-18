# ADR-0005: GraphQL reference emission is edge-only

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** labadorf
- **Related:** sec3 (graph-shaped API over relational storage), sec3b (reference stored
  inline as the target id string), sec9 §9.5 (reference storage), ADR-0002 (multivalued
  reference slots persist as relationships; no-silent-loss invariant), ADR-0004 (Hippo→Mosaic
  rename, in flight); consumer: Aperture (reference-rendering bugs to be filed against this
  outcome). Code: `src/mosaic/graphql/schema_builder.py` (`_slot_spec`, `SlotKind.REFERENCE`).
- **Tracking issue:** [#131](https://github.com/datahelix-platform/mosaic/issues/131)

## Context

Today a single reference slot renders as **two** GraphQL output fields
(`schema_builder.py:13-16`, `_slot_spec:379-414`):

- a **raw scalar** — `donor` → `donorId: ID` (or multivalued `samples` → `sampleIds: [ID]`),
  the target's stored id;
- a **resolved edge** — `donor: Donor` / `samples: [Sample]`, a DataLoader-batched traversal
  to the target object — emitted only when the target class is exposed and the resolved name
  doesn't collide (`resolvable`).

This dual emission is the PostGraphile/Hasura reflect-relational idiom, consistent with
Mosaic's "config-driven relational storage with a graph-shaped API." But it has never been
recorded as a decision — it lives only in the builder — and two forces now put it in question:

1. **The scalar is type-erased, and that erasure leaks into consumers.** `donorId: ID` drops
   the `Donor` class the schema knows (`range: Donor`). A generic consumer (Aperture) that
   introspects `__schema` cannot tell `donorId` from a primary key or any other `ID` scalar,
   so it can neither link it to the right collection nor distinguish it from the entity's own
   identity. The resolved `donor: Donor` edge carries the class correctly; the scalar is a
   lossy duplicate of the same fact.

2. **The scalar hard-codes a storage detail into the API contract.** Emitting the *stored*
   reference value pins the API to whatever the storage adapter persists. Today logical id ==
   physical key (the `id` is the TEXT/UUID primary key — sec3b), so there is no visible leak.
   But if a storage adapter later introduces an **int surrogate key** for join performance
   (text/UUID FK joins degrade at scale), the raw field would expose that surrogate — an
   identifier meaningless across adapters, unstable across reloads, and not the entity's
   logical identity. The API would then depend on the storage layer's key strategy.

These surface a latent invariant Mosaic otherwise honors but has never stated:

> **Logical-identity boundary.** The API expresses references in terms of the target's
> *logical* identity (its `id`). Physical / surrogate storage keys are adapter-private and
> never cross the API boundary.

The question: **should a reference emit the raw stored scalar at all, or only the resolved
edge?** The *storage* question (int surrogates) is explicitly **not** decided here — it is a
future storage-adapter matter with its own ADR. It appears only as a force, because the
emission choice determines whether that optimization stays independent of the API.

### Forces / constraints

- **Graph-shaped API (sec3):** references are edges; the object field is the native
  expression of an edge.
- **Storage independence (three-layer architecture):** the adapter layer must be swappable
  without changing the API contract.
- **List-view ergonomics (consumer force):** a table showing 25 rows wants to display and link
  "which donor" without materializing 25 full `Donor` objects. The raw scalar gave that in the
  already-fetched row; an edge requires a resolve (DataLoader-batched to one query per page per
  target type — cheap, not free).
- **Writes are unaffected:** create/update inputs already accept the target id under the slot
  name (`_build_one_input:585-619`) — that is logical identity, honoring the invariant. This
  ADR concerns **output** emission only.

## Decision

**Mosaic will emit reference slots edge-only.** A reference slot renders as exactly one output
field — the resolved relationship field (`donor: Donor`, `samples: [Sample!]!`) — and the raw
`*_id` / `*_ids` scalar is **removed** from generated output types. A consumer that needs the
target id asks through the edge (`donor { id }`); the DataLoader already batches that per
request.

Mosaic adopts the **logical-identity boundary** as a stated invariant: references in the API
carry the target's logical `id`; physical/surrogate storage keys are adapter-private. This ADR
governs the API surface; it imposes no storage change and permits future storage-key
optimization precisely because that optimization becomes API-invisible.

**References always resolve.** Value-type ranges render as `SlotKind.STRUCTURED` (JSON), not as
references; infrastructure classes are never reference targets; therefore every `SlotKind.REFERENCE`
slot targets an exposed domain class and has an edge to emit. A slot whose generated resolved
field would **collide** with a sibling attribute is a **build-time error** — Mosaic fails loud
at schema load rather than silently falling back to a raw-id scalar (the ADR-0002
no-silent-loss discipline). Edge-only removes the graceful raw-id downgrade, so the collision
case that previously downgraded now must be surfaced and fixed in the schema. (If the type
model can ever classify a reference to a *non-exposed* class as `REFERENCE`, it joins the same
fail-loud path — see implementation notes.)

**Multivalued cardinality is resolve-to-count for now.** A multivalued reference renders as the
resolved list only; a consumer counts by resolving. A cheap-count optimization (a `count`
field, or reuse of a relationship total) is explicitly **deferred to its own tracking issue**,
not decided here.

Scope: this decision covers `SlotKind.REFERENCE` output emission only. It does **not** change
filter inputs (which key on slot names / logical id), create/update inputs, `SlotKind.STRUCTURED`
JSON emission (a separate concern), or the `findByXref` reverse-lookup surface.

## Consequences

- **Breaking API change — clean break.** The raw `*_id`/`*_ids` output field is removed
  outright; there is no `@deprecated` window (early software; a clean break is acceptable).
  Any client reading `donorId` / `sampleIds` breaks. Aperture is the known consumer; its
  reference-rendering fixes are **deliberately sequenced after** this lands, so they are
  written against the edge-only contract rather than entrenching heuristics against the
  type-erased scalar. Ships as a breaking version bump (Mosaic just cut 0.11.0) and moves the
  certified-frontier ledger + re-pins the deploy recipes in one coordinated step.
- **Concerns cleanly separated.** The producer (Mosaic) owns the contract; the consumer
  (Aperture) reads it — no `aperture:`-namespaced annotations in domain schemas, no UI concerns
  in the LinkML source.
- **Storage layer freed.** With no physical key on the API surface, a storage adapter may adopt
  int surrogates (or any physical key) with zero API impact — the emission and storage
  decisions become independent, which they are not under the status quo.
- **Cost: list-view resolves.** Displaying/linking a single-ref column now costs a batched
  resolve per page per target type instead of a free column read — one extra query per page,
  not per row. Multivalued counts cost a resolve until the deferred cheap-count optimization
  lands.
- **Obligation: the invariant.** Future storage/adapter ADRs must honor the logical-identity
  boundary; surrogate-key work translates at the adapter boundary (it already must, on the
  filter/write path).
- **`id` present on every exposed type** for the edge to yield an identifier — it is
  (`_output_annotation:431-432`).

## Alternatives considered

- **Status quo — emit both raw scalar and resolved edge.** The reflect-relational idiom; gives
  cheap list display + filtering. **Rejected:** the scalar is a type-erased duplicate that
  misleads generic consumers (the Aperture bugs) and hard-codes a storage detail into the
  contract, coupling the API to the storage key strategy.
- **Option B — keep a scalar, but contract it as the *logical* id, never the physical key.**
  Preserves cheap display/cardinality and storage independence by requiring the adapter to map
  surrogate→logical on read. **Rejected:** retains the "two fields, one fact" awkwardness and
  the erasure that confuses consumers, and reintroduces a lookup that spends part of a
  surrogate's performance win whenever the id is materialized. Reconsider only if list-view
  resolve cost proves unacceptable (see notes).
- **Expose the reference as a custom scalar typed to the target class** (e.g. `DonorRef`).
  Carries the class without an object resolve. **Rejected:** non-idiomatic, invents a type
  system parallel to GraphQL's, and still isn't the edge a graph API should present.

## Notes / open sub-questions

- **Multivalued cheap-count (deferred, [#132](https://github.com/datahelix-platform/mosaic/issues/132)).**
  resolve-to-count is the interim; the optimization — a `count` field or relationship total
  that avoids resolving the whole list — is tracked separately and is not a blocker for
  ratifying this ADR. **Settled at implementation (issue #132):** `MosaicClient.count_relationship`
  / GraphQL `<field>Count`, a single indexed `COUNT(*)` over the relationships table joined to
  the target's table (reusing ADR-0006 M5b's edge resolution and availability rule) — resolve-to-count
  remains available for callers that also want the members.
- **List-view resolve cost.** Validate the per-page batched-resolve cost against a realistically
  large collection (the brainbank demo) before ratifying — the one force that could push back
  toward Option B.
- **Confirm the resolvability assumption in code.** Verify `build_type_model`
  (`core/schema_typing`) never classifies a reference to a non-exposed class as `REFERENCE`
  (value types → STRUCTURED; infrastructure classes excluded). If it can, route that case to
  the same build-time fail-loud path as name collisions.
- **Internal consumers.** Confirm no Mosaic-internal surface (typed client, OpenAPI renderer,
  tests) reads the raw output field before removal.
