# reference-list Specification

## Purpose
TBD - created by archiving change epic-007-feature-004. Update Purpose after archive.
## Requirements
### Requirement: User can list installed reference loaders
Given a user has installed reference loaders, when they run 'mosaic reference list', then the system displays all available reference loader packages with their descriptions, version numbers, and installation status.

#### Scenario: List shows all installed loaders
- **WHEN** user has one or more reference loader packages installed
- **AND** they run 'mosaic reference list'
- **THEN** the system displays a list of all installed loaders
- **AND** each entry includes the package name
- **AND** each entry includes the version number
- **AND** each entry includes the description
- **AND** each entry shows installation status as "installed"

#### Scenario: List displays in readable format
- **WHEN** user runs 'mosaic reference list'
- **THEN** the output is formatted as a table or list with aligned columns
- **AND** includes a header row with column names

### Requirement: Message when no reference loaders installed
Given a user runs 'mosaic reference list' with no installed loaders, when they request reference listing, then the system shows a message indicating that no reference loaders are currently installed.

#### Scenario: No loaders installed shows message
- **WHEN** user runs 'mosaic reference list' with no reference loaders installed
- **THEN** the system displays a message: "No reference loaders installed. Use 'mosaic reference install <package>' to add one."
- **AND** exits with zero status code (informational, not an error)

#### Scenario: Empty installation directory shows message
- **WHEN** user runs 'mosaic reference list'
- **AND** the references directory exists but is empty
- **THEN** the system displays the same message as when no loaders are installed

