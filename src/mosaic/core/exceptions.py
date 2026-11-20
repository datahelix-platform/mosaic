"""Exception classes for Mosaic SDK."""

from typing import Any, Optional


class MosaicError(Exception):
    """Base exception class for all Mosaic SDK errors."""

    def __init__(self, message: str, **context: Any):
        self.message = message
        self.context = context
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.context:
            context_str = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{self.message} ({context_str})"
        return self.message


class ConfigError(MosaicError):
    """Exception raised for configuration loading and validation errors."""

    def __init__(
        self,
        message: str,
        field_name: Optional[str] = None,
        **context: Any,
    ):
        self.field_name = field_name
        context["field_name"] = field_name
        super().__init__(message, **context)


class SchemaError(MosaicError):
    """Exception raised for schema parsing and processing errors."""

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        field_name: Optional[str] = None,
        cycle_path: Optional[list[str]] = None,
        **context: Any,
    ):
        self.error_code = error_code
        self.field_name = field_name
        self.cycle_path = cycle_path or []
        context["error_code"] = error_code
        context["field_name"] = field_name
        context["cycle_path"] = cycle_path
        super().__init__(message, **context)


class ValidationError(MosaicError):
    """Exception raised for data validation errors."""

    def __init__(
        self,
        message: str,
        expected_type: Optional[str] = None,
        actual_value: Optional[Any] = None,
        field_name: Optional[str] = None,
        **context: Any,
    ):
        self.expected_type = expected_type
        self.actual_value = actual_value
        self.field_name = field_name
        context["expected_type"] = expected_type
        context["actual_value"] = actual_value
        context["field_name"] = field_name
        super().__init__(message, **context)


class XrefUniquenessError(ValidationError):
    """A ``mosaic_external_xref`` ``(system, value)`` pair is already claimed.

    Raised by the storage adapter inside the entity-write transaction
    (create / update / replace / availability-on) when inserting the
    side-index rows for an annotated slot would violate the global
    ``UNIQUE (system, value)`` constraint among available entities. The
    triggering write is rolled back. Subclasses :class:`ValidationError`
    so the standard transport error paths apply (REST 422 /
    GraphQL ``VALIDATION_FAILED``).
    """

    def __init__(
        self,
        message: str,
        system: Optional[str] = None,
        value: Optional[str] = None,
        conflicting_entity_id: Optional[str] = None,
        conflicting_entity_type: Optional[str] = None,
        **context: Any,
    ):
        self.system = system
        self.value = value
        self.conflicting_entity_id = conflicting_entity_id
        self.conflicting_entity_type = conflicting_entity_type
        context["system"] = system
        context["value"] = value
        context["conflicting_entity_id"] = conflicting_entity_id
        context["conflicting_entity_type"] = conflicting_entity_type
        super().__init__(message, **context)


class DanglingReferenceError(ValidationError):
    """A single-valued reference points at an id that does not exist.

    Raised by the storage adapter at commit for references whose range is a
    **polymorphic base** (abstract, or concrete with concrete subclasses).
    Those references cannot be expressed as a SQL foreign key — subtype
    instances live in their own per-class tables, so no single table can be
    the FK target (issue #93) — and the DDL drops the constraint. The
    referential-integrity check is instead performed in the application layer
    against the ``_entity_registry`` cross-class index, deferred to commit so
    that forward references within a staged bundle still resolve (mirroring
    the deferred-FK behavior for non-polymorphic references, issue #95). A
    dangling or out-of-range reference rolls the write back (issue #127).

    Subclasses :class:`ValidationError` so the standard transport error paths
    apply (REST 422 / GraphQL ``VALIDATION_FAILED``).
    """

    def __init__(
        self,
        message: str,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        slot: Optional[str] = None,
        target_id: Optional[str] = None,
        target_range: Optional[str] = None,
        **context: Any,
    ):
        self.entity_id = entity_id
        self.entity_type = entity_type
        self.slot = slot
        self.target_id = target_id
        self.target_range = target_range
        context["entity_id"] = entity_id
        context["entity_type"] = entity_type
        context["target_id"] = target_id
        context["target_range"] = target_range
        super().__init__(message, field_name=slot, **context)


