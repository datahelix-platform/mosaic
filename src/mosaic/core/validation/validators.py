"""Validation module for Mosaic.

sec9 §9.9 formalizes a three-tier validation pipeline (LinkML → CEL →
Python plugin) with a unified ``ValidationResult`` envelope. Each
failure carries the tier that produced it so callers can surface
structured errors uniformly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field as _dc_field
from importlib.metadata import entry_points
from typing import Any, Iterable, Iterator, Literal, Optional

# sec9 §9.9 tier identifiers. LinkML-native shape/type/enum/unique
# constraints run first; CEL boolean expressions over entity data run
# second; Python plugin escape-hatches run last.
Tier = Literal["linkml", "cel", "python"]


@dataclass
class ValidationFailure:
    """A single validation failure carrying its producing tier.

    sec9 §9.9 envelope element. Every failure names its tier so
    callers — REST handlers, typed-client exceptions, batch-ingest
    reporters — can render tier-specific behavior uniformly.
    """

    tier: Tier
    rule: str
    message: str
    field: Optional[str] = None
    details: dict[str, Any] = _dc_field(default_factory=dict)

    def to_string(self) -> str:
        """Legacy string rendering. Kept for back-compat with callers that
        read ``ValidationResult.errors: list[str]``.
        """
        prefix = f"[{self.tier}:{self.rule}]"
        if self.field:
            return f"{prefix} {self.field}: {self.message}"
        return f"{prefix} {self.message}"


@dataclass
class ValidationResult:
    """Result of a validation operation — sec9 §9.9 envelope.

    Two views of the same failure set are preserved for backward
    compatibility:

    - ``failures: list[ValidationFailure]`` — the sec9 envelope shape
      with tier annotation (preferred for new code).
    - ``errors: list[str]`` — the legacy string list, populated for
      every failure.

    Either constructor form works:

    - ``ValidationResult(is_valid=False, errors=["..."])`` — legacy;
      errors are synthesized into ``failures`` with ``tier="python"``.
    - ``ValidationResult(failures=[ValidationFailure(...)])`` — new
      code; ``errors`` is derived from each failure's ``to_string()``.

    Attributes:
        is_valid: Whether validation passed. Alias: ``passed``.
        errors: Legacy string rendering of failures.
        failures: sec9 envelope entries with tier annotation.
        entity_id: Optional entity ID for context.
    """

    is_valid: bool
    errors: list[str] = _dc_field(default_factory=list)
    failures: list[ValidationFailure] = _dc_field(default_factory=list)
    entity_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.is_valid, bool):
            raise TypeError("is_valid must be a boolean")
        if isinstance(self.errors, str):
            self.errors = [self.errors]
        elif not isinstance(self.errors, Iterable):
            raise TypeError("errors must be an iterable")
        elif not isinstance(self.errors, list):
            self.errors = list(self.errors)
        if self.entity_id is not None and not isinstance(self.entity_id, str):
            raise TypeError("entity_id must be a string or None")

        # Reconcile the two views. If a caller supplied `failures`, make
        # sure `errors` reflects them (for back-compat readers) —
        # ``errors`` carries raw messages so legacy substring assertions
        # keep working; the tier prefix is reserved for
        # ``ValidationFailure.to_string()`` when it's explicitly
        # requested. If a caller supplied only `errors`, synthesize
        # `failures` with tier="python" as the default — legacy
        # write-validator plugins produce free-form strings and default
        # to the python tier under sec9 §9.9.
        if self.failures and not self.errors:
            self.errors = [f.message for f in self.failures]
        elif self.errors and not self.failures:
            self.failures = [
                ValidationFailure(tier="python", rule="legacy", message=e)
                for e in self.errors
            ]

    @property
    def passed(self) -> bool:
        """sec9 §9.9 spelling of ``is_valid``."""
        return self.is_valid

    def failures_for_tier(self, tier: Tier) -> list[ValidationFailure]:
        """Filter failures by producing tier."""
        return [f for f in self.failures if f.tier == tier]

    def to_envelope(self) -> dict[str, Any]:
        """Render the sec9 §9.9 envelope as a plain dict (REST body shape)."""
        return {
            "passed": self.is_valid,
            "failures": [
                {
                    "tier": f.tier,
                    "rule": f.rule,
                    "field": f.field,
                    "message": f.message,
                    "details": f.details,
                }
                for f in self.failures
            ],
        }


@dataclass
class BatchValidationResult:
    """Aggregated result of validating a *set* of write operations.

    Increment 1 of the batch unit-of-work (see BU-Neuromics/hippo#84): the
    whole-set dry-run validates a proposed group of related entities and
    reports per-entity outcomes **without writing anything**.

    Unlike the single-operation pipeline, the set is validated
    *aggregating* (not fail-fast): every operation is validated and its
    result retained, so a caller sees all problems across the set at once.

    Attributes:
        is_valid: True iff every per-entity result is valid.
        results: Per-operation ``ValidationResult`` in input order. Each
            result's ``entity_id`` identifies which operation it came from
            (provisional ids are assigned by ``validate_batch`` when an
            operation omits one).
    """

    is_valid: bool
    results: list["ValidationResult"] = _dc_field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.is_valid, bool):
            raise TypeError("is_valid must be a boolean")
        if not isinstance(self.results, list):
            raise TypeError("results must be a list")

    @property
    def passed(self) -> bool:
        """sec9 §9.9 spelling of ``is_valid``."""
        return self.is_valid

    @property
    def failures(self) -> list[ValidationFailure]:
        """All failures across the set, flattened (tier-annotated)."""
        out: list[ValidationFailure] = []
        for r in self.results:
            out.extend(r.failures)
        return out

    @property
    def errors(self) -> list[str]:
        """Legacy string rendering of every failure across the set."""
        out: list[str] = []
        for r in self.results:
            out.extend(r.errors)
        return out

    def invalid_results(self) -> list["ValidationResult"]:
        """The subset of per-entity results that failed validation."""
        return [r for r in self.results if not r.is_valid]

    def to_envelope(self) -> dict[str, Any]:
        """Render the batch as a plain dict (REST/GraphQL body shape)."""
        return {
            "passed": self.is_valid,
            "results": [
                {"entity_id": r.entity_id, **r.to_envelope()} for r in self.results
            ],
        }


@dataclass
class BatchWriteResult:
    """Result of an atomic multi-entity write (``MosaicClient.batch_put``).

    Increment 2 of the batch unit-of-work (BU-Neuromics/hippo#84). The set is
    validated as a whole, then — if valid and not a dry run — every entity (and
    any intra-batch relationship) is written inside a single
    ``staged_transaction`` so the group commits all-or-nothing.

    Attributes:
        committed: True iff the whole set was written and committed. False on a
            validation failure or a dry run (nothing was written in either case).
        dry_run: True if the caller requested a dry run — the set was validated
            and a write plan computed, but storage was not touched.
        validation: The whole-set ``BatchValidationResult`` (always populated).
        entities: On commit, the per-operation result dicts in input order. On a
            valid dry run, the planned ``{id, entity_type, operation}`` per op.
            On a validation failure, empty.
        relationships: On commit, the created relationship dicts (empty otherwise).
    """

    committed: bool
    dry_run: bool
    validation: "BatchValidationResult"
    entities: list[dict[str, Any]] = _dc_field(default_factory=list)
    relationships: list[dict[str, Any]] = _dc_field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.committed, bool):
            raise TypeError("committed must be a boolean")
        if not isinstance(self.dry_run, bool):
            raise TypeError("dry_run must be a boolean")

    @property
    def is_valid(self) -> bool:
        """Whether the whole-set validation passed."""
        return self.validation.is_valid


@dataclass
class WriteOperation:
    """Represents a write operation to be validated.

    Attributes:
        operation: Type of operation (insert, update, delete).
        entity_type: The type of entity being operated on.
        data: The data to be written.
    """

    operation: str
    entity_type: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str):
            raise TypeError("operation must be a string")
        if not isinstance(self.entity_type, str):
            raise TypeError("entity_type must be a string")
        if not isinstance(self.data, dict):
            raise TypeError("data must be a dictionary")


class WriteValidator(ABC):
    """Abstract base class for write operation validators.

    Subclasses must implement the validate method.
    """

    @property
    def priority(self) -> int:
        """Priority of the validator.

        Higher priority validators execute first.
        Defaults to 0.
        """
        return 0

    @property
    def tier(self) -> Tier:
        """Validation tier this validator runs under (sec9 §9.9).

        Defaults to ``"python"`` — the escape-hatch tier. LinkML-native
        and CEL validators override to return ``"linkml"`` / ``"cel"``
        so pipeline ordering and failure tagging are correct.
        """
        return "python"

    @abstractmethod
    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Validate a write operation.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult indicating success or failure with errors.
        """
        ...


# Only ``mosaic.write_validators`` is read; the legacy ``hippo.*`` group was
# removed by ADR-0015.
ENTRY_POINT_GROUPS = ("mosaic.write_validators",)
#: Canonical group name.
ENTRY_POINT_GROUP = ENTRY_POINT_GROUPS[0]


class ValidatorRegistry:
    """Registry for discovering and managing write validators via entry points.

    Discovers validators registered via the 'mosaic.write_validators' entry
    point group and orders
    them by priority (highest first).
    """

    def __init__(self) -> None:
        self._validators: list[WriteValidator] = []
        self._discovered = False

    def _discover_validators(self) -> None:
        """Discover validators from entry points."""
        self._validators = []
        eps = entry_points()
        validator_eps = []
        seen: set[str] = set()
        for group in ENTRY_POINT_GROUPS:
            try:
                if hasattr(eps, "select"):
                    group_eps = list(eps.select(group=group))
                else:
                    group_eps = list(eps.get(group, []))  # type: ignore[union-attr]
            except TypeError:
                group_eps = []
            for ep in group_eps:
                if ep.name in seen:
                    continue
                seen.add(ep.name)
                validator_eps.append(ep)

        for ep in validator_eps:
            try:
                validator = ep.load()
                self._validators.append(validator())
            except Exception:
                pass

        self._validators.sort(key=lambda v: v.priority, reverse=True)
        self._discovered = True

    def get_validators(self) -> list[WriteValidator]:
        """Get all discovered validators ordered by priority (highest first).

        Returns:
            List of WriteValidator instances ordered by priority descending.
        """
        if not self._discovered:
            self._discover_validators()
        return self._validators

    def discover(self) -> list[WriteValidator]:
        """Force rediscovery of validators from entry points.

        Returns:
            List of WriteValidator instances ordered by priority descending.
        """
        self._discovered = False
        return self.get_validators()


class ValidatorPipeline:
    """Pipeline for executing validators in priority order.

    Executes all registered validators in order (highest priority first)
    and aggregates results.
    """

    def __init__(self, registry: ValidatorRegistry | None = None) -> None:
        self._registry = registry or ValidatorRegistry()

    def execute(self, operation: WriteOperation) -> list[ValidationResult]:
        """Execute all validators in priority order.

        Args:
            operation: The write operation to validate.

        Returns:
            List of ValidationResult from each validator (in execution order).
        """
        results: list[ValidationResult] = []
        validators = self._registry.get_validators()
        for validator in validators:
            result = validator.validate(operation)
            results.append(result)
        return results

    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Execute all validators and aggregate results.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult with aggregated success/failure and all errors.
        """
        results = self.execute(operation)
        all_failures: list[ValidationFailure] = []
        for result in results:
            all_failures.extend(result.failures)
        return ValidationResult(
            is_valid=all(r.is_valid for r in results),
            failures=all_failures,
        )
