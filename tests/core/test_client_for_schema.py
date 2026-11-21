"""Consumer schema + installed reference loader → spanning client (issue #67).

Covers the public path that lets a consumer obtain a registry/client
spanning **its own schema + an installed reference loader** with zero
hand-assembled registry code:

- ``mosaic.core.loaders.discovery.fragment_specs_for_requires`` — resolve a
  schema's ``requires:`` pins to installed loader fragments;
- ``mosaic.core.factory.build_schema_registry(..., merge_requires=True)`` and
  the public ``mosaic.registry_for_schema`` / ``mosaic.client_for_schema``.

The ``fake`` reference loader (registered via the ``mosaic.reference_loaders``
entry point, shipping a ``FakeTerm`` class) stands in for a real
``mosaic-reference-*`` package. ``mosaic.requires._dist_version`` is
monkeypatched so the exact-match version gate treats ``fake`` as installed —
the gate compares against the *pip* distribution version, which a synthetic
entry-point loader has none of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mosaic
from mosaic.core.exceptions import SchemaError
from mosaic.core.factory import build_schema_registry
from mosaic.core.loaders.discovery import fragment_specs_for_requires


# Consumer schema: links its own ``Annotation`` to the loader's ``FakeTerm``
# via a slot ranged on that class, and declares the loader in ``requires:``.
_CONSUMER_SCHEMA = """\
id: https://example.org/consumer
name: consumer
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string
requires:
  - fake==v1
classes:
  Annotation:
    is_a: Entity
    attributes:
      note:
        range: string
      term:
        range: FakeTerm
"""


@pytest.fixture
def consumer_schema(tmp_path: Path) -> Path:
    schema = tmp_path / "schema.yaml"
    schema.write_text(_CONSUMER_SCHEMA)
    return schema


@pytest.fixture
def fake_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``fake`` loader pass the exact-match version gate."""
    monkeypatch.setattr("mosaic.requires._dist_version", lambda name: "v1")


# ---------------------------------------------------------------------------
# fragment_specs_for_requires — resolver
# ---------------------------------------------------------------------------


class TestFragmentSpecsForRequires:
    def test_no_requires_returns_empty(self, tmp_path: Path):
        schema = tmp_path / "s.yaml"
        schema.write_text("id: https://example.org/x\nname: x\n")
        assert fragment_specs_for_requires(schema) == []

    def test_resolves_pin_to_installed_fragment(
        self, consumer_schema: Path, fake_installed: None
    ):
        specs = fragment_specs_for_requires(consumer_schema)
        assert [s.loader_name for s in specs] == ["fake"]
        # The spec carries the loader's fragment (FakeTerm) ready to merge.
        assert "FakeTerm" in specs[0].fragment["classes"]

    def test_unsatisfied_gate_raises(self, consumer_schema: Path):
        # No monkeypatch — `fake` is not a pip distribution, so the gate fails.
        with pytest.raises(SchemaError) as exc:
            fragment_specs_for_requires(consumer_schema)
        assert exc.value.error_code == "REQUIRES_UNSATISFIED"

    def test_check_versions_false_skips_gate(
        self, consumer_schema: Path
    ):
        # Bypassing the gate still resolves the discoverable loader.
        specs = fragment_specs_for_requires(consumer_schema, check_versions=False)
        assert [s.loader_name for s in specs] == ["fake"]

    def test_installed_but_not_discoverable_warns_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A pin that passes the gate but exposes no entry point contributes
        # no fragment — a warning, not a hard error (the gate is the contract).
        monkeypatch.setattr("mosaic.requires._dist_version", lambda name: "3.3")
        schema = tmp_path / "s.yaml"
        schema.write_text(
            "id: https://example.org/x\nname: x\n"
            "requires:\n  - mosaic-reference-absent==3.3\n"
        )
        with pytest.warns(UserWarning, match="registers no discoverable schema"):
            specs = fragment_specs_for_requires(schema)
        assert specs == []


# ---------------------------------------------------------------------------
# build_schema_registry / registry_for_schema — spanning registry
# ---------------------------------------------------------------------------


class TestSpanningRegistry:
    def test_merge_requires_false_is_user_schema_only(
        self, consumer_schema: Path
    ):
        registry = build_schema_registry(consumer_schema, merge_requires=False)
        assert registry.has_class("Annotation")
        assert not registry.has_class("FakeTerm")
        # The cross-loader range is unknown ⇒ not recognized as a reference.
        assert ("term", "FakeTerm") not in registry.reference_slots("Annotation")

    def test_merge_requires_spans_both(
        self, consumer_schema: Path, fake_installed: None
    ):
        registry = build_schema_registry(consumer_schema, merge_requires=True)
        assert registry.has_class("Annotation")
        assert registry.has_class("FakeTerm")
        # `range: FakeTerm` is now a recognized cross-loader reference slot.
        assert ("term", "FakeTerm") in registry.reference_slots("Annotation")

    def test_public_registry_for_schema(
        self, consumer_schema: Path, fake_installed: None
    ):
        registry = mosaic.registry_for_schema(consumer_schema)
        assert registry.has_class("Annotation")
        assert registry.has_class("FakeTerm")


