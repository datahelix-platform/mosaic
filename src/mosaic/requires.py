"""Schema-level ``requires:`` directive (v1 exact-match only).

Implements decision D2.14.C from ``hippo/design/sec2_architecture.md`` §2.14.1.
A user schema declares the reference-loader packages it depends on::

    # schema.yaml
    requires:
      - mosaic-reference-fma==3.3
      - mosaic-reference-ensembl==mus_musculus.GRCm39.115

v1 accepts only exact-match (``==``) pins. Range comparators (``>=``,
``~=``, ``^=``, ``<``, ``>``, ``<=``, ``!=``) raise :class:`SchemaError`.

``mosaic validate`` cross-checks each pin against the installed Python
distributions (``importlib.metadata``) and fails fast with an install
hint when a loader is missing or its version disagrees with the pin.

v1 deferral — installed-version source
--------------------------------------
The v1 check compares the pin's RHS against ``importlib.metadata.version
(<package>)``. That works for pins of the form ``mosaic-reference-fma==3.3``
where the package version *is* the load-bearing version. Spec §2.14 step 5
ultimately wants pins to be matched against the **data version slug**
recorded in ``mosaic_meta.reference_versions`` at install time (e.g.,
``mus_musculus.GRCm39.115``); that table is not populated yet because the
full reference-install lifecycle has not landed. Until it does, schemas
should pin against the pip version their loader publishes. The v2 surface
will swap the inner ``_dist_version`` lookup for a registry read without
changing the parser or CLI surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path
from typing import Any, Iterable

import yaml

from mosaic.core.exceptions import SchemaError

V1_RANGE_REJECT_MESSAGE = (
    "v1 supports only exact-match pins; "
    "use ==<version> and pin the lowest acceptable version"
)

# Pip packages distributing a reference loader follow this convention
# (``mosaic-reference-<name>``; the legacy ``hippo-reference-<name>`` prefix
# is no longer recognized — ADR-0015). The matching short name is what
# ``mosaic reference install`` accepts.
_LOADER_PACKAGE_PREFIXES = ("mosaic-reference-",)

# Tokens that introduce a non-exact-match comparator. ``==`` is the only
# accepted operator. ``<=`` and ``!=`` are not in the spec's enumeration
# but follow the same v1 deferral; treating them identically keeps the
# rejection surface coherent.
_RANGE_OPERATORS: tuple[str, ...] = (">=", "<=", "~=", "^=", "!=", "<", ">")

_PIN_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)
    \s*==\s*
    (?P<version>\S+?)
    \s*$
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class RequirePin:
    """A single parsed ``<package>==<version>`` requirement pin."""

    package_name: str
    version: str

    @property
    def short_name(self) -> str:
        """Name accepted by ``mosaic reference install``.

        Strips the ``mosaic-reference-`` convention prefix if present;
        otherwise returns the package name unchanged.
        """
        for prefix in _LOADER_PACKAGE_PREFIXES:
            if self.package_name.startswith(prefix):
                return self.package_name[len(prefix):]
        return self.package_name


def parse_requires(raw: Any) -> list[RequirePin]:
    """Parse the raw ``requires:`` value from a schema YAML document.

    ``raw`` is the value of the top-level ``requires:`` key (the result
    of ``yaml.safe_load``). ``None`` and missing keys yield an empty
    list. Anything else must be a list of pin strings.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SchemaError(
            "`requires:` must be a list of '<loader-name>==<version>' pins, "
            f"got {type(raw).__name__}",
            field_name="requires",
        )

    pins: list[RequirePin] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str):
            raise SchemaError(
                f"`requires[{index}]` must be a string pin, "
                f"got {type(entry).__name__}",
                field_name="requires",
            )

        if _has_range_operator(entry):
            raise SchemaError(
                V1_RANGE_REJECT_MESSAGE,
                field_name="requires",
            )

        match = _PIN_PATTERN.match(entry)
        if not match:
            raise SchemaError(
                f"`requires[{index}]` is malformed: expected "
                f"'<loader-name>==<version>', got {entry!r}",
                field_name="requires",
            )
        pins.append(
            RequirePin(
                package_name=match.group("name"),
                version=match.group("version"),
            )
        )
    return pins


def extract_requires(schema_path: str | Path) -> list[RequirePin]:
    """Read ``requires:`` from a schema file or directory.

    For a directory, every ``*.yaml`` / ``*.yml`` file is scanned and
    pins are accumulated (matching ``SchemaRegistry._from_directory``'s
    merge model). Files without ``requires:`` contribute nothing.
    """
    path = Path(schema_path)
    if path.is_dir():
        pins: list[RequirePin] = []
        files = sorted(list(path.glob("*.yaml")) + list(path.glob("*.yml")))
        for file_path in files:
            pins.extend(_extract_from_file(file_path))
        return pins
    return _extract_from_file(path)


def _extract_from_file(path: Path) -> list[RequirePin]:
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise SchemaError(
            f"Failed to parse {path} while reading `requires:`: {exc}",
            field_name="requires",
        ) from exc
    if not isinstance(doc, dict):
        return []
    return parse_requires(doc.get("requires"))


def check_requires(pins: Iterable[RequirePin]) -> list[str]:
    """Verify each pin against the installed Python distributions.

    Returns a list of human-readable error messages — empty when every
    pin is satisfied. Each entry follows the format mandated by
    PTS-227 so ``mosaic validate`` can echo them directly.
    """
    errors: list[str] = []
    for pin in pins:
        try:
            installed = _dist_version(pin.package_name)
        except PackageNotFoundError:
            errors.append(
                f"requires {pin.package_name} but it is not installed. "
                f"Install with: mosaic reference install {pin.short_name} "
                f"--version {pin.version}"
            )
            continue
        if installed != pin.version:
            errors.append(
                f"requires {pin.package_name}=={pin.version} but version "
                f"{installed} is installed. "
                f"Install with: mosaic reference install {pin.short_name} "
                f"--version {pin.version}"
            )
    return errors


def _has_range_operator(entry: str) -> bool:
    # ``==`` is allowed but contains ``=``; strip both halves before
    # inspecting so a ``==`` pin doesn't trigger on its own ``=``.
    if "==" in entry:
        left, _, right = entry.partition("==")
        candidate = f"{left} {right}"
    else:
        candidate = entry
    return any(op in candidate for op in _RANGE_OPERATORS)
