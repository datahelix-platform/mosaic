# Tasks — `search-composition`

Tracking issue: datahelix-platform/mosaic#157

> Implemented 2026-08-19 (after ADR-0006 increments 1–2 and ADR-0007 landed, so `where:`
> composition and `order_by` precedence shipped in the same change).

## 0. Design gate

- [x] 0.1 bm25-rank vs. `order_by` precedence confirmed as recommended — rank by default,
  explicit `order_by` overrides (via the ADR-0007 pushdown) — and recorded in ADR-0007's
  notes.
- [x] 0.2 Breaking envelope change (bare list → `Page`) recorded as a BREAKING changelog
  entry for the next minor; Aperture (the known consumer) asked for exactly this envelope
  and its REST/GraphQL clients tolerate both shapes during rollout.

## 1. Id-set composition in the search service

- [x] 1.1 `QueryService.search` rewritten: both adapters' ranked FTS path (`storage.search`
  → `ScoredMatch`) → one `find()` with the id set ANDed into the `where` tree (so
  `filter_mode="or"` can never escape the hit set); per-hit `get` loop removed.
- [x] 1.2 Rank order preserved through composition: the filtered match set is re-ordered
  by FTS rank in the service, then paged (hit set bounded at 1000 ids, so the re-order is
  trivially cheap; a rank-join pushdown was not warranted).
- [x] 1.3 Postgres search path composes identically (`TestPostgresSearchComposition`,
  expectations mirrored from the SQLite suite).
- [x] 1.4 Availability filtering identical to list queries — enforced twice (the ranked FTS
  path filters availability, and the composed `find()` applies the list rule); tested on
  both adapters.

## 2. Page envelope + filter arguments on the transports

- [x] 2.1 GraphQL search twins return the `Page` type (`items/total/limit/offset`);
  resolver-side slicing deleted (kills the `offset >= limit → []` bug).
- [x] 2.2 Search twins accept `filters:`, `where:`, `filterMode`, and `orderBy`/`orderDir`
  (typed-filter-inputs and aggregation-and-ordering landed first), with the coded-error
  discipline for unknown fields.
- [x] 2.3 REST search endpoint mirrored: same paginated envelope as `GET /entities`, same
  arbitrary field-filter query params, plus `order_by`/`order_dir`; SDK
  `MosaicClient.search` returns `PaginatedResult`.

## 3. Tests & docs

- [x] 3.1 Regression tests at SDK, GraphQL, and REST layers: `offset >= limit` returns the
  correct page; full page-through visits every hit exactly once.
- [x] 3.2 Instrumented no-N+1 assertion: exactly one `find()` and one batched
  `get_temporal()` per page, zero `read()` calls.
- [x] 3.3 `total` correctness under composed filters on both adapters, including the
  availability rule and the FTS-bound-vs-unbounded count contrast.
- [x] 3.4 Changelog BREAKING entry; `docs/graphql.md` + sec4 §4.7 search sections updated.
