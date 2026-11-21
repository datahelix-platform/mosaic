# Polymorphic Ingest

This guide explains how Mosaic ingests **polymorphic collections** — collections
whose members can be one of several subtypes of a common base class — and how to
fix the most common error you may hit when authoring them.

If you landed here from an ingest error like *"…would silently drop those
field(s)…"* or *"…is an abstract base class…"*, jump to
[Fixing the dispatch error](#fixing-the-dispatch-error).

---

## Background

Mosaic stores **one table per concrete class**. A `SolidSample` is stored in the
`SolidSample` table, an `RNASeqAssay` in the `RNASeqAssay` table, and so on. An
entity only keeps its class-specific fields if it is stored as that class — there
is no shared "samples" table that could hold every subtype's columns.

When you `mosaic ingest` a bundle, each top-level key is a **collection accessor**
and its value is a list of instances:

```yaml
samples:
  - id: S1
    category: SolidSample
    tissue: brain
assays:
  - id: A1
    category: RNASeqAssay
    platform: Illumina
    read_length: 150
```

The question Mosaic must answer for every instance is: **which concrete class do I
store this as?** For a collection of a single concrete type the answer is obvious.
For a *polymorphic* collection — `samples:` holding both `SolidSample` and
`LiquidSample`, or `assays:` holding `RNASeqAssay` — Mosaic needs a signal in the
data that names each instance's real type. That signal is the LinkML
**type designator**.

---

## The idiomatic pattern: `designates_type`

Mark one slot on the base class with `designates_type: true`. Its value on each
instance names the concrete subclass that instance should be stored as. This is
standard LinkML — `linkml-validate` understands it, and so does Mosaic.

```yaml
classes:
  Sample:
    is_a: Entity
    abstract: true          # a base may be abstract or concrete
    attributes:
      category:
        range: string
        designates_type: true   # ← the discriminator
  SolidSample:
    is_a: Sample
    attributes:
      tissue:
        range: string
  LiquidSample:
    is_a: Sample
    attributes:
      volume_ml:
        range: string
```

With this in place:

- The base class gets a collection accessor (`samples:`) even when it is
  **abstract** — so a bundle keyed by the base validates and ingests.
- Each instance is **dispatched** to the concrete subclass named by `category`
  and stored there, so subtype fields (`tissue`, `volume_ml`) persist and the
  instance is queryable as its real type:

```python
client.query("SolidSample")   # → S1
client.get("SolidSample", "S1")["data"]["tissue"]   # → "brain"
```

The designator value is matched against each candidate subclass's **name** first,
then its `class_uri`, then the CURIE/short form of that URI. Using the bare class
name (`category: SolidSample`) is the simplest and recommended form.

!!! note "Accessor names"
    Collection keys follow Mosaic's accessor convention — `snake_case(ClassName)`
    pluralized (e.g. `Sample` → `samples`, `RNASeqAssay` → `rna_seq_assays`),
    overridable per class with the `mosaic_accessor` annotation. Both the **base**
    accessor (`samples:`, dispatched by `category`) and the **concrete** accessors
    (`solid_samples:`, `liquid_samples:`) work; use whichever reads best.

---

## Fixing the dispatch error

Mosaic refuses to ingest an instance when it cannot determine the concrete class to
store it as — rather than silently dropping the subtype's fields. You will see one
of these messages:

### "…carries field(s) […] that '<Base>' does not define …declares no type designator"

The collection's base class has subclasses, and your instance has fields that only
exist on a subclass — but the base declares no `designates_type` slot, so Mosaic
can't tell which subclass you mean. **Two ways to fix it:**

1. **Add a discriminator (recommended).** Mark a slot on the base
   `designates_type: true` and set it on each instance to the concrete subclass
   name:

    ```yaml
    classes:
      Assay:
        attributes:
          category:
            range: string
            designates_type: true   # add this
    ```
    ```yaml
    assays:
      - id: A1
        category: RNASeqAssay       # set this on each instance
        platform: Illumina
        read_length: 150
    ```

2. **Ingest under the concrete accessor.** Key the bundle by the subclass instead
   of the base:

    ```yaml
    rna_seq_assays:                 # instead of `assays:`
      - id: A1
        platform: Illumina
        read_length: 150
    ```

### "…is an abstract base class and is never stored directly…"

You ingested an instance under an **abstract** base accessor but Mosaic couldn't
route it to a concrete subclass — either the base declares no designator, or this
instance is missing the designator value. Fix it by setting the designator slot on
each instance to a concrete subclass name (the error lists the valid options), or
by ingesting under a concrete accessor as above.

### "Type designator <slot>=<value> does not name '<Base>' or any of its subclasses"

The discriminator value doesn't match any subclass. Check for a typo and set it to
one of the concrete subclass names listed in the error (matched by class name).

---

## Why Mosaic refuses instead of guessing

Storing a subtype instance as its base class would compile and "succeed" — but the
subtype's columns don't exist on the base table, so those values would vanish with
no error. Because that silent data loss is exactly the failure mode this behavior
prevents, Mosaic fails loudly with a fix instead. Declaring the `designates_type`
discriminator (or using the concrete accessor) is the idiomatic, lossless path.

## References to a polymorphic base

A slot whose range **is** a polymorphic base — e.g. `Sighting.animal` ranged on
`Animal`, or `Measurement.person` ranged on a `Person` base — points at a
referent that may be any of the base's concrete subtypes. Because each subtype
is dispatched into its own table (above), the base table is not where those
referents live, so Mosaic stores such a reference as a plain id value rather
than a table-level foreign key: the id resolves across the subtype tables at
read time. This applies to both abstract bases and concrete bases that have
concrete subclasses. A reference to a concrete *leaf* class (no subclasses)
keeps an enforced foreign key, since every referent lives in that one table.

You author these references exactly as any other — just give the referent's
`id`. No annotation or special handling is required (issue #93).

See also: [Schema Guide](schema-guide.md), [CLI Reference](cli-reference.md).
