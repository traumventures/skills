---
name: sql-enum
description: Store tagged unions (JSON Schema oneOf with a const discriminator; Rust enums with data) in Postgres as flat columns with a native ENUM type and column both named `*_variant`, per-variant field columns and a CHECK constraint enforcing each variant's shape. Use when creating, migrating or querying such columns, e.g. "store this payment method union in Postgres" or "add a variant to payment_method".
---

# SQL Enum

The source of truth is a JSON Schema **tagged union**: a `oneOf` whose object branches share a discriminator property with one `const` string per branch. Postgres stores it flat, and constraints keep it valid for every writer. For the Rust side, read [rust.md](rust.md).

```json
{
  "title": "PaymentMethod",
  "oneOf": [
    {"title": "Card", "type": "object",
     "properties": {"variant": {"const": "card"}, "last4": {"type": "string"},
                    "network": {"title": "CardNetwork", "enum": ["visa", "mastercard", "amex"]}},
     "required": ["variant", "last4", "network"]},
    {"title": "BankTransfer", "type": "object",
     "properties": {"variant": {"const": "bank_transfer"}, "iban": {"type": "string"},
                    "bic": {"type": ["string", "null"]}},
     "required": ["variant", "iban"]},
    {"title": "Cash", "type": "object",
     "properties": {"variant": {"const": "cash"}}, "required": ["variant"]}
  ]
}
```

```sql
CREATE TYPE card_network AS ENUM ('visa', 'mastercard', 'amex');
CREATE TYPE payment_method_variant AS ENUM ('card', 'bank_transfer', 'cash');

CREATE TABLE payment (
    id bigint PRIMARY KEY,
    payment_method_variant payment_method_variant NOT NULL,
    payment_method_card_last4 text,
    payment_method_card_network card_network,
    payment_method_bank_transfer_iban text,
    payment_method_bank_transfer_bic text,
    CONSTRAINT payment_method_variant_check CHECK (
        CASE payment_method_variant
            WHEN 'card' THEN payment_method_card_last4 IS NOT NULL AND payment_method_card_network IS NOT NULL AND payment_method_bank_transfer_iban IS NULL AND payment_method_bank_transfer_bic IS NULL
            WHEN 'bank_transfer' THEN payment_method_card_last4 IS NULL AND payment_method_card_network IS NULL AND payment_method_bank_transfer_iban IS NOT NULL
            WHEN 'cash' THEN payment_method_card_last4 IS NULL AND payment_method_card_network IS NULL AND payment_method_bank_transfer_iban IS NULL AND payment_method_bank_transfer_bic IS NULL
            ELSE false
        END
    )
);
```

## Mapping

| JSON Schema | Postgres |
| --- | --- |
| Union `title` `PaymentMethod` | Enum name `payment_method` (snake_case); override with `--name` |
| Discriminator `const` values | Labels of the native type `payment_method_variant`, in branch order |
| Discriminator property `variant` (other names work) | Column `payment_method_variant` of that type, `NOT NULL` |
| Branch property `last4` of `card` | Column `payment_method_card_last4`, always nullable at column level |
| Property in `required` and not allowing `null` | `IS NOT NULL` inside its own variant |
| Property not required, or `"type": [.., "null"]` | Unconstrained inside its own variant |
| `string`, `integer`, `number`, `boolean` | `text`, `bigint`, `double precision`, `boolean` |
| `format`: `date-time`, `date`, `time`, `uuid`, `duration`, `ipv4`/`ipv6` | `timestamptz`, `date`, `time`, `uuid`, `interval`, `inet` |
| `format`: `int16`/`int32`/`int64`, `float`/`double`; `contentEncoding: base64` | `smallint`/`integer`/`bigint`, `real`/`double precision`; `bytea` |
| String `enum` with a `title` (or a `$defs` name) | Its own native type `<title>` (e.g. `card_network`) |
| `array` of a scalar | `<type>[]` |
| `"x-sql": "bigint REFERENCES device(id)"` on a property | Column type verbatim; a nullable FK works as a variant-only FK |
| Union used as an optional value (`Option<PaymentMethod>`) | Table option `nullable`: discriminator column without `NOT NULL` |

**Naming:** everything is named after the enum, and the discriminator is called *variant* everywhere: JSON property `variant`, SQL type and column `<enum>_variant` (Postgres keeps type and column names apart, so sharing the name is fine), Rust `PaymentMethodVariant`. Field columns are `<enum>_<variant>_<field>`, constraint `<enum>_variant_check`. Never shorten it for the columns: for `method_*` columns, name the union `Method`. The only exception is when one table stores the same enum twice. Then each copy gets a role prefix ending in the enum name (`refund_payment_method_*`, via `--table 'refund,prefix=refund_payment_method'`). The type stays `payment_method_variant`.

