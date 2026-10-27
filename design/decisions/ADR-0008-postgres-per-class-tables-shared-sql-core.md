# ADR-0008: Postgres storage converges to generated per-class typed tables (shared SQL core)

- **Status:** Proposed
- **Date:** 2026-08-19
- **Deciders:** labadorf (pending), design session
- **Related:** sec3b (Relational Storage Mapping — the per-class, LinkML-generated design SQLite
  implements; class-table-over-single-table rationale in §3b), ADR-0002 (multivalued reference
  slots as relationships — already physically identical on both backends), ADR-0006/0007 (the
  typed filter and aggregation surfaces whose estimates are double-priced by this divergence);
  history: v0.6.0 / PTS-168–174 (SQLite's migration to per-class typed tables; the legacy
  `entities` blob table dropped), issues #79/#81 and the v0.10.3–v0.10.6 releases (Postgres
  *feature*-parity campaigns that shipped against the JSONB model), `core/storage/
  pg_ddl_generator.py` (`PostgresDDLGenerator` — "the not-yet-wired-into-CRUD per-class-table
  migration path", maintained in lockstep with the SQLite DDL generator).
- **Tracking issue:** [#162](https://github.com/datahelix-platform/mosaic/issues/162)

## Context

The two storage adapters implement **different physical models**. SQLite stores each exposed
entity class in its own typed table generated from the LinkML schema (`SQLTableGenerator`;
per-class columns with real types); Postgres stores every entity in one `entities` table with
a JSONB `data` document. The divergence is **vestigial, not mechanical**: both adapters began
on the generic single-table model, v0.6.0 (PTS-168–174) migrated SQLite to per-class typed
tables and dropped the legacy blob table, and Postgres simply never received the equivalent
migration. The intended end state is already acknowledged in-tree — `PostgresDDLGenerator`
mirrors the SQLite DDL generator with Postgres types (`TIMESTAMPTZ`, `DOUBLE PRECISION`,
`NUMERIC`) and tsvector/GIN in place of FTS5, has been kept current with every DDL rule change
since, and the xref lookup surface raises `NotImplementedError` on Postgres "pending its
per-class-table migration".

Nothing about Postgres mechanics favors the JSONB model; if anything Postgres is the better
fit for generated typed relational tables (native types, planner statistics, `ALTER TABLE`).
The divergence survived because each Postgres parity effort (#81 multivalued refs, FTS parity,
write parity) was scoped as *feature* parity and shipped against JSONB — cheaper per increment,
compounding overall.

The compounding cost is the "double-priced SQLite/Postgres" tax on every query-surface
feature (named in ADR-0006/0007's estimates): per-range JSONB cast machinery
(`_filter_cast`, `::numeric`/`::timestamptz`), `jsonb_exists` absence semantics, the
`BIGSERIAL seq` workaround for SQLite's implicit rowid ordering, hydration differences, and —
ahead of us — divergent GROUP BY/MIN/MAX/ORDER BY rendering (ADR-0007) and genuinely different
correlated-`EXISTS` join shapes for relationship predicates (ADR-0006 M5a/M5b).

What is *not* double-priced anymore: the operator vocabulary, leaf/tree validation, and the
Python-side as-of evaluators already live once in `core/storage` (`validate_leaf`,
`normalize_where`, `matches_operator`, `matches_tree`), and both adapters share
`_leaf_predicate`-shaped compilers whose remaining difference reduces to one question — *how
is field F referenced as a typed SQL expression on this backend* (`"age"` vs
`(data->>'age')::numeric`).

## Decision

**Mosaic will converge the Postgres adapter onto generated per-class typed tables — finishing
the PTS-168–174 migration on the second backend — and extract the shared SQL core the
convergence makes natural.** Concretely:

1. **Wire `PostgresDDLGenerator` into CRUD.** Postgres deployments get the same per-class
   typed tables SQLite has (same LinkML-driven DDL, Postgres types), with a one-time,
   idempotent data migration from the `entities` JSONB store (mirroring how SQLite's
   `_run_migrations` dropped its legacy tables). Read/write/hydration paths move off
   `data->>` extraction onto typed columns; the xref-lookup `NotImplementedError` gap closes.
2. **Extract the shared SQL core** ("GenericSQLAdapter" in template-method form, or an
   equivalent shared compiler module): find, filter-tree compilation, and the
   aggregation/ordering surface (ADR-0007) written **once**, parameterized by a
   `column_expr(entity_type, field)` hook and a small dialect descriptor (parameter
   placeholder style, `LIKE`/`ILIKE`, boolean literals). Increments of this extraction may
   land *before* the physical convergence — they pay for themselves immediately in the
   remaining query-surface waves — and the hook collapses to a quoted column name on both
   backends afterwards.
3. **Parity tests are retained**, not retired: shared code shrinks them, but semantic
   backend differences (locale vs. ASCII case folding, numeric edge cases, FTS ranking)
   still need pinning, and the parity suite is what has caught real divergences (the
   three-valued-`NOT` case) to date.

**Stays per-backend, deliberately:** FTS engines (FTS5 vs. tsvector/trigram), connection and
transaction plumbing (embedded file + thread-local vs. pooled server), and provenance-store
internals. The convergence is about the *entity storage model and its query compiler*, not
about pretending the engines are identical.

**Sequencing (decided with the problem owner, 2026-08-19):** this ADR is the **next feature
after the current query-surface campaign** (OpenSpec #155–#158 / waves A3–A7), not a blocker
inside it. The initial deployment target is SQLite, so SQLite correctness leads; Postgres
keeps feature parity where the existing JSONB machinery makes it cheap, and where the JSONB
model makes a remaining feature disproportionately expensive (candidate: M5b's link-table
`EXISTS` against the single `entities` table), that piece may be explicitly deferred onto
this ADR with a coded not-implemented error rather than built twice — an honest gap, never a
silent one.

## Consequences

- **The double-pricing dissolves at the root.** Post-convergence, a new query-surface feature
  is written once against typed columns; backend differences reduce to dialect trivia carried
  by the shared core's hooks. The `_filter_cast` machinery, `jsonb_exists` absence handling,
  and `_jsonb_text` rendering are deleted.
- **A breaking storage migration for existing Postgres deployments.** The JSONB → per-class
  migration must be idempotent, transactional (`staged_transaction` scope), and shippable as
  a normal versioned upgrade; Beta status and the weekly release cadence make this tractable
  now and worse later.
- **Aggregation and relationship predicates get cheap on Postgres** exactly where they are
  currently the most expensive: GROUP BY/MIN/MAX/ORDER BY on typed columns need no casts, and
  M5a/M5b `EXISTS` takes the same per-class join shape as SQLite.
- **Rewrite surface:** the Postgres adapter's create/read/update/find/hydration paths and its
  FTS table wiring move to per-class tables; the provenance store, relationships table, and
  reference-write-log are unaffected (already shared shapes).
- **Risk:** schema evolution on Postgres now requires `ALTER TABLE` migrations where JSONB
  absorbed additive change for free. Mitigation: SQLite has run this exact model since v0.6.0,
  and the migration planner/DDL diff machinery it uses is LinkML-driven and backend-agnostic
  in design (sec3b).

## Alternatives considered

- **Keep the JSONB single-table model and record it as deliberate.** Rejected: no recorded
  rationale exists, the in-tree evidence (maintained `PostgresDDLGenerator`, "pending its
  per-class-table migration" gaps) shows the opposite intent, the model contradicts sec3b's
  design, and it makes every future query feature permanently double-priced.
- **`GenericSQLAdapter` over the divergent physical models (shared base class only, no
  physical convergence).** Captures the compiler-level duplication (worth doing — adopted as
  increment-able step 2) but cannot erase cast machinery, absence semantics, or the divergent
  `EXISTS` join shapes; risks a god-base-class absorbing per-backend concerns like pooling
  and FTS. Insufficient alone.
- **Adopt SQLAlchemy Core as the abstraction layer.** Solves dialect rendering, not the
  storage-model divergence (the actual cost center); adds a heavyweight dependency to a repo
  that deliberately uses raw drivers; the shared-core-with-two-hooks design captures the same
  benefit at a fraction of the surface.
- **Converge downward (SQLite adopts the single JSONB-ish table).** Strictly worse: gives up
  typed columns, real indexes, and the shipped v0.6.0 migration on the primary deployment
  backend to match the legacy shape.

## Notes / open sub-questions

- Migration UX: auto-migrate on adapter init (SQLite `_run_migrations` precedent) vs. an
  explicit `mosaic migrate` step for server deployments — decide at implementation.
- Whether the shared-core extraction (step 2) lands as an early increment during waves A3–A7
  (recommended: before the aggregation wave, so GROUP BY/ORDER BY rendering is written once)
  or together with the physical convergence.
- `entities`-table retirement: keep a read-only compatibility view during a deprecation
  window, or clean break per the ADR-0005 precedent.
- Implementation follows as its own OpenSpec change once ratified; do not implement ahead of
  the current campaign (#155–#158).
