"""Tests for HttpsResolver (sec10 §10.4.2).

PR 6 of PTS-291. Verifies HTTPS fetch + content-addressable cache:
fetch on miss, skip-network on hit, mandatory-digest verification,
and the failure modes ``RecipeFetchError`` and
``RecipeDigestMismatchError``.

A real loopback ``http.server`` is preferred over mocking
``urllib.request`` — it exercises the actual network plumbing and is
more honest about timeout / URL-parsing behavior. Tests still use
``http://`` (not ``https://``) because spinning up an in-process TLS
cert is overkill for this surface; the resolver ``can_handle`` both
schemes, so the test is faithful.
"""

from __future__ import annotations

import io
import os
import tarfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from mosaic.core.exceptions import (
    RecipeDigestMismatchError,
    RecipeFetchError,
)
from mosaic.core.recipe import (
    HttpsResolver,
    canonical_content_hash,
    default_recipe_cache_dir,
)


MINIMAL_MANIFEST = (
    b"id: org.example.https\n"
    b"name: https-test\n"
    b"version: 0.1.0\n"
    b"created_at: '2026-05-27T00:00:00+00:00'\n"
    b"mosaic_version: '>=0.3'\n"
)
MINIMAL_SCHEMA = b"classes: {}\n"


def _make_recipe(root: Path) -> Path:
    d = root / "recipe"
    d.mkdir(parents=True)
    (d / "recipe.yaml").write_bytes(MINIMAL_MANIFEST)
    (d / "schema.yaml").write_bytes(MINIMAL_SCHEMA)
    return d


def _pack_tarball(recipe_dir: Path, out_path: Path) -> Path:
    with tarfile.open(out_path, "w:gz") as tf:
        tf.add(recipe_dir, arcname=recipe_dir.name)
    return out_path


