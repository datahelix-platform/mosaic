"""SchemaManager — schema access, validation pipeline, and FTS metadata facade.

Operates on a LinkML-backed :class:`SchemaRegistry`. All schema introspection
(FTS slots, reference slots, search capabilities) reads from LinkML
``annotations:`` via the registry.
"""

from __future__ import annotations

from typing import Optional

from mosaic.core.pipeline import ValidationPipeline
from mosaic.core.storage.adapters.sqlite_adapter import SQLiteAdapter
from mosaic.core.storage.fts import FTSFieldMetadata, FTSTableMetadata
from mosaic.core.validation.validators import (
    ValidationResult,
    WriteOperation,
    WriteValidator,
)
from mosaic.linkml_bridge import (
    PROVIDED_BY_ANNOTATION,
    SchemaRegistry,
    annotation_value,
)


class SchemaManager:
    """Owns schema access, the write-validation pipeline, and FTS metadata."""

    def __init__(
        self,
        registry: Optional[SchemaRegistry] = None,
        pipeline: Optional[ValidationPipeline] = None,
        bypass_validation: bool = False,
        storage: Optional[SQLiteAdapter] = None,
    ) -> None:
        self._registry = registry
        self._pipeline = pipeline
        self._bypass_validation = bypass_validation
        self._storage = storage
        self._fts_table_metadata: dict[str, list[FTSTableMetadata]] = {}
        self._build_fts_metadata()
        self._validate_search_capabilities()

    @property
    def registry(self) -> Optional[SchemaRegistry]:
        return self._registry

    @property
    def pipeline(self) -> Optional[ValidationPipeline]:
        return self._pipeline

    @pipeline.setter
    def pipeline(self, value: Optional[ValidationPipeline]) -> None:
        self._pipeline = value

    @property
    def bypass_validation(self) -> bool:
        return self._bypass_validation

    @property
    def fts_table_metadata(self) -> dict[str, list[FTSTableMetadata]]:
        return self._fts_table_metadata

    def _non_abstract_class_names(self) -> list[str]:
        if self._registry is None:
            return []
        sv = self._registry.schema_view
        return [
            name
            for name in self._registry.class_names()
            if not (sv.get_class(name) and sv.get_class(name).abstract)
        ]

    def _build_fts_metadata(self) -> None:
        if self._registry is None:
            return
        for class_name in self._non_abstract_class_names():
            fts_tables: list[FTSTableMetadata] = []
            for slot, mode in self._registry.searchable_slots(class_name):
                fts_tables.append(
                    FTSTableMetadata(
                        table_name=FTSTableMetadata.generate_table_name(
                            class_name, slot.name
                        ),
                        source_entity_type=class_name,
                        fts_version=mode,
                        content_table="entities",
                        content_rowid="rowid",
                        fields=[
                            FTSFieldMetadata(
                                field_name=slot.name,
                                field_type=slot.range or "string",
                                search_type=mode,
                                source_entity_type=class_name,
                            )
                        ],
                    )
                )
            if fts_tables:
                self._fts_table_metadata[class_name] = fts_tables

    def _validate_search_capabilities(self) -> None:
        from mosaic.core.exceptions import SearchCapabilityError

        if self._storage is None or self._registry is None:
            return

        adapter_capabilities = self._storage.search_capabilities()
        declared_modes: set[str] = set()
        for class_name in self._non_abstract_class_names():
            for _slot, mode in self._registry.searchable_slots(class_name):
                normalized = "fts" if mode in ("fts", "fts5") else mode
                declared_modes.add(normalized)

        unsupported = declared_modes - adapter_capabilities
        if unsupported:
            raise SearchCapabilityError(
                message=(
                    "Storage adapter does not support search modes: "
                    + ", ".join(sorted(unsupported))
                ),
                unsupported_modes=list(unsupported),
            )

    def schema_references(self, entity_type: str) -> list[dict]:
        if self._registry is None or not self._registry.has_class(entity_type):
            return []
        return [
            {"field": slot_name, "target_entity_type": target}
            for slot_name, target in self._registry.reference_slots(entity_type)
        ]

    def fts_entity_types(self) -> list[str]:
        """Entity types with at least one ``mosaic_search``-annotated slot —
        the classes cross-class search fans over (issue #158)."""
        return sorted(self._fts_table_metadata)

    def get_fts_tables_for_entity_type(
        self, entity_type: str
    ) -> list[FTSTableMetadata]:
        return self._fts_table_metadata.get(entity_type, [])

    def add_validator(self, validator: WriteValidator) -> None:
        if self._pipeline is None:
            self._pipeline = ValidationPipeline()
        self._pipeline.add_validator(validator)

    def validate(self, operation: WriteOperation) -> ValidationResult:
        if self._bypass_validation or self._pipeline is None:
            return ValidationResult(is_valid=True, errors=[])
        return self._pipeline.execute(operation)

    def check_no_inplace_override(
        self,
        fragment: dict,
        *,
        importing_provided_by: Optional[str] = None,
    ) -> None:
        """Reject recipe fragments that redefine upstream-provided content.

        Implements sec10 §10.7.2 / invariant 6 — the merge seam Phase 3
        will call before invoking ``merge_fragment``. A fragment may
        introduce brand-new classes/slots, and may subclass upstream
        content via ``is_a:`` (that creates a NEW class), but it must
        NOT redefine an existing class/slot whose ``provided_by``
        annotation names a different recipe or loader.

        Args:
            fragment: Parsed ``schema.yaml`` dict. Only the top-level
                ``classes`` and ``slots`` sections are inspected; nested
                ``attributes`` are not checked separately because a
                fragment that touches them must already have redeclared
                the owning class, which the class-level check catches.
            importing_provided_by: The ``provided_by`` attribution the
                merge layer will inject for THIS recipe (e.g.,
                ``recipe.org.example.foo@1.0``). When supplied, an
                existing element with the exact same attribution is
                permitted — re-merging the same recipe identity is not
                an in-place override. When ``None``, ANY existing
                element with a ``recipe.`` or ``loader.`` attribution
                triggers rejection.

        Raises:
            RecipeSchemaError: When the fragment redefines an upstream
                class or slot.
        """
        from mosaic.core.exceptions import RecipeSchemaError

        if self._registry is None:
            return
        sv = self._registry.schema_view

        def _check(name: str, existing: object, kind: str) -> None:
            pb = annotation_value(existing, PROVIDED_BY_ANNOTATION)
            if not pb:
                return
            pb_str = str(pb)
            if not (
                pb_str.startswith("recipe.") or pb_str.startswith("loader.")
            ):
                return
            if (
                importing_provided_by is not None
                and pb_str == importing_provided_by
            ):
                return
            raise RecipeSchemaError(
                f"Cannot redefine {kind} {name!r}: it is provided by "
                f"{pb_str!r}. In-place override of upstream classes/slots "
                f"is rejected (sec10 §10.7.2, invariant 6). Subclass via "
                f"`is_a:` to specialise instead.",
                element_name=name,
                element_kind=kind,
                provided_by=pb_str,
            )

        for class_name in (fragment.get("classes") or {}):
            cls = sv.get_class(class_name)
            if cls is not None:
                _check(class_name, cls, "class")

        for slot_name in (fragment.get("slots") or {}):
            slot = sv.get_slot(slot_name)
            if slot is not None:
                _check(slot_name, slot, "slot")

    def merge_fragment(
        self,
        fragment: dict,
        *,
        recipe_id: str,
        recipe_version: str,
    ) -> SchemaRegistry:
        """Merge a recipe ``schema.yaml`` fragment into the live registry.

        Implements the recipe-side of sec2 §2.14.5/§2.14.6 merge rules:

        - Calls :meth:`check_no_inplace_override` first (invariant 6).
        - Injects ``provided_by: recipe.<id>@<version>`` on every class,
          top-level slot, and per-class attribute the fragment
          introduces (invariant 7). Author-written annotations are
          overwritten — manifest identity wins.
        - Returns a fresh :class:`SchemaRegistry` over the merged
          :class:`SchemaView`. The caller is responsible for swapping
          the new registry in (``RecipeService.import_`` does this via
          :meth:`set_registry`).

        Args:
            fragment: Parsed ``schema.yaml`` dict. Mutated only locally —
                the input dict is not modified.
            recipe_id: The recipe's manifest ``id`` (reverse-DNS form).
            recipe_version: The recipe's manifest ``version``.

        Returns:
            A new :class:`SchemaRegistry` containing every class and slot
            of the previous registry plus the fragment, with
            ``provided_by`` stamped.

        Raises:
            RecipeSchemaError: When the fragment redefines an upstream
                class/slot (invariant 6).
        """
        from mosaic.linkml_bridge import (
            _inject_provided_by_annotation,
            _merge_prepared_fragment,
        )
        import copy

        if self._registry is None:
            raise ValueError(
                "Cannot merge a recipe fragment: SchemaManager has no "
                "registry. Pass `registry=` when constructing MosaicClient."
            )

        attribution = f"recipe.{recipe_id}@{recipe_version}"
        self.check_no_inplace_override(
            fragment, importing_provided_by=attribution
        )

        prepared = copy.deepcopy(fragment)
        prepared.pop("imports", None)  # recipes do not currently extend imports

        for cls_name, cls in list((prepared.get("classes") or {}).items()):
            if cls is None:
                cls = {}
                prepared.setdefault("classes", {})[cls_name] = cls
            if isinstance(cls, dict):
                _inject_provided_by_annotation(cls, attribution)
                for attr_name, attr in list((cls.get("attributes") or {}).items()):
                    if attr is None:
                        attr = {}
                        cls.setdefault("attributes", {})[attr_name] = attr
                    if isinstance(attr, dict):
                        _inject_provided_by_annotation(attr, attribution)

        for slot_name, slot in list((prepared.get("slots") or {}).items()):
            if slot is None:
                slot = {}
                prepared.setdefault("slots", {})[slot_name] = slot
            if isinstance(slot, dict):
                _inject_provided_by_annotation(slot, attribution)

        merged_sv = _merge_prepared_fragment(self._registry.schema_view, prepared)
        return SchemaRegistry(merged_sv)

    def set_registry(self, registry: SchemaRegistry) -> None:
        """Swap in a new :class:`SchemaRegistry` after a recipe merge.

        Rebuilds the FTS metadata cache against the new schema. Search
        capability re-validation is intentionally skipped — recipe
        imports cannot change adapter capabilities, and re-running the
        check would re-touch the storage adapter inside the recipe
        transaction.
        """
        self._registry = registry
        self._fts_table_metadata = {}
        self._build_fts_metadata()
