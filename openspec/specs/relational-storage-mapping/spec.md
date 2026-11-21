## ADDED Requirements

### Requirement: Relational storage mapping document exists as a separate section
A new design spec section (`sec3b_relational_storage.md`) SHALL exist as a sibling to `sec3_data_model.md`. It SHALL be explicitly scoped as the reference implementation for SQLite and PostgreSQL storage adapters. Its header SHALL state that other adapter types would have their own mapping documents.

#### Scenario: Reader identifies the document's scope
- **WHEN** a reader opens `sec3b_relational_storage.md`
- **THEN** the document header states it describes how the conceptual data model from sec3 maps to relational storage, and that it is the reference specification for SQLite/PostgreSQL adapters only

### Requirement: Entity table schema is defined in the relational storage section
The relational storage section SHALL define the physical table structure for entity types, including that each entity type maps to its own table with `id` (UUID primary key) and `is_available` (boolean) as stored columns, plus user-defined field columns generated from schema config.

#### Scenario: Entity table structure
- **WHEN** a coding agent reads the relational storage section
- **THEN** it finds a clear specification of what columns exist on entity tables: `id`, `is_available`, and schema-config-driven user-defined field columns

### Requirement: Partial index strategy is defined in the relational storage section
The relational storage section SHALL specify that `mosaic migrate` creates partial indexes on every `indexed: true` field, scoped to `is_available = true`. The SQL pattern SHALL be included.

#### Scenario: Partial index SQL pattern
- **WHEN** a coding agent implements `mosaic migrate` for the relational adapter
- **THEN** it finds the exact SQL index pattern: `CREATE INDEX idx_{entity_type}_{field}_available ON {entity_type}s ({field}) WHERE is_available = true`

### Requirement: External IDs table schema is defined in the relational storage section
The relational storage section SHALL define the `external_ids` table schema with columns: `id` (UUID PK), `entity_id` (UUID, indexed), `entity_type` (string, indexed), `system` (string, indexed), `external_id` (string, indexed), `is_active` (boolean), `created_at` (datetime, written by provenance system).

#### Scenario: External IDs table structure
- **WHEN** a coding agent implements the relational adapter's external ID storage
- **THEN** it finds the complete table schema for `external_ids` in the relational storage section

### Requirement: Entity relationships table schema is defined in the relational storage section
The relational storage section SHALL define the `entity_relationships` edge table schema with columns: `id` (UUID PK), `from_id`, `from_type`, `to_id`, `to_type`, `relationship`, `properties` (JSON), `status` (enum: active|removed), `created_at`.

#### Scenario: Relationships table structure
- **WHEN** a coding agent implements the relational adapter's relationship storage
- **THEN** it finds the complete table schema for `entity_relationships` in the relational storage section

### Requirement: mosaic_meta table is defined in the relational storage section
The relational storage section SHALL define the `mosaic_meta` key-value table and its standard keys (`schema_version`, `schema_hash`, `deprecated_fields`, `migration_history`).

#### Scenario: mosaic_meta table structure
- **WHEN** a coding agent implements schema versioning for the relational adapter
- **THEN** it finds the `mosaic_meta` table schema and standard key definitions in the relational storage section

### Requirement: Migration DDL mechanics are defined in the relational storage section
The relational storage section SHALL specify the DDL operations for each migration rule type (new entity type → CREATE TABLE, new field → ALTER TABLE ADD COLUMN, etc.). The conceptual migration rules (what changes are allowed/rejected) SHALL remain in sec3.

#### Scenario: Migration DDL for new entity type
- **WHEN** a coding agent implements `mosaic migrate` for the relational adapter
- **THEN** it finds DDL-level instructions (CREATE TABLE with columns, CREATE INDEX) in the relational storage section, cross-referencing the conceptual rules in sec3

### Requirement: Computed field derivation is documented
The relational storage section SHALL specify how `created_at`, `updated_at`, and `schema_version` are derived from the provenance log at read time in a relational context (e.g., JOIN or subquery patterns).

#### Scenario: Computed field implementation guidance
- **WHEN** a coding agent implements entity reads for the relational adapter
- **THEN** it finds guidance on how to derive temporal and version fields from the provenance log
