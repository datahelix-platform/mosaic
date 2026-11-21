"""Recipe resolvers — map a ``source`` (URI or path) to a recipe directory.

A :class:`RecipeResolver` exposes ``resolve(source, *, base_dir=None,
expected_digest=None)`` which returns a context manager yielding a
:class:`pathlib.Path` to a recipe *directory* — the form
``RecipeService`` operates on (parse ``recipe.yaml``, hash the
contents, etc.). Tarball sources are extracted into a temp directory
whose lifetime is bounded by the context manager.

Phase 3 (PTS-291) ships two implementations:

- :class:`FileResolver` (PR 5) — ``file:`` URIs, bare absolute paths,
  bare relative paths. Relative paths resolve against ``base_dir``.
  ``expected_digest`` is verified when supplied but never required.
- :class:`HttpsResolver` (PR 6) — fetches over HTTPS into a
  content-addressable cache rooted at ``cache_dir/<sha256>/``. The
  cache directory **persists** across calls (it IS the cache);
  ``FileResolver``'s tarball temp dir does not.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import ContextManager, Iterator, Optional
from urllib.parse import unquote, urlparse

from mosaic.core.exceptions import (
    RecipeDigestMismatchError,
    RecipeFetchError,
)


_TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar")


def _is_tarball(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suf) for suf in _TARBALL_SUFFIXES)


def _tar_filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
    """Strict tarfile extraction filter rejecting traversal + exotic entries.

    Symlinks, hardlinks, devices, and absolute or parent-traversing
    paths are rejected outright — v1 recipes are flat content trees.
    """
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise ValueError(f"Unsafe path in recipe tarball: {name!r}")
    if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
        raise ValueError(f"Unsupported entry in recipe tarball: {name!r}")
    if member.issym() or member.islnk():
        raise ValueError(
            f"Symlinks/hardlinks are not allowed in recipe tarballs: {name!r}"
        )
    return member


def _extract_tarball(tarball: Path, dest: Path) -> Path:
    """Extract ``tarball`` under ``dest`` and return the recipe root.

    The tarball MUST contain the recipe root as a single top-level
    directory (sec10 §10.4 layout). When the archive contains a
    single top-level directory, that directory is returned as the
    recipe root. When the archive contains multiple top-level
    entries, ``dest`` itself is treated as the recipe root.
    """
    with tarfile.open(tarball, "r:*") as tf:
        tf.extractall(dest, filter=_tar_filter)

    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def _strip_digest_prefix(digest: str) -> str:
    """Return the bare lowercase-hex form of a digest, stripping ``sha256:``."""
    return digest.removeprefix("sha256:").lower()


def default_recipe_cache_dir() -> Path:
    """Resolve the recipe cache root (sec10 §10.4.2).

    ``MOSAIC_RECIPE_CACHE`` wins when set; otherwise defaults to
    ``~/.mosaic/recipe-cache/``. The directory is created on demand by
    :class:`HttpsResolver`, not here.
    """
    from mosaic.config.env import get_env

    env = get_env("RECIPE_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".mosaic" / "recipe-cache"


class RecipeResolver(ABC):
    """Locate a recipe by source URI/path and yield a directory to operate on.

    Implementations are stateless beyond construction-time config.
    ``can_handle(source)`` is used by ``RecipeService`` to dispatch to
    the right resolver from its registered list.
    """

    @abstractmethod
    def can_handle(self, source: str) -> bool:
        """Return True if this resolver knows how to materialize ``source``."""

    @abstractmethod
    def resolve(
        self,
        source: str,
        *,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> ContextManager[Path]:
        """Return a context manager yielding the recipe root directory.

        Tarball sources are extracted into a temp directory whose
        lifetime equals the with-block. Cached HTTPS sources are
        yielded from the persistent cache and never deleted by the
        resolver. Directory sources are yielded as-is.

        ``base_dir`` resolves bare relative paths and ``file:`` URIs
        whose path component is relative. ``expected_digest`` is
        verified post-fetch when supplied; :class:`HttpsResolver`
        requires it implicitly (the install path is the gatekeeper for
        invariant 4), while :class:`FileResolver` treats it as optional.
        """


class FileResolver(RecipeResolver):
    """Resolver for ``file:`` URIs and bare filesystem paths (sec10 §10.4.1).

    Accepted source forms:

    - ``file:///abs/path`` — RFC 8089 absolute file URI.
    - ``file:./rel/path`` or ``file:rel/path`` — file URI with a
      relative path component (Mosaic-specific convenience documented
      in sec10 §10.3.3).
    - ``/abs/path`` — bare absolute path.
    - ``rel/path`` or ``./rel/path`` — bare relative path, resolved
      against ``base_dir``.

    Both directory recipes and ``.tar.gz`` / ``.tgz`` / ``.tar`` tarballs
    are accepted; tarballs are extracted into a temp dir whose root
    must be the recipe directory (sec10 §10.4 layout).
    """

    def can_handle(self, source: str) -> bool:
        if source.startswith("https:") or source.startswith("http:"):
            return False
        if source.startswith("file:"):
            return True
        return True

    @contextmanager
    def resolve(
        self,
        source: str,
        *,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> Iterator[Path]:
        path = self._resolve_path(source, base_dir=base_dir)
        if not path.exists():
            raise FileNotFoundError(
                f"Recipe source not found: {source} (resolved to {path})"
            )

        if path.is_dir():
            _verify_digest_if_requested(path, expected_digest, source=source)
            yield path
            return

        if path.is_file() and _is_tarball(path):
            with tempfile.TemporaryDirectory(prefix="mosaic-recipe-") as tmp:
                tmp_path = Path(tmp)
                recipe_dir = _extract_tarball(path, tmp_path)
                _verify_digest_if_requested(
                    recipe_dir, expected_digest, source=source
                )
                yield recipe_dir
            return

        raise ValueError(
            f"Recipe source must be a directory or tarball: {source} "
            f"(resolved to {path})"
        )

    def _resolve_path(self, source: str, *, base_dir: Optional[Path]) -> Path:
        if source.startswith("file:"):
            parsed = urlparse(source)
            raw = unquote(parsed.path or "")
            if parsed.netloc and parsed.netloc not in ("", "localhost"):
                raise ValueError(
                    f"file: URIs with a non-empty/non-localhost netloc are not "
                    f"supported: {source}"
                )
            candidate = Path(raw)
        else:
            candidate = Path(source)

        if candidate.is_absolute():
            return candidate
        if base_dir is None:
            base_dir = Path.cwd()
        return (base_dir / candidate).resolve()


class HttpsResolver(RecipeResolver):
    """Resolver for ``https:`` URIs with a content-addressable cache (sec10 §10.4.2).

    Fetches a recipe tarball over HTTPS into a temp file, verifies its
    canonical-content-hash digest against ``expected_digest`` when
    supplied, then atomically moves the extracted recipe directory
    into ``cache_dir/<digest>/``. Cache lookup is by digest, not URL:
    two URLs serving identical bytes resolve to the same cache entry.

    Cache contract:

    - **Cache hit** (cache dir exists for ``expected_digest``): skip
      the network. The yielded path is the cached directory; the
      caller MUST NOT delete it.
    - **Cache miss**: fetch → extract under a temp dir → compute digest
      → verify (if ``expected_digest`` was supplied) → atomically rename
      the extracted directory into ``cache_dir/<digest>/``.
    - **No digest supplied**: fetch + extract + compute + cache under
      the computed digest. (The install path normally enforces an
      ``expected_digest`` for ``https:`` — invariant 4 — but ``inspect``
      may legally call here without one.)

    Cache directories are persistent. They are NOT cleaned up by the
    context manager on exit.
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        *,
        timeout_seconds: float = 60.0,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        """Construct an HTTPS resolver.

        Args:
            cache_dir: Root of the content-addressable cache. Defaults
                to :func:`default_recipe_cache_dir`. Tests should pass
                a ``tmp_path``-rooted directory to keep host caches
                clean.
            timeout_seconds: Per-request timeout in seconds. Passed to
                ``urlopen``. Defaults to 60s.
            opener: Optional injected URL opener (for tests that want
                to bypass the default global handler). Defaults to
                ``urllib.request.urlopen``.
        """
        self._cache_dir = cache_dir or default_recipe_cache_dir()
        self._timeout = timeout_seconds
        self._opener = opener

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def can_handle(self, source: str) -> bool:
        return source.startswith("https:") or source.startswith("http:")

    @contextmanager
    def resolve(
        self,
        source: str,
        *,
        base_dir: Optional[Path] = None,
        expected_digest: Optional[str] = None,
    ) -> Iterator[Path]:
        if expected_digest is not None:
            normalized = _strip_digest_prefix(expected_digest)
            cached = self._cache_dir / normalized
            if cached.is_dir():
                yield cached
                return
        else:
            normalized = None

        # Cache miss → fetch + extract into a temp dir, verify, install.
        with tempfile.TemporaryDirectory(prefix="mosaic-recipe-fetch-") as tmp:
            tmp_path = Path(tmp)
            tarball_path = tmp_path / "recipe.tar.gz"
            self._download(source, tarball_path)
            extract_root = tmp_path / "extracted"
            extract_root.mkdir()
            try:
                recipe_dir = _extract_tarball(tarball_path, extract_root)
            except (tarfile.TarError, ValueError) as e:
                raise RecipeFetchError(
                    f"Failed to extract recipe tarball from {source}: {e}",
                    source=source,
                ) from e

            from mosaic.core.recipe.digest import canonical_content_hash

            actual = canonical_content_hash(recipe_dir)
            if normalized is not None and actual != normalized:
                raise RecipeDigestMismatchError(
                    f"Fetched recipe digest does not match declared digest.",
                    source=source,
                    expected_digest=normalized,
                    actual_digest=actual,
                )

            cache_key = normalized if normalized is not None else actual
            installed = self._install_into_cache(recipe_dir, cache_key)
            yield installed

    def _download(self, source: str, dest: Path) -> None:
        """Download ``source`` to ``dest``, mapping errors to RecipeFetchError."""
        opener = self._opener
        try:
            if opener is None:
                response = urllib.request.urlopen(source, timeout=self._timeout)
            else:
                response = opener.open(source, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            raise RecipeFetchError(
                f"HTTP {e.code} fetching recipe from {source}: {e.reason}",
                source=source,
                status_code=e.code,
            ) from e
        except urllib.error.URLError as e:
            raise RecipeFetchError(
                f"Failed to fetch recipe from {source}: {e.reason}",
                source=source,
            ) from e
        except (TimeoutError, OSError) as e:
            raise RecipeFetchError(
                f"Network error fetching recipe from {source}: {e}",
                source=source,
            ) from e

        try:
            with response, open(dest, "wb") as out:
                shutil.copyfileobj(response, out)
        except OSError as e:
            raise RecipeFetchError(
                f"Failed to write recipe tarball from {source}: {e}",
                source=source,
            ) from e

    def _install_into_cache(self, recipe_dir: Path, digest: str) -> Path:
        """Atomically install ``recipe_dir`` into the cache, return the cached path.

        Concurrency: if another process already populated the slot,
        keep the existing entry (its digest matches by construction).
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._cache_dir / digest
        if target.is_dir():
            return target

        staging = self._cache_dir / f".{digest}.{os.getpid()}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(recipe_dir, staging)
        try:
            os.replace(staging, target)
        except OSError:
            # Lost the race — another writer landed first.
            if target.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
                return target
            raise
        return target


def _verify_digest_if_requested(
    recipe_dir: Path,
    expected_digest: Optional[str],
    *,
    source: str,
) -> None:
    """Verify ``recipe_dir``'s canonical content hash matches ``expected_digest``.

    No-op when ``expected_digest`` is ``None``. Used by :class:`FileResolver`
    so callers that pass a declared digest get the same verification
    semantics they would over HTTPS.
    """
    if expected_digest is None:
        return
    from mosaic.core.recipe.digest import canonical_content_hash

    expected = _strip_digest_prefix(expected_digest)
    actual = canonical_content_hash(recipe_dir)
    if actual != expected:
        raise RecipeDigestMismatchError(
            "Recipe digest does not match declared digest.",
            source=source,
            expected_digest=expected,
            actual_digest=actual,
        )


__all__ = [
    "FileResolver",
    "HttpsResolver",
    "RecipeResolver",
    "default_recipe_cache_dir",
]
