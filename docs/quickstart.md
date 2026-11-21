# Mosaic Quickstart

Mosaic is a **LinkML runtime engine** — you bring a LinkML schema and Mosaic gives you a queryable entity store with full provenance tracking.

This guide uses a bibliography citation-graph: authors, publications, and the citation edges between them. By the end you will have entities stored and retrieved via both the Python SDK and the REST API.

**Time:** ~5 minutes.

## Prerequisites

- Python 3.11+
- `pip install datahelix-mosaic`
- `curl` for the REST example

## Step 1: Initialize a Project

Create a new Mosaic project directory:

```bash
mosaic init --path biblio_qs
```

Expected output:

```
Created biblio_qs/
Created biblio_qs/data/
Created biblio_qs/schema.yaml
Created biblio_qs/config.json
Created biblio_qs/README.md
Created biblio_qs/.gitignore

Mosaic project initialized at biblio_qs
Template: bibliography
Storage: sqlite
Run 'mosaic serve' to start the server
Created biblio_qs/mosaic.yaml
```

Project layout:

```
biblio_qs/
├── data/         ← SQLite database lives here
├── schema.yaml   ← LinkML schema (edit or replace this)
├── config.json
├── mosaic.yaml
├── README.md
└── .gitignore
```

The default `schema.yaml` is the full bibliography schema (with `JournalArticle`, `Preprint`, `Venue`, etc.). For this quickstart we use a trimmed version.

## Step 2: Define the Schema

Open `biblio_qs/schema.yaml` and replace its contents with this trimmed three-class schema:

```yaml
id: https://example.org/mosaic/quickstart/bibliography
name: bibliography

prefixes:
  linkml: https://w3id.org/linkml/

imports:
  - linkml:types
  - mosaic_core

default_range: string

classes:
  Author:
    is_a: Entity
    attributes:
      name:
        range: string
        required: true
      orcid:
        range: string
      email:
        range: string

  Publication:
    is_a: Entity
    attributes:
      title:
        range: string
        required: true
      year:
        range: integer
      doi:
        range: string
      journal:
        range: string

  Citation:
    is_a: Entity
    attributes:
      citing_id:
        range: Publication
        required: true
      cited_id:
        range: Publication
        required: true
```

This defines three entity types:
- **Author**: a researcher, with an optional ORCID identifier
- **Publication**: a citable work (article, preprint, proceedings paper, etc.)
- **Citation**: a directed edge from one Publication to another

### Entity Reference Attributes

The `citing_id` and `cited_id` attributes on `Citation` have `range: Publication`. This is an **entity reference** — it differs from a plain `string` attribute in several important ways:

- **Semantic relationship** — `range: Publication` declares that this attribute points to a `Publication` entity. Mosaic records and exposes this as a typed edge.
- **Holds a Mosaic internal UUID** — the value stored is the UUID Mosaic assigns at ingest time, not a human-visible title or DOI. User-visible identifiers belong in a plain `string` attribute or as an `ExternalID`.
- **Write-time validation** — Mosaic checks at ingest time that the referenced UUID exists and is available. Writing a `Citation` whose `citing_id` points to a non-existent `Publication` is rejected.

## Step 3: Create and Query Entities via the SDK

The Python SDK initializes the database schema automatically on first use. Save this as `biblio_qs/example.py`:

