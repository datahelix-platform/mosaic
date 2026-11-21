# Mosaic

> **Formerly Hippo.** The component was renamed to **Mosaic** (ADR-0004): the
> distribution is `datahelix-mosaic`, the import package is `mosaic`, and the
> CLI is `mosaic`. Since v0.16.0 no `hippo` spelling is honored any more
> (ADR-0015) — see [Installation](installation.md#upgrading-from-hippo) for
> the upgrade notes.

A runtime for LinkML schemas. Point it at a schema and you get a typed Python SDK, a REST API, a relational database with append-only PROV-O provenance, and CEL-based dynamic validation — without writing any of that infrastructure yourself. **The schema defines the application; Mosaic runs it.**

## Start here

- **[Why Mosaic?](why-mosaic.md)** — the elevator pitch and core idea.
- **[Introduction](introduction.md)** — what Mosaic does and how it fits together.
- **[Installation](installation.md)** — install from PyPI or source.
- **[Quick Start](quickstart.md)** — your first schema, store, and query in 10 minutes.

## Build

- **[Schema Guide](schema-guide.md)** — author entities, attributes, and relationships in LinkML.
- **[Data Model](data-model.md)** — how schemas map to relational storage and the graph-shaped API.
- **[Configuration](configuration.md)** — `mosaic.yaml`, storage backends, and adapters.

## Use

- **[CLI Reference](cli-reference.md)** — `mosaic` command surface.
- **[TUI](tui.md)** — interactive terminal browser (`mosaic tui`) over the SDK or REST API.
- **[API Reference](api-reference.md)** — `MosaicClient` Python SDK.
- **[Typed Client](reference_typed_client.md)** — schema-derived typed client.

## Extend

- **[Reference Loaders](reference-loaders.md)** and **[Writing a Reference Loader](writing-a-reference-loader.md)** — pull static reference data into Mosaic.
- **[Writing a Recipe](writing-a-recipe.md)**, **[Installing Recipes](installing-recipes.md)**, **[Recipe Reference](recipe-reference.md)** — package and share schema fragments.

## Background

- **[Design Principles](design-principles.md)** — what Mosaic is opinionated about, and why.
- **[Comparison](comparison.md)** — how Mosaic relates to LinkML, Datasette, and friends.

---

Mosaic is open source under the MIT license. Source at [github.com/BU-Neuromics/hippo](https://github.com/BU-Neuromics/hippo).
