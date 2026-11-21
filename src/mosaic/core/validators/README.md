# CEL Validator Engine

This module provides the CEL (Common Expression Language) validator engine for Mosaic, enabling config-driven business rule validation through `validators.yaml`.

## Overview

The CEL validator engine loads validation rules defined in YAML and evaluates CEL expressions against entity data at runtime. It provides:

- **CEL Expression Evaluation**: Parse and evaluate CEL conditions against entity contexts
- **Multi-Rule Processing**: Handle multiple validator rules with priority ordering
- **Error Aggregation**: Collect and report all validation errors, not just the first
- **YAML Integration**: Load validators from `validators.yaml` with full structure validation

## Installation

The module requires the `common-expression-language` package:

```bash
pip install common-expression-language
```

## Quick Start

### Basic Usage

```python
from mosaic.core.validators import ValidatorEngine

# Create and load validator engine
engine = ValidatorEngine()
engine.load("path/to/validators.yaml")

# Validate entity data
result = engine.validate(
    entity_type="Sample",
    operation="create",
    entity_data={"name": "test", "age": 25},
)

if not result.is_valid:
    for error in result.errors:
        print(f"Validation failed: {error['message']}")
```

### Integration with MosaicClient

```python
from mosaic.core.client import MosaicClient
from mosaic.core.validators import CELWriteValidator

# Create CEL validator
validator = CELWriteValidator(validators_path="path/to/validators.yaml")

# Create client with validator
client = MosaicClient()
client.add_validator(validator)

# Create entity - validation runs automatically
client.create("Sample", {"name": "test"})
```

## validators.yaml Format

```yaml
validators:
  - name: sample_name_required
    entity_types: [Sample]
    'on': [create, update]
    condition: "entity.name != ''"
    error: "Sample {entity_id}: name is required"
    priority: 0

  - name: sample_age_positive
    entity_types: [Sample]
    'on': [create, update]
    condition: "entity.age > 0"
    error: "Sample {entity_id}: age must be positive"
    priority: 1
```

### Validator Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique validator name |
| `entity_types` | list | No | Entity types to apply to (null = all) |
| `on` | list | No | Operations: create, update, delete |
| `priority` | int | No | Execution order (lower = earlier) |
| `when` | string | No | CEL pre-condition expression |
| `condition` | string | Yes* | CEL validation expression |
| `error` | string | No | Error message template |

* Either `condition` or `requires` must be present

## CEL Expression Basics

CEL expressions evaluate against an `entity` variable containing the entity data:

```cel
entity.name != ''           # Field not empty
entity.age > 18             # Numeric comparison
entity.tags.exists(x, x > 0) # Collection operations
has(entity.optional_field)  # Field existence check
```

## Exception Classes

- `ValidationError`: Base exception for validation errors
- `CELParseError`: CEL syntax parsing errors (with line number)
- `CELEvaluationError`: CEL runtime errors (with field reference)

## API Reference

### ValidatorEngine

```python
engine = ValidatorEngine()
engine.load("validators.yaml")  # Load from file
engine.validate(entity_type, operation, entity_data)  # Validate
```

### CELWriteValidator

```python
validator = CELWriteValidator(validators_path="validators.yaml")
client.add_validator(validator)  # Add to MosaicClient
```

## Testing

Run tests with pytest:

```bash
pytest tests/core/test_cel_validator.py
pytest tests/core/test_cel_write_validator.py
```
