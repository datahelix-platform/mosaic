#!/usr/bin/env python3
"""Merged-schema closure integrity check (sec11 §11.6.3).

CI guard: proves that the schema-merge machinery detects structural
incoherence before it ships.  Run this after installing mosaic:

    python scripts/check_schema_closure.py

Exits 0 if all checks pass; exits 1 and names the offending element(s)
if any check fails.

Checks
------
1. mosaic_core base schema — every class's induced-slot set resolves.
2. Valid synthetic fragment merges cleanly — the merge path exercises all
   three merge layers without errors.
3. Incoherent fragment is caught (negative gate) — a class with a dangling
   ``is_a`` surfaces a failure naming the offending class and the missing
   base.  This proves the guard is live and not permanently green.
4. Bundle manifest integrity — ``Bundle.from_manifest().to_requires()``
   generates exact-version (``==x.y.z``) pins for every package.

Why class_induced_slots?
------------------------
``mosaic validate --schema`` calls ``SchemaRegistry.from_path`` which loads
the LinkML schema but does not resolve every class's inheritance chain.
``_validate_mosaic_annotations`` wraps ``class_induced_slots`` in a
try/except that skips failures so the annotation checker stays tolerant.
Iterating ``class_induced_slots`` for every class is the reliable call that
surfaces a dangling ``is_a`` (or missing-range class) with a clear message
naming the class that caused the failure.
"""

from __future__ import annotations

import importlib.resources
import sys

from linkml_runtime.utils.schemaview import SchemaView

from mosaic.core.loaders.bundle import Bundle
from mosaic.linkml_bridge import LoaderFragmentSpec, SchemaRegistry, merge_loader_fragment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_base_registry() -> SchemaRegistry:
    mosaic_core_path = str(
        importlib.resources.files("mosaic.schemas").joinpath("mosaic_core.yaml")
    )
    sv = SchemaView(mosaic_core_path)
    return SchemaRegistry(sv)


def _merge(registry: SchemaRegistry, pkg_name: str, fragment: dict) -> SchemaRegistry:
    spec = LoaderFragmentSpec(
        loader_name=pkg_name,
        package_name=pkg_name,
        package_version="1.0.0",
        fragment=fragment,
    )
    merged_sv = merge_loader_fragment(registry._sv, spec)
    return SchemaRegistry(merged_sv)


def _validate_closure(registry: SchemaRegistry) -> list[str]:
    """Return error strings for every class whose induced-slot chain fails to resolve.

    A non-empty list names the offending element and why it failed
    (e.g. ``"BadClass: No such class: 'NoSuchBase'"``).
    """
    failures: list[str] = []
    for class_name in registry.class_names():
        try:
            registry._sv.class_induced_slots(class_name)
        except Exception as exc:
            failures.append(f"{class_name}: {exc}")
    return failures


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_base_schema() -> bool:
    """mosaic_core base schema — all class induced-slot chains resolve."""
    print("--- Check 1: mosaic_core base schema ---")
    reg = _load_base_registry()
    failures = _validate_closure(reg)
    if failures:
        print(f"FAIL: base schema has {len(failures)} structural error(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return False
    print(f"OK: {len(reg.class_names())} classes, all induced-slot resolutions pass")
    return True


def check_good_merged_closure() -> bool:
    """Valid synthetic fragment merges without error."""
    print("--- Check 2: merged closure with valid fragment ---")
    reg = _load_base_registry()
    good_fragment = {
        "default_prefix": "testpkg",
        "classes": {
            "Sample": {
                "is_a": "Entity",
                "attributes": {
                    "label": {"range": "string"},
                    "tissue_type": {"range": "string"},
                },
            },
            "BrainSample": {
                "is_a": "Sample",
                "attributes": {
                    "brain_region": {"range": "string"},
                },
            },
        },
    }
    merged = _merge(reg, "testpkg", good_fragment)
    failures = _validate_closure(merged)
    if failures:
        print(
            f"FAIL: merged closure has {len(failures)} structural error(s):",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return False
    print(f"OK: {len(merged.class_names())} classes, all induced-slot resolutions pass")
    return True


def check_incoherent_closure_is_caught() -> bool:
    """Negative gate: a dangling is_a is detected and the offending class is named.

    This check MUST fail on the incoherent closure and PASS (return True) when
    that failure is correctly surfaced.  If the validator always returns clean,
    this check fails — proving the guard is live.
    """
    print("--- Check 3: incoherent fragment detected (negative gate) ---")
    reg = _load_base_registry()
    bad_fragment = {
        "default_prefix": "badpkg",
        "classes": {
            "BadClass": {
                "is_a": "NoSuchBaseClass",
                "attributes": {"x": {"range": "string"}},
            }
        },
    }
    merged = _merge(reg, "badpkg", bad_fragment)
    failures = _validate_closure(merged)
    if not failures:
        print(
            "FAIL: expected structural incoherence to be detected, "
            "but the closure validated cleanly — the guard is not working.",
            file=sys.stderr,
        )
        return False
    for f in failures:
        print(f"Detected (expected): {f}")
    print("OK: negative gate caught structural incoherence; offending element named")
    return True


def check_bundle_integrity() -> bool:
    """Bundle.from_manifest().to_requires() produces exact-version (==x.y.z) pins."""
    print("--- Check 4: bundle manifest integrity ---")
    bundle = Bundle.from_manifest(
        {
            "name": "brainbank-bundle",
            "version": "2024.1",
            "ontology_snapshot": "2024-01",
            "packages": {"core": "2.0.0", "subject": "1.4.0"},
            "coordinates": [{"core": "1.3.0"}, {"core": "1.4.0"}],
        }
    )
    requires = bundle.to_requires()
    errors: list[str] = []
    for pkg_name, pin in requires.items():
        # Exact-pin gate: every generated pin must be "==<version>"
        if not pin.startswith("==") or len(pin) < 4:
            errors.append(
                f"package {pkg_name!r}: invalid pin {pin!r} (expected ==<version>)"
            )
    if errors:
        print(f"FAIL: {len(errors)} malformed pin(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return False

    # Validate the merged closure for the bundle's packages using the same
    # two synthetic fragments from check_good_merged_closure.  The bundle says
    # "core" and "subject" must be installed; we exercise the merge path with
    # fragments that carry those default_prefix names.
    reg = _load_base_registry()
    core_fragment = {
        "default_prefix": "core",
        "classes": {
            "Sample": {"is_a": "Entity", "attributes": {"label": {"range": "string"}}},
        },
    }
    subject_fragment = {
        "default_prefix": "subject",
        "classes": {
            "Subject": {
                "is_a": "Entity",
                "attributes": {"external_id": {"range": "string"}},
            },
        },
    }
    merged = _merge(_merge(reg, "core", core_fragment), "subject", subject_fragment)
    failures = _validate_closure(merged)
    if failures:
        print(
            f"FAIL: bundle merged closure has {len(failures)} structural error(s):",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return False

    print(f"OK: {bundle.name!r} to_requires() = {requires}")
    print(f"OK: bundle merged closure ({len(merged.class_names())} classes) validates")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    checks = [
        check_base_schema,
        check_good_merged_closure,
        check_incoherent_closure_is_caught,
        check_bundle_integrity,
    ]
    results = []
    for check in checks:
        ok = check()
        results.append(ok)
        print()

    passed = sum(results)
    failed = len(results) - passed
    if failed:
        print(f"{failed}/{len(results)} check(s) FAILED.", file=sys.stderr)
        return 1
    print(f"All {passed} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
