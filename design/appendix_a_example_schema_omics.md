## Appendix A: Example Schema — Omics Research Deployment

**Document status:** Draft v0.1
**Depends on:** sec3_data_model.md

---

Below is the schema used by Hippo's first production deployment at the VA National PTSD Brain Bank. It demonstrates how a real biomedical metadata model is expressed in LinkML and run by Hippo. The same engine, given a different schema, would run a different application.

> **This is an example deployment configuration, not part of the Hippo system spec.**
> The entity types, fields, and relationships below illustrate how a multi-modal omics
> research group might configure Hippo for tracking biological samples, sequencing data
> files, and analysis workflows. A deployment targeting a different domain (e.g.,
> manufacturing QC, environmental monitoring, clinical trials) would author an entirely
> different schema using the same LinkML features documented in sec3. Hippo's core
> behavior is identical regardless of schema content.

---

### A.1 Entity Types

The following six entity types form a complete omics metadata tracking schema. Fields
listed are **user-defined fields only** — system fields (sec3 §3.2) and availability
(sec3 §3.3) are present on all entities implicitly and are not repeated here.

#### Subject

A biological subject who contributed one or more samples.

| Field | Type | Required | Indexed | Notes |
|---|---|---|---|---|
| `external_id` | string | yes | yes | ID from upstream subject registry |
| `species` | enum | yes | no | Values: Homo sapiens, Mus musculus, Rattus norvegicus |
| `biological_sex` | enum | no | no | Values: male, female, unknown |
| `age_at_collection` | float | no | no | Age in years at time of sample collection |
| `diagnosis` | string | no | yes | Primary clinical diagnosis |

#### Sample

A piece of biological material derived from a Subject. May be derived from another Sample
(e.g., dissection, aliquoting).

| Field | Type | Required | Indexed | Notes |
|---|---|---|---|---|
| `external_id` | string | yes | yes | ID from upstream sample registry |
| `tissue_type` | string | yes | yes | Tissue of origin |
| `tissue_region` | string | no | yes | Sub-region within tissue (e.g., hippocampus) |
| `collection_date` | date | no | no | |
| `passage` | int | no | no | For cell lines |

#### Datafile

A file at a known location containing data derived from one or more Samples.

| Field | Type | Required | Indexed | Notes |
|---|---|---|---|---|
| `uri` | uri | yes | yes | S3 URI, local path, or HTTPS URL |
| `file_type` | enum | yes | yes | Values: fastq, bam, vcf, tsv, h5ad, idat, ... |
| `modality` | enum | yes | yes | Values: RNASeq, WGBS, WGS, ATAC, genotyping, ... |
| `file_size_bytes` | int | no | no | |
| `checksum_md5` | string | no | no | Integrity verification |
| `read_count` | int | no | no | For sequencing files |
| `genome_build` | string | no | yes | e.g., GRCh38 |
| `is_primary` | bool | no | yes | True if raw/unprocessed |

#### Dataset

A named, versioned logical collection of Datafiles.

| Field | Type | Required | Indexed | Notes |
|---|---|---|---|---|
| `name` | string | yes | yes | Human-readable name |
| `version` | string | yes | no | Semver or date string |
| `description` | string | no | no | |
| `is_public` | bool | no | yes | Placeholder for future auth layer |

#### Workflow

A versioned analysis pipeline definition from which WorkflowRuns are instantiated.

| Field | Type | Required | Indexed | Notes |
|---|---|---|---|---|
| `name` | string | yes | yes | Pipeline name |
| `version` | string | yes | yes | Semver or git SHA |
| `description` | string | no | no | |
| `repository_uri` | uri | no | no | Git repository of pipeline definition |
| `language` | enum | no | no | Values: nextflow, snakemake, wdl, cwl, ... |

#### WorkflowRun

A single execution of a Workflow, consuming input Datafiles and producing output Datafiles.
The `instance_of` relationship links a WorkflowRun to its Workflow definition. Execution
state is tracked via the `execution_state` field.

| Field | Type | Required | Indexed | Notes |
|---|---|---|---|---|
| `execution_state` | enum | yes | yes | Values: pending, running, succeeded, failed |
| `executor` | string | no | no | Nextflow run ID, AWS Batch job ID, etc. |
| `started_at` | datetime | no | no | |
| `completed_at` | datetime | no | no | |
| `parameters` | json | yes | no | Full parameter set used for this run |

---

### A.2 Relationship Declarations

```yaml
relationships:
  - name: donated
    from: Subject
    to: Sample
    cardinality: one-to-many
    description: "A subject donated one or more samples"

  - name: derived_from
    from: Sample
    to: Sample
    cardinality: many-to-many
    description: "A sample derived from another (e.g. dissection, aliquot)"
    properties:
      method: {type: string}

  - name: generated
    from: Sample
    to: Datafile
    cardinality: one-to-many

  - name: input_to
    from: Datafile
    to: WorkflowRun
    cardinality: many-to-many

  - name: output_of
    from: Datafile
    to: WorkflowRun
    cardinality: many-to-many

  - name: instance_of
    from: WorkflowRun
    to: Workflow
    cardinality: many-to-one
    required: true

  - name: contains
    from: Dataset
    to: Datafile
    cardinality: many-to-many
```

---

### A.3 Entity Relationship Graph

