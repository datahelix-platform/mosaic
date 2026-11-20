"""Network-free ``ReferenceLoader`` for use in Mosaic test suites.

Implements every abstract method against an in-memory dataset so tests
exercising loader discovery, the install/upgrade lifecycle, and CLI
plumbing have a concrete entry-point target without depending on a real
reference package.

The loader is deterministic: ``versions()`` lists ``"test"`` and ``"v1"``,
both of which write a small fixed set of ``FakeTerm`` rows through the
``MosaicClient`` it is handed. Loader unit tests that need to simulate a
failure mid-load can pass ``params=FakeLoadParams(fail_after=N)`` to
abort the write after ``N`` entities; the rows written before the abort
remain in the database (matching how a real loader behaves if its HTTP
fetch dies mid-stream).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer
from pydantic import BaseModel, Field

from mosaic.core.loaders.reference import (
    EntityRef,
    LoadResult,
    ReferenceLoader,
    SchemaPackage,
)

if TYPE_CHECKING:
    from mosaic.core.client import MosaicClient


class FakeLoadParams(BaseModel):
    """Parameter model rendered by the CLI when the fake loader is
    invoked with ``--flag`` args."""

    tag: str = "default"
    # Internal test knob — if set, ``load()`` raises after persisting
    # this many rows. Real loaders never expose this; it lives here so
    # CLI tests can simulate a partial-load failure without monkey-
    # patching the loader class.
    fail_after: int | None = None
    # If True, ``load()`` writes rows via ``client.put()`` but returns
    # an empty ``LoadResult.entities`` list — simulates a large-scale
    # loader that leaves the prune substrate to the write log
    # (sec2 §2.14.8 advisory contract).
    omit_entity_refs: bool = False
    # If True, the loader passes a stable ``id`` derived from the row's
    # label into ``client.put()``, so shared labels across versions
    # collide on the same entity row. Exercises the
    # stable-id-upgrade-overlap prune behaviour (sec2 §2.14.9).
    stable_ids: bool = False


class FakeReferenceLoader(ReferenceLoader):
    """Deterministic, in-memory ``ReferenceLoader`` for the test suite."""

    name = "fake"
    description = "In-memory fake reference loader (test fixture)"
    load_params_schema = FakeLoadParams

    # In-memory dataset keyed by version. Kept tiny on purpose.
    _DATASET: dict[str, list[dict[str, Any]]] = {
        "test": [
            {"label": "alpha"},
            {"label": "beta"},
        ],
        "v1": [
            {"label": "alpha"},
            {"label": "beta"},
            {"label": "gamma"},
        ],
        "v2": [
            {"label": "alpha"},
            {"label": "beta"},
            {"label": "gamma"},
            {"label": "delta"},
        ],
    }

    def versions(self) -> list[str]:
        return list(self._DATASET.keys())

    def populates_types(self) -> list[str]:
        return ["FakeTerm"]

    def schema_fragment(self) -> dict:
        return {
            "id": "https://example.org/mosaic/fake",
            "name": "fake",
            "default_prefix": "fake",
            "prefixes": {"fake": "https://example.org/mosaic/fake/"},
            "classes": {
                "FakeTerm": {
                    "is_a": "Entity",
                    "attributes": {
                        "label": {"range": "string", "required": True},
                    },
                },
            },
        }

    def load(
        self,
        client: "MosaicClient",
        version: str,
        params: BaseModel | None = None,
    ) -> LoadResult:
        rows = self._DATASET.get(version)
        if rows is None:
            return LoadResult(
                errors=1,
                error_messages=[f"unknown version: {version}"],
            )

        fail_after: int | None = None
        omit_entity_refs = False
        stable_ids = False
        if isinstance(params, FakeLoadParams):
            fail_after = params.fail_after
            omit_entity_refs = params.omit_entity_refs
            stable_ids = params.stable_ids

        entities: list[EntityRef] = []
        created = 0
        for index, row in enumerate(rows):
            if fail_after is not None and index >= fail_after:
                raise RuntimeError(
                    f"FakeReferenceLoader simulated failure after "
                    f"{fail_after} rows"
                )
            payload = dict(row)
            if stable_ids:
                payload["id"] = f"fake-{row['label']}"
            ref = EntityRef.from_put_result(client.put("FakeTerm", payload))
            created += 1
            if not omit_entity_refs:
                entities.append(ref)

        return LoadResult(
            created=created,
            entities=entities,
        )


# ---------------------------------------------------------------------------
# Fixture loaders exercising the load_params_schema → --flag pipeline
# (D2.14.D / PTS-230).
# ---------------------------------------------------------------------------


class RichParams(BaseModel):
    """All-supported-type Pydantic schema for CLI-rendering tests."""

    # required scalar
    name: str
    # int with default + Pydantic constraint (drives the validation-error test)
    count: int = Field(default=10, ge=1, le=1000)
    # bool with default False — exercises ``--<name>`` / ``--no-<name>``
    cleanup: bool = False
    # list[str] with non-empty default — exercises append-replaces-default
    tags: list[str] = Field(default_factory=lambda: ["primary"])
    # Optional[str] with default None — omitted flag yields None
    optional_tag: str | None = None


class RichParamsLoader(ReferenceLoader):
    """Loader with one field per supported type.

    ``load()`` echoes the received params model into ``LoadResult`` so
    tests can round-trip flag → model → loader call. ``params`` MUST be a
    :class:`RichParams` instance (i.e. CLI parsing actually happened).
    """

    name = "rich"
    description = "Fixture loader covering all CLI-renderable parameter types"
    load_params_schema = RichParams

    _DATASET: dict[str, list[dict[str, Any]]] = {
        "test": [{"label": "rich-test"}],
        "v1": [{"label": "rich-v1"}],
    }

    # Most-recent params model received by .load(); inspected by tests.
    last_params: RichParams | None = None

    def versions(self) -> list[str]:
        return list(self._DATASET.keys())

    def populates_types(self) -> list[str]:
        return ["RichTerm"]

    def schema_fragment(self) -> dict:
        return {
            "id": "https://example.org/mosaic/rich",
            "name": "rich",
            "default_prefix": "rich",
            "prefixes": {"rich": "https://example.org/mosaic/rich/"},
            "classes": {
                "RichTerm": {
                    "is_a": "Entity",
                    "attributes": {
                        "label": {"range": "string", "required": True},
                    },
                },
            },
        }

    def load(
        self,
        client: "MosaicClient",
        version: str,
        params: BaseModel | None = None,
    ) -> LoadResult:
        if not isinstance(params, RichParams):
            raise AssertionError(
                f"RichParamsLoader.load expected RichParams instance, "
                f"got {type(params).__name__}"
            )
        # Stash the parsed model on the *class* so test assertions can
        # read it after the CLI invocation returns. Tests reset this
        # between cases.
        type(self).last_params = params

        rows = self._DATASET.get(version, [])
        entities: list[EntityRef] = [
            EntityRef.from_put_result(client.put("RichTerm", dict(row)))
            for row in rows
        ]
        return LoadResult(
            created=len(entities),
            entities=entities,
        )


class BareReferenceLoader(ReferenceLoader):
    """Loader with ``load_params_schema = None``.

    Used by tests asserting that ``load()`` receives ``params=None`` when
    the loader declares no schema, and that any extra ``--flag`` args
    are rejected with a clear error.
    """

    name = "bare"
    description = "Fixture loader with no parameter schema"
    load_params_schema = None

    _DATASET: dict[str, list[dict[str, Any]]] = {
        "test": [{"label": "bare-test"}],
        "v1": [{"label": "bare-v1"}],
    }

    # Sentinel: distinguishes "load() never called" from "load(params=None)".
    last_params_was_none: bool | None = None

    def versions(self) -> list[str]:
        return list(self._DATASET.keys())

    def populates_types(self) -> list[str]:
        return ["BareTerm"]

    def schema_fragment(self) -> dict:
        return {
            "id": "https://example.org/mosaic/bare",
            "name": "bare",
            "default_prefix": "bare",
            "prefixes": {"bare": "https://example.org/mosaic/bare/"},
            "classes": {
                "BareTerm": {
                    "is_a": "Entity",
                    "attributes": {
                        "label": {"range": "string", "required": True},
                    },
                },
            },
        }

    def load(
        self,
        client: "MosaicClient",
        version: str,
        params: BaseModel | None = None,
    ) -> LoadResult:
        type(self).last_params_was_none = params is None
        rows = self._DATASET.get(version, [])
        entities: list[EntityRef] = [
            EntityRef.from_put_result(client.put("BareTerm", dict(row)))
            for row in rows
        ]
        return LoadResult(
            created=len(entities),
            entities=entities,
        )


# ---------------------------------------------------------------------------
# Pure-schema ``SchemaPackage`` (genus, no data hooks) — Doc 2 §2A / S0.
# Registered under the ``mosaic.schema_packages`` entry-point group only,
# so the test suite can prove the genus path: discovery resolves it, its
# fragment merges, and it pins via ``requires:`` — all with **no**
# hand-written ``load()`` / ``provision()`` (the lifecycle hooks stay the
# genus no-op).
# ---------------------------------------------------------------------------


class FakeSchemaPackage(SchemaPackage):
    """A pure-schema package: contributes a versioned fragment, no data.

    Implements only the two abstract genus methods (:meth:`versions` and
    :meth:`schema_fragment`); ``provision``/``evolve``/``deprovision``
    inherit the genus no-op. Exercises the "hand-written no-op ``load()``
    disappears" acceptance criterion (Doc 2 §2A / §9 S0).
    """

    name = "fake_schema"
    description = "Pure-schema SchemaPackage with no data hooks (test fixture)"

    def versions(self) -> list[str]:
        return ["test", "v1"]

    def schema_fragment(self) -> dict:
        return {
            "id": "https://example.org/mosaic/fake_schema",
            "name": "fake_schema",
            "default_prefix": "fake_schema",
            "prefixes": {
                "fake_schema": "https://example.org/mosaic/fake_schema/"
            },
            "classes": {
                "FakeSchemaTerm": {
                    "is_a": "Entity",
                    "attributes": {
                        "label": {"range": "string", "required": True},
                    },
                },
            },
        }


# ---------------------------------------------------------------------------
# Loader-provided Typer sub-app (D2.14.A / PTS-228). Registered via the
# ``mosaic.reference_loader_cli`` entry point under the same key as the
# loader (`fake`), Mosaic mounts it as ``mosaic reference fake ...``.
# ---------------------------------------------------------------------------


fake_cli_app = typer.Typer(
    name="fake",
    help="Fake reference loader subcommands (test fixture).",
)


@fake_cli_app.command(name="echo")
def _fake_echo(
    message: str = typer.Argument("hello", help="Message to echo."),
) -> None:
    """Echo a message — exercises the mounted sub-app code path."""
    typer.echo(message)


@fake_cli_app.command(name="cache-path")
def _fake_cache_path() -> None:
    """Print the cache directory the loader would use.

    Resolved via the canonical :meth:`MosaicClient.cache_dir_for` path so
    the sub-app and ``load()`` agree on the same per-loader cache root
    (PTS-228 acceptance: same ``cache_dir_for('fake')``). The subcommand
    instantiates an in-memory ``MosaicClient`` because cache resolution
    is stateless and doesn't depend on the on-disk database.
    """
    from mosaic.core.client import MosaicClient
    from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
    from mosaic.linkml_bridge import SchemaRegistry
    from linkml_runtime.utils.schemaview import SchemaView
    import importlib.resources

    mosaic_core_path = importlib.resources.files("mosaic.schemas").joinpath(
        "mosaic_core.yaml"
    )
    registry = SchemaRegistry(SchemaView(str(mosaic_core_path)))
    storage = SQLiteAdapter(":memory:", schema_registry=registry)
    client = MosaicClient(storage=storage, registry=registry)
    typer.echo(str(client.cache_dir_for("fake")))