@contextmanager
def _serve(routes: dict[str, bytes | int]) -> Iterator[str]:
    """Spin up a ThreadingHTTPServer that returns canned responses.

    ``routes`` maps URL paths to bytes (200 OK) or an int (status code
    with empty body). The context manager yields the base URL
    ``http://127.0.0.1:<port>``.
    """
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            handler = routes.get(self.path)
            if isinstance(handler, int):
                self.send_response(handler)
                self.end_headers()
                return
            if handler is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(handler)))
            self.end_headers()
            self.wfile.write(handler)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Isolated cache root rooted under ``tmp_path``."""
    return tmp_path / "cache"


@pytest.fixture
def recipe_bytes(tmp_path: Path) -> tuple[bytes, str]:
    """Pack a minimal recipe tarball and return (bytes, expected_digest)."""
    recipe_dir = _make_recipe(tmp_path / "src")
    expected_digest = canonical_content_hash(recipe_dir)
    tarball = _pack_tarball(recipe_dir, tmp_path / "recipe.tar.gz")
    return tarball.read_bytes(), expected_digest


class TestCanHandle:
    def test_https_accepted(self) -> None:
        assert HttpsResolver().can_handle("https://example.org/x.tar.gz") is True

    def test_http_accepted(self) -> None:
        # http+https both routed through HttpsResolver (the network path).
        assert HttpsResolver().can_handle("http://example.org/x.tar.gz") is True

    def test_file_rejected(self) -> None:
        assert HttpsResolver().can_handle("file:///tmp/x") is False


class TestFetchHappyPath:
    def test_fetch_extracts_and_yields_directory(
        self,
        cache_dir: Path,
        recipe_bytes: tuple[bytes, str],
    ) -> None:
        body, expected_digest = recipe_bytes
        with _serve({"/r.tar.gz": body}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with resolver.resolve(
                f"{base}/r.tar.gz", expected_digest=expected_digest
            ) as p:
                assert (p / "recipe.yaml").is_file()
                assert (p / "schema.yaml").is_file()

    def test_fetch_populates_cache_under_digest(
        self,
        cache_dir: Path,
        recipe_bytes: tuple[bytes, str],
    ) -> None:
        body, expected_digest = recipe_bytes
        with _serve({"/r.tar.gz": body}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with resolver.resolve(
                f"{base}/r.tar.gz", expected_digest=expected_digest
            ) as _:
                pass
            cached = cache_dir / expected_digest
            assert cached.is_dir()
            assert (cached / "recipe.yaml").read_bytes() == MINIMAL_MANIFEST

    def test_cache_persists_after_with_block(
        self,
        cache_dir: Path,
        recipe_bytes: tuple[bytes, str],
    ) -> None:
        body, expected_digest = recipe_bytes
        with _serve({"/r.tar.gz": body}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with resolver.resolve(
                f"{base}/r.tar.gz", expected_digest=expected_digest
            ) as p:
                captured = p
            # Cache contents must remain on disk.
            assert captured.exists()


class TestCacheHit:
    def test_cache_hit_skips_network(
        self,
        cache_dir: Path,
        recipe_bytes: tuple[bytes, str],
    ) -> None:
        body, expected_digest = recipe_bytes
        # First fetch populates the cache.
        with _serve({"/r.tar.gz": body}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with resolver.resolve(
                f"{base}/r.tar.gz", expected_digest=expected_digest
            ) as _:
                pass

        # Second fetch happens with the server SHUT DOWN — a cache hit
        # must not touch the network at all.
        with resolver.resolve(
            "https://offline.invalid/r.tar.gz",
            expected_digest=expected_digest,
        ) as p:
            assert (p / "recipe.yaml").is_file()


class TestDigestMismatch:
    def test_wrong_digest_raises_mismatch(
        self,
        cache_dir: Path,
        recipe_bytes: tuple[bytes, str],
    ) -> None:
        body, _digest = recipe_bytes
        bogus = "0" * 64
        with _serve({"/r.tar.gz": body}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with pytest.raises(RecipeDigestMismatchError) as exc:
                with resolver.resolve(
                    f"{base}/r.tar.gz", expected_digest=bogus
                ) as _:
                    pass
            assert exc.value.expected_digest == bogus
            assert exc.value.actual_digest is not None
            # Mismatch must NOT populate the cache.
            assert not (cache_dir / bogus).exists()

    def test_sha256_prefix_accepted(
        self,
        cache_dir: Path,
        recipe_bytes: tuple[bytes, str],
    ) -> None:
        body, expected_digest = recipe_bytes
        with _serve({"/r.tar.gz": body}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with resolver.resolve(
                f"{base}/r.tar.gz",
                expected_digest=f"sha256:{expected_digest}",
            ) as p:
                assert (p / "recipe.yaml").is_file()


class TestFetchErrors:
    def test_http_404_raises_fetch_error(self, cache_dir: Path) -> None:
        with _serve({"/missing": 404}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with pytest.raises(RecipeFetchError) as exc:
                with resolver.resolve(
                    f"{base}/missing",
                    expected_digest="a" * 64,
                ) as _:
                    pass
            assert exc.value.status_code == 404

    def test_connection_refused_raises_fetch_error(
        self, cache_dir: Path
    ) -> None:
        # Port 1 is the IANA "tcpmux" port — almost never bound on
        # modern hosts. urlopen surfaces it as a URLError.
        resolver = HttpsResolver(cache_dir=cache_dir, timeout_seconds=1)
        with pytest.raises(RecipeFetchError):
            with resolver.resolve(
                "http://127.0.0.1:1/r.tar.gz",
                expected_digest="a" * 64,
            ) as _:
                pass

    def test_corrupt_tarball_raises_fetch_error(self, cache_dir: Path) -> None:
        with _serve({"/r.tar.gz": b"this is not a tarball"}) as base:
            resolver = HttpsResolver(cache_dir=cache_dir)
            with pytest.raises(RecipeFetchError):
                with resolver.resolve(
                    f"{base}/r.tar.gz",
                    expected_digest="a" * 64,
                ) as _:
                    pass


class TestCacheDirEnvVar:
    def test_default_uses_env_var_when_set(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"MOSAIC_RECIPE_CACHE": str(tmp_path / "x")}):
            assert default_recipe_cache_dir() == tmp_path / "x"

    def test_default_falls_back_to_home(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOSAIC_RECIPE_CACHE", None)
            assert (
                default_recipe_cache_dir()
                == Path.home() / ".mosaic" / "recipe-cache"
            )


class TestFileResolverDigestVerification:
    """``FileResolver`` honours ``expected_digest`` when callers pass one."""

    def test_matching_digest_passes(self, tmp_path: Path) -> None:
        from mosaic.core.recipe import FileResolver

        recipe_dir = _make_recipe(tmp_path)
        digest = canonical_content_hash(recipe_dir)
        with FileResolver().resolve(
            str(recipe_dir), expected_digest=digest
        ) as p:
            assert (p / "recipe.yaml").is_file()

    def test_mismatch_raises(self, tmp_path: Path) -> None:
        from mosaic.core.recipe import FileResolver

        recipe_dir = _make_recipe(tmp_path)
        with pytest.raises(RecipeDigestMismatchError):
            with FileResolver().resolve(
                str(recipe_dir), expected_digest="a" * 64
            ) as _:
                pass

    def test_no_digest_is_no_op(self, tmp_path: Path) -> None:
        from mosaic.core.recipe import FileResolver

        recipe_dir = _make_recipe(tmp_path)
        with FileResolver().resolve(str(recipe_dir)) as p:
            assert (p / "recipe.yaml").is_file()
