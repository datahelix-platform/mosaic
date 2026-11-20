"""Tests for built-in preset validators."""

import pytest

from mosaic.core.validation import (
    PresetConfig,
    RefCheckPreset,
    CountConstraintPreset,
    ImmutableFieldPreset,
    FieldRequiredIfPreset,
    NoSelfRefPreset,
    WriteOperation,
    register_presets,
)


register_presets()


class TestRefCheckPreset:
    """Tests for RefCheckPreset."""

    def test_valid_reference_passes(self):
        """Valid reference passes validation."""
        config = PresetConfig(
            preset_type="mosaic:ref_check",
            config={"fields": [{"field": "owner", "entity_type": "User"}]},
        )

        def entity_exists(entity_type, entity_id):
            return entity_id in ["user-1", "user-2"]

        validator = RefCheckPreset(config, entity_exists_fn=entity_exists)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "owner": "user-1"},
        )
        result = validator.validate(op)

        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_invalid_reference_fails(self):
        """Invalid reference fails validation."""
        config = PresetConfig(
            preset_type="mosaic:ref_check",
            config={"fields": [{"field": "owner", "entity_type": "User"}]},
        )

        def entity_exists(entity_type, entity_id):
            return entity_id in ["user-1", "user-2"]

        validator = RefCheckPreset(config, entity_exists_fn=entity_exists)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "owner": "user-999"},
        )
        result = validator.validate(op)

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "Reference constraint violation" in result.errors[0]
        assert "user-999" in result.errors[0]

    def test_reference_with_nested_object(self):
        """Nested object reference is validated."""
        config = PresetConfig(
            preset_type="mosaic:ref_check",
            config={"fields": [{"field": "owner", "entity_type": "User"}]},
        )

        def entity_exists(entity_type, entity_id):
            return entity_id in ["user-1"]

        validator = RefCheckPreset(config, entity_exists_fn=entity_exists)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "owner": {"id": "user-1"}},
        )
        result = validator.validate(op)

        assert result.is_valid is True


class TestCountConstraintPreset:
    """Tests for CountConstraintPreset."""

    def test_valid_count_passes(self):
        """Valid count passes validation."""
        config = PresetConfig(
            preset_type="mosaic:count_constraint",
            config={"fields": [{"field": "items", "max_count": 5}]},
        )

        validator = CountConstraintPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "items": ["a", "b", "c"]},
        )
        result = validator.validate(op)

        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_exceeds_count_fails(self):
        """Exceeding count fails validation."""
        config = PresetConfig(
            preset_type="mosaic:count_constraint",
            config={"fields": [{"field": "items", "max_count": 5}]},
        )

        validator = CountConstraintPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "items": ["a", "b", "c", "d", "e", "f"]},
        )
        result = validator.validate(op)

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "Count constraint violation" in result.errors[0]
        assert "exceeds maximum" in result.errors[0]

    def test_empty_collection_passes(self):
        """Empty collection passes validation."""
        config = PresetConfig(
            preset_type="mosaic:count_constraint",
            config={"fields": [{"field": "items", "max_count": 5}]},
        )

        validator = CountConstraintPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "items": []},
        )
        result = validator.validate(op)

        assert result.is_valid is True


class TestImmutableFieldPreset:
    """Tests for ImmutableFieldPreset."""

    def test_field_not_modified_passes(self):
        """Unmodified field passes validation."""
        config = PresetConfig(
            preset_type="mosaic:immutable_field",
            config={"fields": [{"field": "id"}]},
        )

        validator = ImmutableFieldPreset(config, original_data={"id": "sample-1"})

        op = WriteOperation(
            operation="update",
            entity_type="Sample",
            data={"id": "sample-1", "name": "Updated"},
        )
        result = validator.validate(op)

        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_field_modified_fails(self):
        """Modified immutable field fails validation."""
        config = PresetConfig(
            preset_type="mosaic:immutable_field",
            config={"fields": [{"field": "id"}]},
        )

        validator = ImmutableFieldPreset(config, original_data={"id": "sample-1"})

        op = WriteOperation(
            operation="update",
            entity_type="Sample",
            data={"id": "sample-2", "name": "Updated"},
        )
        result = validator.validate(op)

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "Immutable field violation" in result.errors[0]


class TestFieldRequiredIfPreset:
    """Tests for FieldRequiredIfPreset."""

    def test_condition_met_field_present_passes(self):
        """Condition met with field present passes validation."""
        config = PresetConfig(
            preset_type="mosaic:field_required_if",
            config={
                "fields": [
                    {
                        "field": "end_date",
                        "when_field": "status",
                        "when_value": "completed",
                    }
                ]
            },
        )

        validator = FieldRequiredIfPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "status": "completed", "end_date": "2024-01-01"},
        )
        result = validator.validate(op)

        assert result.is_valid is True

    def test_condition_met_field_missing_fails(self):
        """Condition met with field missing fails validation."""
        config = PresetConfig(
            preset_type="mosaic:field_required_if",
            config={
                "fields": [
                    {
                        "field": "end_date",
                        "when_field": "status",
                        "when_value": "completed",
                    }
                ]
            },
        )

        validator = FieldRequiredIfPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "status": "completed"},
        )
        result = validator.validate(op)

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "Field required violation" in result.errors[0]

    def test_condition_not_met_passes(self):
        """Condition not met passes validation."""
        config = PresetConfig(
            preset_type="mosaic:field_required_if",
            config={
                "fields": [
                    {
                        "field": "end_date",
                        "when_field": "status",
                        "when_value": "completed",
                    }
                ]
            },
        )

        validator = FieldRequiredIfPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "status": "pending"},
        )
        result = validator.validate(op)

        assert result.is_valid is True


class TestNoSelfRefPreset:
    """Tests for NoSelfRefPreset."""

    def test_no_self_reference_passes(self):
        """No self-reference passes validation."""
        config = PresetConfig(
            preset_type="mosaic:no_self_ref",
            config={"fields": [{"field": "parent"}]},
        )

        validator = NoSelfRefPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "parent": "sample-2"},
        )
        result = validator.validate(op)

        assert result.is_valid is True

    def test_self_reference_fails(self):
        """Self-reference fails validation."""
        config = PresetConfig(
            preset_type="mosaic:no_self_ref",
            config={"fields": [{"field": "parent"}]},
        )

        validator = NoSelfRefPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "parent": "sample-1"},
        )
        result = validator.validate(op)

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "Self-reference violation" in result.errors[0]

    def test_self_reference_in_list_fails(self):
        """Self-reference in list fails validation."""
        config = PresetConfig(
            preset_type="mosaic:no_self_ref",
            config={"fields": [{"field": "children"}]},
        )

        validator = NoSelfRefPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"id": "sample-1", "children": ["sample-2", "sample-1"]},
        )
        result = validator.validate(op)

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "Self-reference violation" in result.errors[0]

    def test_no_id_passes(self):
        """Operation without ID passes validation."""
        config = PresetConfig(
            preset_type="mosaic:no_self_ref",
            config={"fields": [{"field": "parent"}]},
        )

        validator = NoSelfRefPreset(config)

        op = WriteOperation(
            operation="insert",
            entity_type="Sample",
            data={"name": "Sample"},
        )
        result = validator.validate(op)

        assert result.is_valid is True