class EntityNotFoundError(MosaicError):
    """Exception raised when an entity is not found in the system."""

    def __init__(
        self,
        message: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        **context: Any,
    ):
        self.entity_type = entity_type
        self.entity_id = entity_id
        context["entity_type"] = entity_type
        context["entity_id"] = entity_id
        super().__init__(message, **context)


class EntityTypeConflictError(MosaicError):
    """Raised when a write targets an ``id`` already registered under a
    different concrete class.

    ``id`` values are globally unique across all classes (``_entity_registry``
    resolves an id to exactly one class), so a ``put``/``create`` for
    ``entity_type`` on an id already owned by ``existing_entity_type`` can
    never be a same-class update: it would either overwrite the wrong
    table's row or silently no-op. Raised before any write is attempted.
    """

    def __init__(
        self,
        message: str,
        entity_id: Optional[str] = None,
        requested_entity_type: Optional[str] = None,
        existing_entity_type: Optional[str] = None,
        **context: Any,
    ):
        self.entity_id = entity_id
        self.requested_entity_type = requested_entity_type
        self.existing_entity_type = existing_entity_type
        context["entity_id"] = entity_id
        context["requested_entity_type"] = requested_entity_type
        context["existing_entity_type"] = existing_entity_type
        super().__init__(message, **context)


class EntityAlreadySupersededError(MosaicError):
    """Exception raised when supersede_entity() is called on an already-superseded entity.

    Raised before any writes are performed, ensuring no state change occurs.
    """

    def __init__(
        self,
        message: str,
        entity_id: Optional[str] = None,
        superseded_by: Optional[str] = None,
        **context: Any,
    ):
        self.entity_id = entity_id
        self.superseded_by = superseded_by
        context["entity_id"] = entity_id
        context["superseded_by"] = superseded_by
        super().__init__(message, **context)


class AdapterError(MosaicError):
    """Exception raised for adapter-specific errors."""

    def __init__(
        self,
        message: str,
        adapter_name: Optional[str] = None,
        adapter_type: Optional[str] = None,
        **context: Any,
    ):
        self.adapter_name = adapter_name
        self.adapter_type = adapter_type
        context["adapter_name"] = adapter_name
        context["adapter_type"] = adapter_type
        super().__init__(message, **context)


class ValidationFailure(MosaicError):
    """Exception raised when a write operation fails validation.

    Contains detailed information about the validation failure including
    the rule that failed, the error message, and the input context.
    """

    def __init__(
        self,
        message: str,
        rule_id: Optional[str] = None,
        input_context: Optional[dict[str, Any]] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        **context: Any,
    ):
        self.rule_id = rule_id
        self.input_context = input_context or {}
        self.entity_type = entity_type
        self.entity_id = entity_id
        context["rule_id"] = rule_id
        context["input_context"] = input_context
        context["entity_type"] = entity_type
        context["entity_id"] = entity_id
        super().__init__(message, **context)

    def format_detailed_message(self) -> str:
        """Format a detailed failure message with all context.

        Returns:
            Human-readable string with all failure details.
        """
        parts = [self.message]

        if self.rule_id:
            parts.append(f"Rule: {self.rule_id}")

        if self.entity_type:
            entity_info = f"Entity type: {self.entity_type}"
            if self.entity_id:
                entity_info += f" (ID: {self.entity_id})"
            parts.append(entity_info)

        if self.input_context:
            context_str = ", ".join(f"{k}={v!r}" for k, v in self.input_context.items())
            parts.append(f"Context: {context_str}")

        return " | ".join(parts)


