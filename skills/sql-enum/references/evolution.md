# Changing an existing enum

All of these were verified on Postgres 17. Regenerate the constraint with
`scripts/generate_enum_sql.py constraint <updated spec>` instead of editing it by hand.

## Transaction rule

A label added with `ALTER TYPE .. ADD VALUE` cannot be used, including in a CHECK expression, until the
transaction that added it has committed. Postgres reports `unsafe use of new value`. Migration runners such as
sqlx and refinery wrap each file in a transaction, so split the change into separate migration files.

## Add a variant

1. Migration A: `ALTER TYPE payment_method_variant ADD VALUE 'paypal';` Add `AFTER 'cash'` only when sort order matters.
2. Migration B:
   - `ALTER TABLE payment ADD COLUMN method_paypal_email text;`
   - Replace the constraint, which the generator's `constraint` mode does: `DROP CONSTRAINT`, `ADD CONSTRAINT .. NOT VALID`, then `VALIDATE CONSTRAINT`. `NOT VALID` plus `VALIDATE` avoids holding a long exclusive lock while existing rows are checked.
3. Update every `CASE <col>_variant` in SQL, views and functions, and the Rust `match` arms. Search for the discriminator column name.

Between A and B the old constraint's `ELSE false` rejects rows using the new label. That is intended: the new
label is unusable until its shape is defined.

If several tables share the type, every table's constraint must be regenerated in migration B.

## Add a field to a variant

Add the nullable column, backfill existing rows of that variant if the field is required, then regenerate the
constraint. For a large table, regenerate with `NOT VALID` first and `VALIDATE` after the backfill.

## Rename a variant

`ALTER TYPE payment_method_variant RENAME VALUE 'cash' TO 'cash_payment';`. The CHECK constraint follows
automatically. Field column names still contain the old name, so rename them with `ALTER TABLE .. RENAME
COLUMN` and regenerate the constraint so names and the spec stay in sync. Update the Rust `#[sqlx(rename)]`
or variant name.

## Remove a variant

Postgres cannot drop an enum label. Swap the type instead:

```sql
-- 1. No rows may use the label any more (migrate or delete them first).
-- 2. In one migration:
CREATE TYPE payment_method_variant_new AS ENUM ('card', 'bank_transfer');
ALTER TABLE payment DROP CONSTRAINT payment_method_variant_check;  -- it references the old type
ALTER TABLE payment ALTER COLUMN method_variant TYPE payment_method_variant_new
    USING method_variant::text::payment_method_variant_new;
DROP TYPE payment_method_variant;
ALTER TYPE payment_method_variant_new RENAME TO payment_method_variant;
-- Drop the removed variant's field columns here, if it had any.
-- 3. Re-add the regenerated constraint.
```

Dropping the constraint first is required: `ALTER COLUMN .. TYPE` fails with `operator does not exist:
payment_method_variant_new = payment_method_variant` while the old CHECK exists. Views, functions, defaults,
and other columns that use the type must also be dropped and recreated, or altered, before `DROP TYPE`.

## Make the column optional or required

Required to optional: `ALTER COLUMN <col>_variant DROP NOT NULL` and regenerate with `"nullable": true`.
Optional to required: backfill NULL rows, `SET NOT NULL`, and regenerate with `"nullable": false`.
