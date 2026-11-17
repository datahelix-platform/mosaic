"""Closure-aware reference resolution, SDK on SQLite (#241, ADR-0014 Decision 1).

The cases and fixture live in ``tests/support/closure_fixture.py``; the
lost-row cases are ``xfail(strict=True)`` until closure-aware resolution
lands. Postgres parity: ``tests/integration/test_postgres_closure_resolution.py``.
GraphQL: ``tests/graphql/test_closure_resolution.py``.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from tests.support.closure_fixture import ClosureSDKCases, build_closure_registry, seed


@pytest.fixture
def client():
    registry = build_closure_registry()
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SQLiteAdapter(os.path.join(tmpdir, "closure241.db"), schema_registry=registry)
        c = MosaicClient(storage=storage, bypass_validation=True)
        seed(c)
        yield c


class TestSQLiteClosureResolution(ClosureSDKCases):
    pass