class ValidationFailed(MosaicError):
    """Raised by typed-client write methods when validation fails.

    Carries the full sec9 §9.9 envelope (``ValidationResult``) so callers
    can introspect per-tier failures rather than parsing concatenated
    error strings. The REST layer catches this and maps to HTTP 400/422
    with a structured body (see ``mosaic.api.app``).
    """

    def __init__(
        self,
        message: str,
        result: Any = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        **context: Any,
    ):
        self.result = result
        self.entity_type = entity_type
        self.entity_id = entity_id
        context["entity_type"] = entity_type
        context["entity_id"] = entity_id
        super().__init__(message, **context)


class TemporalQueryError(MosaicError):
    """Exception raised for temporal query errors.

    Raised when querying entity state at a point in time that is invalid,
    such as before the entity was created.
    """

    def __init__(
        self,
        message: str,
        entity_id: Optional[str] = None,
        requested_timestamp: Optional[str] = None,
        entity_creation_time: Optional[str] = None,
        **context: Any,
    ):
        self.entity_id = entity_id
        self.requested_timestamp = requested_timestamp
        self.entity_creation_time = entity_creation_time
        context["entity_id"] = entity_id
        context["requested_timestamp"] = requested_timestamp
        context["entity_creation_time"] = entity_creation_time
        super().__init__(message, **context)


class ProvenanceIntegrityError(MosaicError):
    """Exception raised when provenance state is missing or inconsistent.

    Every mutation emits a ``ProvenanceRecord`` transactionally with the
    entity write (sec9 §9.6), so an entity that exists in the ``entities``
    table with no matching provenance is a data-integrity defect — not an
    expected degraded state. Per sec9 §9.2 (*Provenance integrity is
    transactional and loud*), the SDK refuses to return the entity.

    Also raised on other inconsistency shapes: a non-``create`` record as
    the earliest entry, a record with missing ``actor_id``, or a
    ``schema_version`` unrecognized by the merged view.
    """

    def __init__(
        self,
        message: str,
        entity_id: Optional[str] = None,
        inconsistency: Optional[str] = None,
        **context: Any,
    ):
        self.entity_id = entity_id
        self.inconsistency = inconsistency
        context["entity_id"] = entity_id
        context["inconsistency"] = inconsistency
        super().__init__(message, **context)


class IngestionError(MosaicError):
    """Exception raised for data ingestion errors.

    Raised when file reading, parsing, or processing fails.
    """

    def __init__(
        self,
        message: str,
        input_context: Optional[dict[str, Any]] = None,
        entity_type: Optional[str] = None,
        **context: Any,
    ):
        self.input_context = input_context or {}
        self.entity_type = entity_type
        context["input_context"] = input_context
        context["entity_type"] = entity_type
        super().__init__(message, **context)


class IngestionValidationError(IngestionError):
    """Exception raised for data ingestion validation errors.

    Raised when input data fails validation checks (e.g., missing headers).
    """

    def __init__(
        self,
        message: str,
        input_context: Optional[dict[str, Any]] = None,
        entity_type: Optional[str] = None,
        **context: Any,
    ):
        super().__init__(
            message, input_context=input_context, entity_type=entity_type, **context
        )


class CacheIntegrityError(MosaicError):
    """Raised when a cached or freshly downloaded file fails sha256 verification.

    Triggered by :meth:`MosaicClient.cached_fetch` when ``expected_sha256`` is
    supplied and the computed digest does not match — either on the initial
    download or on a subsequent cache hit that has been corrupted out-of-band
    (sec2 §2.14.3, decision D2.14.E).
    """

    def __init__(
        self,
        message: str,
        url: Optional[str] = None,
        path: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        actual_sha256: Optional[str] = None,
        **context: Any,
    ):
        self.url = url
        self.path = path
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        context["url"] = url
        context["path"] = path
        context["expected_sha256"] = expected_sha256
        context["actual_sha256"] = actual_sha256
        super().__init__(message, **context)