# ---------------------------------------------------------------------------
# client_for_schema — spanning client (the issue's headline use case)
# ---------------------------------------------------------------------------


class TestClientForSchema:
    def test_client_registry_spans_both(
        self, consumer_schema: Path, fake_installed: None, tmp_path: Path
    ):
        client = mosaic.client_for_schema(
            consumer_schema, database_url=str(tmp_path / "consumer.db")
        )
        assert client.registry.has_class("Annotation")
        assert client.registry.has_class("FakeTerm")

    def test_put_and_join_across_loader_boundary(
        self, consumer_schema: Path, fake_installed: None, tmp_path: Path
    ):
        # The DESeq2→Gene scenario in miniature: write a loader entity, write a
        # consumer entity that links to it, read both back through one client.
        client = mosaic.client_for_schema(
            consumer_schema, database_url=str(tmp_path / "consumer.db")
        )
        term = client.put("FakeTerm", {"label": "alpha"})
        annotation = client.put(
            "Annotation", {"note": "links to alpha", "term": term["id"]}
        )

        got_term = client.get("FakeTerm", term["id"])
        got_ann = client.get("Annotation", annotation["id"])
        assert got_term["data"]["label"] == "alpha"
        assert got_ann["data"]["term"] == term["id"]

    def test_uninstalled_loader_fails_fast(self, consumer_schema: Path, tmp_path: Path):
        # No monkeypatch ⇒ the gate fails and client construction raises,
        # mirroring `mosaic validate`.
        with pytest.raises(SchemaError) as exc:
            mosaic.client_for_schema(
                consumer_schema, database_url=str(tmp_path / "x.db")
            )
        assert exc.value.error_code == "REQUIRES_UNSATISFIED"


# ---------------------------------------------------------------------------
# Layer 3 — loader-prefixed CURIE ranges (`range: <loader>:<Class>`)
# ---------------------------------------------------------------------------


# Consumer schema using the documented loader-prefixed CURIE range form.
_CURIE_SCHEMA = """\
id: https://example.org/consumer
name: consumer
prefixes:
  linkml: https://w3id.org/linkml/
imports:
  - linkml:types
  - mosaic_core
default_range: string
requires:
  - fake==v1
classes:
  Annotation:
    is_a: Entity
    attributes:
      term:
        range: fake:FakeTerm
"""


class TestLoaderPrefixedRanges:
    def test_curie_range_resolves_to_merged_class(
        self, tmp_path: Path, fake_installed: None
    ):
        schema = tmp_path / "schema.yaml"
        schema.write_text(_CURIE_SCHEMA)
        registry = build_schema_registry(schema, merge_requires=True)

        # `range: fake:FakeTerm` is rewritten to the bare merged class, so the
        # slot is now a recognized cross-loader reference (non-advisory).
        assert registry.has_class("FakeTerm")
        assert ("term", "FakeTerm") in registry.reference_slots("Annotation")
        # The CURIE form no longer leaks through as an opaque range.
        term_slot = next(
            s for s in registry.induced_slots("Annotation") if s.name == "term"
        )
        assert term_slot.range == "FakeTerm"

    def test_loader_to_loader_curie_stays_advisory(self):
        # A loader-owned slot referencing another loader's class keeps its
        # CURIE range untouched — cross-loader FKs are advisory in v1
        # (decision D2.14.H). Drive the resolver directly with a synthetic
        # merged schema: a consumer class plus two provided_by-stamped loader
        # classes that reference each other by CURIE.
        from linkml_runtime.utils.schemaview import SchemaView

        from mosaic.linkml_bridge import _resolve_loader_prefixed_ranges

        merged = """\
id: https://example.org/merged
name: merged
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
classes:
  Annotation:                       # consumer class — no provided_by
    attributes:
      gene:
        range: ensembl:Gene
  Gene:                             # loader-owned (ensembl)
    annotations:
      provided_by: {value: 'ensembl@1'}
    attributes:
      region:
        range: fma:Region           # loader-to-loader CURIE — stays advisory
  Region:                           # loader-owned (fma)
    annotations:
      provided_by: {value: 'fma@1'}
"""
        resolved = _resolve_loader_prefixed_ranges(SchemaView(merged))
        classes = resolved.schema.classes
        # Consumer reference is rewritten to the bare merged class.
        assert classes["Annotation"].attributes["gene"].range == "Gene"
        # Loader-owned reference to another loader's class is untouched.
        assert classes["Gene"].attributes["region"].range == "fma:Region"

    def test_bare_range_unaffected(
        self, consumer_schema: Path, fake_installed: None
    ):
        # The bare-class-name form keeps working (no CURIE to resolve).
        registry = build_schema_registry(consumer_schema, merge_requires=True)
        assert ("term", "FakeTerm") in registry.reference_slots("Annotation")
