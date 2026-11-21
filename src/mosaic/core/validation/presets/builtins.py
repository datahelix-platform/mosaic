"""Built-in preset validators for Mosaic.

This module provides five built-in presets:
- ref_check: Validates reference constraints
- count_constraint: Validates collection count limits
- immutable_field: Prevents field modification
- field_required_if: Enforces conditional field requirements
- no_self_ref: Prevents self-references
"""

from typing import Any, Callable

from mosaic.core.validation.presets import PresetConfig, PresetValidator
from mosaic.core.validation.presets.errors import ErrorMessageBuilder
from mosaic.core.validation.validators import ValidationResult, WriteOperation

REF_CHECK_PRESET = "mosaic:ref_check"
COUNT_CONSTRAINT_PRESET = "mosaic:count_constraint"
IMMUTABLE_FIELD_PRESET = "mosaic:immutable_field"
FIELD_REQUIRED_IF_PRESET = "mosaic:field_required_if"
NO_SELF_REF_PRESET = "mosaic:no_self_ref"


class RefCheckPreset(PresetValidator):
    """Preset validator for reference constraints.

    Validates that referenced entities exist when using the ref_check preset
    with a reference constraint.
    """

    def __init__(
        self,
        config: PresetConfig | None = None,
        entity_exists_fn: Callable[[str, str], bool] | None = None,
    ) -> None:
        """Initialize the ref_check preset.

        Args:
            config: The preset configuration.
            entity_exists_fn: Function to check if an entity exists.
                Signature: (entity_type: str, entity_id: str) -> bool
        """
        super().__init__(config)
        self._entity_exists_fn = entity_exists_fn

    @property
    def preset_type(self) -> str:
        return REF_CHECK_PRESET

    @property
    def priority(self) -> int:
        return 15

    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Validate the write operation for reference constraints.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult indicating success or failure with errors.
        """
        errors: list[str] = []
        data = operation.data
        entity_id = data.get("id") if data else None

        fields = self._config.config.get("fields", [])
        if not fields:
            return ValidationResult(is_valid=True, errors=[], entity_id=entity_id)

        for field_config in fields:
            field_path = field_config.get("field")
            if not field_path:
                continue

            entity_type = field_config.get("entity_type")
            ref_id = self._get_ref_id(data, field_path)

            if ref_id is not None and entity_type and self._entity_exists_fn:
                if not self._entity_exists_fn(entity_type, ref_id):
                    error_msg = ErrorMessageBuilder.reference_violation(
                        field_path, entity_type, ref_id
                    )
                    errors.append(error_msg)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            entity_id=str(entity_id) if entity_id else None,
        )

    def _get_ref_id(self, data: dict[str, Any], field_path: str) -> str | None:
        """Extract reference ID from data using dot-notation path."""
        value, _ = self._get_field_value(data, field_path)
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("id")
        return str(value)


class CountConstraintPreset(PresetValidator):
    """Preset validator for collection count limits.

    Validates that collection fields do not exceed the configured maximum count
    when using the count_constraint preset.
    """

    def __init__(
        self,
        config: PresetConfig | None = None,
    ) -> None:
        """Initialize the count_constraint preset.

        Args:
            config: The preset configuration.
        """
        super().__init__(config)

    @property
    def preset_type(self) -> str:
        return COUNT_CONSTRAINT_PRESET

    @property
    def priority(self) -> int:
        return 15

    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Validate the write operation for count constraints.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult indicating success or failure with errors.
        """
        errors: list[str] = []
        data = operation.data
        entity_id = data.get("id") if data else None

        fields = self._config.config.get("fields", [])
        if not fields:
            return ValidationResult(is_valid=True, errors=[], entity_id=entity_id)

        for field_config in fields:
            field_path = field_config.get("field")
            max_count = field_config.get("max_count")

            if not field_path or max_count is None:
                continue

            value, _ = self._get_field_value(data, field_path)

            if value is not None:
                if isinstance(value, list):
                    actual_count = len(value)
                elif isinstance(value, dict):
                    actual_count = len(value)
                else:
                    continue

                if actual_count > max_count:
                    error_msg = ErrorMessageBuilder.count_violation(
                        field_path, actual_count, max_count
                    )
                    errors.append(error_msg)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            entity_id=str(entity_id) if entity_id else None,
        )


