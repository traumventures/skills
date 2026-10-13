---
name: sql-enum
description: Store tagged unions (JSON Schema oneOf with a const discriminator; Rust enums with data) in Postgres as flat columns - a native ENUM `*_variant` type, per-variant field columns and a CHECK constraint enforcing each variant's shape - including nested unions, schema evolution and the Rust/sqlx mapping. Use when creating, migrating or querying such columns, e.g. "store this payment method union in Postgres" or "add a variant to payment_method".
---

# SQL Enum

The source of truth is a JSON Schema **tagged union**: a `oneOf` whose object branches share a discriminator property `variant` with one `const` string per branch. Postgres stores it flat, and constraints keep it valid for every writer. For the Rust side, read [rust.md](rust.md).

This is a **baseline**, not the whole validation story. The layout guarantees each row has exactly one variant with that variant's required fields set, and the types guarantee basic shape. Everything else is extra and can go on top: `pattern`/`minLength`/`maximum` in the JSON Schema, per-column `CHECK`s (`length(..) = 4`, ranges, regexes), domain types, and parsing into richer Rust types (an `Iban` newtype instead of `String`). Put such rules where they belong. The generated constraint only checks presence.

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
    payment_method payment_method_variant NOT NULL,
    payment_method_card_last4 text,
    payment_method_card_network card_network,
    payment_method_bank_transfer_iban text,
    payment_method_bank_transfer_bic text,
    CONSTRAINT payment_method_check CHECK (
        CASE payment_method
            WHEN 'card' THEN payment_method_card_last4 IS NOT NULL AND payment_method_card_network IS NOT NULL AND payment_method_bank_transfer_iban IS NULL AND payment_method_bank_transfer_bic IS NULL
            WHEN 'bank_transfer' THEN payment_method_card_last4 IS NULL AND payment_method_card_network IS NULL AND payment_method_bank_transfer_iban IS NOT NULL
            WHEN 'cash' THEN payment_method_card_last4 IS NULL AND payment_method_card_network IS NULL AND payment_method_bank_transfer_iban IS NULL AND payment_method_bank_transfer_bic IS NULL
            ELSE false
        END
    )
);
```

## Naming

Everything is named after the union (`PaymentMethod` -> `payment_method`), and the discriminator is called *variant* everywhere:

| | JSON Schema | Postgres | Rust |
| --- | --- | --- | --- |
| Union | `"title": "PaymentMethod"` | prefix `payment_method` | `enum PaymentMethod` |
| Discriminator | property `variant`, `const` per branch | column `payment_method` of type `payment_method_variant` | `enum PaymentMethodSqlVariant` |
| Field `last4` of `card` | branch property | column `payment_method_card_last4` | field `last4` / column field `payment_method_card_last4` |
| Constraint | | `payment_method_check` | |

- Never shorten the prefix for the columns. For `method_*` columns, name the union `Method`. The Rust struct field matches the column: `payment_method: PaymentMethod`.
- The type is created once and shared by every table storing the union.
- When one table stores the same union twice, each copy gets a role prefix ending in the union name: `refund_payment_method`, `refund_payment_method_card_last4`, via `--table 'refund,prefix=refund_payment_method'`. The type stays `payment_method_variant`.
- Keep identifiers within Postgres' 63 characters. If one gets too long, shorten the union, variant or field name, not just the column.

## Type mapping

| JSON Schema | Postgres column |
| --- | --- |
| `string` (also `format: email`, `uri`, ...) | `text` |
| `integer` / `format: int64` · `int32` · `int16` | `bigint` · `integer` · `smallint` |
| `format: uint8` · `uint16` · `uint32` · `uint64` | `smallint` · `integer` · `bigint` · `numeric(20)` (next wider signed type) |
| `number` / `format: double` · `float` | `double precision` · `real` |
| `string` + `format: decimal` | `numeric` |
| `boolean` | `boolean` |
| `string` + `contentEncoding: base64` | `bytea` |
| `format: date-time` · `date` · `time` · `duration` | `timestamptz` · `date` · `time` · `interval` |
| `format: uuid` · `ipv4`/`ipv6` | `uuid` · `inet` |
| String `enum` with a `title` (or `$defs` name) | its own native type, e.g. `card_network` |
| `array` of a scalar or string enum | `text[]`, `card_network[]`, ... |
| Nested tagged union | a nested `_variant` column plus its own field columns (below) |
| Anything else, or to override | `"x-sql": "<column type>"`, e.g. `"timestamp"`, `"varchar(20)"`, `"jsonb"`, `"bigint REFERENCES device(id)"` |
| Property in `required` and not allowing `null` | `IS NOT NULL` inside its own variant |
| Not required, or `"type": [.., "null"]` | unconstrained inside its own variant |
| The union itself optional (`Option<PaymentMethod>`) | table option `nullable`: the variant column has no `NOT NULL` |

## The constraint

Per variant: other variants' fields `IS NULL`, own required fields `IS NOT NULL`. The `CASE` ends in `ELSE false`. That matters because a CHECK passes on NULL: a label added later is rejected until the constraint describes its shape. The native enum already rejects unknown labels. An optional union adds `(payment_method IS NULL AND <all fields> IS NULL) OR` in front.

## Nested unions

A field whose schema is itself a tagged union is flattened recursively. Its discriminator becomes a nullable column named after the field, of the nested union's `_variant` type, and its fields continue the prefix. Each level gets its own constraint. The parent's constraint treats the nested column like any other field.

```json
"Card": {"properties": {"variant": {"const": "card"}, "last4": {"type": "string"},
                        "wallet": {"anyOf": [{"$ref": "#/$defs/Wallet"}, {"type": "null"}]}},
         "required": ["variant", "last4"]},
