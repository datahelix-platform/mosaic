# ADR-0012: Honour `inverse` declared on the forward slot, so import-layered schemas can declare reverse edges

- **Status:** Proposed
- **Date:** 2026-10-23 (proposed)
- **Deciders:** — (proposed by clandaverde; awaiting maintainer ratification)
- **Related:** ADR-0011 (inverse-declared slots as virtual reverse edges; this ADR amends
  where the declaration may live, answering its first open sub-question); ADR-0013 (reverse of
  multivalued forward slots); datahelix#113 item M3.
- **Tracking issue:** [#230](https://github.com/datahelix-platform/mosaic/issues/230)

## Context

ADR-0011 reads `inverse` only on the **derived, multivalued side**: to get `Donor → samples`
the schema author writes `Donor.samples: {range: Sample, multivalued: true, inverse: donor}`
on `Donor`. In a schema layered by imports, that points a lower module at a higher one.
brainbank-hippo-schema (upstream `05c58aa`) is layered `core ← person ← tissue ← pathology`:
`Donor` lives in `person.yaml`, which imports only `core`, so it cannot name `Sample`
(`tissue.yaml`). Every high-value brain-bank reverse in datahelix#113 has this shape
(Donor → samples; Person/Sample → diagnoses, measurements; Donor → banking events). It is not
brain-bank specific: any import-layered schema hits it.

Measured on mosaic `main` (`e4d93e6`) against the vendored brain-bank schema:

| Declared where | Mosaic | Upstream schema CI |
|---|---|---|
| `Donor.samples … inverse: donor` in `person.yaml` (ADR-0011's site) | Loads (the directory is merged through `brainbank.yaml`); resolves across all 33 concrete `Sample` tables (#224) | `linkml-lint person.yaml` → `error … range 'Sample' is not defined (no_undeclared_ranges)`; `gen-pydantic person.yaml` fails |
| `Sample.donor … inverse: samples` in `tissue.yaml` (forward side) | Loads, **silently ignored** — no `Donor.samples`, no error | Lint output identical to the unpatched file; merged `gen-pydantic brainbank.yaml` passes |

So the only declaration that keeps module boundaries clean is the one Mosaic drops without
saying so. ADR-0011's Notes already list this ("Whether `inverse` should also be honoured on
the *forward* side").

## Decision (recommended)

Mosaic will honour `inverse: <name>` on a **single-valued forward reference slot** by
synthesizing a virtual, multivalued reverse slot `<name>` on the forward slot's range class,
with exactly the storage, query, manifest and transport semantics ADR-0011 gives a
derived-side declaration.

1. **Naming** comes from the `inverse` value (LinkML's own construct — portable, lint-clean,
   shown above), not a Mosaic-specific annotation.
2. **Both sides declared** must agree (each names the other); otherwise `SchemaError`.
3. **Collision:** if `<name>` already exists on the range class as a real slot → `SchemaError`.
4. **Abstract ranges:** the reverse is placed on the (possibly abstract) range class and is
   inherited by its concrete descendants; #224 already resolves the target closure.
5. **Existing ADR-0011 validation still applies:** the forward slot must be single-valued
   (the multivalued case is ADR-0013) and the range an entity class, not a value type.

## Consequences

- Import-layered schemas can declare every FK-backed reverse without breaking their module
  DAG; brain-bank datahelix#113 item S2 becomes a schema-only change upstream.
- A forward-side `inverse` stops being silently ignored — a behaviour change for any schema
  that already carries one expecting nothing to happen (none known; to confirm before
  ratifying).
- The synthesized slot exists only in Mosaic's view of the schema; generic LinkML tooling
  (gen-pydantic, gen-doc) will not show it. Document that in the user-facing schema guide.
- Usability of a reverse ranged on an abstract class (e.g. `Donor.samples → Sample`) still
  depends on polymorphic GraphQL types (#228, datahelix#113 M2): without them the field
  falls back to raw ids and is not predicate-filterable.

## Alternatives considered

- **Keep declaring on the derived side only (status quo).** Forces a layering violation
  that upstream CI rejects; rejected as a hard blocker for import-layered schemas.
- **Mosaic annotation on the forward slot (`hippo_reverse: samples`),** as datahelix#113
  first suggested. More explicit that the slot is synthesized, but Mosaic-specific; ADR-0011
  already rejected a Mosaic annotation in favour of LinkML's `inverse` for portability. Kept
  as the fallback if ratification decides naming a not-yet-existing slot via `inverse` is too
  loose a reading of LinkML.
- **Restructure the schema** (move `Donor` up, or merge modules). Pushes a Mosaic limitation
  onto every schema author; rejected.

## Notes / open sub-questions

- Confirm no deployed schema carries a forward-side `inverse` today (behaviour change).
- Should a synthesized reverse be reported by `inverse_reference_slots()` indistinguishably
  from a declared one, or flagged as synthesized (for docs/diagnostics)?
