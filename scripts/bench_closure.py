"""Benchmark closure-aware resolution on SQLite (ADR-0014 Decision 1, #276 WP6).

ADR-0014 asks for the cost of the SQLite ``UNION ALL`` collection to be
measured. This is a standalone script, not part of the pytest run (the NFR
harness proper is mosaic#104). It seeds one SQLite file and times:

1. ``query(Proc, limit=50)`` for page 1 and a deep page, where ``Proc`` is a
   concrete parent with 3 concrete subclasses and ``--rows`` rows in total
   spread evenly over the 4 tables;
2. ``count(Proc)``;
3. the GraphQL DataLoader pattern: ``query(Proc, filters=[{"field": "id",
   "value": k}, ...], filter_mode="or")`` over ``--ids`` ids;
4. ``count_relationship(Src, id, "inputs")`` (``inputsCount``) for
   ``--sources`` sources whose to-many ``inputs`` ranges on an abstract
   class with 4 leaves.

Each workload reports the median and p95 of ``--repeat`` runs, after one
warm-up run. Usage::

    uv run --no-sync python scripts/bench_closure.py [--rows 100000] [--db PATH]

``--db`` reuses a seeded file across runs (seeding is the slow part).
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import tempfile
import time
from typing import Any, Callable

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry

SCHEMA = """\
id: https://example.org/bench_closure
name: bench_closure
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - hippo_core
default_range: string
classes:
  Proc:
    is_a: Entity
    attributes:
      name:
      rank:
        range: integer
  ProcA:
    is_a: Proc
    attributes:
      a_only:
  ProcB:
    is_a: Proc
    attributes:
      b_only:
  ProcC:
    is_a: Proc
    attributes:
      c_only:
  Item:
    is_a: Entity
    abstract: true
    attributes:
      label:
  Item1:
    is_a: Item
  Item2:
    is_a: Item
  Item3:
    is_a: Item
  Item4:
    is_a: Item
  Src:
    is_a: Entity
    attributes:
      name:
      inputs:
        range: Item
        multivalued: true
"""

PROC_CLASSES = ("Proc", "ProcA", "ProcB", "ProcC")
ITEM_CLASSES = ("Item1", "Item2", "Item3", "Item4")


def _client(path: str) -> MosaicClient:
    registry = SchemaRegistry.from_yaml(SCHEMA)
    storage = SQLiteAdapter(path, schema_registry=registry)
    return MosaicClient(storage=storage, registry=registry, bypass_validation=True)


def _seed(client: MosaicClient, rows: int, sources: int, chunk: int = 2000) -> None:
    storage = client._storage  # noqa: SLF001 — bench-only bulk transaction
    per_class = rows // len(PROC_CLASSES)
    started = time.perf_counter()
    for cls in PROC_CLASSES:
        for start in range(0, per_class, chunk):
            with storage.staged_transaction():
                for i in range(start, min(start + chunk, per_class)):
                    client.put(cls, {"id": f"{cls}-{i}", "name": f"{cls} {i}", "rank": i})
    items: list[str] = []
    with storage.staged_transaction():
        for cls in ITEM_CLASSES:
            for i in range(250):
                item_id = f"{cls}-{i}"
                client.put(cls, {"id": item_id, "label": f"{cls} {i}"})
                items.append(item_id)
    rng = random.Random(0)
    with storage.staged_transaction():
        for i in range(sources):
            client.put("Src", {"id": f"Src-{i}", "name": f"src {i}",
                               "inputs": rng.sample(items, 8)})
    print(f"seeded {per_class * len(PROC_CLASSES)} Proc-closure rows, "
          f"{len(items)} items, {sources} sources in "
          f"{time.perf_counter() - started:.1f}s")


def _time(fn: Callable[[], Any], repeat: int) -> tuple[float, float]:
    fn()  # warm-up
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return statistics.median(samples), p95


def run(client: MosaicClient, rows: int, ids: int, sources: int, repeat: int) -> list[tuple[str, float, float]]:
    rng = random.Random(1)
    per_class = rows // len(PROC_CLASSES)
    id_sample = [f"{rng.choice(PROC_CLASSES)}-{rng.randrange(per_class)}" for _ in range(ids)]
    id_filters = [{"field": "id", "value": k} for k in id_sample]
    deep = max(0, per_class * len(PROC_CLASSES) - 100)
    src_ids = [f"Src-{i}" for i in range(sources)]

    workloads: list[tuple[str, Callable[[], Any]]] = [
        ("query(Proc, limit=50) page 1", lambda: client.query("Proc", limit=50)),
        (f"query(Proc, limit=50, offset={deep})",
         lambda: client.query("Proc", limit=50, offset=deep)),
        ("query(Proc, limit=50, order_by=rank) page 1",
         lambda: client.query("Proc", limit=50, order_by="rank")),
        ("query(Proc, limit=50) type-exact (baseline)",
         lambda: client.query("Proc", limit=50, include_subclasses=False)),
        ("count(Proc)", lambda: client.count("Proc")),
        ("count(Proc) type-exact (baseline)",
         lambda: client.count("Proc", include_subclasses=False)),
        (f"DataLoader: query(Proc, {ids} id filters, or)",
         lambda: client.query("Proc", filters=id_filters, filter_mode="or")),
        (f"inputsCount: count_relationship x {sources} (4-leaf range)",
         lambda: [client.count_relationship("Src", s, "inputs") for s in src_ids]),
    ]
    results = []
    for name, fn in workloads:
        med, p95 = _time(fn, repeat)
        results.append((name, med, p95))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--ids", type=int, default=100)
    parser.add_argument("--sources", type=int, default=500)
    parser.add_argument("--repeat", type=int, default=15)
    parser.add_argument("--db", help="SQLite file to (re)use; seeded when new")
    args = parser.parse_args()

    tmp = None
    path = args.db
    if path is None:
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "bench.db")
    fresh = not os.path.exists(path)
    client = _client(path)
    if fresh:
        _seed(client, args.rows, args.sources)
    results = run(client, args.rows, args.ids, args.sources, args.repeat)
    width = max(len(name) for name, _, _ in results)
    print(f"{'workload':<{width}}  {'median ms':>10}  {'p95 ms':>9}")
    for name, med, p95 in results:
        print(f"{name:<{width}}  {med:>10.2f}  {p95:>9.2f}")
    if tmp is not None:
        tmp.cleanup()


if __name__ == "__main__":
    main()