class RecipeManifestError(MosaicError):
    """Raised when a recipe's ``recipe.yaml`` fails manifest validation.

    Covers closed-schema LinkML validation of the manifest document
    against ``src/mosaic/schemas/recipe_manifest.yaml`` (sec10 §10.3.2):
    missing required fields, unknown keys, type mismatches. Distinct
    from :class:`RecipeSchemaError`, which fires on the embedded
    ``schema.yaml`` fragment.

    Every error must include the failing ``RecipeRef.source`` (the
    path or URI Mosaic loaded the manifest from), plus the manifest's
    ``id``/``version`` when those parsed successfully (sec10 error model).
    """

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        errors: Optional[list[str]] = None,
        **context: Any,
    ):
        self.source = source
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        self.errors = errors or []
        context["source"] = source
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        super().__init__(message, **context)


class RecipeVersionIncompatibleError(MosaicError):
    """Raised when a recipe's ``mosaic_version`` excludes the running Mosaic.

    Parsed via ``packaging.specifiers.SpecifierSet`` (sec10 §10.3.2 /
    error model). Always raised before any state change.
    """

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        specifier: Optional[str] = None,
        mosaic_version: Optional[str] = None,
        **context: Any,
    ):
        self.source = source
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        self.specifier = specifier
        self.mosaic_version = mosaic_version
        context["source"] = source
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        context["specifier"] = specifier
        context["mosaic_version"] = mosaic_version
        super().__init__(message, **context)


class RecipeRequiresUnsatisfiedError(MosaicError):
    """Raised when a declared recipe or reference-loader dependency is missing.

    ``requires.recipes`` entries resolve via the resolver chain; this
    error fires when fetch/parse fails or when the referenced manifest
    declares an ``id`` that does not match the ``RecipeRef.id``.
    ``requires.reference_loaders`` entries are pin strings of the form
    ``name==version``; this error fires when the named loader is not
    installed at the declared version (sec10 §10.4.5 — preconditions,
    not transitive installs).
    """

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        unresolved_source: Optional[str] = None,
        loader_pin: Optional[str] = None,
        **context: Any,
    ):
        self.source = source
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        self.unresolved_source = unresolved_source
        self.loader_pin = loader_pin
        context["source"] = source
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        context["unresolved_source"] = unresolved_source
        context["loader_pin"] = loader_pin
        super().__init__(message, **context)


class RecipeLineageCycleError(MosaicError):
    """Raised when the ``parent``/``requires.recipes`` graph contains a cycle.

    Recipe ``A`` requires ``B`` requires ``A`` is the canonical case
    (sec10 §10.4.4). Cycle detection covers ``parent`` and
    ``requires.recipes`` uniformly.
    """

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        cycle: Optional[list[str]] = None,
        **context: Any,
    ):
        self.source = source
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        self.cycle = cycle or []
        context["source"] = source
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        context["cycle"] = self.cycle
        super().__init__(message, **context)


class RecipeFetchError(MosaicError):
    """Raised when a recipe resolver cannot retrieve the source artifact.

    Covers HTTP errors (4xx/5xx), network failures (DNS, refused
    connection, timeout), and corrupt/unreadable tarballs returned by
    a remote endpoint (sec10 §10.4.2). Distinct from
    :class:`RecipeDigestMismatchError`, which fires after a successful
    fetch when bytes don't match the declared digest.

    Every error must include the failing ``RecipeRef.source`` URI
    plus the recipe ``id``/``version`` when those are known.
    """

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        status_code: Optional[int] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        **context: Any,
    ):
        self.source = source
        self.status_code = status_code
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        context["source"] = source
        context["status_code"] = status_code
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        super().__init__(message, **context)


