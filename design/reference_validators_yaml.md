## Reference: `validators.yaml` Format

**Document status:** Draft v0.2 (sec9 §9.9 tiering contract added)
**Depends on:** sec2_architecture.md §2.13, reference_cel_context.md,
sec9 §9.9 (validation-tiering-clarification)

This document is the authoritative format reference for `validators.yaml` — the
config-driven business rule validator file. See sec2 §2.13 for the validation
architecture overview and execution model; see sec9 §9.9 and Decision 9.9.A for
the three-tier contract this file participates in.

---

### The three-tier pipeline (sec9 §9.9)

Hippo runs three tiers of write validation in a fixed order. `validators.yaml`
is the CEL tier. The other two are listed here so coding agents can decide
where a new rule belongs without round-tripping.

| Tier | What it expresses | Where it lives |
|---|---|---|
| `linkml` | Static shape: types, patterns, enums, ranges, `required`, multivalued, `unique_keys`. | User schema YAML. Enforced by `SchemaRegistry.validate_envelope()`. |
| `cel` | Dynamic, pure-function predicates over entity data. Optionally pre-fetches via `expand`. | `validators.yaml` — this file. |
| `python` | Escape hatch for rules neither tier above can express (network calls, stateful checks, cross-cluster consistency). | `mosaic.write_validators` entry-point plugins. |

Execution: cheapest first, `fail_fast=True` by default. Batch ingest can
opt into `collect_all=True` to aggregate failures across tiers.

**Boundary rules.** Pick the cheapest tier that can express the rule.
If LinkML can express it, it MUST be in LinkML. If CEL can express it
(pure function over entity data, optionally with `expand`-pre-fetched
references), it MUST be in CEL. Python plugins are the last resort.

**Result envelope.** Every tier returns a `ValidationResult` (defined in
`mosaic.core.validation.validators`) carrying a list of `ValidationFailure`
objects. Each failure records its producing `tier` so the REST layer and
typed client can render tier-aware errors uniformly.

```python
@dataclass
class ValidationFailure:
    tier: Literal["linkml", "cel", "python"]
    rule: str
    message: str
    field: Optional[str] = None
    details: dict = {}
```

REST surface: `ValidationFailed` maps to HTTP 422 with a structured body
(`passed`, `failures[].tier`, `failures[].rule`, `failures[].field`,
`failures[].message`, `failures[].details`). See `src/mosaic/api/factory.py`.

---

### Top-Level Structure

```yaml
validators:          # required top-level key; value is a list of validator entries
  - name: ...        # validator entries, evaluated in priority order
  - name: ...
```

The file must have exactly one top-level key: `validators`. Unknown top-level keys
produce a `ConfigError` at startup.

---

### Validator Entry Fields

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | ✅ yes | — | Unique identifier; used in error messages and logs. Must be unique across all validators (config + plugin). |
| `entity_types` | list[string] \| null | no | null | Entity types this validator applies to. `null` means all types. Subtype-aware: `[Sample]` covers `BrainSample`, `CellLine`, etc. Startup warning if both parent and subtype are listed. |
| `on` | list[string] | no | `[create, update, delete]` | Operations that trigger this validator. Valid values: `create`, `update`, `delete`. |
| `priority` | int | no | `0` | Execution order among config validators. Lower values run first. Schema validation (Tier 1) always runs at priority `-1` before any config validator. Plugin validators (Tier 3) run after all config validators regardless of their declared priority. |
| `when` | string (CEL) | no | null | Pre-condition expression. If evaluates to `false`, the validator is skipped entirely for this write. See reference_cel_context.md. |
| `expand` | list[ExpandEntry] | no | `[]` | Fields to pre-fetch before CEL evaluation. Required for any validator that needs to traverse relationships. See §Expand Entries. |
| `condition` | string (CEL) | no* | — | The validation condition. Must evaluate to `bool`. `true` = write allowed; `false` = write rejected. Required unless `requires` shorthand is used. |
| `requires` | list[PresetEntry] | no* | — | Built-in preset validators as ergonomic shortcuts. Expands internally to `condition` expressions. See §Built-in Presets. Either `condition` or `requires` (or both) must be present. |
| `error` | string | no | `"Validation failed: {name}"` | Error message returned to the caller on failure. Supports template variables: `{name}` (validator name), `{entity_type}`, `{entity_id}`. |
| `max_expand_list_size` | int | no | global default (200) | Per-validator override for the list expansion cap. Cannot exceed global hard cap of 1000. |

\* Either `condition` or `requires` must be present (or both).

---

### Expand Entries

Each entry in the `expand` list specifies one field path to pre-fetch before CEL
evaluation. Paths support dot notation and list traversal.

```yaml
expand:
  - path: subject                  # simple ref field — fetch the referenced entity
  - path: subject.diagnosis_group  # nested ref — fetch subject, then its diagnosis_group
  - path: samples[]                # list ref — fetch all entities in the list field
  - path: samples[].tissue_region  # list traversal — fetch samples, then each tissue_region ref
```

**Path syntax:**

| Syntax | Meaning |
|---|---|
| `field` | Fetch the entity referenced by `field` (must be type `ref`) |
| `field.child` | Fetch `field`, then fetch `child` from that entity |
| `field[]` | Fetch all entities referenced in `field` (must be type `json` list of refs or a list field) |
| `field[].child` | Fetch all entities in `field[]`, then fetch `child` from each |

