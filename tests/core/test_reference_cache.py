"""Tests for MosaicClient reference-loader caching surface (PTS-225)."""

from __future__ import annotations

import hashlib
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from mosaic.core.client import MosaicClient
from mosaic.core.exceptions import CacheIntegrityError, MosaicError


# --- Fixture HTTP server -------------------------------------------------


class _PayloadHandler(http.server.BaseHTTPRequestHandler):
    """Serve a static payload from the server's shared state.

    The test sets ``server.payload`` and ``server.hit_count``; the
    handler returns the payload on every GET and increments the counter
    so tests can assert cache-hit behavior (zero extra hits).
    """

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        server: "_FixtureServer" = self.server  # type: ignore[assignment]
        server.hit_count += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(server.payload)))
        self.end_headers()
        self.wfile.write(server.payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib API
        # Silence stderr noise during test runs.
        return


class _FixtureServer(socketserver.TCPServer):
    allow_reuse_address = True
    payload: bytes = b""
    hit_count: int = 0


@pytest.fixture()
def fixture_server():
    """Yield a localhost HTTP server with a swappable payload.

    The server runs on an ephemeral port in a daemon thread so each test
    gets an isolated origin, exercising the real ``urllib.request`` path
    inside ``cached_fetch`` without touching the network.
    """
    server = _FixtureServer(("127.0.0.1", 0), _PayloadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Pin ``$MOSAIC_CACHE_DIR`` to a tmp path for the duration of a test."""
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("MOSAIC_CACHE_DIR", str(cache_root))
    return cache_root


# --- cache_dir_for resolution -------------------------------------------


class TestCacheDirFor:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MOSAIC_CACHE_DIR", str(tmp_path / "envcache"))
        client = MosaicClient()
        path = client.cache_dir_for("ensembl")
        assert path == tmp_path / "envcache" / "ensembl"
        assert path.is_dir()

    def test_default_resolves_under_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MOSAIC_CACHE_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        client = MosaicClient()
        path = client.cache_dir_for("fma")
        assert path == tmp_path / ".cache" / "mosaic" / "references" / "fma"
        assert path.is_dir()

    def test_per_loader_directories_are_disjoint(self, isolated_cache):
        client = MosaicClient()
        a = client.cache_dir_for("a")
        b = client.cache_dir_for("b")
        assert a != b
        assert a.parent == b.parent

    def test_empty_loader_name_rejected(self, isolated_cache):
        client = MosaicClient()
        with pytest.raises(ValueError):
            client.cache_dir_for("")


# --- cached_fetch stability + verification ------------------------------


class TestCachedFetch:
    def test_returns_stable_path_across_calls(self, fixture_server, isolated_cache):
        server, base_url = fixture_server
        server.payload = b"hello world"
        client = MosaicClient()

        first = client.cached_fetch(f"{base_url}/x", loader_name="demo")
        second = client.cached_fetch(f"{base_url}/x", loader_name="demo")

        assert first == second
        assert first.read_bytes() == b"hello world"
        # Second call must be a cache hit — no second HTTP request.
        assert server.hit_count == 1

    def test_writes_under_loader_cache_dir(self, fixture_server, isolated_cache):
        server, base_url = fixture_server
        server.payload = b"payload"
        client = MosaicClient()
        path = client.cached_fetch(f"{base_url}/y", loader_name="ensembl")
        assert path.parent == client.cache_dir_for("ensembl")

    def test_expected_sha256_matches(self, fixture_server, isolated_cache):
        server, base_url = fixture_server
        server.payload = b"hello"
        digest = hashlib.sha256(server.payload).hexdigest()
        client = MosaicClient()
        path = client.cached_fetch(
            f"{base_url}/file",
            expected_sha256=digest,
            loader_name="demo",
        )
        assert path.read_bytes() == b"hello"

    def test_expected_sha256_mismatch_on_download(
        self, fixture_server, isolated_cache
    ):
        server, base_url = fixture_server
        server.payload = b"hello"
        client = MosaicClient()
        with pytest.raises(CacheIntegrityError) as exc:
            client.cached_fetch(
                f"{base_url}/bad",
                expected_sha256="0" * 64,
                loader_name="demo",
            )
        # CacheIntegrityError is in the MosaicError family per the
        # documented exception hierarchy.
        assert isinstance(exc.value, MosaicError)
        # The offending file must not be left behind to satisfy a later
        # cache hit silently.
        assert not (client.cache_dir_for("demo") / hashlib.sha256(
            f"{base_url}/bad".encode("utf-8")
        ).hexdigest()).exists()

    def test_expected_sha256_mismatch_on_cache_hit(
        self, fixture_server, isolated_cache
    ):
        server, base_url = fixture_server
        server.payload = b"hello"
        client = MosaicClient()
        path = client.cached_fetch(f"{base_url}/file", loader_name="demo")
        # Corrupt the cached file out-of-band, then re-fetch with an
        # expected digest that matches the *original* payload.
        path.write_bytes(b"tampered")
        original_digest = hashlib.sha256(b"hello").hexdigest()
        with pytest.raises(CacheIntegrityError):
            client.cached_fetch(
                f"{base_url}/file",
                expected_sha256=original_digest,
                loader_name="demo",
            )
        # File should be removed after the failed verification.
        assert not path.exists()


# --- clean_reference_cache helper ---------------------------------------


class TestCleanCache:
    def test_clean_named_removes_only_that_loader(self, isolated_cache):
        from mosaic.cli.commands.reference import (
            clean_reference_cache,
            reference_cache_root,
        )

        # Seed two loader caches with sentinel files.
        root = reference_cache_root()
        (root / "a").mkdir(parents=True)
        (root / "a" / "file").write_bytes(b"a")
        (root / "b").mkdir(parents=True)
        (root / "b" / "file").write_bytes(b"b")

        result = clean_reference_cache("a")
        assert result["removed"] is True
        assert result["scope"] == "a"
        assert not (root / "a").exists()
        # Other loader's cache untouched.
        assert (root / "b" / "file").read_bytes() == b"b"

    def test_clean_all_removes_entire_root(self, isolated_cache):
        from mosaic.cli.commands.reference import (
            clean_reference_cache,
            reference_cache_root,
        )

        root = reference_cache_root()
        (root / "a").mkdir(parents=True)
        (root / "a" / "file").write_bytes(b"a")
        (root / "b").mkdir(parents=True)
        (root / "b" / "file").write_bytes(b"b")

        result = clean_reference_cache(None)
        assert result["removed"] is True
        assert result["scope"] is None
        assert not root.exists()

    def test_clean_missing_loader_is_silent_noop(self, isolated_cache):
        from mosaic.cli.commands.reference import clean_reference_cache

        result = clean_reference_cache("nonexistent")
        assert result["removed"] is False
        assert result["scope"] == "nonexistent"

    def test_clean_missing_root_is_silent_noop(self, isolated_cache):
        from mosaic.cli.commands.reference import clean_reference_cache

        result = clean_reference_cache(None)
        assert result["removed"] is False
        assert result["scope"] is None


# --- CLI integration ----------------------------------------------------


class TestCleanCacheCLI:
    def test_clean_cache_named_command(self, isolated_cache):
        from typer.testing import CliRunner

        from mosaic.cli.commands.reference import reference_cache_root
        from mosaic.cli.main import app

        root = reference_cache_root()
        (root / "ensembl").mkdir(parents=True)
        (root / "ensembl" / "file").write_bytes(b"x")
        (root / "fma").mkdir(parents=True)

        runner = CliRunner()
        result = runner.invoke(app, ["reference", "clean-cache", "ensembl"])
        assert result.exit_code == 0, result.output
        assert not (root / "ensembl").exists()
        assert (root / "fma").exists()

    def test_clean_cache_all_command(self, isolated_cache):
        from typer.testing import CliRunner

        from mosaic.cli.commands.reference import reference_cache_root
        from mosaic.cli.main import app

        root = reference_cache_root()
        (root / "ensembl").mkdir(parents=True)
        (root / "fma").mkdir(parents=True)

        runner = CliRunner()
        result = runner.invoke(app, ["reference", "clean-cache"])
        assert result.exit_code == 0, result.output
        assert not root.exists()
