# Design Principles

These six principles describe the architectural commitments behind Mosaic. They exist to give you a vocabulary for two purposes: to advocate for Mosaic to colleagues who are evaluating it, and to argue about whether a proposed feature or change belongs in the engine.

---

## 1. The schema is the application.

Mosaic ships no data model of its own. When you define `Author`, `Publication`, and `Citation` in a LinkML file, Mosaic generates the relational tables, the Python SDK methods, and the REST endpoints for those types at startup — without you editing a line of Mosaic source. If you add a field, you add it in the schema; Mosaic picks it up on the next `mosaic migrate`. This principle is why there is no "Mosaic entity type library" to learn: the entity types that exist in your deployment are exactly the ones your schema defines.

## 2. Append-only by construction.

There is no `client.delete()`. Removing an entity means setting `is_available = False`, which records the transition in the provenance log and hides the entity from default queries — the row stays in the database. Every call to `client.put()` writes a new `ProvenanceRecord` row logging who changed what and when, so you can reconstruct any entity's state at any prior timestamp. This is not a feature flag or a policy decision you configure; it is how the storage layer is built.

## 3. Provenance is PROV-O, not PROV-adjacent.

`ProvenanceRecord` is a first-class LinkML class with `class_uri: prov:Activity`. Its fields map directly to PROV-O predicates: `prov:wasGeneratedBy`, `prov:wasAttributedTo`, `prov:startedAtTime`. A Mosaic deployment can serialize its entire provenance graph to RDF or JSON-LD and load it into a W3C PROV reasoner, an RO-Crate packager, or a lineage tool — with no transformation step, because the internal representation *is* a PROV graph. Systems that say they are "PROV-compatible" typically mean they can export to PROV formats. Mosaic means it can't not.

## 4. Transport is an adapter, not the architecture.

`MosaicClient` is the system. When you run `mosaic serve`, the REST API spins up a FastAPI app whose route handlers call the same `MosaicClient` methods you call from a notebook — there is no separate "server code path." This means the SDK and the API surface behave identically, bugs are fixed once, and you can switch between embedded library use and REST service use without changing the code that talks to Mosaic. Adding a new transport (GraphQL, gRPC) is a new adapter on top of the SDK, not a new implementation of the business logic.

## 5. LinkML upstream, not downstream.

Mosaic extends LinkML schemas with `mosaic_*` annotations for features the engine needs — validation tiers, reference-loader hooks, provenance configuration. Outside those annotations, Mosaic uses LinkML's own toolchain: `linkml-validate`, `gen-sqlddl`, and the LinkML Python dataclasses runtime. If there is a bug in LinkML's schema parsing, the fix is a version bump in Mosaic's `pyproject.toml`, not a shim in Mosaic's source. The engine does not reimplement or override upstream LinkML behavior; it extends it at well-defined, bounded points.

## 6. Mosaic doesn't know about your domain.

The only entity types the Mosaic core ships are `Entity`, `ProvenanceRecord`, `Process`, `Validator`, and `ReferenceLoader`. There are no biomedical entity types, no sample-tracking conventions, no ontology bindings, no field-naming standards in the engine. The VA National PTSD Brain Bank deployment — Mosaic's first production use — works because it supplies a schema that defines biomedical entities; that schema is not in the Mosaic repository. Domain-specific validators, reference loaders, and external adapters live in user-installed packages that register themselves via entry points. Keeping the core domain-free is what makes the same engine usable for a bibliography, a software inventory, or a clinical registry without modification.