class RecipeDigestMismatchError(MosaicError):
    """Raised when fetched bytes do not match the declared canonical-content digest.

    Triggered by the install path (and by the resolver when an
    ``expected_digest`` is provided) when ``sha256`` of the canonical
    content hash disagrees with what the ``RecipeRef`` declared
    (sec10 §10.4.3 / invariant 4). Always raised before any state
    change.
    """

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        expected_digest: Optional[str] = None,
        actual_digest: Optional[str] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        **context: Any,
    ):
        self.source = source
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        context["source"] = source
        context["expected_digest"] = expected_digest
        context["actual_digest"] = actual_digest
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        super().__init__(message, **context)


class RecipeSchemaError(MosaicError):
    """Raised when a recipe's embedded schema fragment violates a merge invariant.

    Covers both LinkML-shape failures of ``schema.yaml`` and the
    no-in-place-override check (sec10 §10.7.2, invariant 6) — a recipe
    must not redefine a class or slot whose ``provided_by`` annotation
    names a different recipe or loader. Users override by subclassing
    (``is_a:``) instead.
    """

    def __init__(
        self,
        message: str,
        element_name: Optional[str] = None,
        element_kind: Optional[str] = None,
        provided_by: Optional[str] = None,
        recipe_id: Optional[str] = None,
        recipe_version: Optional[str] = None,
        **context: Any,
    ):
        self.element_name = element_name
        self.element_kind = element_kind
        self.provided_by = provided_by
        self.recipe_id = recipe_id
        self.recipe_version = recipe_version
        context["element_name"] = element_name
        context["element_kind"] = element_kind
        context["provided_by"] = provided_by
        context["recipe_id"] = recipe_id
        context["recipe_version"] = recipe_version
        super().__init__(message, **context)


class SearchCapabilityError(MosaicError):
    """Exception raised when a search operation is attempted on a field
    that does not support full-text search.

    Raised when searching a field not declared with `search: fts` in the schema,
    or when the adapter does not support a search mode declared in the schema.
    """

    def __init__(
        self,
        message: str,
        field_name: Optional[str] = None,
        entity_type: Optional[str] = None,
        unsupported_modes: Optional[list[str]] = None,
        **context: Any,
    ):
        self.field_name = field_name
        self.entity_type = entity_type
        self.unsupported_modes = unsupported_modes or []
        context["field_name"] = field_name
        context["entity_type"] = entity_type
        context["unsupported_modes"] = self.unsupported_modes
        super().__init__(message, **context)

    def suggest_fts_enablement(self) -> str:
        """Suggest how to enable FTS for the field."""
        if self.field_name and self.entity_type:
            return f"To enable full-text search, add 'search: fts' to the '{self.field_name}' field definition in the {self.entity_type} entity schema."
        return "To enable full-text search, add 'search: fts' to the field definition in your schema."


class MigrationStepNotFoundError(MosaicError):
    """Raised when no declared migration step covers a requested hop.

    S2 resolves a single declared ``(from_version, to_version)`` edge by
    exact match (Doc 2 §2A / sec11 §11.3.4). Multi-hop path-finding over
    the migration DAG (composing intermediate steps, shortcut edges, the
    below-floor fail-loud) lands in S3; until then a hop with no directly
    declared step fails loud here rather than silently doing nothing.
    """

    def __init__(
        self,
        message: str,
        package: Optional[str] = None,
        from_version: Optional[str] = None,
        to_version: Optional[str] = None,
        available_steps: Optional[list[tuple[str, str]]] = None,
        **context: Any,
    ):
        self.package = package
        self.from_version = from_version
        self.to_version = to_version
        self.available_steps = available_steps or []
        context["package"] = package
        context["from_version"] = from_version
        context["to_version"] = to_version
        context["available_steps"] = self.available_steps
        super().__init__(message, **context)


