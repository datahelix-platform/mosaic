"""Bibliography worked example for the Mosaic SDK.

Demonstrates:
  - Client initialisation (SchemaRegistry + SQLiteAdapter + MosaicClient)
  - create / get / update
  - Polymorphic queries (Publication hierarchy)
  - Full-text search across abstract Publication titles
  - Entity supersession: Preprint → JournalArticle
  - Provenance history walk

Run from the examples/bibliography/ directory:

    pip install datahelix-mosaic          # or `pip install -e ../../` for dev
    python sdk_example.py

The script creates a fresh SQLite database at ./data/mosaic.db each run
(the data/ directory is created if it does not exist).
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Initialise the client
# ---------------------------------------------------------------------------
# Use SchemaRegistry.from_path() to load the schema — it handles the
# bundled mosaic_core importmap so user schemas can say `imports: [mosaic_core]`
# without shipping a copy of mosaic_core.yaml.

from mosaic.linkml_bridge import SchemaRegistry
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.validators.write_validator import CELWriteValidator

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "mosaic.db"
DB_PATH.unlink(missing_ok=True)  # fresh DB each run

registry = SchemaRegistry.from_path(HERE / "schemas")

storage = SQLiteAdapter(str(DB_PATH), schema_registry=registry)

pipeline = ValidationPipeline()
pipeline.add_validator(
    CELWriteValidator(validators_path=str(HERE / "validators.yaml"))
)

client = MosaicClient(storage=storage, registry=registry, pipeline=pipeline)

print("=== Mosaic Bibliography Example ===\n")

# ---------------------------------------------------------------------------
# 2. Authors
# ---------------------------------------------------------------------------

vaswani = client.create("Author", {
    "name": "Ashish Vaswani",
    "orcid": "0000-0003-3988-4444",
    "email": "vaswani@example.org",
})
shazeer = client.create("Author", {
    "name": "Noam Shazeer",
    "orcid": "0000-0001-7777-2222",
})
parmar = client.create("Author", {
    "name": "Niki Parmar",
    "orcid": "0000-0002-9999-1111",
})

print(f"Created authors: {vaswani['id'][:8]}…, {shazeer['id'][:8]}…, {parmar['id'][:8]}…")

# ---------------------------------------------------------------------------
# 3. Preprint (arXiv version)
# ---------------------------------------------------------------------------

preprint = client.create("Preprint", {
    "title": "Attention Is All You Need",
    "year": 2017,
    "abstract_text": (
        "The dominant sequence transduction models are based on complex recurrent "
        "or convolutional neural networks that include an encoder and a decoder. "
        "We propose a new simple network architecture, the Transformer."
    ),
    "server": "arXiv",
    "preprint_id": "arXiv:1706.03762",
    # No DOI — preprints often lack one
})

print(f"\nCreated Preprint: {preprint['id'][:8]}…  (arXiv:1706.03762)")

# Link authors to preprint via Authorship join entities
for pos, author in enumerate([vaswani, shazeer, parmar], start=1):
    client.create("Authorship", {
        "author_id": author["id"],
        "publication_id": preprint["id"],
        "position": pos,
    })

# ---------------------------------------------------------------------------
# 4. Journal article (NeurIPS proceedings version)
# ---------------------------------------------------------------------------

article = client.create("JournalArticle", {
    "title": "Attention Is All You Need",
    "year": 2017,
    "abstract_text": (
        "The dominant sequence transduction models are based on complex recurrent "
        "or convolutional neural networks. We propose the Transformer, based "
        "solely on attention mechanisms, dispensing with recurrence entirely."
    ),
    "doi": "10.5555/3295222.3295349",
    "journal": "Advances in Neural Information Processing Systems",
    "volume": "30",
    "pages": "5998–6008",
})

print(f"Created JournalArticle: {article['id'][:8]}…  doi:{article['data']['doi']}")

for pos, author in enumerate([vaswani, shazeer, parmar], start=1):
    client.create("Authorship", {
        "author_id": author["id"],
        "publication_id": article["id"],
        "position": pos,
    })

# ---------------------------------------------------------------------------
# 5. A second paper that cites the transformer paper
# ---------------------------------------------------------------------------

bert_preprint = client.create("Preprint", {
    "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
    "year": 2018,
    "abstract_text": (
        "We introduce BERT, Bidirectional Encoder Representations from Transformers. "
        "Unlike recent language representation models, BERT is designed to pre-train "
        "deep bidirectional representations from unlabeled text."
    ),
    "server": "arXiv",
    "preprint_id": "arXiv:1810.04805",
})

citation = client.create("Citation", {
    "citing_id": bert_preprint["id"],
    "cited_id": article["id"],
})

print(f"Created Citation: BERT ({bert_preprint['id'][:8]}…) → Transformer ({article['id'][:8]}…)")

# ---------------------------------------------------------------------------
# 6. update: fix a typo in the BERT preprint
# ---------------------------------------------------------------------------

client.update("Preprint", bert_preprint["id"], {
    "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
    "year": 2018,
    "abstract_text": (
        "We introduce a new language representation model called BERT, Bidirectional "
        "Encoder Representations from Transformers. BERT is pre-trained from unlabeled "
        "text by jointly conditioning on both left and right context."
    ),
    "server": "arXiv",
    "preprint_id": "arXiv:1810.04805",
})

print(f"Updated BERT preprint abstract")

# ---------------------------------------------------------------------------
# 7. Polymorphic queries
# ---------------------------------------------------------------------------
# Publication is abstract — it has no backing table. Query each concrete
# subtype separately, then merge for a unified publication list.

print("\n--- Polymorphic query: all Publications ---")

all_publications: list[dict] = []
for subtype in ("JournalArticle", "Preprint", "ConferencePaper"):
    result = client.query(subtype)
    for item in result.items:
        all_publications.append(item)
        label = f"[{subtype}]"
        print(f"  {label:<20} {item['data']['title'][:55]}")

print(f"Total publications: {len(all_publications)}")

# ---------------------------------------------------------------------------
# 8. Full-text search (FTS5) across titles
# ---------------------------------------------------------------------------
# FTS search requires the schema to declare `mosaic_search: fts5` on the field
# and the underlying FTS tables to exist. The SQLiteAdapter creates them
# automatically from the schema annotations.

print("\n--- FTS search: 'transformer' ---")
fts_results = client.search("JournalArticle", "transformer")
for r in fts_results:
    print(f"  {r['data']['title'][:60]}")

print("\n--- FTS search: 'BERT' across Preprint ---")
fts_results = client.search("Preprint", "BERT")
# Note: FTS indexes each write separately — an updated entity may appear
# multiple times. Deduplicate by ID if needed:
seen = set()
for r in fts_results:
    if r["id"] not in seen:
        seen.add(r["id"])
        print(f"  {r['data']['title'][:60]}")

# ---------------------------------------------------------------------------
# 9. get: fetch a single entity
# ---------------------------------------------------------------------------

fetched_preprint = client.get("Preprint", preprint["id"])
print(f"\nget(Preprint, {preprint['id'][:8]}…):")
print(f"  title   : {fetched_preprint['data']['title']}")
print(f"  server  : {fetched_preprint['data']['server']}")
print(f"  version : {fetched_preprint['version']}")

# ---------------------------------------------------------------------------
# 10. Supersession: Preprint → JournalArticle
# ---------------------------------------------------------------------------
# Once the paper is published, mark the preprint as superseded by the article.
# supersede_entity() atomically:
#   - sets preprint.is_available = False
#   - writes a ProvenanceRecord(operation="supersede") on the preprint
#   - writes a ProvenanceRecord(operation="update") on the article noting the link

print("\n--- Supersession: arXiv preprint → NeurIPS article ---")

client.supersede_entity(
    entity_id=preprint["id"],
    replacement_id=article["id"],
    reason="Published as NeurIPS 2017 proceedings paper",
    actor=vaswani["id"],
)

# The preprint is now unavailable in normal queries
result = client.query("Preprint")
print(f"Preprints visible after supersede: {len(result.items)}")
assert not any(p["id"] == preprint["id"] for p in result.items), \
    "superseded preprint should be hidden from default queries"

# Fetch including unavailable to confirm it still exists
still_there = client.get("Preprint", preprint["id"], include_unavailable=True)
print(f"Preprint still in DB (include_unavailable=True): id={still_there['id'][:8]}…")

# ---------------------------------------------------------------------------
# 11. Provenance history
# ---------------------------------------------------------------------------

print("\n--- Provenance history for the preprint ---")

history = client.history(preprint["id"])
for entry in history:
    op = entry["operation_type"]
    ts = entry["timestamp"]
    snap = json.dumps(entry.get("state_snapshot") or {})
    print(f"  {op:<12}  {ts}  {snap[:60]}")

print("\n--- Done ---")
