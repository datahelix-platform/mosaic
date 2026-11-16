# Mosaic Schema Writer's Guide

A practical guide for writing LinkML schema files for your lab. This document covers the most common schema patterns with real examples. For the full field reference, see [Configuration Reference](configuration.md).

---

## Quick Start

A minimal schema with two linked entity types:

```yaml
id: https://example.org/my-lab
name: my_lab
prefixes:
  linkml: https://w3id.org/linkml/
  my_lab: https://example.org/my-lab/
imports:
  - linkml:types
default_range: string

classes:
  Donor:
    attributes:
      external_id:
        range: string
        required: true
      diagnosis:
        range: string
        required: true
      sex:
        range: string
      age_at_death:
        range: integer

  Sample:
    attributes:
      external_id:
        range: string
        required: true
      tissue:
        range: string
        required: true
      donor:
        range: Donor          # links this attribute to the Donor class
        required: true
```

Save this as `schema.yaml`, then point your `MosaicConfig` at it:

```python
from mosaic.config import MosaicConfig
config = MosaicConfig(schema_path="schema.yaml", db_path="my_lab.db")
```

---

## Schema Header

Every LinkML schema file needs a header with metadata and imports. This tells Mosaic (and any LinkML tooling) how to interpret the schema.

```yaml
id: https://example.org/brain-study     # unique schema identifier (URI)
name: brain_study                         # short name (alphanumeric, underscores, dashes)
prefixes:
  linkml: https://w3id.org/linkml/
  brain_study: https://example.org/brain-study/
imports:
  - linkml:types                          # imports built-in types (string, integer, date, etc.)
default_range: string                     # default type for attributes without an explicit range
```

**Required fields:**

- `id` — A unique URI identifying this schema
- `name` — A short name for the schema
- `prefixes` — Must include `linkml` to use standard imports
- `imports` — Almost always includes `linkml:types` for built-in data types

---

## Classes

Each class defines an entity type in Mosaic. Classes are defined under the top-level `classes:` key as a dictionary keyed by class name.

```yaml
classes:
  SequencingDataset:
    description: "An RNA-seq or ATAC-seq dataset"
    attributes:
      ...
```

**Rules:**
- Class names must be unique across your schema
- `description` is optional but recommended
- Use `PascalCase` for class names (e.g., `SequencingDataset`, `GenomeBuild`)

---

## Attributes

### Basic Attributes

Attributes are declared under a class's `attributes:` key. Each attribute specifies a `range` (data type).

```yaml
attributes:
  sample_id:
    range: string
    required: true           # raises ValidationFailure if missing on create

  quality_score:
    range: float
    required: false          # optional, defaults to null

  read_count:
    range: integer

  is_paired:
    range: boolean

  collected_at:
    range: date              # YYYY-MM-DD

  processed_at:
    range: datetime          # ISO 8601: 2026-03-01T14:30:00Z

  file_uri:
    range: uri               # URI/URL string
```

### Built-in Range Types

These types are available when you import `linkml:types`:

| Range | Description |
|-------|-------------|
| `string` | Text data |
| `integer` | Integer numbers |
| `float` | Floating-point numbers |
| `boolean` | True/false values |
| `date` | Date (YYYY-MM-DD) |
| `datetime` | Date and time (ISO 8601) |
| `uri` | URI/URL string |
| `uriorcurie` | URI or compact URI (CURIE) |

### Enum Attributes

Restrict an attribute to a fixed set of values by defining an enum and referencing it:

```yaml
enums:
  AssayType:
    permissible_values:
      RNASeq:
      ATACSeq:
      ChIPSeq:
      WGS:

classes:
  SequencingDataset:
    attributes:
      assay:
        range: AssayType
        required: true
```

Validation raises `ValidationFailure` if an entity is created with a value not in the enum's `permissible_values`.

You can add descriptions to enum values:

```yaml
enums:
  TissueType:
    permissible_values:
      DLPFC:
        description: "Dorsolateral prefrontal cortex"
      HC:
        description: "Hippocampus"
      SN:
        description: "Substantia nigra"
      CB:
        description: "Cerebellum"
      STR:
        description: "Striatum"
```

### Default Values

Use `ifabsent` to specify default values:

```yaml
  status:
    range: string
    ifabsent: "string(pending)"

  priority:
    range: integer
    ifabsent: "int(0)"

  is_active:
    range: boolean
    ifabsent: "true"
```

### Multivalued Attributes

Use `multivalued: true` for list-valued attributes:

```yaml
  tags:
    range: string
    multivalued: true

  diagnoses:
    range: string
    multivalued: true
```

---

## Linking Classes (References)

Use `range` to declare that an attribute points to another class. This is the foundation for entity graph traversal in Cappella and explicit relationship queries.

