## Reference: `mosaic_ext` Annotation Vocabulary

**Document status:** Draft v0.1
**Depends on:** sec9_linkml_redesign.md §9.4, `src/mosaic/schemas/mosaic_ext.yaml`

This document is the authoritative per-annotation reference for `mosaic_ext.yaml` —
Hippo's formal declaration of every `mosaic_*` annotation recognized by the schema
pipeline. Every `mosaic_*` annotation used in any Hippo-managed schema MUST be
declared here; undeclared annotations, value-type mismatches, and wrong-target
attachments fail at schema load. See sec9 §9.4 for the design rationale.

The live schema lives at `src/mosaic/schemas/mosaic_ext.yaml` and ships with the
Hippo Python package. `SchemaRegistry` loads it at startup and validates every
`mosaic_*` annotation in the merged user schema against the declarations below.

---

### Modeling note

LinkML has no native metamodel for declaring a vocabulary of allowed annotation
tags. `mosaic_ext` repurposes LinkML's `slot` declaration as the container for
each annotation's metadata. Each `mosaic_*` annotation is declared as a slot with:

- `range` — the annotation's value type (boolean, string, enum, etc.)
- `description` — human-readable semantics
- `ifabsent` — the value assumed when the annotation is omitted on an element
- `in_subset` — encodes the `applies_to` target: `slot_annotation` means the
  annotation may attach only to a LinkML `slot`; `class_annotation` means only
  to a `class`.

User schemas do not import these slots as data slots. `SchemaRegistry`
introspects `mosaic_ext` to obtain the vocabulary; user schemas use the
annotations via the standard `annotations:` block on their own classes / slots.

---

### Vocabulary

`mosaic_unique`, `mosaic_index`, `mosaic_index_partial`, and `mosaic_search`
landed with Wave 1 (`hippo-ext-vocabulary`); `mosaic_append_only` landed
with Wave 2 (`provenance-as-linkml-class`) as a declaration — adapter
enforcement follows in `provenance-migration`. `mosaic_accessor` joins the
vocabulary with Wave 3's `typed-client` — see *Deferred annotations*
below. `mosaic_external_xref` landed with issue #48 (ExternalReference
value type).

#### `mosaic_unique`

| Attribute | Value |
|---|---|
| Applies to | slot |
| Value type | boolean |
| Default | `false` |
| Consumer | DDL generators (SQLite, PostgreSQL) |

Emit a single-column `UNIQUE` constraint for the annotated slot at the storage
layer. For composite uniqueness spanning multiple slots, use LinkML's native
`unique_keys` on the class — not this annotation.

**Example.**
```yaml
classes:
  Sample:
    attributes:
      barcode:
        range: string
        annotations:
          mosaic_unique: true
```

#### `mosaic_index`

| Attribute | Value |
|---|---|
| Applies to | slot |
| Value type | boolean |
| Default | `false` |
| Consumer | DDL generators |

Emit a single-column index for the annotated slot. Interacts with
`mosaic_index_partial`.

**Example.**
```yaml
classes:
  Sample:
    attributes:
      collected_at:
        range: datetime
        annotations:
          mosaic_index: true
```

#### `mosaic_index_partial`

| Attribute | Value |
|---|---|
| Applies to | slot |
| Value type | boolean |
| Default | `false` |
| Consumer | DDL generators |
| Requires | `mosaic_index: true` on the same slot |

When `mosaic_index` is true on the same slot, emit the index as partial with
the predicate `WHERE is_available = 1`. Has no effect when `mosaic_index` is
false (but SchemaRegistry does not currently warn about this combination).

**Example.**
```yaml
classes:
  Aliquot:
    attributes:
      external_id:
        range: string
        annotations:
          mosaic_index: true
          mosaic_index_partial: true
```

#### `mosaic_search`

| Attribute | Value |
|---|---|
| Applies to | slot |
| Value type | string (free-form; adapter validates) |
| Default | *(none)* |
| Consumer | FTS DDL planner in the SQLite adapter |

Include the annotated slot in a full-text search index of the declared mode.
The mode value is a free-form string — the adapter is the authority on which
modes it supports, and MUST fail at startup with a clear error when it cannot
serve the requested mode. Schema-level validation does not constrain the
value; per sec9 §9.10, schemas declare intent and adapters enforce
capability. Current canonical mode: `fts5` (SQLite FTS5).

**Example.**
```yaml
classes:
  Sample:
    attributes:
      description:
        range: string
        annotations:
          mosaic_search: fts5
```

#### `mosaic_append_only`

| Attribute | Value |
|---|---|
| Applies to | class |
| Value type | boolean |
| Default | `false` |
| Consumer | Storage adapter (SQL triggers for relational adapters) |

Classes annotated `mosaic_append_only: true` are append-only: the storage
adapter MUST reject `UPDATE` and `DELETE` against rows of the class. Only
`INSERT` is permitted. Applied in `mosaic_core` to `ProvenanceRecord`
(see `reference_mosaic_core.md`).

**Enforcement.** SQLite and Postgres adapters emit `BEFORE UPDATE` and
`BEFORE DELETE` triggers on the backing table that raise at the SQL
level (`RAISE ABORT` on SQLite; `RAISE EXCEPTION` on Postgres). This
survives direct-SQL bypass of the Python SDK — Python-level adapter
checks would not. See sec9 Decision 9.6.C for the rationale.
`SchemaRegistry.append_only_classes()` returns the set of annotated
class names for adapters that can't use triggers (Neo4j, future
DDL-generator-driven emission paths, etc.).

