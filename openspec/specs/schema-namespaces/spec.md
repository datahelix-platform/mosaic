# schema-namespaces Specification

## Purpose
Defines the namespace system for Hippo schema files. Allows schema entities to be scoped to named namespaces using an optional `namespace:` key in schema YAML files, with full FQN resolution and cross-namespace reference validation.

## Requirements

### Requirement: Namespace declaration in schema file
A schema file SHALL support an optional top-level `namespace` key that scopes all entities in that file to a named namespace. Files without a `namespace` key SHALL contribute their entities to the root namespace.

#### Scenario: Schema file with namespace key loads entities into named namespace
- **WHEN** a schema file contains `namespace: tissue` and defines an entity `Sample`
- **THEN** the entity is registered in the namespace registry as `tissue.Sample`

#### Scenario: Schema file without namespace key loads entities into root namespace
- **WHEN** a schema file has no `namespace` key and defines an entity `Donor`
- **THEN** the entity is registered in the namespace registry as `Donor` (root namespace)

### Requirement: Multi-file namespace merging
Multiple schema files declaring the same `namespace` value SHALL have their entity lists merged into a single namespace at load time. Duplicate `(namespace, entity_name)` pairs across files SHALL raise a `SchemaValidationError`.

#### Scenario: Two files sharing a namespace merge their entities
- **WHEN** two schema files both declare `namespace: tissue` with distinct entity names
- **THEN** both entity sets are accessible under the `tissue` namespace

#### Scenario: Duplicate entity name within the same namespace raises error
- **WHEN** two schema files both declare `namespace: tissue` and both define an entity named `Sample`
- **THEN** the system raises `SchemaValidationError` identifying the conflicting entity

### Requirement: Fully-qualified entity type name (FQN) semantics
The system SHALL resolve entity type names using FQN rules: `<namespace>.<EntityType>` for named namespaces, and bare `<EntityType>` (or equivalently `root.<EntityType>`) for the root namespace. `root` SHALL be the only implicit namespace prefix.

#### Scenario: Namespaced entity is addressable by FQN
- **WHEN** `tissue.Sample` is declared in the schema
- **THEN** `client.put("tissue.Sample", {...})` and `client.get("tissue.Sample", id)` resolve to that entity type

#### Scenario: Root entity is addressable as bare string
- **WHEN** `Donor` is declared in the root namespace
- **THEN** `client.put("Donor", {...})` and `client.put("root.Donor", {...})` both resolve to the same entity type

#### Scenario: `root.EntityType` and bare `EntityType` are equivalent
- **WHEN** a root-namespace entity `Donor` is stored and queried
- **THEN** querying with `"root.Donor"` returns the same results as querying with `"Donor"`

### Requirement: NamespaceRegistry construction by SchemaLoader
The system SHALL construct a `NamespaceRegistry` during schema loading that maps `(namespace, entity_name)` to `EntityConfig`. The registry SHALL be fully populated before any cross-namespace reference validation occurs.

#### Scenario: Registry is populated after loading all schema files
- **WHEN** `SchemaLoader` recurses the schema directory and finds files in multiple namespaces
- **THEN** the `NamespaceRegistry` contains entries for every entity across all discovered files before validation begins

#### Scenario: FQN lookup on registry returns the correct EntityConfig
- **WHEN** the registry is queried with `tissue.Sample`
- **THEN** it returns the `EntityConfig` for that entity, not the root `Sample` (if one exists)

### Requirement: Cross-namespace reference resolution
Schema fields using `references.entity_type` SHALL support FQNs to reference entities in other namespaces. The registry SHALL resolve these references at validation time.

#### Scenario: Cross-namespace reference using FQN resolves successfully
- **WHEN** a field in `tissue.Sample` declares `references.entity_type: Donor` (root entity)
- **THEN** the registry resolves `Donor` to the root-namespace `Donor` entity without error

#### Scenario: Cross-namespace reference to a named namespace resolves successfully
- **WHEN** a field in `omics.Datafile` declares `references.entity_type: tissue.Sample`
- **THEN** the registry resolves `tissue.Sample` to the `tissue` namespace entity without error

### Requirement: Unknown FQN reference raises SchemaValidationError
The system SHALL raise `SchemaValidationError` if a `references.entity_type` value refers to a namespace or entity type that does not exist in the registry.

#### Scenario: Reference to unknown namespace raises error
- **WHEN** a field declares `references.entity_type: ghost.Entity` and no `ghost` namespace is registered
- **THEN** the system raises `SchemaValidationError` identifying the unresolved reference

#### Scenario: Reference to unknown entity within known namespace raises error
- **WHEN** a field declares `references.entity_type: tissue.Ghost` and `tissue` exists but `Ghost` does not
- **THEN** the system raises `SchemaValidationError` identifying the missing entity

### Requirement: Circular namespace dependency detection
The system SHALL detect circular dependencies among namespaces (derived from cross-namespace `references.entity_type` fields) and raise `SchemaValidationError`.

#### Scenario: Circular dependency between two namespaces raises error
- **WHEN** `tissue.Sample` references `omics.Datafile` and `omics.Datafile` references `tissue.Sample`
- **THEN** the system raises `SchemaValidationError` indicating a circular namespace dependency

### Requirement: Backwards compatibility — unqualified schemas unchanged
Existing schema files with no `namespace` key SHALL continue to load and behave exactly as before. No changes to YAML structure, entity field definitions, or CLI usage are required for existing deployments.

#### Scenario: Existing single-file schema without namespace loads correctly
- **WHEN** `schema.yaml` has no `namespace` key and defines entities `Sample` and `Donor`
- **THEN** all entities are accessible as bare strings and all existing `MosaicClient` calls continue to work without modification

#### Scenario: Mixing namespaced and non-namespaced files in the same schema directory works
- **WHEN** the schema directory contains both `legacy.yaml` (no namespace) and `tissue.yaml` (namespace: tissue)
- **THEN** root-namespace entities and `tissue.*` entities are all resolvable without conflict