**How expanded values appear in CEL context:**

Expanded fields are merged into the `entity` map. After expanding `subject`:
- `entity.subject` is the full subject entity map (all fields), not just the ref string
- `entity.subject.diagnosis` accesses the subject's diagnosis field directly

After expanding `samples[]`:
- `entity.samples` is a list of full entity maps (not ref strings)
- `entity.samples[0].tissue_type` accesses the first sample's tissue_type

See reference_cel_context.md for full CEL context variable specification.

**Batch fetch guarantee:** All `field[]` expansions are fetched in a single query per
list, not N individual lookups. Implementers must batch-fetch list expansions.

**Cycle detection:** The expand engine maintains a visited set keyed by `"type:uuid"`.
If a cycle is detected, expansion stops at that node and the validator receives the
entity without further expansion (no error is raised; a debug log entry is written).

---

### Built-in Presets

Presets are ergonomic shortcuts that expand to `condition` expressions internally.
They are not separate code paths — they produce standard CEL conditions.

#### `ref_check`

Validates that a `ref` field points to an available entity of the expected type.

```yaml
requires:
  - type: ref_check
    field: subject              # required — the ref field to check
    target_type: Subject        # optional — if set, also checks __type__ matches
    allow_unavailable: false    # optional; default: false — reject refs to unavailable entities
```

Equivalent condition (generated internally):
```
entity.subject != null && entity.subject.is_available == true
```

#### `count_constraint`

Validates a count constraint on a list or relationship.

```yaml
requires:
  - type: count_constraint
    field: samples[]            # required — the list field or expand path
    min: 1                      # optional; default: no minimum
    max: 10                     # optional; default: no maximum
```

Equivalent condition: `entity.samples.size() >= 1 && entity.samples.size() <= 10`

#### `immutable_field`

Rejects updates that change a specific field value once set.

```yaml
requires:
  - type: immutable_field
    field: external_id          # required — field that must not change after create
    allow_null_to_value: true   # optional; default: true — allow setting a null field
```

Equivalent condition (for updates):
`existing == null || existing.external_id == null || entity.external_id == existing.external_id`

#### `field_required_if`

Makes a field required when a condition is met.

```yaml
requires:
  - type: field_required_if
    field: post_mortem_interval_hours    # required — field that must be present
    when: "entity.__type__ == 'BrainSample'"  # required — condition under which field is required
```

Equivalent condition: `!(entity.__type__ == 'BrainSample') || entity.post_mortem_interval_hours != null`

#### `no_self_ref`

Rejects an entity that references itself (e.g. a `derived_from` self-reference that
would create a trivial cycle).

```yaml
requires:
  - type: no_self_ref
    field: parent_sample        # required — the ref field to check
```

Equivalent condition: `entity.parent_sample == null || entity.parent_sample.id != entity.id`

---

### Full Example

```yaml
validators:

  # Validate that a Sample's subject is present and available
  - name: sample_subject_available
    entity_types: [Sample]
    on: [create, update]
    expand:
      - path: subject
    condition: "entity.subject != null && entity.subject.is_available == true"
    error: "Sample {entity_id}: referenced subject is unavailable or missing"

  # Prevent changing external_id after it has been set
  - name: sample_external_id_immutable
    entity_types: [Sample]
    on: [update]
    requires:
      - type: immutable_field
        field: external_id
    error: "Sample {entity_id}: external_id is immutable once set"

  # BrainSample must have post_mortem_interval_hours
  - name: brain_sample_pmi_required
    entity_types: [BrainSample]
    on: [create]
    condition: "entity.post_mortem_interval_hours != null"
    error: "BrainSample {entity_id}: post_mortem_interval_hours is required"

  # A WorkflowRun must reference a Workflow that is available
  - name: workflow_run_valid_workflow
    entity_types: [WorkflowRun]
    on: [create]
    expand:
      - path: workflow
    requires:
      - type: ref_check
        field: workflow
        target_type: Workflow
    error: "WorkflowRun {entity_id}: referenced workflow is unavailable or missing"

  # A Dataset must contain at least one Datafile (checked on update; creates can be empty)
  - name: dataset_not_empty
    entity_types: [Dataset]
    on: [update]
    when: "entity.is_public == true"        # only enforce for public datasets
    expand:
      - path: datafiles[]
    requires:
      - type: count_constraint
        field: datafiles[]
        min: 1
    error: "Dataset {entity_id}: public datasets must contain at least one datafile"
```

---

### Execution Semantics

1. All validators for a given entity type and operation are collected (config + plugin)
2. Schema validators (Tier 1) run first at implicit priority -1
3. Config validators run in ascending priority order (lower number = earlier)
4. For config validators with the same priority, order is stable (file order)
5. Plugin validators (Tier 3) run after all config validators, in their declared priority order
6. First failure stops execution and rolls back the transaction
7. `when` pre-condition is evaluated before `expand` — if `when` is false, no expansion occurs

### Unknown Keys

Unknown keys at any level in `validators.yaml` produce a `ConfigError` at startup. This
prevents silently-ignored typos in validator config.

---