"Wallet": {"title": "Wallet", "oneOf": [
  {"properties": {"variant": {"const": "apple_pay"}, "device_account": {"type": "string"}}, "required": ["variant", "device_account"]},
  {"properties": {"variant": {"const": "google_pay"}}, "required": ["variant"]}]}
```

```sql
CREATE TYPE wallet_variant AS ENUM ('apple_pay', 'google_pay');
    payment_method payment_method_variant NOT NULL,
    payment_method_card_last4 text,
    payment_method_card_wallet wallet_variant,
    payment_method_card_wallet_apple_pay_device_account text,
    CONSTRAINT payment_method_check CHECK (CASE payment_method
        WHEN 'card' THEN payment_method_card_last4 IS NOT NULL
        WHEN 'cash' THEN payment_method_card_last4 IS NULL AND payment_method_card_wallet IS NULL
        ELSE false END),
    CONSTRAINT payment_method_card_wallet_check CHECK (
        (payment_method_card_wallet IS NULL AND payment_method_card_wallet_apple_pay_device_account IS NULL)
        OR CASE payment_method_card_wallet
            WHEN 'apple_pay' THEN payment_method_card_wallet_apple_pay_device_account IS NOT NULL
            WHEN 'google_pay' THEN payment_method_card_wallet_apple_pay_device_account IS NULL
            ELSE false END)
```

For the Rust side see the nested example in [rust.md](rust.md). Recursive unions (a union that contains itself) cannot be flattened. Neither can a nested union under an array (below).

## Relations and arrays

- **Outgoing relations work.** A field column can be a foreign key: `"device_id": {"type": "integer", "x-sql": "bigint REFERENCES device(id)"}`. Since it is NULL in every other variant, it is a variant-only foreign key, and `ON DELETE SET NULL` is rejected by the CHECK when the field is required. Use `RESTRICT`/`CASCADE` there.
- **Incoming relations only reach the row, never a variant.** A variant is not a row, so nothing can reference "the card part" of a payment. Other tables reference `payment(id)`. To require a specific variant, reference `(id, payment_method)` with a fixed second column. This also blocks changing the referenced row's variant:

  ```sql
  ALTER TABLE payment ADD UNIQUE (id, payment_method);
  CREATE TABLE chargeback (
      id bigint PRIMARY KEY,
      payment_id bigint NOT NULL,
      payment_method payment_method_variant NOT NULL GENERATED ALWAYS AS ('card'::payment_method_variant) STORED,
      FOREIGN KEY (payment_id, payment_method) REFERENCES payment (id, payment_method)
  );
  ```

  If a variant needs its own identity, its own incoming relations, or many fields, use one table per variant keyed by the parent id instead of this layout.
- **Arrays of scalars map to Postgres arrays**, but their elements cannot be foreign keys: `bigint[] REFERENCES ..` is rejected. Use a link table for that.
- **Arrays of objects or unions cannot be mapped**, because one row stores one value. A list of payment methods is a child table with one row per element, each storing one union with these columns. Alternatively, use `"x-sql": "jsonb"` when the elements are not queried or constrained.
- **Object fields** are not flattened. Model their parts as fields of the variant, or use `jsonb`.

## Tool

`scripts/sql_enum.py` has no dependencies. Run it with `uv run scripts/sql_enum.py ...` (or `python3`). It reads the JSON Schema from a file or `-`, prints SQL, and gives machine-readable output with `--json`. Pick a union with `--def NAME` when the file has several. Pass `--table` once for every table that stores the union.

```bash
uv run scripts/sql_enum.py show payment.schema.json                        # variants, columns, SQL types
uv run scripts/sql_enum.py setup payment.schema.json --table payment       # types + column lines for CREATE TABLE
uv run scripts/sql_enum.py setup payment.schema.json --table 'subscription,nullable' --form alter --no-types
uv run scripts/sql_enum.py modify payment.schema.json --table payment --table 'subscription,nullable' \
    --add-variant 'paypal={"properties":{"email":{"type":"string"}}}' --write
