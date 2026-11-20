# REST API Example — Bibliography

All examples below assume `mosaic serve` is running locally on port 8000.
Every mutating endpoint requires `Authorization: Bearer <token>`.

Start the server (from the `examples/bibliography/` directory):

```bash
mosaic serve --config config.json --host 127.0.0.1 --port 8000
```

---

## 1. Ingest an Author

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_type": "Author",
    "data": {
      "name": "Ashish Vaswani",
      "orcid": "0000-0003-3988-4444",
      "email": "vaswani@example.org"
    }
  }' | jq .
```

Response:
```json
{
  "id": "4d04f383-...",
  "entity_type": "Author",
  "data": {
    "name": "Ashish Vaswani",
    "orcid": "0000-0003-3988-4444",
    "email": "vaswani@example.org"
  },
  "version": 1,
  "created_at": "2026-05-26T11:00:00+00:00",
  "updated_at": "2026-05-26T11:00:00+00:00"
}
```

Save the returned `id` for subsequent calls:

```bash
AUTHOR_ID="4d04f383-..."
```

---

## 2. Ingest a JournalArticle

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_type": "JournalArticle",
    "data": {
      "title": "Attention Is All You Need",
      "year": 2017,
      "doi": "10.5555/3295222.3295349",
      "journal": "Advances in Neural Information Processing Systems",
      "volume": "30",
      "pages": "5998-6008"
    }
  }' | jq .
```

```bash
ARTICLE_ID="5cdfb56f-..."
```

---

## 3. Ingest a Preprint

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_type": "Preprint",
    "data": {
      "title": "Attention Is All You Need",
      "year": 2017,
      "server": "arXiv",
      "preprint_id": "arXiv:1706.03762"
    }
  }' | jq .
```

```bash
PREPRINT_ID="e941d21b-..."
```

---

## 4. Register an External ID (DOI)

Attach the arXiv preprint ID to the preprint entity so it can be looked
up by external identifier. The `source_system` is a free-form stable name.

```bash
curl -s -X POST "http://localhost:8000/entities/${PREPRINT_ID}/external-ids" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "external_id": "arXiv:1706.03762",
    "source_system": "arxiv"
  }' | jq .
```

---

## 5. Get a single entity by ID

```bash
curl -s "http://localhost:8000/entities/${ARTICLE_ID}" \
  -H "Authorization: Bearer dev-token" | jq .
```

Response:
```json
{
  "id": "5cdfb56f-...",
  "entity_type": "JournalArticle",
  "data": {
    "title": "Attention Is All You Need",
    "doi": "10.5555/3295222.3295349",
    ...
  },
  "version": 1,
  "created_at": "...",
  "updated_at": "..."
}
```

---

## 6. List entities by type (paginated)

```bash
curl -s "http://localhost:8000/entities?entity_type=JournalArticle&limit=20&offset=0" \
  -H "Authorization: Bearer dev-token" | jq '.items | length'
```

Merge a polymorphic "all publications" list in the client:

```bash
for TYPE in JournalArticle Preprint ConferencePaper; do
  curl -s "http://localhost:8000/entities?entity_type=${TYPE}&limit=100" \
    -H "Authorization: Bearer dev-token" | jq '.items[]'
done
```

---

## 7. Full-text search

The `search` endpoint queries FTS5 indexes created from `mosaic_search: fts5`
schema annotations.

```bash
# Search JournalArticle titles for "transformer"
curl -s "http://localhost:8000/search?entity_type=JournalArticle&q=transformer" \
  -H "Authorization: Bearer dev-token" | jq '.[].data.title'

# Search Preprint abstracts for "BERT"
curl -s "http://localhost:8000/search?entity_type=Preprint&q=BERT" \
  -H "Authorization: Bearer dev-token" | jq '.[].data.title'
```

---

## 8. Supersede an External ID (DOI reassignment)

The REST supersede endpoint is scoped to **External IDs only** — it reassigns
a cross-system identifier from one value to another within a source system.
Entity-level supersession (Preprint → JournalArticle) is SDK-only; see
`sdk_example.py` for the `client.supersede_entity()` call.

Example: the preprint's arXiv ID is updated to a canonical form.

```bash
curl -s -X POST "http://localhost:8000/entities/${PREPRINT_ID}/supersede" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "old_external_id": "arXiv:1706.03762",
    "new_external_id": "arXiv:1706.03762v5",
    "source_system": "arxiv"
  }' | jq .
```

---

## 9. Provenance history

```bash
curl -s "http://localhost:8000/entities/${PREPRINT_ID}/history" \
  -H "Authorization: Bearer dev-token" | jq '.[].operation_type'
```

Expected output (after SDK supersession via `sdk_example.py`):

```
"create"
"supersede"
```

---

## 10. Full replacement (PUT)

Replace all fields of an existing entity. All required fields must be present.

```bash
curl -s -X PUT "http://localhost:8000/entities/Author/${AUTHOR_ID}" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Ashish Vaswani",
    "orcid": "0000-0003-3988-4444",
    "email": "vaswani@google.com"
  }' | jq .data.email
```

---

## 11. Soft delete

```bash
curl -s -X DELETE "http://localhost:8000/entities/${PREPRINT_ID}" \
  -H "Authorization: Bearer dev-token" | jq .
```

Response:
```json
{"status": "deleted", "entity_id": "e941d21b-..."}
```

The entity is marked `is_available: false`; it does not appear in list or
search responses but can still be retrieved with `include_unavailable=true`
directly via the SDK.