```
                    ┌─────────────┐
                    │   Subject   │
                    └──────┬──────┘
                           │ donated (1:many)
                    ┌──────▼──────┐
               ┌────│   Sample    │◄──────────────┐
               │    └──────┬──────┘  derived_from  │
               │           │         (many:many,   │
               │           │          self-ref)    │
               │           │ generated (1:many)
               │    ┌──────▼──────┐
               │    │  Datafile   │
               │    └──┬──────┬───┘
               │       │      │
               │       │      │ output_of (many:many)
               │       │      │
               │       │  ┌───▼──────────────────────────┐
               │       │  │        WorkflowRun            │
               │       │  │  (parameters, executor)       │
               │       │  └───▲──────────┬────────────────┘
               │       │      │          │ instance_of (many:1)
               │       │      │   ┌──────▼──────┐
               │       │      │   │  Workflow   │
               │       │      │   │  (name,     │
               │       │      │   │   version)  │
               │       │      │   └─────────────┘
               │       │      │
               │       └──────┘
               │       input_to (many:many)
               │
               │    ┌─────────────┐
               └───►│   Dataset   │
                    │  (contains  │
                    │  Datafiles) │
                    └─────────────┘

System relationships (built-in, available on all entity types):
  [any entity] ──superseded_by──► [any entity]
```

---

### A.4 Complete Schema YAML

The full `schema.yaml` for this example deployment. This is what a deployer would author
and commit to their config repository.

```yaml
version: "1.0"
format: linkml

# This example does not reference any external ontology packages.
# A deployment that uses FMA anatomy terms or Ensembl gene IDs would declare them here:
# requires:
#   - mosaic-reference-fma>=3.3
#   - mosaic-reference-ensembl>=GRCh38.109

entities:

  Subject:
    description: "A biological subject who contributed one or more samples."
    fields:
      external_id:
        type: string
        required: true
        indexed: true
      species:
        type: enum
        values: [Homo sapiens, Mus musculus, Rattus norvegicus]
        required: true
      biological_sex:
        type: enum
        values: [male, female, unknown]
      age_at_collection:
        type: float
        description: "Age in years at time of sample collection"
      diagnosis:
        type: string
        indexed: true
        search: fts    # enables full-text search on diagnosis field

  Sample:
    description: "A piece of biological material derived from a Subject."
    fields:
      external_id:
        type: string
        required: true
        indexed: true
      tissue_type:
        type: string
        required: true
        indexed: true
      tissue_region:
        type: string
        indexed: true
        search: fts    # enables full-text search on region descriptions
      collection_date:
        type: date
      passage:
        type: int

  # Example of polymorphic extension (base: = is-a inheritance).
  # BrainSample IS a Sample — it is queryable as Sample and validates against Sample rules.
  # A brain bank deployment would extend the generic omics schema this way.
  BrainSample:
    base: Sample
    description: "A sample from a brain tissue collection."
    fields:
      brain_region:
        type: string
        indexed: true
        search: fts
      hemisphere:
        type: enum
        values: [left, right, bilateral, unknown]
      post_mortem_interval_hours:
        type: float

  Datafile:
    description: "A file at a known location containing data derived from one or more Samples."
    fields:
      uri:
        type: uri
        required: true
        indexed: true
      file_type:
        type: enum
        values: [fastq, bam, vcf, tsv, h5ad, idat, csv, other]
        required: true
        indexed: true
      modality:
        type: enum
        values: [RNASeq, WGBS, WGS, ATAC, genotyping, proteomics, metabolomics, other]
        required: true
        indexed: true
      file_size_bytes:
        type: int
      checksum_md5:
        type: string
      read_count:
        type: int
        validators:
          - type: range
            min: 0
      genome_build:
        type: string
        indexed: true
      is_primary:
        type: bool
        indexed: true

  Dataset:
    description: "A named, versioned logical collection of Datafiles."
    fields:
      name:
        type: string
        required: true
        indexed: true
        search: fts
      version:
        type: string
        required: true
      description:
        type: string
        search: fts
      is_public:
        type: bool
        indexed: true

  Workflow:
    description: "A versioned analysis pipeline definition."
    fields:
      name:
        type: string
        required: true
        indexed: true
        search: fts
      version:
        type: string
        required: true
        indexed: true
      description:
        type: string
        search: fts
      repository_uri:
        type: uri
      language:
        type: enum
        values: [nextflow, snakemake, wdl, cwl, other]

  WorkflowRun:
    description: "A single execution of a Workflow."
    fields:
      execution_state:
        type: enum
        values: [pending, running, succeeded, failed]
        required: true
        indexed: true
      executor:
        type: string
        description: "Nextflow run ID, AWS Batch job ID, etc."
      started_at:
        type: datetime
      completed_at:
        type: datetime
      parameters:
        type: json
        required: true

relationships:
  - name: donated
    from: Subject
    to: Sample
    cardinality: one-to-many
    description: "A subject donated one or more samples"

  - name: derived_from
    from: Sample
    to: Sample
    cardinality: many-to-many
    description: "A sample derived from another (e.g. dissection, aliquot)"
    properties:
      method: {type: string}

  - name: generated
    from: Sample
    to: Datafile
    cardinality: one-to-many

  - name: input_to
    from: Datafile
    to: WorkflowRun
    cardinality: many-to-many

  - name: output_of
    from: Datafile
    to: WorkflowRun
    cardinality: many-to-many

  - name: instance_of
    from: WorkflowRun
    to: Workflow
    cardinality: many-to-one
    required: true

  - name: contains
    from: Dataset
    to: Datafile
    cardinality: many-to-many
```

> **Note:** The `BrainSample` type is included here to illustrate polymorphic extension.
> A deployment that doesn't need brain-specific fields would simply omit it. The `donated`
> and `derived_from` relationships declared `from: Sample` also cover `BrainSample` entities
> because `BrainSample` is a subtype of `Sample`.

---