```yaml
  donor:
    range: Donor              # the class this attribute points to
    required: true
```

When the `range` is a class (not a built-in type like `string`), Mosaic treats the attribute as an entity reference. This enables:
- Schema introspection (`MosaicClient.schema_references()`)
- Cappella collection resolver entity graph traversal
- Documentation and tooling

!!! note
    Reference attributes hold Mosaic internal IDs (UUIDs), not user-facing identifiers. User-facing identifiers belong in a plain `string` attribute (such as `external_id`) or as an ExternalID.

### Self-Referential Links

```yaml
  parent:
    range: Sample             # links back to the same class
```

### Multi-Level Chains

Build graph traversal paths by chaining references:

```yaml
classes:
  Donor:
    attributes:
      ...

  Sample:
    attributes:
      donor:
        range: Donor                       # Sample -> Donor

  SequencingDataset:
    attributes:
      sample:
        range: Sample                      # Dataset -> Sample -> Donor
```

With this schema, Cappella can traverse `Dataset.sample -> Sample.donor -> Donor` automatically when you pass criteria like `donor.diagnosis=CTE`.

### Reverse References (`inverse`)

A single-valued reference is stored once, on the side that holds it: `Sample.donor` is a
column on the `Sample` table. To traverse it the other way — "the samples of this donor" —
declare the reverse side with LinkML's own `inverse` keyword instead of adding a second,
independently stored slot:

```yaml
classes:
  Donor:
    attributes:
      samples:
        range: Sample
        multivalued: true
        inverse: donor          # Sample.donor is the stored FK; this side is derived

  Sample:
    attributes:
      donor:
        range: Donor
```

`Donor.samples` is a **virtual** reverse edge: it owns no column, no link table and no
relationship rows. Every read, filter and count through it resolves against `Sample.donor`,
so there is exactly one stored encoding of the relationship and the two directions can never
drift apart. Concretely:

- **Reads** hydrate `donor["data"]["samples"]` with the ids of the *available* samples whose
  `donor` points at the donor, on `get` and `query` alike.
- **Filters** accept it as a to-many relationship predicate — `{"edge": "samples",
  "quantifier": "some" | "none", "where": {...}}` in the SDK/`where:` tree, a
  `RelatedCondition` in a `QuerySpec`, `donors(where: {samples: {some: {...}}})` in GraphQL.
- **Counts**: `client.count_relationship("Donor", donor_id, "samples")` and GraphQL's
  `samplesCount`.
- **Writes ignore it.** A payload that carries `samples` (for example a `get`-then-`put`
  round-trip) is accepted and the key dropped; it never reaches storage or the provenance
  log. GraphQL Create/Update inputs omit it and OpenAPI marks it `readOnly`. Change the
  relationship by writing the forward side (`Sample.donor`).
- Introspection lists it as a relationship (`MosaicClient.schema_references()`), and the
  typed surfaces — the MCP `mosaic://schema` / `mosaic://capabilities` resources and GraphQL's
  `hippoSchema` query — report it with `inverse_of: "donor"` so tools can tell a derived
  reverse edge from a stored reference.

Rules, checked when the schema loads (a violation is a `SchemaError`):

- `inverse` names a slot that exists on the range class and points back at the declaring
  class (or one of its ancestors);
- that forward slot is **single-valued** — the reverse of a multivalued reference (which is
  stored as relationship rows) is not supported;
- the declaring slot is `multivalued: true` and is not `required`, an `identifier`, or
  `inlined`.

!!! note
    Do not model the reverse side as a plain `multivalued: true` reference without `inverse`.
    That is a second, independently writable relationship (stored as relationship rows), not
    a view of the first — the two can silently disagree. Only an explicit `inverse` tells
    Mosaic "one fact, two directions". See ADR-0011.

#### Declaring the reverse on the forward slot (import-layered schemas)

The declaration above lives on the derived side, so `Donor` has to name `Sample`. In a
schema split across imported modules that can point a lower module at a higher one — if
`Donor` lives in `person.yaml` and `Sample` in `tissue.yaml` (which imports `person`),
`person.yaml` cannot range a slot on `Sample` without breaking its import DAG (and
`linkml-lint` rejects it). Put `inverse` on the **forward** slot instead; Mosaic synthesizes
the reverse onto the range class (ADR-0012):

```yaml
# tissue.yaml — imports person; person.yaml never mentions Sample
classes:
  Sample:
    attributes:
      donor:
        range: Donor
        inverse: samples        # Mosaic synthesizes Donor.samples (multivalued, inverse: donor)
```

