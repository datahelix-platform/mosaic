"""Shared schema + seed for traversal ``columns`` tests (issue #236).

One shape, used by the SQLite core/MCP tests and the Postgres parity tests so
both adapters answer the identical questions:

- to-one chain: ``Sample.donor -> Donor.age_at_death``;
- stored forward to-many: ``Sample.aliquots -> Aliquot`` (relationship rows);
- a two-hop path whose list is the first hop: ``aliquots -> container -> kind``;
- a two-hop path whose list is the second hop: ``donor -> samples -> name``;
- a reverse hop declared on the derived side, as in the certification
  fixture (``Author.books``, ``inverse: author``);
- a reference ranged on an abstract class (``Sample.protocol``), which the
  manifest does not expose, so it is not a legal hop;
- a reverse hop whose range has a concrete subclass (``Donor.samples`` also
  reaches ``SolidSample`` rows), so members are read under the class that
  holds them, not the declared range.
"""

from __future__ import annotations

from typing import Any

from mosaic.linkml_bridge import SchemaRegistry
from tests.support.linkml_schemas import build_registry

CLASSES: dict[str, Any] = {
    "Donor": {
        "attributes": {
            "id": {"identifier": True},
            "name": {"range": "string", "required": True},
            "age_at_death": {"range": "integer"},
            "samples": {"range": "Sample", "multivalued": True, "inverse": "donor"},
        }
    },
    "Sample": {
        "attributes": {
            "id": {"identifier": True},
            "name": {"range": "string", "required": True},
            "donor": {"range": "Donor"},
            "aliquots": {"range": "Aliquot", "multivalued": True},
            # Ranged on an abstract class: not exposed, so not traversable
            # (NOT_PREDICATE_FILTERABLE) until ADR-0014 exposes it.
            "protocol": {"range": "Protocol"},
        }
    },
    "Protocol": {
        "abstract": True,
        "attributes": {"id": {"identifier": True}, "title": {"range": "string"}},
    },
    "SolidSample": {"is_a": "Sample", "attributes": {"mass_mg": {"range": "float"}}},
    "Aliquot": {
        "attributes": {
            "id": {"identifier": True},
            "label": {"range": "string", "required": True},
            "volume_ul": {"range": "float"},
            "container": {"range": "Container"},
        }
    },
    "Container": {
        "attributes": {
            "id": {"identifier": True},
            "kind": {"range": "string"},
        }
    },
    "Author": {
        "attributes": {
            "id": {"identifier": True},
            "name": {"range": "string", "required": True},
            "books": {"range": "Book", "multivalued": True, "inverse": "author"},
        }
    },
    "Book": {
        "attributes": {
            "id": {"identifier": True},
            "title": {"range": "string", "required": True},
            "author": {"range": "Author", "required": True},
        }
    },
}


def registry() -> SchemaRegistry:
    return build_registry(CLASSES)


def seed(client: Any) -> None:
    """Deterministic ids so assertions read as data, not lookups."""
    put = client.put
    put("Donor", {"id": "d1", "name": "D1", "age_at_death": 71})
    put("Donor", {"id": "d2", "name": "D2"})
    put("Container", {"id": "c1", "kind": "tube"})
    put("Container", {"id": "c2", "kind": "plate"})
    put("Aliquot", {"id": "a1", "label": "A1", "volume_ul": 10.0, "container": "c1"})
    put("Aliquot", {"id": "a2", "label": "A2", "volume_ul": 20.0, "container": "c2"})
    put("Aliquot", {"id": "a3", "label": "A3", "volume_ul": 5.0})
    put("Sample", {"id": "s1", "name": "S1", "donor": "d1", "aliquots": ["a1", "a2"]})
    put("Sample", {"id": "s2", "name": "S2", "donor": "d2", "aliquots": ["a3"]})
    put("Sample", {"id": "s3", "name": "S3"})
    put("SolidSample", {"id": "s4", "name": "S4", "donor": "d1", "mass_mg": 2.5})
    put("Author", {"id": "au1", "name": "Le Guin"})
    put("Author", {"id": "au2", "name": "Nobody"})
    put("Book", {"id": "b1", "title": "The Dispossessed", "author": "au1"})
    put("Book", {"id": "b2", "title": "The Lathe of Heaven", "author": "au1"})