```python
from pathlib import Path
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.pipeline import ValidationPipeline

# Load the schema and open the database (created automatically on first run).
HERE = Path(__file__).parent
registry = SchemaRegistry.from_path(HERE / "schema.yaml")
storage = SQLiteAdapter(str(HERE / "data" / "mosaic.db"), schema_registry=registry)
client = MosaicClient(storage=storage, registry=registry, pipeline=ValidationPipeline())

# ── Create entities ──────────────────────────────────────────────────────────
vaswani = client.create("Author", {
    "name": "Ashish Vaswani",
    "orcid": "0000-0003-3988-4444",
    "email": "vaswani@example.org",
})
print(f"Created Author      : {vaswani['id'][:8]}…  {vaswani['data']['name']}")

transformer = client.create("Publication", {
    "title": "Attention Is All You Need",
    "year": 2017,
    "doi": "10.5555/3295222.3295349",
    "journal": "Advances in Neural Information Processing Systems",
})
print(f"Created Publication : {transformer['id'][:8]}…  {transformer['data']['title']}")

bert = client.create("Publication", {
    "title": "BERT: Pre-training of Deep Bidirectional Transformers",
    "year": 2018,
    "journal": "NAACL-HLT",
})
print(f"Created Publication : {bert['id'][:8]}…  {bert['data']['title'][:45]}")

# Citation: BERT cites the Transformer paper.
# citing_id and cited_id hold the Mosaic UUIDs returned above, not titles.
citation = client.create("Citation", {
    "citing_id": bert["id"],
    "cited_id": transformer["id"],
})
print(f"Created Citation    : {citation['id'][:8]}… (BERT → Transformer)")

# ── Query ────────────────────────────────────────────────────────────────────
print("\nAll Publications:")
result = client.query("Publication")
for pub in result.items:
    print(f"  [{pub['data']['year']}] {pub['data']['title'][:55]}")

# Retrieve a single entity by ID
fetched = client.get("Author", vaswani["id"])
print(f"\nFetched Author: {fetched['data']['name']}  (version {fetched['version']})")
```

Run it:

```bash
cd biblio_qs && python example.py
```

Expected output:

```
Created Author      : a1b2c3d4…  Ashish Vaswani
Created Publication : e5f6a7b8…  Attention Is All You Need
Created Publication : c9d0e1f2…  BERT: Pre-training of Deep Bidirectional Tran
Created Citation    : f3a4b5c6… (BERT → Transformer)

All Publications:
  [2017] Attention Is All You Need
  [2018] BERT: Pre-training of Deep Bidirectional Transformers

Fetched Author: Ashish Vaswani  (version 1)
```

(UUIDs are assigned at runtime and will differ on each run.)

## Step 4: One REST Endpoint

Mosaic's REST transport wraps the same SDK client. Create `biblio_qs/serve.py`:

```python
from pathlib import Path
from mosaic.linkml_bridge import SchemaRegistry
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.pipeline import ValidationPipeline
from mosaic.serve import create_default_app
import uvicorn

HERE = Path(__file__).parent
registry = SchemaRegistry.from_path(HERE / "schema.yaml")
storage = SQLiteAdapter(str(HERE / "data" / "mosaic.db"), schema_registry=registry)
client = MosaicClient(storage=storage, registry=registry, pipeline=ValidationPipeline())
app = create_default_app(mosaic_client=client)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
```

Start the server in a separate terminal (or as a background process):

```bash
cd biblio_qs && python serve.py &
SERVER_PID=$!
```

Ingest an author via the REST API:

```bash
curl -s -X POST http://127.0.0.1:8000/ingest \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_type": "Author",
    "data": {"name": "Noam Shazeer", "orcid": "0000-0001-7777-2222"}
  }'
```

Response:

```json
{
  "id": "...",
  "entity_type": "Author",
  "data": {
    "name": "Noam Shazeer",
    "orcid": "0000-0001-7777-2222"
  },
  "version": 1,
  "created_at": "...",
  "updated_at": "..."
}
```

List all authors (including those created by the SDK in Step 3):

```bash
curl -s "http://127.0.0.1:8000/entities?entity_type=Author&limit=10" \
  -H "Authorization: Bearer dev-token"
```

Stop the server when done:

```bash
kill $SERVER_PID
```

> **Note:** The dev server accepts any `Bearer <token>` value. Wire a real auth middleware at the transport layer for production deployments.

## Next Steps

- **[Data Model](data-model.md)** — entity types, relationships, and schema design
- **[SDK Reference](reference_typed_client.md)** — full Python SDK documentation
- **[API Reference](api-reference.md)** — complete REST API reference
- **[Schema Guide](schema-guide.md)** — writing and evolving LinkML schemas for Mosaic
- **[Configuration](configuration.md)** — configure Mosaic for different deployment scenarios
- **[Bibliography Worked Example](../examples/bibliography/README.md)** — deeper dive: polymorphism, validators, full-text search, entity supersession, and provenance