**Example.**
```yaml
classes:
  ProvenanceRecord:
    is_a: Entity
    annotations:
      mosaic_append_only: true
```

#### `mosaic_external_xref`

| Attribute | Value |
|---|---|
| Applies to | slot |
| Value type | boolean |
| Default | `false` |
| Consumer | DDL generators + storage adapter write path; `MosaicClient.find_by_xref` / `list_xrefs`; REST `GET /xref/{system}/{value}`; GraphQL `findByXref` |
| Requires | `range: ExternalReference` on the same slot (DDL generation fails at startup otherwise) |

Marks an `ExternalReference`-ranged slot as a **reverse-lookup key**
(issue #48). The slot's `(system, value)` pairs are maintained in the
`mosaic_xref_index` side table:

```
mosaic_xref_index(entity_id, entity_type, slot, system, value)
UNIQUE (system, value)
```

- **Maintenance.** The SQLite adapter's per-class write helpers re-derive
  the entity's index rows (delete-then-insert) on every create, update,
  replace, availability transition, soft delete, and supersession — on the
  same cursor, so index maintenance shares the entity write's transaction.
  Works identically for single- and multivalued slots (the slot stores
  inline JSON either way).
- **Availability semantics.** Index rows exist ONLY for available
  (`is_available = 1`) entities — the same "unique among live records"
  semantics as `mosaic_unique`'s partial index (PTS-348), realised by row
  lifecycle instead of a `WHERE` predicate. Turning an entity unavailable
  frees its pairs; turning it back available re-claims them (and fails
  with a clear conflict if another live entity took a pair in between).
- **Uniqueness.** `(system, value)` is globally unique among available
  entities — across classes and slots. Violations raise
  `XrefUniquenessError` (a `ValidationError`) naming the system, value,
  and conflicting entity; the triggering write rolls back. Transports map
  it to the standard validation path (REST 422, GraphQL
  `VALIDATION_FAILED`).
- **Without the annotation** a slot can still range `ExternalReference` —
  it then carries the structured value but is not indexed, not
  uniqueness-constrained, and not resolvable via the lookup APIs.
- **Adapter support.** SQLite: full. PostgreSQL: the lookup surface raises
  `NotImplementedError` (parity is scoped to that adapter's
  per-class-table migration).

**Example.**
```yaml
classes:
  Sample:
    is_a: Entity
    attributes:
      starlims_ref:
        range: ExternalReference
        inlined: true
        annotations:
          mosaic_external_xref: true
      donor_refs:                 # multivalued works the same way
        range: ExternalReference
        multivalued: true
        inlined: true
        inlined_as_list: true
        annotations:
          mosaic_external_xref: true
```

---

### Deferred annotations

The following annotation is part of the target vocabulary in sec9 but is
introduced by a later OpenSpec change — declared alongside its consumer,
per Decision 9.4.B in `sec9_decisions.md`.

| Annotation | Applies to | Introduced by | Wave |
|---|---|---|---|
| `mosaic_accessor` | class | `typed-client` | 3 |

Not currently declared in `mosaic_ext.yaml`. Using it in a user schema
before `typed-client` lands will fail at schema load with an
undeclared-annotation error.

---

### Version discipline

`mosaic_ext` declares a `version:` attribute (currently `0.2.0`). Bump rules
(per sec9 §9.3):

| Change | Bump |
|---|---|
| Add an annotation | Minor |
| Add a permissible value to `mosaic_search_mode` or another enum | Minor |
| Refine or clarify description text | Patch |
| Rename or remove an annotation | Major — requires an OpenSpec proposal scoping the migration |
| Change an annotation's value type, `applies_to`, cardinality, or default | Major |

`mosaic_core` declares which `mosaic_ext` major version it targets. A running
Hippo instance pins a specific `(mosaic_ext, mosaic_core)` pair; user schemas
are validated against that pair before any data operation.

---

### Adding a new annotation

Per sec9 §9.4 *Extensibility*, adding a new annotation is a four-step change,
scoped in an OpenSpec proposal:

1. Declare the annotation in `mosaic_ext.yaml` — add a slot with `range`,
   `description`, `ifabsent`, and `in_subset`.
2. Implement the consumer in the subsystem that reads the annotation
   (DDL generator, validator, typed-client generator, etc.).
3. Add the annotation to this reference document with its usage example.
4. Bump `mosaic_ext`'s minor version.

Removing or renaming an annotation bumps the major version and requires the
OpenSpec proposal to scope the migration path for user schemas using the
affected annotation.

---

### What `mosaic_ext` does NOT declare

Some capabilities intentionally have no `mosaic_*` annotation because LinkML
covers them natively. An agent implementing a user schema should reach for
the LinkML-native attribute first and only use a `mosaic_*` annotation when no
LinkML equivalent exists.

| Concept | LinkML-native mechanism |
|---|---|
| Default value | `ifabsent: <literal>` or `ifabsent: uuid()`, `datetime(now)`, `int(0)`, etc. |
| Composite uniqueness | `unique_keys` on the class |
| Required slot | `required: true` |
| Pattern constraint | `pattern: "^..."` |
| Range constraint | `minimum_value: 0`, `maximum_value: 100` |
| Multivalued slot | `multivalued: true` |
| Enum membership | `range: <enum>` with `permissible_values` |
