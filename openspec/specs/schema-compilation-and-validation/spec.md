# schema-compilation-and-validation Specification

## Purpose
TBD - created by archiving change epic-007-feature-006. Update Purpose after archive.
## Requirements
### Requirement: Schema Validation
The system SHALL allow users to validate LinkML schema files using the 'mosaic validate-schema' command. When a user provides a valid schema file, the CLI should validate the schema structure and output a success message to stdout.

#### Scenario: Successful validation
- **WHEN** user runs 'mosaic validate-schema' with a valid LinkML schema file
- **THEN** system validates the schema and outputs a success message to stdout

### Requirement: Syntax Error Handling
The system SHALL display a clear error message when a user attempts to validate a schema file with syntax errors. The error message should indicate the location and type of syntax error in the schema file.

#### Scenario: Invalid syntax
- **WHEN** user runs 'mosaic validate-schema' with a schema file containing invalid syntax
- **THEN** system displays an appropriate error message indicating the file path, line number, and syntax error type

### Requirement: Schema Validation
The system SHALL allow users to validate schema files against defined rules using the `mosaic validate --schema <file>` command. When a user provides a valid schema file, all validation rules defined for the schema SHALL be applied — including namespace graph validation — and return a success status code with no errors reported.

#### Scenario: Valid schema validation
- **WHEN** user runs `mosaic validate --schema <file>` with a valid JSON/YAML schema file
- **THEN** system applies all validation rules (including namespace resolution and cross-namespace reference checks) and returns a success status code with no errors

#### Scenario: Valid multi-file namespace schema passes validation
- **WHEN** user runs `mosaic validate --schema` against a schema directory containing files with `namespace` keys and valid cross-namespace references
- **THEN** system resolves the namespace graph, validates all cross-namespace references, and returns a success status code

### Requirement: Validation Error Reporting
The system SHALL report specific validation errors when users run the validator on schema files with validation errors. The error messages SHALL clearly indicate the location and nature of each violation, including namespace errors such as unknown references, duplicate entities, and circular dependencies.

#### Scenario: Invalid schema validation
- **WHEN** user runs `mosaic validate --schema <file>` with a schema file containing validation errors
- **THEN** system reports specific validation errors with clear error messages indicating the location and nature of each violation

#### Scenario: Unknown cross-namespace reference error is reported
- **WHEN** user runs `mosaic validate --schema` on a schema containing a `references.entity_type` pointing to a namespace or entity that does not exist
- **THEN** system reports a `SchemaValidationError` identifying the unresolved FQN reference and the file where it appears

#### Scenario: Circular namespace dependency error is reported
- **WHEN** user runs `mosaic validate --schema` on a schema set where two namespaces reference each other in a cycle
- **THEN** system reports a `SchemaValidationError` identifying the circular dependency path

### Requirement: Schema Diff Functionality
The system SHALL allow users to compare two different schema versions using the 'mosaic schema diff' command. When users provide file paths for two different schema versions, the CLI should detect and display all differences between the schemas in a human-readable format.

#### Scenario: Schema comparison with valid files
- **WHEN** user runs 'mosaic schema diff <file1> <file2>' with valid schema files  
- **THEN** system detects and displays all differences including added/removed/modified elements in a human-readable format

### Requirement: Invalid File Path Handling
The system SHALL output an appropriate error message when users run 'mosaic schema diff' with invalid file paths. The error should specify which files could not be found or are invalid.

#### Scenario: Missing file during diff
- **WHEN** user runs 'mosaic schema diff <file1> <file2>' with one or both files not existing
- **THEN** system outputs an appropriate error message specifying which files could not be found or are invalid