**The constraint**, per variant: other variants' fields `IS NULL`, own required fields `IS NOT NULL`, `ELSE false` at the end. `ELSE false` matters because a CHECK passes on NULL: a label added later is rejected until the constraint describes its shape. The native enum already rejects unknown labels.

**Not flattenable:** a field that is itself a tagged union, or a free-form object. Use a separate table per variant (keyed by the parent id), or keep that value as JSON with `"x-sql": "jsonb"`. If the whole union is rarely queried by field, a single `jsonb` column validated by the application is simpler than this layout. If no variant has fields, a plain native enum column is enough.

## Tool

`scripts/sql_enum.py` reads the JSON Schema (file or `-`) and prints SQL; `--json` gives machine-readable output. Pick a union with `--def NAME` when the file has several. Pass `--table` once per table that stores the enum.

```bash
python3 scripts/sql_enum.py show payment.schema.json                       # variants, columns, SQL types
python3 scripts/sql_enum.py setup payment.schema.json --table payment      # CREATE TYPE + columns for CREATE TABLE
python3 scripts/sql_enum.py setup payment.schema.json --table 'subscription,nullable' --form alter --no-types
python3 scripts/sql_enum.py modify payment.schema.json --table payment --table 'subscription,nullable' \
    --add-variant 'paypal={"properties":{"email":{"type":"string"}}}' --write
```

`modify` applies the actions in order, prints the migration(s) and notes, and prints the updated schema (or saves it with `--write`). Actions use labels and property names:

| Action | Example |
| --- | --- |
| `--add-variant LABEL[=BRANCH]` | `paypal='{"properties":{"email":{"type":"string"}}}'` (properties are required unless they allow null) |
| `--remove-variant LABEL` | `cash` |
| `--rename-variant OLD=NEW` | `bank_transfer=sepa` |
| `--add-field LABEL.FIELD=PROPERTY` | `card.expiry='{"type":"string","x-backfill":"$$12/30$$"}'` |
| `--remove-field LABEL.FIELD` | `bank_transfer.bic` |
| `--rename-field LABEL.OLD=NEW` | `card.last4=last_four` |

## Creating

1. Write or extend the JSON Schema union. Check it with `show`.
2. Run `setup`. Put `CREATE TYPE` in the migration once per enum; other tables storing the same enum use `--no-types`.
3. For a new table, paste the column lines into `CREATE TABLE`. For an existing table, use `--form alter`. On a table that already has rows, add the variant column without `NOT NULL`, fill it, then set `NOT NULL` and add the constraint.
4. Write the application mapping, e.g. [rust.md](rust.md).

## Evolving

Always change the schema through `modify` and run what it prints. Every table storing the enum must be passed as `--table`, because they share the type.

| Change | What the migration does | Watch out |
| --- | --- | --- |
| Add variant | Migration 1: `ADD VALUE`. Migration 2: new columns, regenerated constraint. | A new value is unusable, even in a CHECK, until its transaction commits. Keep them as two migration files. Between them, `ELSE false` rejects the new label. |
| Add field | `ADD COLUMN`, backfill existing rows of that variant, regenerated constraint | A required field needs `"x-backfill": "<SQL expr>"` or must allow null. |
| Rename variant | `RENAME VALUE`, rename its field columns | The constraint follows renames automatically. |
| Rename field | `RENAME COLUMN` | |
| Remove field | `DROP COLUMN`, regenerated constraint | |
| Remove variant | Guard (fails if rows still use it), drop constraints, swap in a new type, drop its columns, re-add constraints | Postgres cannot drop enum values. Migrate or delete those rows first. Partial indexes, views, functions and defaults on these columns break the swap: drop them before and recreate them after. |

After any label change, update SQL matching on `<enum>_variant` (`CASE`, `WHERE`, partial indexes, views) and the application types. Postgres does not check `CASE` exhaustiveness: an unlisted label yields NULL.

## Querying and writing

- Filter with `payment_method_variant = 'card'`. Inside that filter the variant's required fields are guaranteed non-NULL (sqlx: `AS "last4!"`).
- Match with `CASE payment_method_variant WHEN 'card' THEN .. WHEN 'bank_transfer' THEN .. WHEN 'cash' THEN .. END`. List every label instead of using `ELSE`, so new variants are found by searching.
- Variant-specific uniqueness and indexes are partial: `CREATE UNIQUE INDEX .. ON payment (payment_method_bank_transfer_iban) WHERE payment_method_variant = 'bank_transfer'`.
- Write the variant column and all field columns in the same `INSERT`/`UPDATE`, nulling the other variants' fields. Change a single field only under a guard: `.. WHERE id = $1 AND payment_method_variant = 'card'`.
