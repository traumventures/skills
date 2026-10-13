# Rust

Three types per union, all named after it, plus one shared module:

- **Native type** `PaymentMethod`: the enum the application uses. With serde it serializes to exactly the JSON Schema shape.
- **Variant type** `PaymentMethodVariant`: fieldless enum for the SQL type `payment_method_variant`, i.e. the values of the JSON `variant` property.
- **Columnar type** `PaymentMethodSqlColumns`: one field per SQL column, with the same names (nested unions' columns included). It is a plain `sqlx::FromRow` struct.
- `From<PaymentMethod> for PaymentMethodSqlColumns` (writing) and `TryFrom<PaymentMethodSqlColumns> for PaymentMethod` (reading).

All code below is tested with sqlx 0.9 against Postgres 17. Like the SQL, it is the baseline: the native type may use richer types (newtypes such as `Iban`, parsed or validated values), and the conversions are the place to parse them. Only `SqlColumns` has to stay plain sqlx types.

## Reuse

Write this module once per crate; every union's conversions use it.

```rust
//! Shared by every `*SqlColumns` conversion. Write it once per crate.

/// Columns that do not form a valid value. Only happens when the CHECK
/// constraint is missing or wrong, or a value does not fit the Rust type.
#[derive(Debug, thiserror::Error)]
pub enum SqlColumnsError {
    #[error("column {column} is NULL, but its variant requires it")]
    Missing { column: &'static str },
    #[error("column {column} does not fit the Rust type: {message}")]
    OutOfRange { column: &'static str, message: String },
}

/// Lets `?` turn it into `sqlx::Error::Decode` in functions returning `sqlx::Result`.
impl From<SqlColumnsError> for sqlx::Error {
    fn from(e: SqlColumnsError) -> Self {
        sqlx::Error::Decode(Box::new(e))
    }
}

/// A required field of the stored variant.
pub fn required<T>(value: Option<T>, column: &'static str) -> Result<T, SqlColumnsError> {
    value.ok_or(SqlColumnsError::Missing { column })
}

/// Narrow a column value into the native type (`i16` -> `u8`, `Decimal` -> `u64`, ...).
pub fn fit<S, T>(value: S, column: &'static str) -> Result<T, SqlColumnsError>
where
    T: TryFrom<S>,
    T::Error: std::fmt::Display,
{
    T::try_from(value).map_err(|e| SqlColumnsError::OutOfRange { column, message: e.to_string() })
}
```

### Errors

`TryFrom` only fails when the database holds columns that are not a valid value: a missing or wrong CHECK constraint, or a value that does not fit a narrower Rust type. `SqlColumnsError` keeps that matchable and works with every error style:

- **thiserror** application errors: add `#[from] SqlColumnsError` next to `#[from] sqlx::Error`.
- **anyhow**: `?` works directly, and `.context(..)` adds where it happened.
- **`sqlx::Result`** functions: `?` converts it into `sqlx::Error::Decode` through the `From` impl.

```rust
#[derive(Debug, thiserror::Error)]
enum AppError {
    #[error("payment {0} not found")]
    NotFound(i64),
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error(transparent)]
    Columns(#[from] SqlColumnsError),
}

async fn find(pool: &PgPool, id: i64) -> Result<Payment, AppError> {
    let row: PaymentRow = sqlx::query_as("SELECT * FROM payment WHERE id = $1")
        .bind(id)
        .fetch_optional(pool)
        .await?
        .ok_or(AppError::NotFound(id))?;
    Ok(row.try_into()?)
}

// Code that stays in sqlx::Result: `?` converts via From<SqlColumnsError> for sqlx::Error.
async fn find_sqlx(pool: &PgPool, id: i64) -> sqlx::Result<PaymentMethod> {
    let c: PaymentMethodSqlColumns = sqlx::query_as("SELECT * FROM payment WHERE id = $1").bind(id).fetch_one(pool).await?;
    Ok(c.try_into()?)
}

use anyhow::Context;

async fn find_anyhow(pool: &PgPool, id: i64) -> anyhow::Result<PaymentMethod> {
    use anyhow::Context;
    let c: PaymentMethodSqlColumns = sqlx::query_as("SELECT * FROM payment WHERE id = $1")
        .bind(id)
        .fetch_one(pool)
        .await?;
    Ok(PaymentMethod::try_from(c).with_context(|| format!("payment {id}"))?)
}

use anyhow::Context;

async fn find_anyhow(pool: &PgPool, id: i64) -> anyhow::Result<PaymentMethod> {
    let c: PaymentMethodSqlColumns =
        sqlx::query_as("SELECT * FROM payment WHERE id = $1").bind(id).fetch_one(pool).await?;
    PaymentMethod::try_from(c).with_context(|| format!("payment {id}"))
}
```

Messages as they come out:

```text
thiserror: column payment_method_card_last4 is NULL, but its variant requires it
anyhow:    payment 99: column payment_method_card_last4 is NULL, but its variant requires it
sqlx:      error occurred while decoding: column payment_method_card_last4 is NULL, but its variant requires it
range:     column sample_all_byte does not fit the Rust type: number too large to fit in target type
```

Why not return `sqlx::Error` from `TryFrom` directly? It works, but the error is then a boxed string you cannot match on. sqlx's `#[sqlx(flatten, try_from = "PaymentMethodSqlColumns")]` loses the message either way: it reports `unprintable error: (unprintable type)`. So read the columns with `flatten` and convert explicitly, as shown below.

## Type mapping

`SqlColumns` holds what sqlx decodes. The native type may be narrower or richer, converted in `From`/`TryFrom`. Crate features: `chrono` (or `time`), `uuid`, `rust_decimal` (or `bigdecimal`), `ipnet` (or `ipnetwork`), `json`.

| JSON Schema | Postgres | Native Rust | `SqlColumns` field | Conversion |
| --- | --- | --- | --- | --- |
| `string` | `text` | `String` | `Option<String>` | |
| `integer` `int16` · `int32` · `int64` | `smallint` · `integer` · `bigint` | `i16` · `i32` · `i64` | same | |
| `integer` `uint8` · `uint16` · `uint32` | `smallint` · `integer` · `bigint` | `u8` · `u16` · `u32` | `i16` · `i32` · `i64` | `.into()` / `fit()` |
| `integer` `uint64` | `numeric(20)` | `u64` | `rust_decimal::Decimal` | `.into()` / `fit()` |
| `number` `float` · `double` | `real` · `double precision` | `f32` · `f64` | same | |
| `string` `decimal` | `numeric` | `rust_decimal::Decimal` | same | |
| `boolean` | `boolean` | `bool` | same | |
| `string` + `contentEncoding: base64` | `bytea` | `Vec<u8>` | same | |
| `string` `date-time` | `timestamptz` | `chrono::DateTime<Utc>` / `time::OffsetDateTime` | same | |
| `"x-sql": "timestamp"` | `timestamp` | `chrono::NaiveDateTime` / `time::PrimitiveDateTime` | same | |
| `string` `date` · `time` | `date` · `time` | `chrono::NaiveDate` · `NaiveTime` | same | |
| `string` `duration` | `interval` | `sqlx::postgres::types::PgInterval` | same | encode-only: `std::time::Duration`, `chrono::TimeDelta` |
| `string` `uuid` | `uuid` | `uuid::Uuid` | same | |
| `string` `ipv4`/`ipv6` | `inet` | `std::net::IpAddr` | same | |
| `array` of `string` | `text[]` | `Vec<String>` | same | |
| string `enum` titled `CardNetwork` | `card_network` | `#[derive(sqlx::Type)] enum CardNetwork` | same | |
| `"x-sql": "jsonb"` | `jsonb` | `serde_json::Value` or `sqlx::types::Json<T>` | same | |
| nullable / not required | nullable | `Option<T>` | `Option<T>` | passed through |
| nested union `Wallet` | `wallet_variant` + columns | `Option<Wallet>` / `Wallet` | `Option<WalletVariant>` + columns | nested `match` |

Rust `char`, `u128`/`i128` and `usize` have no sqlx Postgres mapping. Store them as `text`/`numeric`/`bigint` and convert. Tuple variants (`Card(String)`) have no property names and do not work with `#[serde(tag = ..)]`, so always use named fields.

## Implementation

```rust
use crate::sql_enum::{required, SqlColumnsError};
use serde::{Deserialize, Serialize};

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

#[derive(Debug, Clone, Copy, PartialEq, Eq, sqlx::Type)]
#[sqlx(type_name = "payment_method_variant", rename_all = "snake_case")]
pub enum PaymentMethodVariant { Card, BankTransfer, Cash }

#[derive(Debug, Clone, PartialEq, sqlx::FromRow)]
pub struct PaymentMethodSqlColumns {
    pub payment_method: PaymentMethodVariant,
    pub payment_method_card_last4: Option<String>,
    pub payment_method_card_network: Option<CardNetwork>,
    pub payment_method_bank_transfer_iban: Option<String>,
    pub payment_method_bank_transfer_bic: Option<String>,
}

impl From<PaymentMethod> for PaymentMethodSqlColumns {
    fn from(value: PaymentMethod) -> Self {
        let mut c = PaymentMethodSqlColumns {
            payment_method: PaymentMethodVariant::Cash,
            payment_method_card_last4: None,
            payment_method_card_network: None,
            payment_method_bank_transfer_iban: None,
            payment_method_bank_transfer_bic: None,
        };
        match value {
            PaymentMethod::Card { last4, network } => {
                c.payment_method = PaymentMethodVariant::Card;
                c.payment_method_card_last4 = Some(last4);
                c.payment_method_card_network = Some(network);
            }
            PaymentMethod::BankTransfer { iban, bic } => {
                c.payment_method = PaymentMethodVariant::BankTransfer;
                c.payment_method_bank_transfer_iban = Some(iban);
                c.payment_method_bank_transfer_bic = bic;
            }
            PaymentMethod::Cash => c.payment_method = PaymentMethodVariant::Cash,
        }
        c
    }
}

impl TryFrom<PaymentMethodSqlColumns> for PaymentMethod {
    type Error = SqlColumnsError;

    fn try_from(c: PaymentMethodSqlColumns) -> Result<Self, SqlColumnsError> {
        Ok(match c.payment_method {
            PaymentMethodVariant::Card => PaymentMethod::Card {
                last4: required(c.payment_method_card_last4, "payment_method_card_last4")?,
                network: required(c.payment_method_card_network, "payment_method_card_network")?,
            },
            PaymentMethodVariant::BankTransfer => PaymentMethod::BankTransfer {
                iban: required(c.payment_method_bank_transfer_iban, "payment_method_bank_transfer_iban")?,
                bic: c.payment_method_bank_transfer_bic,
            },
            PaymentMethodVariant::Cash => PaymentMethod::Cash,
        })
    }
}
```

Both `match`es are exhaustive, so after a schema change the compiler points at every place to update.

## Reading and writing

```rust
#[derive(Debug, sqlx::FromRow)]
struct PaymentRow {
    id: i64,
    #[sqlx(flatten)]
    payment_method: PaymentMethodSqlColumns,
}

#[derive(Debug)]
struct Payment {
    id: i64,
    payment_method: PaymentMethod,
}

impl TryFrom<PaymentRow> for Payment {
    type Error = SqlColumnsError;
    fn try_from(r: PaymentRow) -> Result<Self, SqlColumnsError> {
        Ok(Payment { id: r.id, payment_method: r.payment_method.try_into()? })
    }
}

async fn insert(pool: &PgPool, id: i64, method: PaymentMethod) -> sqlx::Result<()> {
    let c = PaymentMethodSqlColumns::from(method);
    sqlx::query(
        "INSERT INTO payment (id, payment_method, payment_method_card_last4, payment_method_card_network,
                              payment_method_bank_transfer_iban, payment_method_bank_transfer_bic)
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(id)
    .bind(c.payment_method)
    .bind(c.payment_method_card_last4)
    .bind(c.payment_method_card_network)
    .bind(c.payment_method_bank_transfer_iban)
    .bind(c.payment_method_bank_transfer_bic)
    .execute(pool)
    .await?;
    Ok(())
}

// App-level error enum: sqlx errors (including failed conversions) pass through.

async fn insert(pool: &PgPool, id: i64, method: PaymentMethod) -> sqlx::Result<()> {
    let c = PaymentMethodSqlColumns::from(method);
    sqlx::query(
        "INSERT INTO payment (id, payment_method, payment_method_card_last4, payment_method_card_network,
                              payment_method_bank_transfer_iban, payment_method_bank_transfer_bic)
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(id)
    .bind(c.payment_method)
    .bind(c.payment_method_card_last4)
    .bind(c.payment_method_card_network)
    .bind(c.payment_method_bank_transfer_iban)
    .bind(c.payment_method_bank_transfer_bic)
    .execute(pool)
    .await?;
    Ok(())
}
```

The variant column needs `PaymentMethodVariant`; binding a `String` fails with "column is of type payment_method_variant but expression is of type text". With compile-time checked `query!`, custom types and narrowed fields need overrides:

```rust
sqlx::query!(
    r#"SELECT id, payment_method_card_last4 AS "last4!", payment_method_card_network AS "network!: CardNetwork"
       FROM payment WHERE payment_method = 'card'"#
)
```

## Nested unions

One `SqlColumns` struct holds the columns of every level, because the nested columns' names depend on where the union is stored. The nested union only adds its own variant type and a nested `match` in each conversion.

```rust
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "variant", rename_all = "snake_case")]
pub enum PaymentMethod {
    Card { last4: String, wallet: Option<Wallet> },
    Cash,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "variant", rename_all = "snake_case")]
pub enum Wallet {
    ApplePay { device_account: String },
    GooglePay,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, sqlx::Type)]
#[sqlx(type_name = "payment_method_variant", rename_all = "snake_case")]
pub enum PaymentMethodVariant { Card, Cash }

#[derive(Debug, Clone, Copy, PartialEq, Eq, sqlx::Type)]
#[sqlx(type_name = "wallet_variant", rename_all = "snake_case")]
pub enum WalletVariant { ApplePay, GooglePay }

/// All columns of the union, nested ones included.
#[derive(Debug, Clone, PartialEq, sqlx::FromRow)]
pub struct PaymentMethodSqlColumns {
    pub payment_method: PaymentMethodVariant,
    pub payment_method_card_last4: Option<String>,
    pub payment_method_card_wallet: Option<WalletVariant>,
    pub payment_method_card_wallet_apple_pay_device_account: Option<String>,
}

impl From<PaymentMethod> for PaymentMethodSqlColumns {
    fn from(value: PaymentMethod) -> Self {
        let mut c = PaymentMethodSqlColumns {
            payment_method: PaymentMethodVariant::Cash,
            payment_method_card_last4: None,
            payment_method_card_wallet: None,
            payment_method_card_wallet_apple_pay_device_account: None,
        };
        match value {
            PaymentMethod::Card { last4, wallet } => {
                c.payment_method = PaymentMethodVariant::Card;
                c.payment_method_card_last4 = Some(last4);
                match wallet {
                    Some(Wallet::ApplePay { device_account }) => {
                        c.payment_method_card_wallet = Some(WalletVariant::ApplePay);
                        c.payment_method_card_wallet_apple_pay_device_account = Some(device_account);
                    }
                    Some(Wallet::GooglePay) => c.payment_method_card_wallet = Some(WalletVariant::GooglePay),
                    None => {}
                }
            }
            PaymentMethod::Cash => c.payment_method = PaymentMethodVariant::Cash,
        }
        c
    }
}

impl TryFrom<PaymentMethodSqlColumns> for PaymentMethod {
    type Error = SqlColumnsError;

    fn try_from(c: PaymentMethodSqlColumns) -> Result<Self, SqlColumnsError> {
        Ok(match c.payment_method {
            PaymentMethodVariant::Card => PaymentMethod::Card {
                last4: required(c.payment_method_card_last4, "payment_method_card_last4")?,
                wallet: match c.payment_method_card_wallet {
                    Some(WalletVariant::ApplePay) => Some(Wallet::ApplePay {
                        device_account: required(
                            c.payment_method_card_wallet_apple_pay_device_account,
                            "payment_method_card_wallet_apple_pay_device_account",
                        )?,
                    }),
                    Some(WalletVariant::GooglePay) => Some(Wallet::GooglePay),
                    None => None,
                },
            },
            PaymentMethodVariant::Cash => PaymentMethod::Cash,
        })
    }
}
```

JSON for the first value: `{"variant":"card","last4":"1","wallet":{"variant":"apple_pay","device_account":"dev"}}`.

## After `modify`

| Change | Native type | Variant type, `SqlColumns`, conversions |
| --- | --- | --- |
| Add variant | New variant | New `PaymentMethodVariant` variant, new `Option` fields, new arms in both `match`es |
| Add field | New field (`Option` if nullable) | New `Option` field; set it in `From`, read it in `TryFrom` |
| Rename variant | Rename the variant | Rename the variant type's variant and the column fields containing the label |
| Rename field | Rename the field | Rename the column field (and nested ones) |
| Remove variant or field | Remove it | Remove the variant, fields and arms |
