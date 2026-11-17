"""Pytest configuration for tests."""

import sys
from pathlib import Path
import pytest

src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# Shared test-case mixins live in tests/support; rewrite their asserts so
# failures show the compared values (closure_fixture, #241).
pytest.register_assert_rewrite("tests.support.closure_fixture")


def _build_minimal_schema_registry():
    """Return a SchemaRegistry over hippo_core plus generic test classes.

    Plain helper (not a fixture) so that adapter constructions inside
    legacy test fixtures — which take ``db_path`` but no
    ``minimal_schema_registry`` — can build a registry inline without
    rewriting every fixture signature.

    PR 2.3 dropped the legacy ``entities`` blob-table fallback. Every
    ``entity_type`` an adapter writes must therefore correspond to a
    real per-class typed table in the schema; tests that previously
    relied on the fallback for arbitrary type names now need those
    classes to exist in the registry. This helper bundles a set of
    generic user-domain classes (``Sample``, ``Document``, ``Donor``,
    ``Project``, ``Study``, ``TestEntity``, ``OtherType``, etc.) as
    stand-ins so existing tests keep working.

    If you add a new test that writes a previously-unused
    ``entity_type``, either reuse one of the bundled classes above or
    extend the ``classes`` dict below to declare it.
    """
    import yaml
    from linkml_runtime.utils.schemaview import SchemaView

    from mosaic.linkml_bridge import SchemaRegistry, _bundled_importmap

    overlay = {
        "id": "https://example.org/hippo/test_minimal",
        "name": "test_minimal",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "hippo": "https://w3id.org/hippo/",
        },
        "imports": ["linkml:types", "hippo_core"],
        "default_range": "string",
        "classes": {
            cls: {
                "is_a": "Entity",
                "attributes": {
                    "name": {"range": "string"},
                    "tissue": {"range": "string"},
                    "title": {"range": "string"},
                    "description": {"range": "string"},
                    "notes": {"range": "string"},
                    "content": {"range": "string"},
                    "value": {"range": "string"},
                    "stage": {"range": "string"},
                    "category": {"range": "string"},
                    "diagnosis": {"range": "string"},
                    "external_id": {"range": "string"},
                    "label": {"range": "string"},
                    "status": {"range": "string"},
                },
            }
            for cls in (
                "Sample",
                "SampleEntity",
                "Document",
                "Collection",
                "Donor",
                "Project",
                "Study",
                "TestEntity",
                "OtherType",
                "ErrorEntity",
            )
        },
    }
    return SchemaRegistry(
        SchemaView(yaml.safe_dump(overlay), importmap=_bundled_importmap())
    )


@pytest.fixture
def minimal_schema_registry():
    """Provide a minimal SchemaRegistry using the bundled hippo_core schema.

    This fixture is useful for adapter tests that need a SchemaRegistry
    but don't require a custom user schema.
    """
    return _build_minimal_schema_registry()
