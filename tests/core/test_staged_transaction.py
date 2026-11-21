"""Foundational tests for the staged commit-or-rollback scope (sec11 §11.5.2).

The S4 lifecycle orchestrator wraps a whole multi-package, multi-hop
migration chain in a single commit-or-rollback scope
(:meth:`MosaicClient.staged_transaction` →
:meth:`SQLiteAdapter.staged_transaction`). Two load-bearing properties make
that correct on SQLite's one-connection-per-thread model:

* writes staged inside the scope (deferred, uncommitted) are visible to
  reads on the **same** connection — so a later hop's transform reads the
  earlier hop's output via ``client.query``, and the end-to-end gate sees
  the staged base write-set together with the extension's existing rows;
* if anything escapes the scope (e.g. the gate raising
  :class:`MigrationGateError`), every staged write rolls back together —
  nothing is left half-committed.

These tests pin both properties down before the orchestrator is built on
them.
"""

import os
import tempfile

import pytest
import yaml
from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap


def _registry() -> SchemaRegistry:
    overlay = {
        "id": "https://example.org/mosaic/test_staged_txn",
        "name": "test_staged_txn",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "mosaic": "https://w3id.org/datahelix/mosaic/",
        },
        "imports": ["linkml:types", "mosaic_core"],
        "default_range": "string",
        "classes": {
            "Widget": {
                "is_a": "Entity",
                "attributes": {"label": {"range": "string"}},
            }
        },
    }
    return SchemaRegistry(
        SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
    )


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _registry()
        storage = SQLiteAdapter(
            os.path.join(tmpdir, "staged.db"), schema_registry=reg
        )
        yield MosaicClient(storage=storage, registry=reg)


def _ids(client: MosaicClient) -> set[str]:
    return {item["id"] for item in client.query("Widget").items}


def _put(client: MosaicClient, wid: str) -> None:
    client.put(
        "Widget",
        {"id": wid, "label": wid, "is_available": True},
        bypass_validation=True,
    )


class _Boom(RuntimeError):
    """Stand-in for an end-to-end gate failure escaping the scope."""


def test_staged_writes_visible_to_same_scope_reads(client: MosaicClient) -> None:
    with client.staged_transaction():
        _put(client, "w1")
        # Deferred (uncommitted) — yet readable on the same connection.
        assert "w1" in _ids(client)


def test_clean_scope_commits(client: MosaicClient) -> None:
    with client.staged_transaction():
        _put(client, "w1")
    assert "w1" in _ids(client)


def test_exception_rolls_back_whole_scope(client: MosaicClient) -> None:
    with pytest.raises(_Boom):
        with client.staged_transaction():
            _put(client, "w1")
            _put(client, "w2")
            assert {"w1", "w2"} <= _ids(client)  # visible mid-scope
            raise _Boom()
    # Rolled back together: neither row survives.
    assert _ids(client) == set()


def test_prior_commit_survives_later_rollback(client: MosaicClient) -> None:
    """A committed baseline is untouched by a later scope that rolls back —
    rollback undoes only the staged transaction, not earlier history."""
    _put(client, "base")  # committed via the ordinary per-write transaction
    with pytest.raises(_Boom):
        with client.staged_transaction():
            _put(client, "w1")
            raise _Boom()
    assert _ids(client) == {"base"}


def test_nested_scopes_commit_once(client: MosaicClient) -> None:
    """Reference-counted nesting: only the outermost scope commits, so an
    inner orchestration helper can open its own scope harmlessly."""
    with client.staged_transaction():
        _put(client, "outer")
        with client.staged_transaction():
            _put(client, "inner")
        # Inner exit must NOT have committed-and-closed the outer scope.
        assert {"outer", "inner"} <= _ids(client)
    assert {"outer", "inner"} <= _ids(client)


def test_nested_scope_rolls_back_to_outermost(client: MosaicClient) -> None:
    with pytest.raises(_Boom):
        with client.staged_transaction():
            _put(client, "outer")
            with client.staged_transaction():
                _put(client, "inner")
            raise _Boom()
    assert _ids(client) == set()
