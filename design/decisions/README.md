# Hippo Design Decisions (ADRs)

Hippo records design decisions as **ADRs** following the **platform-wide convention** — the
canonical process and template live in the parent repo at
[`platform/design/decisions/README.md`](../../../platform/design/decisions/README.md) (see also
the root [`../CLAUDE.md`](../../CLAUDE.md)). ADR files live in this directory;
[`_template.md`](./_template.md) is a local copy of the canonical template for convenience.

## Hybrid adoption (2026-06-17)

Hippo is a mature component with a large body of already-settled, shipped decisions recorded in
the **Key Decisions Log** of [`../INDEX.md`](../INDEX.md). Hippo therefore adopts ADRs
**forward-only**:

- **New, non-trivial, or still-in-flux decisions** get an ADR here, indexed from the INDEX.
- The existing **Key Decisions Log remains the scannable index** of record for the settled,
  shipped decisions and is **backfilled only opportunistically** — when a settled decision is
  revisited and capturing its alternatives is worthwhile — never as a mass migration.

The supersede-don't-delete discipline already used in the Key Decisions Log (entries gain a
`Superseded by` pointer rather than disappearing) matches the ADR lifecycle exactly.

## Index

| ADR | Decision | Status |
|---|---|---|
| [0001](./ADR-0001-graph-level-as-of-query.md) | Graph-level / query-spanning as-of reconstruction | ✅ Accepted |
| [0002](./ADR-0002-multivalued-reference-slots-as-relationships.md) | Multivalued reference slots persist as relationships (issue #79) | 🟡 Proposed |
| [0003](./ADR-0003-polymorphic-tree-root-ingest.md) | Polymorphic tree-root ingest via `designates_type` dispatch (issue #80) | 🟡 Proposed |
| [0004](./ADR-0004-rename-hippo-to-mosaic.md) | Rename the Hippo component to **Mosaic** (music/art naming convention; PyPI-saturation dissolved by platform ADR-0002) | ✅ Accepted |
| [0005](./ADR-0005-graphql-reference-emission-edge-only.md) | GraphQL reference emission is edge-only — logical-identity boundary (issue #131) | ✅ Accepted |
| [0006](./ADR-0006-graphql-typed-filter-contract.md) | The GraphQL filter contract is typed per-class input objects — not CEL on the wire (issue #153) | ✅ Accepted |
| [0007](./ADR-0007-aggregation-and-ordering-surface.md) | Aggregation & ordering surface — count mode, facet counts, min/max, `order_by` (issue #154) | ✅ Accepted |
| [0008](./ADR-0008-postgres-per-class-tables-shared-sql-core.md) | Postgres storage converges to generated per-class typed tables + shared SQL core (issue #162) | 🟡 Proposed |
| [0009](./ADR-0009-mcp-boundary-capability-manifest-queryspec.md) | Mosaic hosts an MCP boundary over a server-derived capability manifest and the QuerySpec artifact (issue #177) | ✅ Accepted |
| [0010](./ADR-0010-mcp-outbound-delegation-to-a-planning-service.md) | The MCP boundary may delegate outbound to a planning service, as an untrusted planner behind a validating relay (issue #186) | 🟡 Proposed |
| [0011](./ADR-0011-inverse-slots-as-virtual-reverse-edges.md) | `inverse`-declared slots are virtual reverse edges over the forward FK — no storage of their own (issue #204) | ✅ Accepted |
| [0012](./ADR-0012-inverse-declared-on-the-forward-slot.md) | Honour `inverse` declared on the forward slot, so import-layered schemas can declare reverse edges (amends ADR-0011; issue #230) | ✅ Accepted |
| [0013](./ADR-0013-reverse-of-multivalued-forward-slots.md) | Reverse edges over multivalued (relationship-row) forward slots (lifts ADR-0011's deferral; issue #231) | ✅ Accepted |
| [0014](./ADR-0014-polymorphic-graphql-types-for-abstract-ranges.md) | Abstract classes get GraphQL interface types, are queryable anchors, and the manifest carries the class hierarchy (issue #228) | 🟡 Proposed |