The synthesized `Donor.samples` behaves exactly like a declared one — hydrated on reads,
filterable with `some`/`none`, counted, write-ignored, listed in the MCP and GraphQL schema
surfaces with `inverse_of: "donor"`. A forward slot declared on an abstract class
(`Sample.donor` with `Brain`, `CSF`, … subclasses) synthesizes one reverse ranged on the
abstract class, resolved across every concrete descendant's table. Setting `inverse` through
`slot_usage` on a subclass (`Diagnosis` narrowing an inherited `person` with `inverse:
diagnoses`) ranges that reverse on the subclass.

Rules, in addition to the ones above (a violation is a `SchemaError`):

- if **both** sides declare `inverse`, they must name each other;
- the name must not already exist as a slot on the range class — Mosaic never silently reuses
  a real slot;
- a forward slot shared by unrelated classes with the same `inverse` cannot synthesize one
  reverse for both: declare it once, on a slot of a shared ancestor.

!!! note
    A synthesized slot exists only in Mosaic's view of the schema. Generic LinkML tooling
    (`gen-pydantic`, `gen-doc`, `linkml-lint`) does not see it. `SchemaRegistry.
    inverse_reference_slots()` reports where the declaration was written as
    `InverseSlot.declared_on` (`"forward"` for a synthesized reverse, `"reverse"` for a
    declared one); storage, queries and transports treat both identically.

---

## Mosaic Extensions (Annotations)

Mosaic extends standard LinkML with storage and indexing annotations. These are expressed using LinkML's `annotations` mechanism and are specific to Mosaic's storage layer.

### Full-Text Search

Mark an attribute for full-text search:

```yaml
  notes:
    range: string
    annotations:
      hippo_search: fts5

  description:
    range: string
    annotations:
      hippo_search: fts5
```

Query via `MosaicClient.search()` or the REST API:
```python
results = client.search("Sample", "hippocampus cortex")
```

### Database Indexes

Speed up exact lookups on frequently-queried attributes:

```yaml
  diagnosis:
    range: string
    annotations:
      hippo_index: true              # adds a B-tree index

  batch_id:
    range: string
    annotations:
      hippo_index_partial: true      # index only non-null values (smaller index)
```

### Unique Constraints

```yaml
  barcode:
    range: string
    identifier: true                 # makes this the unique primary key for the class
```

For non-primary-key uniqueness, use a class-level `unique_keys` declaration:

```yaml
classes:
  Sample:
    unique_keys:
      barcode_key:
        unique_key_slots:
          - barcode
    attributes:
      barcode:
        range: string
```

---

## Schema Inheritance

Use `is_a` to inherit all attributes from a parent class. The child class is queryable both as itself and as the parent type.

```yaml
classes:
  File:
    attributes:
      file_uri:
        range: uri
        required: true
      checksum_sha256:
        range: string

  AlignmentFile:
    is_a: File                    # inherits file_uri and checksum_sha256
    attributes:
      aligner:
        range: string
      genome_build:
        range: GenomeBuild
```

`client.query("File")` returns both `File` and `AlignmentFile` entities. `client.query("AlignmentFile")` returns only alignment files.

### Mixins

Use `mixins` to compose shared attribute sets without single-inheritance constraints:

```yaml
classes:
  Timestamped:
    mixin: true
    attributes:
      collected_at:
        range: datetime
      processed_at:
        range: datetime

  QCAnnotation:
    mixin: true
    attributes:
      qc_status:
        range: string
      qc_score:
        range: float

  AnnotatedAlignmentFile:
    is_a: AlignmentFile
    mixins:
      - Timestamped
      - QCAnnotation            # inherits from AlignmentFile, plus both mixins
```

---

## Validators

CEL-based validators enforce data quality at write time. Define them in `validators.yaml` (separate from the schema).

```yaml
# validators.yaml
validators:
  - name: sample_name_format
    entity_type: Sample
    operations: [create, update]
    condition: 'entity.external_id.matches("^S[0-9]{3,}$")'
    message: "external_id must match S followed by 3+ digits"

  - name: age_plausible
    entity_type: Donor
    operations: [create]
    condition: 'entity.age_at_death >= 18 && entity.age_at_death <= 120'
    message: "age_at_death must be between 18 and 120"
```

### Referential Integrity Validator

Since Mosaic doesn't enforce foreign keys at the storage level, use a validator if you need hard enforcement:

```yaml
validators:
  - name: donor_must_exist
    entity_type: Sample
    operations: [create]
    expand: donor        # pre-fetches the referenced entity
    condition: 'has(entity.donor) && entity.donor != ""'
    message: "Sample must have a non-empty donor reference"
```

Full cross-entity validation (checking the referenced entity actually exists) requires using the `ref_check` built-in validator preset:

