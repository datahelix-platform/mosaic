# Mosaic Typed Client Reference

The typed client gives each schema class its own accessor on `MosaicClient` —
`client.samples.create(Sample(name="S001"))` — alongside the generic
`client.put("Sample", {...})` that already exists. Both surfaces are **coequal**:
every SDK capability is reachable from either, neither is preferred, and new
features land in both at the same time.

For the full engineering spec see [sec9 §9.8](../design/sec9_linkml_redesign.md#98-typed-client).

---

## Prerequisites

The typed surface is built when `MosaicClient` is constructed with a `SchemaRegistry`.
Without a registry the typed accessors are not present and the generic surface
continues to work normally.

```python
from mosaic.core.client import MosaicClient
from mosaic.linkml_bridge import SchemaRegistry

registry = SchemaRegistry.from_yaml_file("schemas/my_schema.yaml")
client = MosaicClient(storage=storage, registry=registry)
```

---

## Namespace-Aware Access Patterns

The typed client mirrors the namespace structure declared in the schema.

### Root Namespace

Classes declared without a `namespace:` annotation are **root-namespace** classes.
They are reachable both flat on `client` and via the explicit `client.root.*` alias.
The two forms resolve to the same accessor object.

```python
# Flat access (default)
client.samples.create({"name": "S001"})

# Explicit root alias — identical result
client.root.samples.create({"name": "S001"})
```

### Non-Root Namespaces

Classes with `namespace: tissue` are reached through a namespace container:

```python
client.tissue.samples.create({"name": "T001"})
```

Non-root classes are **not** flat on `client`. `client.samples` will not be present
as a typed accessor unless a root-namespace class also has the default accessor
`samples`.

### Nested Namespaces (Dot Notation)

A namespace string like `assay.quant` produces a two-level path:

```python
client.assay.quant.measurements.create({"value": 42.0})
```

Intermediate containers materialize even when no classes are declared directly
in the parent segment. If only `assay.quant` has classes, `client.assay` exists
as a container with a `.quant` sub-attribute and no direct accessors.

### Access Pattern Summary

| Declared `namespace:` | Access form | Example |
|---|---|---|
| *(none)* — root | Flat | `client.samples.create(...)` |
| *(none)* — root | Explicit root | `client.root.samples.create(...)` |
| `tissue` | Namespace | `client.tissue.samples.create(...)` |
| `assay.quant` | Nested | `client.assay.quant.measurements.create(...)` |

---

## Accessor Name Derivation

Within a namespace, the attribute name for a class follows a deterministic rule:

**default accessor = `snake_case(ClassName) + "s"`**

| Class | Default accessor |
|---|---|
| `Sample` | `samples` |
| `TissueType` | `tissue_types` |
| `DNASample` | `dna_samples` |
| `CellLineQC` | `cell_line_qcs` |

The rule is intentionally simple — it does not attempt linguistically correct
plurals for irregular cases. When the default is unsuitable, override it with
the `mosaic_accessor` annotation:

```yaml
classes:
  Analysis:
    is_a: Entity
    annotations:
      mosaic_accessor: analytics     # instead of default "analysises"
```

---

## Accessor API

Every class accessor is an `EntityAccessor` instance. All methods forward to
the same underlying `MosaicClient` internals used by the generic surface.

| Method | Signature | Description |
|---|---|---|
| `create` | `create(data) → dict` | Insert a new entity. Raises `ValidationFailed` on failure. |
| `put` | `put(data, entity_id=None) → dict` | Insert or update by ID. Raises `ValidationFailed` on failure. |
| `replace` | `replace(entity_id, data) → dict` | Full replacement. Raises `ValidationFailed` on failure. |
| `get` | `get(entity_id, expand=None) → dict` | Fetch a single entity by ID. |
| `query` | `query(**kwargs) → list[dict]` | Filter entities; accepts the same kwargs as the generic `query`. |
| `delete` | `delete(entity_id) → bool` | Soft-delete (marks unavailable). SDK hooks run. |
| `history` | `history(entity_id) → list[dict]` | Full provenance history for an entity. |
| `state_at` | `state_at(entity_id, timestamp) → dict \| None` | Entity state as of a given UTC timestamp. |

`data` may be a plain `dict` or a Pydantic model instance — see [Pydantic model access](#pydantic-model-access).

---

## Pydantic Model Access

When `SchemaRegistry` loads a schema, Mosaic generates a Pydantic v2 model class
for each domain class and attaches it to the accessor.

```python
accessor = client.samples
Model = accessor.model_class     # Pydantic class for "Sample"
print(Model.__name__)            # "Sample"
```

You can pass either a plain dict or a Pydantic instance to any write method:

```python
# Dict form
client.samples.create({"name": "S001"})

# Pydantic instance — field validation runs at construction time
from mosaic.core.typed_client import EntityAccessor
sample = client.samples.model_class(id="x1", name="S001")
client.samples.create(sample)
```

When Pydantic generation fails at load time, `model_class` is `None` and the
accessor falls back to plain-dict operation with a logged warning.

### Direct Import via `mosaic.models`

As an alternative to `accessor.model_class`, Mosaic registers every generated
class under `mosaic.models.<namespace>` so you can import classes directly:

```python
# Root-namespace class
from mosaic.models import Sample
sample = Sample(id="x1", name="S001")
client.samples.create(sample)

# Non-root namespace class (mosaic_namespace: tissue)
from mosaic.models.tissue import TissueSample

# Nested namespace class (mosaic_namespace: assay.quant)
from mosaic.models.assay.quant import Measurement
```

The `mosaic.models` package is populated by `MosaicClient.__init__` when a
`SchemaRegistry` is provided. The modules are not available until after the
first `MosaicClient(registry=...)` construction.

Classes are keyed by their LinkML class name (e.g. `Sample`, `TissueSample`)
not by the accessor name (`samples`, `tissue_samples`). Root-namespace classes
land on `mosaic.models` itself; non-root classes land on
`mosaic.models.<namespace>`. Intermediate parent modules (e.g. `mosaic.models.assay`
when only `assay.quant` has classes) are registered as empty modules so import
chains resolve.

**Multi-client note:** If multiple `MosaicClient` instances with different
registries coexist in the same process, the last-constructed client's classes
populate `mosaic.models`. Concurrent multi-registry use in the same process is
not supported.

---

## Error Handling

### Load-Time Errors — `TypedClientError`

`MosaicClient.__init__` raises `TypedClientError` when the schema produces an
ambiguous accessor surface. Four collision cases, each identified by the
`.case` field:

| `.case` | Trigger | Resolution |
|---|---|---|
| `"duplicate_accessor"` | Two classes in the same namespace derive the same accessor name | Add `mosaic_accessor` to at least one class |
| `"accessor_vs_namespace"` | A class accessor conflicts with a sub-namespace segment at the same attribute level | Rename the class, namespace, or add `mosaic_accessor` |
| `"namespace_reserved"` | A namespace segment uses an SDK-reserved name (`query`, `storage`, …) | Rename the namespace |
| `"reserved_root"` | A class declares `namespace: root` | Use a different namespace name — `root` is reserved |
| `"accessor_reserved"` | A class's derived or override accessor conflicts with a public `MosaicClient` attribute | Override with `mosaic_accessor` |

```python
from mosaic.core.typed_client import TypedClientError

try:
    client = MosaicClient(storage=storage, registry=conflicting_registry)
except TypedClientError as exc:
    print(exc.case)   # e.g. "duplicate_accessor"
    print(exc)        # actionable message with fix suggestion
```

Error messages include the class name(s), the conflicting accessor, and a
concrete schema fix. Example:

```
SchemaRegistry load error: typed-client accessor collision.
  Classes `tissue.DNASample` and `tissue.DnaSample` both resolve to
  accessor `dna_samples` in namespace `tissue`.
  Add `mosaic_accessor` to at least one class to disambiguate.
```

### Write-Time Errors — `ValidationFailed`

Write methods (`create`, `put`, `replace`) raise `ValidationFailed` when
validation fails. The exception carries full context:

```python
from mosaic.core.exceptions import ValidationFailed

try:
    client.samples.create({"name": "bad"})
except ValidationFailed as exc:
    print(exc.entity_type)   # "Sample"
    print(exc.entity_id)     # None for create, the ID for replace/put-with-id
    result = exc.result      # ValidationResult envelope (may be None for empty-data rejection)
    if result:
        envelope = result.to_envelope()
        for failure in envelope["failures"]:
            print(failure["message"])
```

Empty data (`{}` or `None`) is rejected uniformly with `ValidationFailed`
before any validators run.

---

## Typed vs. Generic Client

The typed and generic surfaces share the same SDK internals. No capability
exists in one that is absent from the other.

```python
# These two calls produce identical results:
client.samples.create({"name": "S001"})          # typed
client.put("Sample", {"name": "S001"})            # generic

# Entities created via either surface are visible to the other:
entity = client.put("Sample", {"name": "via-generic"})
client.samples.get(entity["id"])                  # works

entity2 = client.samples.create({"name": "via-typed"})
client.get("Sample", entity2["id"])               # works
```

Use the typed surface when you want class-specific IDE completion and
Pydantic field validation. Use the generic surface when operating on
entity types that are only known at runtime.

---

## Limitations

The typed client does not provide:

- **Query-builder syntax.** `client.samples.query(...)` accepts the same
  filter dict as `client.query("Sample", ...)`. A typed query-builder is a
  future refinement, not part of the current surface.
- **Relationship traversal helpers.** Relationship traversal lives on the
  query engine, which is adapter-agnostic.
- **Compile-time schema checks.** The Pydantic classes are generated from
  the schema loaded at runtime; if the schema changes between process starts,
  the accessor surface changes accordingly.
- **`mosaic_core` infrastructure classes.** `ProvenanceRecord`, `Process`,
  `Validator`, `ReferenceLoader`, and `Entity` are system concerns and are
  not exposed as typed accessors.
