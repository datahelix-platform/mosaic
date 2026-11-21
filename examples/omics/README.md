# Omics Reference Application

This directory contains the schema for Mosaic's first production deployment at the **VA National PTSD Brain Bank**. It is one application of the Mosaic engine — the same engine, loaded with a different schema, would run a completely different application.

## What's here

| File | Purpose |
|------|---------|
| `schema.yaml` | The LinkML schema defining entity types, fields, and relationships for an omics metadata deployment |
| `mosaic.yaml` | Minimal Mosaic configuration pointing at this schema with SQLite storage |

## Running this example

Initialize a Mosaic project from this directory:

```bash
mosaic init --schema schema.yaml
mosaic serve
```

Or point an existing Mosaic install at this config:

```bash
mosaic --config mosaic.yaml serve
```

## Schema overview

The schema models six entity types connected by provenance-aware relationships:

- **Subject** — a biological donor
- **Sample** — biological material derived from a Subject (supports derivation chains)
- **BrainSample** — a polymorphic extension of Sample with brain-bank-specific fields
- **Datafile** — a file at a known URI containing assay data
- **Dataset** — a named, versioned collection of Datafiles
- **Workflow** / **WorkflowRun** — pipeline definitions and individual executions

## Further reading

For the full design narrative — entity relationship diagram, field tables, and the reasoning behind this schema — see [design/appendix_a_example_schema_omics.md](../../design/appendix_a_example_schema_omics.md).
