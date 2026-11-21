# LinkML Integration Test Coverage — TODOs

Gaps identified in `tests/integration/` coverage of LinkML-backed behavior. Pattern across the list: DDL emission is well-tested at the unit level, but **runtime enforcement** of constraints and **error propagation** through higher layers (REST, migration failures, Postgres) are the consistent blind spots.

## High severity — production / data-integrity risk

- [ ] **Non-abstract `is_a` inheritance** — DDL generator emits CASCADE-DELETE FKs for parent classes; only abstract parents are unit-tested. Need an integration test that inserts child rows, deletes parents, and asserts cascade behavior.
- [ ] **Composite `unique_keys`** — DDL emission is unit-tested; no integration test actually attempts a duplicate multi-column insert to verify the DB rejects it.
- [ ] **Single-column `mosaic_unique`** — DDL side is covered, runtime enforcement isn't.
- [ ] **Required-field addition during schema evolution** — `TestAdditiveFieldAddition` only covers optional additions. Add a test for the failure/backfill path when a required field is added to a schema with existing rows.
- [ ] **PostgreSQL DDL parity with SQLite** — `test_postgres_adapter.py` only exercises CRUD. Add a DDL-parity test for the same LinkML schema against both adapters (type mappings: `TIMESTAMPTZ`, `DOUBLE PRECISION`, `BOOLEAN`, enum/index/FTS behavior).

## Medium severity — observable gaps

- [ ] **Enum `permissible_values` at write time** — `test_migrate_enum_field` checks DDL only; add a runtime rejection test for an out-of-enum value.
- [ ] **CEL validator errors through REST** — asserted at `HippoClient` level (`pytest.raises`); verify errors surface as HTTP 400/422 with structured detail.
- [ ] **Read-side / cross-entity validators** — pipeline supports priority-ordered validators, but only single-op CEL write-validators are exercised.
- [ ] **`hippo_default` / LinkML `ifabsent`** — DDL emits `DEFAULT`; no test creates an entity with the field omitted and verifies the default lands.
- [ ] **Partial-index correctness** — `mosaic_index_partial` DDL is asserted; add a query-through-the-index test confirming unavailable rows are excluded.
- [ ] **FTS modes beyond `fts5`** — only `fts5` is tested in E2E. Cover other supported modes (or explicitly document that only `fts5` is supported).
- [ ] **Validator timeouts at integration level** — unit-tested only; never tripped in a real E2E flow.

## Low severity — nice-to-have

- [ ] **Advanced LinkML features** — mixins, `slot_usage`, `subsets`, `tree_root` are SchemaView-supported but untested.
- [ ] **`pattern` / `minimum_value` / `maximum_value` slot constraints** — delegated to LinkML's validator; no integration test violates them.
- [ ] **Multi-file schema imports** — `SchemaRegistry._from_directory()` merges YAML files; no test splits a schema across files.
- [ ] **Malformed LinkML YAML** — no test asserts a user-friendly error on bad schema input.
- [ ] **Schema-version tracking on provenance reads** — CLAUDE.md says `schema_version` is computed at read time from the provenance log; `TestProvenanceImmutability` doesn't verify it.
- [ ] **Migration rollback / mid-migration failure** — preview/no-op cases exist; failure recovery and dry-run-vs-apply divergence don't.
- [ ] **REST schema introspection** — `serve/routers/schema.py` exists but only `/health` + `/openapi.json` are hit.
