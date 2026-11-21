# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Component Overview

**Mosaic** (formerly **Hippo** — renamed per ADR-0004; distributed as `datahelix-mosaic`, imported as `mosaic`) is the **LinkML runtime** for the BASS platform — the engine that reads a LinkML schema and *becomes* the typed knowledge graph it describes. In a deployment it is the platform's **structured domain graph** (see the platform `../platform/design/domain-graph.md`): every query returns a knowledge subgraph whose semantics are the schema's. Tracking *where data lives* and *what it describes* is **one role** it plays (file cataloging), not its essence — Mosaic is not a "metadata store," it runs whatever schema you give it. Historical documents under `design/` keep the Hippo name (forward-only convention). The data-contract identifiers were renamed by platform ADR-0007 in v0.16.0: `mosaic_core` / `mosaic_ext`, `mosaic_*` annotation keys, the `mosaic_meta` / `mosaic_xref_index` tables, and the `mosaic:` prefix under `https://w3id.org/datahelix/mosaic/`. A `hippo_*` key in a schema is a validation error. Never rename `hippocampus` (a brain region). See the root `../CLAUDE.md` for repo-wide conventions.

## Spec Structure

Design spec sections live in `design/` and are numbered sequentially. `design/INDEX.md` is the source of truth for section status, key decisions, and open questions — always check it before modifying or drafting spec content.

Current section status (check INDEX.md for latest):
- **sec1** (Overview) and **sec2** (Architecture): Complete and approved
- **sec3** (Data Model): In review — actively evolving
- **sec4–sec7**: Not started — stubs only

Each section declares `Depends on` / `Feeds into` headers. Read dependencies before editing a section.

## Architecture (for spec writing)

Mosaic has three concentric layers — only the Core SDK is required:

1. **Core Python SDK** (`mosaic/core/`): All business logic. Key classes: `MosaicClient` (public API), `QueryEngine`, `IngestionPipeline`, `ProvenanceManager`, `SchemaConfig`.
2. **Transport Layer** (optional): REST via FastAPI (`mosaic serve`), GraphQL (future). These are thin wrappers calling the SDK directly.
3. **Infrastructure Layer** (adapters): Storage backends (SQLite v0.1, PostgreSQL future) and external system adapters (STARLIMS, HALO, Donor DB — all future stubs).

Adapters are registered via entry points: `mosaic.storage_adapters`, `mosaic.external_adapters` (the legacy `hippo.*` groups were removed by ADR-0015).

## Data Model Essentials

- **Config-driven relational** storage with a **graph-shaped API** — entity types and relationships defined in LinkML schema, not hardcoded.
- Schemas are authored directly in **LinkML** format (no intermediate DSL or compilation step).
- System fields (`id`, `is_available`) live on entity tables; temporal fields (`created_at`, `updated_at`, `schema_version`) live exclusively in the provenance log and are computed at read time.
- **No hard deletes** — availability transitions (`is_available` boolean) replace deletion.
- Entity status values: `active`, `archived`, `superseded`, `deleted`, `distributed`, `removed`.

## Writing & Editing Guidelines

- User-facing docs live in `docs/`. Design specs in `design/` are internal engineering documents.
- When drafting new spec sections (sec4–sec7), follow the structure of sec1–sec3: numbered subsections, tables for structured data, ASCII diagrams for architecture.
- Keep the **SDK-first** principle consistent: business logic in SDK, transport layers are thin wrappers.
- Update `design/INDEX.md` whenever a section's status changes or a new key decision is made.
- **Design decisions are recorded as ADRs** per the platform-wide convention (root `../CLAUDE.md`; canonical process in `../platform/design/decisions/README.md`). New/non-trivial decisions get an ADR in `design/decisions/`; the Key Decisions Log in `design/INDEX.md` remains the scannable index (forward-only/hybrid adoption — no mass backfill). See `design/decisions/README.md`.
- The `images/` directory holds diagrams referenced by spec documents.