#: (name, anchor, columns, expected [(anchor id, columns)], expected grain)
#: against :func:`seed`. Each adapter's test runs every case, so the two
#: answer identically (Postgres parity, issue #236 acceptance).
CASES: list[tuple[str, str, list[dict[str, Any]], list[tuple[str, dict[str, Any]]], Any]] = [
    (
        "to-one chain",
        "Sample",
        [{"path": ["name"]}, {"path": ["donor", "age_at_death"]}],
        [
            ("s1", {"name": "S1", "donor.age_at_death": 71}),
            ("s2", {"name": "S2", "donor.age_at_death": None}),
            ("s3", {"name": "S3", "donor.age_at_death": None}),
        ],
        None,
    ),
    (
        "to-many count",
        "Sample",
        [{"path": ["aliquots", "volume_ul"], "mode": "count"}],
        [("s1", {"aliquots.volume_ul": 2}), ("s2", {"aliquots.volume_ul": 1}), ("s3", {"aliquots.volume_ul": 0})],
        None,
    ),
    (
        "to-many joinIds",
        "Sample",
        [{"path": ["aliquots", "volume_ul"], "mode": "joinIds"}, {"path": ["aliquots", "id"], "mode": "joinIds"}],
        [
            ("s1", {"aliquots.volume_ul": [10.0, 20.0], "aliquots.id": ["a1", "a2"]}),
            ("s2", {"aliquots.volume_ul": [5.0], "aliquots.id": ["a3"]}),
            ("s3", {"aliquots.volume_ul": [], "aliquots.id": []}),
        ],
        None,
    ),
    (
        "to-many explode",
        "Sample",
        [{"path": ["name"]}, {"path": ["aliquots", "volume_ul"], "mode": "explode"}, {"path": ["aliquots", "label"], "mode": "explode"}],
        [
            ("s1", {"name": "S1", "aliquots.volume_ul": 10.0, "aliquots.label": "A1"}),
            ("s1", {"name": "S1", "aliquots.volume_ul": 20.0, "aliquots.label": "A2"}),
            ("s2", {"name": "S2", "aliquots.volume_ul": 5.0, "aliquots.label": "A3"}),
            # No members still yields the anchor's row: explode never filters.
            ("s3", {"name": "S3", "aliquots.volume_ul": None, "aliquots.label": None}),
        ],
        {"anchor": 3, "rows": 4, "edge": "aliquots"},
    ),
    (
        "explode with an anchor-grain column alongside",
        "Sample",
        [{"path": ["aliquots", "label"], "mode": "explode"}, {"path": ["donor", "samples", "name"], "mode": "count"}],
        [
            ("s1", {"aliquots.label": "A1", "donor.samples.name": 2}),
            ("s1", {"aliquots.label": "A2", "donor.samples.name": 2}),
            ("s2", {"aliquots.label": "A3", "donor.samples.name": 1}),
            ("s3", {"aliquots.label": None, "donor.samples.name": 0}),
        ],
        {"anchor": 3, "rows": 4, "edge": "aliquots"},
    ),
    (
        "reverse hop (Author.books)",
        "Author",
        [{"path": ["books", "title"], "mode": "explode"}],
        [("au1", {"books.title": "The Dispossessed"}), ("au1", {"books.title": "The Lathe of Heaven"}), ("au2", {"books.title": None})],
        {"anchor": 2, "rows": 3, "edge": "books"},
    ),
    (
        "reverse hop count",
        "Author",
        [{"path": ["books", "title"], "mode": "count"}],
        [("au1", {"books.title": 2}), ("au2", {"books.title": 0})],
        None,
    ),
    (
        "two hops, list first",
        "Sample",
        [{"path": ["aliquots", "container", "kind"], "mode": "explode"}],
        [
            ("s1", {"aliquots.container.kind": "tube"}),
            ("s1", {"aliquots.container.kind": "plate"}),
            # a3 has no container: the member row stays, its value is null.
            ("s2", {"aliquots.container.kind": None}),
            ("s3", {"aliquots.container.kind": None}),
        ],
        {"anchor": 3, "rows": 4, "edge": "aliquots"},
    ),
    (
        "two hops, list second",
        "Sample",
        [{"path": ["donor", "samples", "name"], "mode": "joinIds"}],
        [("s1", {"donor.samples.name": ["S1", "S4"]}), ("s2", {"donor.samples.name": ["S2"]}), ("s3", {"donor.samples.name": []})],
        None,
    ),
    (
        "reverse hop reaches a subclass's rows",
        "Donor",
        [{"path": ["samples", "name"], "mode": "joinIds"}, {"path": ["samples", "id"], "mode": "count"}],
        [("d1", {"samples.name": ["S1", "S4"], "samples.id": 2}), ("d2", {"samples.name": ["S2"], "samples.id": 1})],
        None,
    ),
]


def run_case(client: Any, anchor: str, columns: list[dict[str, Any]]):
    """Validate, compile and execute one case through the core SDK path
    (the same calls ``execute_query_spec`` makes). Returns
    ``([(id, columns)], grain, rows)``."""
    from mosaic.core.query_spec import parse_query_spec, validate_query_spec
    from mosaic.core.query_spec_compiler import compile_query_spec
    from mosaic.core.query_spec_traversal import project_traversal
    from mosaic.core.schema_typing import build_capability_manifest

    manifest = build_capability_manifest(client.registry)
    spec = parse_query_spec({"v": 1, "anchor": anchor, "mode": "AND", "criteria": [], "columns": columns})
    result = validate_query_spec(spec, manifest)
    assert result.valid, result.errors
    compiled = compile_query_spec(spec, manifest)
    page = client.query(entity_type=compiled.entity_type, where=compiled.where, fields=None)
    rows, grain = project_traversal(client, page.model_dump(mode="json")["items"], compiled.traversal)
    return [(r["id"], r["columns"]) for r in rows], grain, rows
