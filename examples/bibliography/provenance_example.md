# Provenance Walk — Preprint → Journal Article Supersession

This document walks through the PROV-O-aligned provenance chain that Mosaic
records when an arXiv preprint is superseded by its published journal article.
Run `sdk_example.py` first to populate the database, then execute the
snippets below.

## Background: ProvenanceRecord shape

Each write operation produces one `ProvenanceRecord` stored in Mosaic's audit
log. The fields (defined in `mosaic_core.yaml`) align with PROV-O predicates:

| Field              | PROV-O predicate           | Description                          |
|--------------------|----------------------------|--------------------------------------|
| `operation_id`     | —                          | UUID of this audit record            |
| `entity_id`        | `prov:wasGeneratedBy`      | UUID of the entity being acted on    |
| `entity_type`      | —                          | Class name at write time             |
| `operation_type`   | —                          | `create`, `update`, `supersede`, … |
| `timestamp`        | `prov:endedAtTime`         | UTC wall-clock time                  |
| `user_id`          | `prov:wasAssociatedWith`   | Actor UUID (or `"unknown"`)          |
| `state_snapshot`   | —                          | Data payload at write time           |

## Scenario

1. A researcher deposits an arXiv preprint (`Preprint` entity).
2. The work is accepted. A `JournalArticle` entity is created for the
   published version.
3. `client.supersede_entity(preprint_id, article_id, ...)` is called,
   which atomically:
   - marks the preprint `is_available = False`
   - writes a `supersede` ProvenanceRecord on the preprint
   - writes an `update` ProvenanceRecord on the article (noting the link)

## Python code to walk the provenance chain

```python
from pathlib import Path
import json

from mosaic.linkml_bridge import SchemaRegistry
from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter

HERE = Path(".")
registry = SchemaRegistry.from_path(HERE / "schemas")
storage  = SQLiteAdapter(str(HERE / "data" / "mosaic.db"), schema_registry=registry)
client   = MosaicClient(storage=storage, registry=registry)

# --- List all preprints to find the superseded one ---
all_preprints = client.query("Preprint").items
# include_unavailable=True to see superseded entities
all_preprints_incl = [
    client.get("Preprint", p["id"], include_unavailable=True)
    for p in all_preprints
]

# If sdk_example.py was run, there should be one unavailable preprint
# (the Attention Is All You Need arXiv version)
unavailable = [
    p for p in all_preprints_incl if not p["data"].get("is_available", True)
]
for p in unavailable:
    print(f"Superseded preprint: {p['data']['title']}")
    print(f"  id: {p['id']}")

    # --- Walk the history ---
    history = client.history(p["id"])
    print(f"  Provenance entries: {len(history)}")
    for entry in history:
        print(f"    [{entry['operation_type']:12}]  {entry['timestamp']}")
        snap = entry.get("state_snapshot") or {}
        if snap:
            print(f"      snapshot: {json.dumps(snap)[:80]}")
```

## Expected output

```
Superseded preprint: Attention Is All You Need
  id: e941d21b-...

  Provenance entries: 2
    [create      ]  2026-05-26T11:14:40.410430+00:00
      snapshot: {"title": "Attention Is All You Need", "year": 2017, ...}
    [supersede   ]  2026-05-26T11:14:40.413581+00:00
      snapshot: {"reason": "Published as NeurIPS 2017 proceedings paper"}
```

## PROV-O graph interpretation

```
prov:Entity (Preprint e941d21b)
    prov:wasInvalidatedBy → prov:Activity (supersede record b2290a11)
        prov:endedAtTime    → 2026-05-26T11:14:40Z
        prov:wasAssociatedWith → prov:Agent (vaswani-uuid)

prov:Entity (JournalArticle 5cdfb56f)
    prov:wasGeneratedBy → prov:Activity (create record for article)
    prov:wasDerivedFrom → prov:Entity (Preprint e941d21b)  [implicit via supersede link]
```

- The superseded preprint maps to `prov:wasInvalidatedBy` — it ceased to be
  the canonical record at the moment of supersession.
- The `actor` UUID passed to `supersede_entity()` appears as `user_id` in
  the ProvenanceRecord, aligning with `prov:wasAssociatedWith`.
- The `reason` string is carried in `state_snapshot.reason`, analogous to
  PROV-O's `prov:description` on an invalidation activity.

## Querying history for the article (the replacement)

```python
article_history = client.history(article_id)
for entry in article_history:
    print(f"[{entry['operation_type']}]  {entry.get('state_snapshot', {})}")
```

After running `sdk_example.py`:
```
[create]  {"title": "Attention Is All You Need", "doi": "10.5555/3295222.3295349", ...}
```

The article itself is not modified by `supersede_entity()` — only the preprint
is marked unavailable. The provenance graph for the article grows as normal
`update` entries if the article data is subsequently corrected.

## Multiple-hop citation chain

```python
# BERT preprint cites the Transformer article
# Walk: BERT preprint → Citation → Transformer article → its provenance

bert = client.query("Preprint").items  # find BERT
citations = client.query("Citation").items

for c in citations:
    citing = c["data"]["citing_id"]
    cited  = c["data"]["cited_id"]
    print(f"Citation: {citing[:8]}… → {cited[:8]}…")

    # Provenance of the cited article
    cited_history = client.history(cited)
    print(f"  Cited entity provenance entries: {len(cited_history)}")
```
