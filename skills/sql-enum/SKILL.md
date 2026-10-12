---
name: sql-enum
description: Model Rust-style enums (sum types whose variants carry fields) in Postgres as flat columns, with a native Postgres ENUM `*_variant` discriminator column, per-variant field columns, and a CHECK constraint that enforces each variant's shape. Use when designing, migrating, querying, or mapping such columns to Rust, e.g. "store this Rust enum in Postgres" or "add a variant to the payment method column".
---

# SQL Enum

Store a Rust enum like

```rust
enum PaymentMethod {
    Card { last4: String, network: String },
    BankTransfer { iban: String, bic: Option<String> },
    Cash,
}
```

in a column `method` of table `payment` as flat columns that the database itself keeps valid:

```sql
CREATE TYPE payment_method_variant AS ENUM ('card', 'bank_transfer', 'cash');

CREATE TABLE payment (
    id bigint PRIMARY KEY,
    method_variant payment_method_variant NOT NULL,
    method_card_last4 text,
    method_card_network text,
    method_bank_transfer_iban text,
    method_bank_transfer_bic text,
    CONSTRAINT payment_method_variant_check CHECK (
        CASE method_variant
            WHEN 'card' THEN method_card_last4 IS NOT NULL AND method_card_network IS NOT NULL AND method_bank_transfer_iban IS NULL AND method_bank_transfer_bic IS NULL
            WHEN 'bank_transfer' THEN method_card_last4 IS NULL AND method_card_network IS NULL AND method_bank_transfer_iban IS NOT NULL
            WHEN 'cash' THEN method_card_last4 IS NULL AND method_card_network IS NULL AND method_bank_transfer_iban IS NULL AND method_bank_transfer_bic IS NULL
            ELSE false
        END
    )
);
```

## Convention

| Part | Name | Notes |
| --- | --- | --- |
| Discriminator type | `<enum_snake>_variant` (e.g. `payment_method_variant`) | Native `CREATE TYPE .. AS ENUM`. Labels are the snake_case variant names, in declaration order. One type per Rust enum, shared by every column that stores that enum. |
| Discriminator column | `<col>_variant` | `NOT NULL`, unless the Rust field is `Option<Enum>`. Never call it `tag`, `kind` or `type`. |
| Field column | `<col>_<variant>_<field>` (e.g. `method_card_last4`) | Always nullable at column level, because it is NULL for the other variants. Unit variants have no columns. |
| Constraint | `<table>_<col>_variant_check` | Single `CASE` over the discriminator, ending in `ELSE false`. |

Rules the constraint must follow, per variant:

- Every field of *other* variants is `IS NULL`.
- Every required field of *this* variant is `IS NOT NULL`. `Option<T>` fields are left unconstrained.
- A variant with no conditions uses `THEN true`.
- Keep `ELSE false`. A CHECK passes when its expression is NULL, so without it a label added later with `ALTER TYPE .. ADD VALUE` would be accepted with arbitrary field values. With it, the new label is rejected until the constraint is updated. The native enum already rejects unknown labels.
- For `Option<Enum>`, drop `NOT NULL` from the discriminator and prefix the CASE with `(<col>_variant IS NULL AND <all fields> IS NULL) OR`. Without that branch, `CASE NULL` falls through to `ELSE false`.

Field types and per-field constraints go on the flat column as usual: `REFERENCES`, `CHECK (length(method_card_last4) = 4)`, defaults, and so on. A nullable foreign-key column works as a variant-only foreign key. Keep identifiers within Postgres' 63-character limit and shorten the column prefix if needed.

Generate the DDL rather than hand-writing long constraints:

```bash
python3 scripts/generate_enum_sql.py create spec.json      # CREATE TYPE + lines for CREATE TABLE
python3 scripts/generate_enum_sql.py alter spec.json       # CREATE TYPE + ALTER TABLE .. ADD COLUMN/CONSTRAINT
python3 scripts/generate_enum_sql.py constraint spec.json  # replace the CHECK after variants change
```

```json
{"table": "payment", "column": "method", "enum_type": "payment_method_variant", "nullable": false,
 "variants": {"card": {"last4": "text", "network": "text"}, "bank_transfer": {"iban": "text", "bic": "text?"}, "cash": {}}}
```

A trailing `?` marks an optional field, and field types are copied verbatim. `enum_type` defaults to `<column>_variant`. Pass it explicitly when the enum type is named after the Rust enum or shared between columns.

## Querying

- Filter by variant with `method_variant = 'card'` or `method_variant IN ('card', 'cash')`. Inside such a filter the CHECK guarantees the variant's required fields are non-NULL. With sqlx `query!`, tell it so with `AS "last4!"`.
- Match with `CASE method_variant WHEN 'card' THEN .. WHEN 'bank_transfer' THEN .. WHEN 'cash' THEN .. END`, listing every label. Postgres does not check exhaustiveness: an unlisted label silently yields NULL. Prefer listing the labels over `ELSE`, so that adding a variant is a search for `<col>_variant` rather than a silent change.
- Variant-specific indexes and uniqueness are partial indexes: `CREATE UNIQUE INDEX .. ON payment (method_bank_transfer_iban) WHERE method_variant = 'bank_transfer'`.
- Never write variant columns one at a time across statements. Set the discriminator and *all* field columns in the same `INSERT`/`UPDATE`, nulling the other variants' fields, or the CHECK rejects the row.
- Changing a field within the current variant is safe when guarded: `UPDATE .. SET method_card_last4 = $2 WHERE id = $1 AND method_variant = 'card'`.

## When this layout is the wrong tool

- Variants with many fields, their own relations, or heavy per-variant indexing are better as one table per variant keyed by the parent id, with a `(id, variant)` composite foreign key and a native-enum `variant` column on the parent.
- If the user only needs labels without data (a C-style enum), a plain native ENUM column is enough.

## References

- Rust/sqlx mapping (variant enum, row struct, `TryFrom`, binding parameters): read [references/rust-mapping.md](references/rust-mapping.md) when writing the Rust side.
- Adding, renaming or removing variants or fields, and migration transaction rules: read [references/evolution.md](references/evolution.md) before writing a migration that changes an existing enum.
