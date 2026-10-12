# Rust

Three types per union, all named after it:

- **Native type** `PaymentMethod`: the Rust enum the application uses. With serde it serializes to exactly the JSON Schema shape.
- **Variant type** `PaymentMethodVariant`: the fieldless enum mapped to the SQL type `payment_method_variant` (the values of the JSON `variant` property).
- **Columnar type** `PaymentMethodSqlFields`: one field per SQL column, with the same names. It is a plain `sqlx::FromRow` struct.

`From<PaymentMethod> for PaymentMethodSqlFields` is used for writing. `TryFrom<PaymentMethodSqlFields> for PaymentMethod` is used for reading; it only fails if the CHECK constraint is missing.

## JSON Schema to Rust

| JSON Schema | Rust |
| --- | --- |
| Union `PaymentMethod`, discriminator `variant` | `#[serde(tag = "variant", rename_all = "snake_case")] enum PaymentMethod` |
| Branch `{"const": "bank_transfer"}` | Struct variant `BankTransfer { .. }` (label = snake_case of the variant name) |
| Branch with no other properties | Unit variant `Cash` |
| Required property | Field `iban: String` |
| Nullable or not-required property | Field `bic: Option<String>` |
| `string` / `integer` / `number` / `boolean` | `String` / `i64` / `f64` / `bool` |
| `format`: `date-time`, `date`, `uuid` | `chrono::DateTime<Utc>` or `time::OffsetDateTime`, `NaiveDate`, `uuid::Uuid` |
| `format`: `int32`, `int16`, `float` | `i32`, `i16`, `f32` |
| String `enum` titled `CardNetwork` | Fieldless enum with `sqlx::Type`, `type_name = "card_network"` |
| Union used as `Option<PaymentMethod>` | Table option `nullable`; the columnar type gets `Option<PaymentMethodVariant>` |

Tuple variants (`Card(String)`) have no property names and do not work with `#[serde(tag = ..)]`, so always use named fields. A variant holding another data-carrying enum cannot be flattened; see SKILL.md.

## Implementation

```rust
use serde::{Deserialize, Serialize};

// Native type: the JSON Schema `PaymentMethod`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "variant", rename_all = "snake_case")]
pub enum PaymentMethod {
    Card { last4: String, network: CardNetwork },
    BankTransfer { iban: String, bic: Option<String> },
    Cash,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[serde(rename_all = "snake_case")]
#[sqlx(type_name = "card_network", rename_all = "snake_case")]
pub enum CardNetwork { Visa, Mastercard, Amex }

// Columnar type: same names as the SQL columns.
#[derive(Debug, Clone, Copy, PartialEq, Eq, sqlx::Type)]
#[sqlx(type_name = "payment_method_variant", rename_all = "snake_case")]
pub enum PaymentMethodVariant { Card, BankTransfer, Cash }

#[derive(Debug, Clone, PartialEq, sqlx::FromRow)]
pub struct PaymentMethodSqlFields {
    pub payment_method_variant: PaymentMethodVariant,
    pub payment_method_card_last4: Option<String>,
    pub payment_method_card_network: Option<CardNetwork>,
    pub payment_method_bank_transfer_iban: Option<String>,
    pub payment_method_bank_transfer_bic: Option<String>,
}

impl From<PaymentMethod> for PaymentMethodSqlFields {
    fn from(value: PaymentMethod) -> Self {
        let mut f = PaymentMethodSqlFields {
            payment_method_variant: PaymentMethodVariant::Cash,
            payment_method_card_last4: None,
            payment_method_card_network: None,
            payment_method_bank_transfer_iban: None,
            payment_method_bank_transfer_bic: None,
        };
        match value {
            PaymentMethod::Card { last4, network } => {
                f.payment_method_variant = PaymentMethodVariant::Card;
                f.payment_method_card_last4 = Some(last4);
                f.payment_method_card_network = Some(network);
            }
            PaymentMethod::BankTransfer { iban, bic } => {
                f.payment_method_variant = PaymentMethodVariant::BankTransfer;
                f.payment_method_bank_transfer_iban = Some(iban);
                f.payment_method_bank_transfer_bic = bic;
            }
            PaymentMethod::Cash => f.payment_method_variant = PaymentMethodVariant::Cash,
        }
        f
    }
}

impl TryFrom<PaymentMethodSqlFields> for PaymentMethod {
    type Error = String;

    fn try_from(f: PaymentMethodSqlFields) -> Result<Self, String> {
        fn req<T>(v: Option<T>, column: &str) -> Result<T, String> {
            v.ok_or_else(|| format!("{column} is NULL for its variant"))
        }
        Ok(match f.payment_method_variant {
            PaymentMethodVariant::Card => PaymentMethod::Card {
                last4: req(f.payment_method_card_last4, "payment_method_card_last4")?,
                network: req(f.payment_method_card_network, "payment_method_card_network")?,
            },
            PaymentMethodVariant::BankTransfer => PaymentMethod::BankTransfer {
                iban: req(f.payment_method_bank_transfer_iban, "payment_method_bank_transfer_iban")?,
                bic: f.payment_method_bank_transfer_bic,
            },
            PaymentMethodVariant::Cash => PaymentMethod::Cash,
        })
    }
}
```

Both `match`es are exhaustive, so after a schema change the compiler points at every place to update.

## Usage with sqlx

```rust
#[derive(sqlx::FromRow)]
struct PaymentRow {
    id: i64,
    #[sqlx(flatten)]
    payment_method: PaymentMethodSqlFields,
}

// Read
let rows: Vec<PaymentRow> = sqlx::query_as("SELECT * FROM payment").fetch_all(&pool).await?;
let method = PaymentMethod::try_from(rows[0].payment_method.clone())
    .map_err(|e| sqlx::Error::Decode(e.into()))?;

// Write: always all columns, so switching variants clears the old fields.
let f = PaymentMethodSqlFields::from(method);
sqlx::query(
    "INSERT INTO payment (id, payment_method_variant, payment_method_card_last4, payment_method_card_network,
                          payment_method_bank_transfer_iban, payment_method_bank_transfer_bic)
     VALUES ($1, $2, $3, $4, $5, $6)",
)
.bind(id)
.bind(f.payment_method_variant)
.bind(f.payment_method_card_last4)
.bind(f.payment_method_card_network)
.bind(f.payment_method_bank_transfer_iban)
.bind(f.payment_method_bank_transfer_bic)
.execute(&pool)
.await?;

// Compile-time checked query!: custom types and narrowed fields need overrides.
sqlx::query!(
    r#"SELECT id, payment_method_card_last4 AS "last4!", payment_method_card_network AS "network!: CardNetwork"
       FROM payment WHERE payment_method_variant = 'card'"#
)
```

The variant column needs `PaymentMethodVariant`; binding a `String` to it fails with "column is of type payment_method_variant but expression is of type text".

## After `modify`

Apply the same change to the Rust types:

| Change | Native type | Columnar type and conversions |
| --- | --- | --- |
| Add variant | New variant | New `PaymentMethodVariant` variant, new `Option` fields, new arms in both `match`es |
| Add field | New field (`Option` if nullable) | New `Option` field; set it in `From`, read it in `TryFrom` |
| Rename variant | Rename the variant | Rename the `PaymentMethodVariant` variant and the field names containing the old label |
| Rename field | Rename the field | Rename the column field |
| Remove variant or field | Remove it | Remove the variant, fields and arms |
