# Mapping to Rust with sqlx

Tested with sqlx 0.9 (`postgres`, `macros`, `derive` features).

## Types

Mirror the native enum with a fieldless Rust enum, and keep the data-carrying enum separate:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, sqlx::Type)]
#[sqlx(type_name = "payment_method_enum", rename_all = "snake_case")]
pub enum PaymentMethodVariant { Card, BankTransfer, Cash }

#[derive(Debug, Clone, PartialEq)]
pub enum PaymentMethod {
    Card { last4: String, network: String },
    BankTransfer { iban: String, bic: Option<String> },
    Cash,
}
```

`rename_all = "snake_case"` must produce exactly the Postgres labels. When a Rust variant name does not
snake-case to its label, use `#[sqlx(rename = "..")]` on that variant.

## Reading

Decode the flat columns into a struct, then convert. A missing required field means the CHECK constraint is
absent or wrong, so return an error instead of panicking:

```rust
#[derive(Debug, sqlx::FromRow)]
pub struct PaymentMethodColumns {
    payment_method_variant: PaymentMethodVariant,
    payment_method_card_last4: Option<String>,
    payment_method_card_network: Option<String>,
    payment_method_bank_transfer_iban: Option<String>,
    payment_method_bank_transfer_bic: Option<String>,
}

fn required<T>(v: Option<T>, column: &str) -> Result<T, sqlx::Error> {
    v.ok_or_else(|| sqlx::Error::Decode(format!("{column} is NULL for its variant").into()))
}

impl TryFrom<PaymentMethodColumns> for PaymentMethod {
    type Error = sqlx::Error;
    fn try_from(c: PaymentMethodColumns) -> Result<Self, sqlx::Error> {
        Ok(match c.payment_method_variant {
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

The `match` on `PaymentMethodVariant` is exhaustive, so adding a label in Rust forces this conversion to be
updated.

Embed the columns in a row struct with `#[sqlx(flatten)]` and `query_as`:

```rust
#[derive(sqlx::FromRow)]
struct PaymentRow {
    id: i64,
    #[sqlx(flatten)]
    payment_method: PaymentMethodColumns,
}

let rows: Vec<PaymentRow> = sqlx::query_as("SELECT * FROM payment").fetch_all(&pool).await?;
```

With compile-time checked `query!`, custom types need a type override, and narrowed fields need a
non-null override:

```rust
sqlx::query!(
    r#"SELECT id, payment_method_variant AS "variant: PaymentMethodVariant", payment_method_card_last4 AS "last4!"
       FROM payment WHERE payment_method_variant = 'card'"#
)
```

## Writing

Produce every flat column from the Rust value in one place, so that writes always set the whole variant:

```rust
impl PaymentMethod {
    pub fn variant(&self) -> PaymentMethodVariant {
        match self {
            Self::Card { .. } => PaymentMethodVariant::Card,
            Self::BankTransfer { .. } => PaymentMethodVariant::BankTransfer,
            Self::Cash => PaymentMethodVariant::Cash,
        }
    }

    /// (variant, card_last4, card_network, bank_transfer_iban, bank_transfer_bic)
    #[allow(clippy::type_complexity)]
    pub fn columns(&self) -> (PaymentMethodVariant, Option<&str>, Option<&str>, Option<&str>, Option<&str>) {
        match self {
            Self::Card { last4, network } => (self.variant(), Some(last4), Some(network), None, None),
            Self::BankTransfer { iban, bic } => (self.variant(), None, None, Some(iban), bic.as_deref()),
            Self::Cash => (self.variant(), None, None, None, None),
        }
    }
}

let (variant, last4, network, iban, bic) = payment_method.columns();
sqlx::query!(
    "INSERT INTO payment (id, payment_method_variant, payment_method_card_last4, payment_method_card_network,
                          payment_method_bank_transfer_iban, payment_method_bank_transfer_bic)
     VALUES ($1, $2, $3, $4, $5, $6)",
    id, variant as PaymentMethodVariant, last4, network, iban, bic
)
```

`query!` needs `variant as PaymentMethodVariant` to accept a custom enum parameter. An `UPDATE` that switches
variants sets the same full column list.