```

`setup` creates each type only if it is missing, and fails if an existing type has different values. `modify` applies the actions in order, prints the migration(s) and notes, and prints the updated schema (or saves it with `--write`):

| Action | Example |
| --- | --- |
| `--add-variant LABEL[=BRANCH]` | `paypal='{"properties":{"email":{"type":"string"}}}'` (properties are required unless they allow null) |
| `--remove-variant LABEL` | `cash` |
| `--rename-variant OLD=NEW` | `bank_transfer=sepa` |
| `--add-field LABEL.FIELD=PROPERTY` | `card.expiry='{"type":"string","x-backfill":"$$12/30$$"}'` |
| `--remove-field LABEL.FIELD` | `bank_transfer.bic` |
| `--rename-field LABEL.OLD=NEW` | `card.last4=last_four` |

Changes inside a nested union (e.g. a new `Wallet` variant) are a `modify` of that union, using its column as prefix: `modify schema.json --def Wallet --table 'payment,nullable,prefix=payment_method_card_wallet' --add-variant ..`.

## Creating

1. Write the JSON Schema union and check it with `show`.
2. Run `setup` and put its output in a migration. For a new table, paste the column lines into `CREATE TABLE`. For an existing table, use `--form alter`; if it has rows, add the variant column without `NOT NULL`, fill it, then set `NOT NULL` and add the constraint.
3. Write the application mapping, e.g. [rust.md](rust.md).
4. Add whatever validation goes beyond the baseline.

## Evolving

Always change the schema through `modify` and run what it prints. Pass every table that stores the union as `--table`, because they share the type.

| Change | What the migration does | Watch out |
| --- | --- | --- |
| Add variant | Migration 1: `ADD VALUE`. Migration 2: columns, constraints. | A new value is unusable, even in a CHECK, until its transaction commits, so keep two migration files. In between, `ELSE false` rejects the new label. |
| Add field | `ADD COLUMN`, backfill rows of that variant, constraints | A required field needs `"x-backfill": "<SQL expression>"` or must allow null. |
| Rename variant | `RENAME VALUE`, rename its field columns (nested ones too) | |
| Rename field | `RENAME COLUMN` for the field and any nested columns | |
| Remove field | `DROP COLUMN` (nested columns too), constraints | |
| Remove variant | Guard that fails while rows use it, then swap in a new type and drop its columns | Postgres cannot drop enum values. Migrate or delete those rows first. Partial indexes, views, functions and defaults on these columns block the swap: drop them before and recreate them after. |

Constraints are dropped and re-added (`NOT VALID` + `VALIDATE`) whenever the shape changes. After any label change, update SQL that matches on the variant column, and the application types. Postgres does not check `CASE` exhaustiveness: an unlisted label yields NULL.

## Querying and writing

- Filter with `payment_method = 'card'`. Inside that filter, the variant's required fields are guaranteed non-NULL (sqlx: `AS "last4!"`).
- Match with `CASE payment_method WHEN 'card' THEN .. WHEN 'bank_transfer' THEN .. WHEN 'cash' THEN .. END`. List every label instead of using `ELSE`, so new variants are found by searching.
- Variant-specific uniqueness and indexes are partial: `CREATE UNIQUE INDEX .. ON payment (payment_method_bank_transfer_iban) WHERE payment_method = 'bank_transfer'`.
- Write the variant column and all field columns in the same `INSERT`/`UPDATE`, nulling the other variants' fields. Change a single field only under a guard: `.. WHERE id = $1 AND payment_method = 'card'`.