class ImmutableFieldPreset(PresetValidator):
    """Preset validator for immutable fields.

    Rejects attempts to modify fields marked as immutable using the
    immutable_field preset.
    """

    def __init__(
        self,
        config: PresetConfig | None = None,
        original_data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the immutable_field preset.

        Args:
            config: The preset configuration.
            original_data: The original data for update operations.
        """
        super().__init__(config)
        self._original_data = original_data or {}

    @property
    def preset_type(self) -> str:
        return IMMUTABLE_FIELD_PRESET

    @property
    def priority(self) -> int:
        return 15

    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Validate the write operation for immutable field violations.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult indicating success or failure with errors.
        """
        errors: list[str] = []
        data = operation.data
        entity_id = data.get("id") if data else None

        fields = self._config.config.get("fields", [])
        if not fields:
            return ValidationResult(is_valid=True, errors=[], entity_id=entity_id)

        for field_config in fields:
            field_path = field_config.get("field")
            if not field_path:
                continue

            new_value, _ = self._get_field_value(data, field_path)
            old_value, _ = self._get_field_value(self._original_data, field_path)

            if old_value is not None and new_value is not None:
                if old_value != new_value:
                    error_msg = ErrorMessageBuilder.immutable_field(field_path)
                    errors.append(error_msg)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            entity_id=str(entity_id) if entity_id else None,
        )


class FieldRequiredIfPreset(PresetValidator):
    """Preset validator for conditional field requirements.

    Makes fields required when specified conditions are met using the
    field_required_if preset.
    """

    def __init__(
        self,
        config: PresetConfig | None = None,
    ) -> None:
        """Initialize the field_required_if preset.

        Args:
            config: The preset configuration.
        """
        super().__init__(config)

    @property
    def preset_type(self) -> str:
        return FIELD_REQUIRED_IF_PRESET

    @property
    def priority(self) -> int:
        return 15

    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Validate the write operation for required field violations.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult indicating success or failure with errors.
        """
        errors: list[str] = []
        data = operation.data
        entity_id = data.get("id") if data else None

        fields = self._config.config.get("fields", [])
        if not fields:
            return ValidationResult(is_valid=True, errors=[], entity_id=entity_id)

        for field_config in fields:
            field_path = field_config.get("field")
            condition_field = field_config.get("when_field")
            condition_value = field_config.get("when_value")

            if not field_path or not condition_field:
                continue

            condition_actual_value, _ = self._get_field_value(data, condition_field)
            field_value, _ = self._get_field_value(data, field_path)

            condition_met = condition_actual_value == condition_value
            if condition_met and field_value is None:
                condition_desc = f"{condition_field} == {condition_value}"
                error_msg = ErrorMessageBuilder.field_required(
                    field_path, condition_desc
                )
                errors.append(error_msg)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            entity_id=str(entity_id) if entity_id else None,
        )


class NoSelfRefPreset(PresetValidator):
    """Preset validator for preventing self-references.

    Rejects documents that contain self-references when using the
    no_self_ref preset.
    """

    def __init__(
        self,
        config: PresetConfig | None = None,
    ) -> None:
        """Initialize the no_self_ref preset.

        Args:
            config: The preset configuration.
        """
        super().__init__(config)

    @property
    def preset_type(self) -> str:
        return NO_SELF_REF_PRESET

    @property
    def priority(self) -> int:
        return 15

    def validate(self, operation: WriteOperation) -> ValidationResult:
        """Validate the write operation for self-reference violations.

        Args:
            operation: The write operation to validate.

        Returns:
            ValidationResult indicating success or failure with errors.
        """
        errors: list[str] = []
        data = operation.data
        entity_id = data.get("id") if data else None

        if entity_id is None:
            return ValidationResult(is_valid=True, errors=[], entity_id=None)

        fields = self._config.config.get("fields", [])
        if not fields:
            return ValidationResult(is_valid=True, errors=[], entity_id=str(entity_id))

        for field_config in fields:
            field_path = field_config.get("field")
            if not field_path:
                continue

            value, _ = self._get_field_value(data, field_path)

            if self._contains_self_reference(value, str(entity_id)):
                error_msg = ErrorMessageBuilder.self_reference(
                    field_path, str(entity_id)
                )
                errors.append(error_msg)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            entity_id=str(entity_id) if entity_id else None,
        )

    def _contains_self_reference(self, value: Any, entity_id: str) -> bool:
        """Check if a value contains a self-reference."""
        if value is None:
            return False
        if isinstance(value, str):
            return value == entity_id
        if isinstance(value, dict):
            ref_id = value.get("id")
            if ref_id == entity_id:
                return True
            return any(
                self._contains_self_reference(v, entity_id) for v in value.values()
            )
        if isinstance(value, list):
            return any(self._contains_self_reference(item, entity_id) for item in value)
        return False


def register_presets() -> None:
    """Register all built-in presets with the preset registry."""
    from mosaic.core.validation.presets import get_preset_registry

    registry = get_preset_registry()
    registry.register(REF_CHECK_PRESET, RefCheckPreset)
    registry.register(COUNT_CONSTRAINT_PRESET, CountConstraintPreset)
    registry.register(IMMUTABLE_FIELD_PRESET, ImmutableFieldPreset)
    registry.register(FIELD_REQUIRED_IF_PRESET, FieldRequiredIfPreset)
    registry.register(NO_SELF_REF_PRESET, NoSelfRefPreset)
