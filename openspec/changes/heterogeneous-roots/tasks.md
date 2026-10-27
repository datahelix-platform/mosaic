# Tasks — `heterogeneous-roots`

Tracking issue: datahelix-platform/mosaic#158

> Implemented 2026-08-19/20 (after `search-composition`, whose ranked path and batched
> materialization discipline `searchAll` reuses).

## 0. Design gate

- [x] 0.1 Decision: **union column edges into traversal** (the preferred option) for
  current-state neighbors — forward slots read off the frontier's data, reverse via
  schema-driven FK queries; `edgeSources` discloses coverage on every response.
- [x] 0.2 Decision: under `asOf`, **column edges are out of scope** (coded notice +
  `edgeSources: [LINK_TABLE]`) — reverse column edges at T would require reconstructing all
  candidate referencing entities, the same cost wall as hippo#71; link-table edges replay
  from provenance and node states reconstruct at T. Never silent.
- [x] 0.3 Decision: merge by the adapters' **per-index-normalized scores** ([0, 1] by max
  rank per index — comparable in relative terms) with the deterministic
  `(score desc, entity_type, id)` tiebreak; no per-class quotas.

## 1. `searchAll`

- [x] 1.1 SDK `search_all(q, limit)` fanning across `SchemaManager.fts_entity_types()` via
  the adapters' ranked search path, merged per 0.3; limit=0 → zero hits (#130).
- [x] 1.2 GraphQL `searchAll(q, limit): [SearchHit]` envelope (`entityId`, `entityType`,
  `score`, `data`, temporal fields) with SDL descriptions.
- [x] 1.3 Batched materialization by type — one composed `find()` per class present in the
  page (instrumented test asserts find-per-class, zero reads).
- [x] 1.4 Availability parity with list queries; both adapters (parity tests mirrored).

## 2. `neighbors`

- [x] 2.1 SDK `neighbors(entity_id, depth, as_of)` — depth-bounded BFS over
  `find_relationships` both directions (whole-neighborhood, not outbound-only traverse);
  as-of via the new `RelationshipManager.live_edges_at` (the store's provenance replay).
  Depth cap 5; 1000-node budget with disclosed truncation.
- [x] 2.2 Column-edge union per 0.1: forward single-valued reference slots read off the
  frontier; reverse edges via schema-driven FK `in` queries (registry-derived, no domain
  nouns).
- [x] 2.3 Batched node materialization by type, never get-per-node (instrumented test:
  exactly one `read` — the start-node existence check); edges kept only when both endpoints
  materialize (availability parity).
- [x] 2.4 GraphQL `neighbors(id, depth, asOf): NeighborhoodGraph` — `{nodes, edges {source,
  target, type, edgeSource}, edgeSources, notices}` with SDL descriptions.
- [x] 2.5 The gap test: a link-table edge AND a column-stored single-valued reference on
  the same entity — both edges present, both adapters (the headline assertion).
- [x] 2.6 asOf behavior per 0.2: provenance-replayed link edges + state-at-T nodes; column
  edges scoped out with the coded notice and `edgeSources` disclosure; tested on both
  adapters.

## 3. Docs

- [x] 3.1 sec4 §4.7 + `docs/graphql.md`: heterogeneous roots documented, including the
  envelope-over-union posture (ADR-0005) and the edge-coverage guarantee.
- [x] 3.2 Changelog entry; the union/interface root remains future work (its own ADR if
  pursued).