class MigrationGateError(MosaicError):
    """Raised when a ``DomainModule.evolve`` staged dry-run gate fails.

    The migration's transform output is staged and validated against the
    fully merged schema *before* any committed write (sec11 §11.5.2 hard
    validation gate). When the staged new-shape records do not validate,
    this is raised and **nothing is committed** — the deployment's domain
    data is left exactly as it was. Carries the underlying LinkML
    validation messages in :attr:`errors`.
    """

    def __init__(
        self,
        message: str,
        package: Optional[str] = None,
        from_version: Optional[str] = None,
        to_version: Optional[str] = None,
        errors: Optional[list[str]] = None,
        **context: Any,
    ):
        self.package = package
        self.from_version = from_version
        self.to_version = to_version
        self.errors = errors or []
        context["package"] = package
        context["from_version"] = from_version
        context["to_version"] = to_version
        context["errors"] = self.errors
        super().__init__(message, **context)


class MigrationFloorError(MosaicError):
    """Raised when the current version is below a package's migration floor.

    The migration DAG ships every step covering the package's supported
    range back to a declared **floor** (sec11 §11.3.2, Alembic
    ``versions/`` style). When the deployment's current version is older
    than the floor — i.e. it is not a node anywhere in the declared DAG —
    the resolver cannot compose a path and fails loud rather than guessing
    (there is no silent fallback; this is the squashed-migration
    boundary). The message instructs the operator to *upgrade to ≥<floor>
    first via package <name>*. :attr:`floor` names the oldest version the
    shipped chain can start from.
    """

    def __init__(
        self,
        message: str,
        package: Optional[str] = None,
        from_version: Optional[str] = None,
        to_version: Optional[str] = None,
        floor: Optional[str] = None,
        **context: Any,
    ):
        self.package = package
        self.from_version = from_version
        self.to_version = to_version
        self.floor = floor
        context["package"] = package
        context["from_version"] = from_version
        context["to_version"] = to_version
        context["floor"] = floor
        super().__init__(message, **context)


class DeprovisionRefusedError(MosaicError):
    """Raised when a ``deprovision`` teardown is refused (sec11 §11.4).

    Two distinct refusals share this type so a single ``except`` covers
    the lifecycle's guard rails (the ``reason`` discriminates):

    * ``"has_dependents"`` — another *installed* package declares this
      one in its ``depends_on()`` (§11.4.4). :attr:`dependents` names
      them. Applies to every species.
    * ``"live_domain_data"`` — a :class:`DomainModule` still owns live,
      first-party records (§11.4.3). Silent soft-deletion on uninstall
      would be a data-loss event, so the operator must pass ``force=True``
      (ideally after exporting). :attr:`live_types` names the populated
      entity types.

    ``ReferenceLoader`` never raises this on its own data — its source is
    external and reconstructible, so it prunes willingly.
    """

    def __init__(
        self,
        message: str,
        package: Optional[str] = None,
        reason: Optional[str] = None,
        dependents: Optional[list[str]] = None,
        live_types: Optional[list[str]] = None,
        **context: Any,
    ):
        self.package = package
        self.reason = reason
        self.dependents = dependents or []
        self.live_types = live_types or []
        context["package"] = package
        context["reason"] = reason
        context["dependents"] = self.dependents
        context["live_types"] = self.live_types
        super().__init__(message, **context)


class OrchestrationError(MosaicError):
    """Raised when the dependency-ordered lifecycle orchestrator cannot
    sequence a multi-package operation (sec11 §11.5).

    The orchestrator resolves the ``depends_on`` graph across the affected
    packages once and topologically sorts it (base before dependents) so
    each hop dispatches in dependency order. This is raised when that
    resolution is impossible — a dependency **cycle** (:attr:`cycle` names
    the packages on it), or a package depending on one absent from the
    operation set (:attr:`missing`). It is distinct from
    :class:`MigrationGateError` (a *validation* failure of an otherwise
    well-ordered chain): this is a *structural* failure of the graph
    itself, surfaced before any migration step runs.
    """

    def __init__(
        self,
        message: str,
        cycle: Optional[list[str]] = None,
        missing: Optional[list[str]] = None,
        **context: Any,
    ):
        self.cycle = cycle or []
        self.missing = missing or []
        context["cycle"] = self.cycle
        context["missing"] = self.missing
        super().__init__(message, **context)