```yaml
  - name: valid_donor_ref
    entity_type: Sample
    preset: ref_check
    field: donor
    ref_entity_type: Donor
```

---

## Namespaces

Group entity types from different domains into named namespaces to avoid collisions. In LinkML, this maps to the `default_prefix` for a schema file:

```yaml
id: https://example.org/clinical
name: clinical
prefixes:
  linkml: https://w3id.org/linkml/
  clinical: https://example.org/clinical/
imports:
  - linkml:types
default_prefix: clinical

classes:
  Assessment:                      # fully qualified: clinical.Assessment
    attributes:
      ...
```

Reference across namespaces using fully-qualified names in attribute ranges:

```yaml
  sample:
    range: tissue.Sample           # namespace.ClassName
```

Root namespace classes (no namespace declared) can be referenced without qualification.

---

## Complete Example: Neuroscience Study

```yaml
# schema.yaml -- example for a brain tissue bank

id: https://example.org/brain-tissue-bank
name: brain_tissue_bank
prefixes:
  linkml: https://w3id.org/linkml/
  btb: https://example.org/brain-tissue-bank/
imports:
  - linkml:types
default_range: string

enums:
  SexType:
    permissible_values:
      M:
      F:
      Unknown:

  TissueRegion:
    permissible_values:
      DLPFC:
        description: "Dorsolateral prefrontal cortex"
      HC:
        description: "Hippocampus"
      SN:
        description: "Substantia nigra"
      CB:
        description: "Cerebellum"
      STR:
        description: "Striatum"

  AssayType:
    permissible_values:
      RNASeq:
      ATACSeq:
      WGS:
      WES:

classes:
  Donor:
    description: "A research subject (human)"
    attributes:
      external_id:
        range: string
        required: true
        identifier: true
        annotations:
          hippo_index: true
      sex:
        range: SexType
        required: true
      age_at_death:
        range: integer
      diagnosis:
        range: string
        required: true
        annotations:
          hippo_index: true
          hippo_search: fts5
      notes:
        range: string
        annotations:
          hippo_search: fts5

  Sample:
    description: "A tissue sample from a donor"
    attributes:
      external_id:
        range: string
        required: true
        identifier: true
      donor:
        range: Donor
        required: true
        annotations:
          hippo_index: true
      tissue:
        range: TissueRegion
        required: true
      brain_region:
        range: string

  SequencingDataset:
    description: "A sequencing run for a sample"
    attributes:
      external_id:
        range: string
        required: true
      sample:
        range: Sample
        required: true
        annotations:
          hippo_index: true
      assay:
        range: AssayType
        required: true
      platform:
        range: string
      read_count:
        range: integer

  GenomeBuild:
    description: "A reference genome assembly"
    attributes:
      name:
        range: string
        required: true
        identifier: true
      source:
        range: string
      release:
        range: string
      source_uri:
        range: uri
      local_uri:
        range: uri

  AlignmentFile:
    is_a: File                     # if you have a base File class
    description: "An aligned sequencing file"
    attributes:
      dataset:
        range: SequencingDataset
        required: true
      genome_build:
        range: GenomeBuild
        required: true
      aligner:
        range: string
      aligner_version:
        range: string
```

---

## Common Mistakes

**Using `type` instead of `range` for attribute data types**
LinkML uses `range` to specify data types:
```yaml
  diagnosis:
    range: string       # correct
```
Not:
```yaml
  diagnosis:
    type: string        # wrong -- not valid LinkML
```

**Forgetting `imports: - linkml:types`**
Without this import, built-in types like `string`, `integer`, `date` are not available. Almost every schema needs this import.

**Defining enums inline on an attribute**
LinkML enums must be defined as separate top-level entries under `enums:`, then referenced by name in `range`:
```yaml
enums:
  AssayType:
    permissible_values:
      RNASeq:
      ATACSeq:

classes:
  Dataset:
    attributes:
      assay:
        range: AssayType   # correct -- references the enum by name
```
Not:
```yaml
      assay:
        enum_values:       # wrong -- not valid LinkML
          - RNASeq
          - ATACSeq
```

**Using `fields` instead of `attributes`**
LinkML uses `attributes` for inline class-specific definitions:
```yaml
classes:
  Donor:
    attributes:            # correct
      name:
        range: string
```
Not:
```yaml
classes:
  Donor:
    fields:                # wrong -- not valid LinkML
      name:
        type: string
```

**Forgetting `identifier: true` on the primary key attribute**
Just naming an attribute `id` or `external_id` does not make it an identifier. You must explicitly set `identifier: true`.

**Not adding `hippo_index` annotation on reference attributes**
Queries filtering on reference attributes (like `donor`) will be slow without an index. Add `hippo_index: true` annotation to any attribute you filter on frequently.
