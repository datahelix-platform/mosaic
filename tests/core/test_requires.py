"""Tests for the schema-level ``requires:`` directive (PTS-227).

Covers parsing (``mosaic.requires.parse_requires`` /
``extract_requires``) and installation cross-checking
(``check_requires``). The CLI integration is exercised separately in
``tests/cli/test_validate_requires.py``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from mosaic.core.exceptions import SchemaError
from mosaic.requires import (
    V1_RANGE_REJECT_MESSAGE,
    RequirePin,
    check_requires,
    extract_requires,
    parse_requires,
)


# ---------------------------------------------------------------------------
# parse_requires — pin shape + v1 range-comparator reject
# ---------------------------------------------------------------------------


class TestParseRequires:
    def test_none_returns_empty(self):
        assert parse_requires(None) == []

    def test_exact_match_pin(self):
        pins = parse_requires(["mosaic-reference-fma==3.3"])
        assert pins == [
            RequirePin(package_name="mosaic-reference-fma", version="3.3")
        ]

    def test_data_version_slug_pin(self):
        pins = parse_requires(
            ["mosaic-reference-ensembl==mus_musculus.GRCm39.115"]
        )
        assert pins == [
            RequirePin(
                package_name="mosaic-reference-ensembl",
                version="mus_musculus.GRCm39.115",
            )
        ]

    def test_multiple_pins_preserve_order(self):
        pins = parse_requires(
            [
                "mosaic-reference-fma==3.3",
                "mosaic-reference-ensembl==v115",
            ]
        )
        assert [p.package_name for p in pins] == [
            "mosaic-reference-fma",
            "mosaic-reference-ensembl",
        ]

    def test_non_list_value_raises(self):
        with pytest.raises(SchemaError) as exc:
            parse_requires("mosaic-reference-fma==3.3")
        assert exc.value.field_name == "requires"
        assert "must be a list" in exc.value.message

    def test_non_string_entry_raises(self):
        with pytest.raises(SchemaError) as exc:
            parse_requires([{"name": "mosaic-reference-fma"}])
        assert "must be a string pin" in exc.value.message

    def test_malformed_pin_raises(self):
        with pytest.raises(SchemaError) as exc:
            parse_requires(["mosaic-reference-fma"])
        assert "malformed" in exc.value.message

    @pytest.mark.parametrize(
        "pin",
        [
            "mosaic-reference-fma>=3.3",
            "mosaic-reference-fma~=3.3",
            "mosaic-reference-fma^=3.3",
            "mosaic-reference-fma<3.3",
            "mosaic-reference-fma>3.3",
        ],
    )
    def test_range_comparator_rejected_with_v1_message(self, pin: str):
        with pytest.raises(SchemaError) as exc:
            parse_requires([pin])
        assert exc.value.message == V1_RANGE_REJECT_MESSAGE
        assert exc.value.field_name == "requires"

    def test_le_and_ne_also_rejected(self):
        # Spec lists >=, ~=, ^=, <, > explicitly; <= and != follow the
        # same v1 deferral.
        for pin in ["mosaic-reference-fma<=3.3", "mosaic-reference-fma!=3.3"]:
            with pytest.raises(SchemaError) as exc:
                parse_requires([pin])
            assert exc.value.message == V1_RANGE_REJECT_MESSAGE

    def test_short_name_strips_convention_prefix(self):
        pin = RequirePin(package_name="mosaic-reference-fma", version="3.3")
        assert pin.short_name == "fma"

    def test_short_name_passes_through_non_convention_name(self):
        pin = RequirePin(package_name="some-other-pkg", version="1.0")
        assert pin.short_name == "some-other-pkg"

    def test_short_name_keeps_retired_hippo_prefix(self):
        # ADR-0015: the legacy ``hippo-reference-`` prefix is no longer
        # recognized, so the name passes through unchanged.
        pin = RequirePin(package_name="hippo-reference-fma", version="3.3")
        assert pin.short_name == "hippo-reference-fma"


# ---------------------------------------------------------------------------
# extract_requires — file/dir reading
# ---------------------------------------------------------------------------


class TestExtractRequires:
    def test_file_without_requires_returns_empty(self, tmp_path: Path):
        schema = tmp_path / "schema.yaml"
        schema.write_text("id: https://example.org/x\nname: x\n")
        assert extract_requires(schema) == []

    def test_file_with_requires_returns_pins(self, tmp_path: Path):
        schema = tmp_path / "schema.yaml"
        schema.write_text(
            "id: https://example.org/x\nname: x\n"
            "requires:\n"
            "  - mosaic-reference-fma==3.3\n"
        )
        assert extract_requires(schema) == [
            RequirePin(package_name="mosaic-reference-fma", version="3.3")
        ]

    def test_directory_accumulates_pins_across_files(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text(
            "id: https://example.org/a\nname: a\n"
            "requires:\n  - mosaic-reference-fma==3.3\n"
        )
        (tmp_path / "b.yaml").write_text(
            "id: https://example.org/b\nname: b\n"
            "requires:\n  - mosaic-reference-ensembl==v115\n"
        )
        pins = extract_requires(tmp_path)
        assert {p.package_name for p in pins} == {
            "mosaic-reference-fma",
            "mosaic-reference-ensembl",
        }

    def test_directory_with_no_requires_returns_empty(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text("id: https://example.org/a\nname: a\n")
        assert extract_requires(tmp_path) == []

    def test_range_comparator_in_file_raises(self, tmp_path: Path):
        schema = tmp_path / "schema.yaml"
        schema.write_text(
            "id: https://example.org/x\nname: x\n"
            "requires:\n  - mosaic-reference-fma>=3.3\n"
        )
        with pytest.raises(SchemaError) as exc:
            extract_requires(schema)
        assert exc.value.message == V1_RANGE_REJECT_MESSAGE


# ---------------------------------------------------------------------------
# check_requires — installed-distribution cross-check
# ---------------------------------------------------------------------------


class TestCheckRequires:
    def test_empty_pins_returns_no_errors(self):
        assert check_requires([]) == []

    def test_exact_match_pass(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "mosaic.requires._dist_version", lambda name: "3.3"
        )
        errors = check_requires(
            [RequirePin(package_name="mosaic-reference-fma", version="3.3")]
        )
        assert errors == []

    def test_missing_loader_gives_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def _missing(name: str) -> str:
            raise PackageNotFoundError(name)

        monkeypatch.setattr("mosaic.requires._dist_version", _missing)
        errors = check_requires(
            [RequirePin(package_name="mosaic-reference-fma", version="3.3")]
        )
        assert len(errors) == 1
        msg = errors[0]
        assert "requires mosaic-reference-fma" in msg
        assert "is not installed" in msg
        assert "mosaic reference install fma --version 3.3" in msg

    def test_version_mismatch_gives_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "mosaic.requires._dist_version", lambda name: "3.2"
        )
        errors = check_requires(
            [RequirePin(package_name="mosaic-reference-fma", version="3.3")]
        )
        assert len(errors) == 1
        msg = errors[0]
        assert "requires mosaic-reference-fma==3.3" in msg
        assert "but version 3.2 is installed" in msg
        assert "mosaic reference install fma --version 3.3" in msg

    def test_pure_schema_package_pins_like_any_other(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Acceptance (§9 S0): a pure-schema SchemaPackage *pins via
        # requires:*. The version-pin gate is package-name agnostic — a
        # schema package (no ``mosaic-reference-`` prefix) resolves
        # identically to a reference loader once installed.
        monkeypatch.setattr("mosaic.requires._dist_version", lambda name: "1.0")
        ok = check_requires(
            [RequirePin(package_name="mosaic-schema-fake", version="1.0")]
        )
        assert ok == []

        # A mismatch on the same pure-schema package still fails loud with
        # an install hint, proving the gate is live for it.
        monkeypatch.setattr("mosaic.requires._dist_version", lambda name: "0.9")
        errors = check_requires(
            [RequirePin(package_name="mosaic-schema-fake", version="1.0")]
        )
        assert len(errors) == 1
        assert "mosaic-schema-fake==1.0" in errors[0]
        assert "but version 0.9 is installed" in errors[0]

    def test_mixed_results_collected(self, monkeypatch: pytest.MonkeyPatch):
        def _per_pkg(name: str) -> str:
            if name == "mosaic-reference-fma":
                return "3.3"  # pass
            if name == "mosaic-reference-ensembl":
                return "wrong"  # mismatch
            raise PackageNotFoundError(name)  # missing

        monkeypatch.setattr("mosaic.requires._dist_version", _per_pkg)
        errors = check_requires(
            [
                RequirePin(package_name="mosaic-reference-fma", version="3.3"),
                RequirePin(
                    package_name="mosaic-reference-ensembl", version="v115"
                ),
                RequirePin(
                    package_name="mosaic-reference-missing", version="1.0"
                ),
            ]
        )
        assert len(errors) == 2
        assert any("mosaic-reference-ensembl" in m for m in errors)
        assert any("mosaic-reference-missing" in m for m in errors)
