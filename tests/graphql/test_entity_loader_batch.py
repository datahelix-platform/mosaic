"""The per-request entity loader batches ids without hitting SQLite's expression-depth cap.

One GraphQL request that resolves many relationship targets of one type
(``donors { samples { id } }`` over every donor, or ``samples { donor }`` over
every sample) sends all their ids to the loader as one batch. Composing that
batch as one ``id = ?`` filter per key joined by OR made a parse tree one level
deeper per key, and stock SQLite builds cap expression depth at 1000 (the
shipped image's Debian build does; some dev builds allow 10000, which is why
this never showed up locally): about 1000 targets of one type in one request
failed with "Expression tree is too large (maximum depth 1000)". The tests pin
the stock cap on every connection so they hold on any SQLite build.
"""

from __future__ import annotations

import sqlite3

import pytest

from mosaic.linkml_bridge import SchemaRegistry

from tests.graphql.test_inverse_slots import INVERSE_SCHEMA

STOCK_EXPR_DEPTH = 1000
#: Comfortably past the cap: the OR-composed batch failed from 999 keys.
N = 1050


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    # Same override as test_inverse_slots: Donor.samples is the inverse of
    # Sample.donor_id, so a reverse list and a forward reference both exist.
    return SchemaRegistry.from_yaml(INVERSE_SCHEMA)


@pytest.fixture
def stock_depth_cap(monkeypatch):
    """Every SQLite connection opened during the test gets the stock cap.

    Requested first by each test, so it is in place before the client fixture
    opens any connection (the adapter connects lazily, one per thread).
    """
    connect = sqlite3.connect

    def capped(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, STOCK_EXPR_DEPTH)
        return conn

    monkeypatch.setattr(sqlite3, "connect", capped)
    probe = sqlite3.connect(":memory:")
    try:
        # The cap really is in force: an OR chain this long no longer parses.
        with pytest.raises(sqlite3.OperationalError, match="Expression tree is too large"):
            probe.execute("SELECT 1 WHERE " + " OR ".join(["1 = ?"] * N), [1] * N)
    finally:
        probe.close()


def test_reverse_list_over_the_depth_cap_resolves_every_target(stock_depth_cap, hippo_client, gql):
    hippo_client.put("Donor", {"id": "d1", "name": "D1"})
    for i in range(N):
        hippo_client.put("Sample", {"id": f"s{i:05d}", "name": f"S{i}", "donor_id": "d1"})

    body = gql('{ donor(id: "d1") { samples { id } samplesCount } }')

    assert "errors" not in body, body["errors"][0]["message"]
    donor = body["data"]["donor"]
    assert donor["samplesCount"] == N
    assert sorted(s["id"] for s in donor["samples"]) == [f"s{i:05d}" for i in range(N)]


def test_forward_references_over_the_depth_cap_resolve_every_target(stock_depth_cap, hippo_client, gql):
    for i in range(N):
        hippo_client.put("Donor", {"id": f"d{i:05d}", "name": f"D{i}"})
        hippo_client.put("Sample", {"id": f"s{i:05d}", "name": f"S{i}", "donor_id": f"d{i:05d}"})

    body = gql(f"{{ samples(limit: {N}) {{ items {{ id donor {{ id }} }} }} }}")

    assert "errors" not in body, body["errors"][0]["message"]
    items = body["data"]["samples"]["items"]
    assert len(items) == N
    assert all(item["donor"] == {"id": "d" + item["id"][1:]} for item in items)
