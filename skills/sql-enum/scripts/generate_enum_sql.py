#!/usr/bin/env python3
"""Generate Postgres DDL for a Rust-like enum stored in flat columns.

Spec (JSON file or stdin):

    {
      "table": "payment",
      "column": "method",
      "enum_type": "payment_method_variant",   # optional, default <column>_variant
      "nullable": false,                        # optional: Option<Enum> column
      "variants": {
        "card":          {"last4": "text", "network": "text"},
        "bank_transfer": {"iban": "text", "bic": "text?"},
        "cash":          {}
      }
    }

Variant names are the Postgres enum labels (snake_case). A trailing `?` on a
field type makes the field optional inside its own variant. Field types are
copied verbatim, so `bigint REFERENCES customer(id)` works.

Modes:
    create      CREATE TYPE + column/constraint lines to paste into CREATE TABLE
    alter       CREATE TYPE + ALTER TABLE adding the columns and the constraint
    constraint  replace the CHECK constraint (after adding/removing variants)
"""

import argparse
import json
import re
import sys

IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
MAX_IDENT = 63


def fail(msg):
    sys.exit(f"error: {msg}")


def ident(name, what):
    if not IDENT.match(name):
        fail(f"{what} `{name}` must be lower snake_case")
    if len(name) > MAX_IDENT:
        fail(f"{what} `{name}` exceeds Postgres' {MAX_IDENT} character identifier limit")
    return name


def load(spec):
    table = ident(spec["table"], "table")
    column = ident(spec["column"], "column")
    enum_type = ident(spec.get("enum_type") or f"{column}_variant", "enum type")
    variants = spec.get("variants")
    if not variants:
        fail("`variants` must be a non-empty object")
    fields = []  # (variant, field, column name, sql type, optional)
    for variant, vfields in variants.items():
        ident(variant, "variant")
        for field, ty in (vfields or {}).items():
            ident(field, "field")
            optional = ty.rstrip().endswith("?")
            ty = ty.rstrip().rstrip("?").strip()
            fields.append((variant, field, ident(f"{column}_{variant}_{field}", "column"), ty, optional))
    names = [f[2] for f in fields]
    if len(names) != len(set(names)):
        fail("two variant fields map to the same column name")
    return {
        "table": table,
        "column": column,
        "variant_column": ident(f"{column}_variant", "column"),
        "enum_type": enum_type,
        "constraint": ident(f"{table}_{column}_variant_check", "constraint"),
        "nullable": bool(spec.get("nullable", False)),
        "variants": list(variants),
        "fields": fields,
    }


def quote(label):
    return "'" + label.replace("'", "''") + "'"


def create_type(s):
    labels = ", ".join(quote(v) for v in s["variants"])
    return f"CREATE TYPE {s['enum_type']} AS ENUM ({labels});"


def column_defs(s):
    null = "" if s["nullable"] else " NOT NULL"
    out = [f"{s['variant_column']} {s['enum_type']}{null}"]
    out += [f"{col} {ty}" for (_, _, col, ty, _) in s["fields"]]
    return out


def check_expr(s, indent="    "):
    lines = [f"CASE {s['variant_column']}"]
    for v in s["variants"]:
        conds = []
        for (fv, _, col, _, optional) in s["fields"]:
            if fv != v:
                conds.append(f"{col} IS NULL")
            elif not optional:
                conds.append(f"{col} IS NOT NULL")
        lines.append(f"    WHEN {quote(v)} THEN {' AND '.join(conds) or 'true'}")
    # ELSE false: a CHECK passes on NULL, so a label added later with
    # ALTER TYPE .. ADD VALUE must not slip through unconstrained.
    lines.append("    ELSE false")
    lines.append("END")
    expr = f"\n{indent}".join(lines)
    if s["nullable"]:
        all_null = " AND ".join(f"{col} IS NULL" for (_, _, col, _, _) in s["fields"]) or "true"
        expr = f"({s['variant_column']} IS NULL AND {all_null})\n{indent}OR {expr}"
    return expr


def constraint_def(s):
    body = check_expr(s, "        ")
    return f"CONSTRAINT {s['constraint']} CHECK (\n        {body}\n    )"


def mode_create(s):
    lines = column_defs(s) + [constraint_def(s)]
    return (
        f"{create_type(s)}\n\n"
        f"-- Paste into CREATE TABLE {s['table']} (...):\n"
        "    " + ",\n    ".join(lines) + "\n"
    )


def mode_alter(s):
    adds = [f"    ADD COLUMN {c}" for c in column_defs(s)]
    adds.append(f"    ADD {constraint_def(s)}")
    return f"{create_type(s)}\n\nALTER TABLE {s['table']}\n" + ",\n".join(adds) + ";\n"


def mode_constraint(s):
    body = check_expr(s, "        ")
    return (
        f"-- Run after `ALTER TYPE {s['enum_type']} ADD VALUE ..` has committed and new columns exist.\n"
        f"ALTER TABLE {s['table']} DROP CONSTRAINT IF EXISTS {s['constraint']};\n"
        f"ALTER TABLE {s['table']} ADD CONSTRAINT {s['constraint']} CHECK (\n        {body}\n    ) NOT VALID;\n"
        f"ALTER TABLE {s['table']} VALIDATE CONSTRAINT {s['constraint']};\n"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["create", "alter", "constraint"])
    p.add_argument("spec", nargs="?", default="-", help="JSON spec file, or - for stdin")
    a = p.parse_args()
    text = sys.stdin.read() if a.spec == "-" else open(a.spec).read()
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON spec: {e}")
    s = load(spec)
    print({"create": mode_create, "alter": mode_alter, "constraint": mode_constraint}[a.mode](s), end="")


if __name__ == "__main__":
    main()
