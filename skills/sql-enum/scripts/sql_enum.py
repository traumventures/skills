#!/usr/bin/env python3
"""Store a tagged union described by JSON Schema in Postgres as flat columns.

The JSON Schema is the source of truth. A tagged union is a `oneOf` (or
`anyOf`) whose object branches share one discriminator property with a
`const` string value per branch:

    {"title": "PaymentMethod", "oneOf": [
      {"title": "Card", "type": "object",
       "properties": {"variant": {"const": "card"},
                      "last4": {"type": "string"}, "network": {"type": "string"}},
       "required": ["variant", "last4", "network"]},
      {"title": "Cash", "type": "object",
       "properties": {"variant": {"const": "cash"}}, "required": ["variant"]}]}

Commands (SQL on stdout; --json for machine-readable output):

  show   SCHEMA                          variants, column names and SQL types
  setup  SCHEMA --table payment          CREATE TYPE, columns and CHECK constraint
  modify SCHEMA --table payment ACTION.. migration(s) and the updated schema

SCHEMA is a file or `-` (stdin). `--def NAME` picks a `$defs` entry when the
file holds several types; `--name` overrides the snake_case enum name (default:
the union's title). `--table` repeats for every table storing the enum and
takes options: `--table 'subscription,nullable'` (optional enum column),
`--table 'refund,prefix=refund_payment_method'` (same enum twice in a table).

Modify actions (applied in order, names are discriminator values = labels):

  --add-variant LABEL[=BRANCH_JSON]  paypal='{"properties":{"email":{"type":"string"}}}'
  --remove-variant LABEL
  --rename-variant OLD=NEW
  --add-field LABEL.FIELD=PROPERTY_JSON
                       card.expiry='{"type":"string","x-backfill":"$$12/30$$"}'
  --remove-field LABEL.FIELD
  --rename-field LABEL.OLD=NEW

Properties are required unless their schema allows null
(`"type": ["string", "null"]`). `x-backfill` (SQL expression, removed from the
saved schema) fills a new required field in existing rows. `x-sql` on a
property sets its column type verbatim, e.g. `"bigint REFERENCES device(id)"`.
`modify --write` saves the updated schema back to the file.
"""

import argparse
import copy
import json
import re
import sys

MAX_IDENT = 63


class Fail(Exception):
    pass


def fail(msg):
    raise Fail(msg)


def snake(s):
    s = re.sub(r"[^0-9A-Za-z]+", "_", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.strip("_").lower()


def pascal(label):
    return "".join(p.capitalize() for p in snake(label).split("_"))


def ident(name, what):
    for part in name.split("."):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", part):
            fail(f"{what} `{name}` must be lower snake_case")
        if len(part) > MAX_IDENT:
            fail(f"{what} `{part}` is longer than Postgres' {MAX_IDENT} character limit; use a shorter enum name or prefix")
    return name


def q(s):
    return "'" + s.replace("'", "''") + "'"


def parse_json(text, what):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        fail(f"{what}: invalid JSON ({e}): {text}")


# ---------------------------------------------------------------------------
# Schema access


class Doc:
    def __init__(self, data):
        self.data = data

    def pointer(self, ref):
        if not ref.startswith("#"):
            fail(f"only local $ref values are supported, got `{ref}`")
        node = self.data
        for part in ref[1:].split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                fail(f"unresolvable $ref `{ref}`")
            node = node[part]
        return node

    def resolve(self, node):
        for _ in range(64):
            if isinstance(node, dict) and "$ref" in node:
                node = self.pointer(node["$ref"])
            else:
                return node
        fail("$ref cycle")

    def defs_key(self):
        return "definitions" if "definitions" in self.data and "$defs" not in self.data else "$defs"

    def defs(self):
        return self.data.get(self.defs_key()) or {}


def ref_name(node):
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def union_key(node):
    if isinstance(node, dict):
        for k in ("oneOf", "anyOf"):
            if isinstance(node.get(k), list):
                return k
    return None


def const_value(doc, prop):
    p = doc.resolve(prop)
    if not isinstance(p, dict):
        return None
    if "const" in p:
        return p["const"]
    e = p.get("enum")
    if isinstance(e, list) and len(e) == 1:
        return e[0]
    return None


def discriminator(doc, node):
    """The property that has a const string in every branch, or None."""
    key = union_key(node)
    if not key or not node[key]:
        return None
    cands = None
    for b in node[key]:
        b = doc.resolve(b)
        if not isinstance(b, dict):
            return None
        c = {k for k, p in (b.get("properties") or {}).items() if isinstance(const_value(doc, p), str)}
        cands = c if cands is None else cands & c
    if not cands:
        return None
    if len(cands) > 1:
        if "variant" in cands:
            return "variant"
        fail(f"ambiguous discriminator: {sorted(cands)} are all constant in every branch")
    return cands.pop()


def nullable(doc, ps):
    p = doc.resolve(ps)
    if not isinstance(p, dict):
        return False
    t = p.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t) or p.get("nullable") is True:
        return True
    for k in ("anyOf", "oneOf"):
        for x in p.get(k) or []:
            x = doc.resolve(x)
            if isinstance(x, dict) and x.get("type") == "null":
                return True
    return False


STRING_FORMATS = {
    "date-time": "timestamptz",
    "date": "date",
    "time": "time",
    "duration": "interval",
    "uuid": "uuid",
    "ipv4": "inet",
    "ipv6": "inet",
}
INTEGER_FORMATS = {
    "int16": "smallint",
    "int32": "integer",
    "int64": "bigint",
    "uint8": "smallint",
    "uint16": "integer",
    "uint32": "bigint",
    "uint64": "numeric(20)",
}
NUMBER_FORMATS = {"float": "real", "double": "double precision"}


def column_type(doc, ps, where):
    """SQL column type of a property, plus (enum type, labels) when it is a plain string enum."""
    if isinstance(ps, dict) and "x-sql" in ps:
        return ps["x-sql"], None
    name = ref_name(ps)
    p = doc.resolve(ps)
    if not isinstance(p, dict):
        fail(f"{where}: property schema must be an object")
    if "x-sql" in p:
        return p["x-sql"], None
    for k in ("anyOf", "oneOf"):
        branches = p.get(k)
        if isinstance(branches, list):
            rest = [x for x in branches if doc.resolve(x).get("type") != "null"]
            if len(rest) == 1 and len(rest) < len(branches):
                return column_type(doc, rest[0], where)
    if discriminator(doc, p):
        fail(
            f"{where}: a field that is itself a tagged union cannot be flattened; "
            "store it in its own table, or keep it as JSON with \"x-sql\": \"jsonb\""
        )
    values = p.get("enum")
    if isinstance(values, list) and len(values) > 1:
        values = [v for v in values if v is not None]
        if not all(isinstance(v, str) for v in values):
            fail(f"{where}: only string enums map to a Postgres enum type")
        tname = p.get("title") or name
        if not tname:
            fail(f"{where}: a string enum needs a \"title\" (or a $defs entry) to name its Postgres type")
        typ = ident(snake(tname) + "_enum", "enum type")
        return typ, (typ, values)
    t = p.get("type")
    if isinstance(t, list):
        t = [x for x in t if x != "null"]
        t = t[0] if len(t) == 1 else None
    fmt = p.get("format")
    if t == "string":
        if p.get("contentEncoding") == "base64":
            return "bytea", None
        return STRING_FORMATS.get(fmt, "text"), None
    if t == "integer":
        return INTEGER_FORMATS.get(fmt, "bigint"), None
    if t == "number":
        return NUMBER_FORMATS.get(fmt, "double precision"), None
    if t == "boolean":
        return "boolean", None
    if t == "array" and isinstance(p.get("items"), dict):
        sql, enum = column_type(doc, p["items"], where + "[]")
        return sql + "[]", enum
    fail(
        f"{where}: no column type for {json.dumps(p)[:100]}; use a scalar type, "
        "flatten objects into fields of the variant, or set \"x-sql\" (e.g. \"jsonb\")"
    )


# ---------------------------------------------------------------------------
# Model


class Field:
    def __init__(self, key, schema, optional):
        self.key = key
        self.part = snake(key)
        self.schema = schema
        self.optional = optional


class Variant:
    def __init__(self, label, title, fields, entry, branch):
        self.label = label
        self.part = snake(label)
        self.title = title
        self.fields = fields
        self.entry = entry
        self.branch = branch

    def field(self, key):
        for f in self.fields:
            if f.key == key:
                return f
        fail(f"variant `{self.label}` has no field `{key}` (fields: {', '.join(f.key for f in self.fields) or 'none'})")


class Enum:
    def __init__(self, doc, node, name, disc, variants):
        self.doc = doc
        self.node = node
        self.name = name
        self.type = ident(f"{name}_enum", "enum type")
        self.disc = disc
        self.key = union_key(node)
        self.variants = variants

    def variant(self, label):
        for v in self.variants:
            if v.label == label:
                return v
        fail(f"no variant `{label}` (variants: {', '.join(v.label for v in self.variants)})")

    def has_fields(self):
        return any(v.fields for v in self.variants)


def find_target(doc, def_name):
    if def_name:
        defs = doc.defs()
        if def_name in defs:
            return defs[def_name], def_name
        if doc.data.get("title") == def_name:
            return doc.data, def_name
        fail(f"no definition `{def_name}` in {doc.defs_key()} or the root title")
    if discriminator(doc, doc.data):
        return doc.data, doc.data.get("title")
    unions = [k for k, v in doc.defs().items() if isinstance(v, dict) and discriminator(doc, v)]
    if len(unions) == 1:
        return doc.defs()[unions[0]], unions[0]
    if unions:
        fail(f"several tagged unions found ({', '.join(unions)}); pick one with --def")
    fail("no tagged union found: expected a oneOf/anyOf whose object branches share a const discriminator property")


def build(doc, node, title, name_override):
    disc = discriminator(doc, node)
    if not disc:
        fail("the selected schema is not a tagged union (oneOf/anyOf with a shared const property)")
    name = name_override or snake(node.get("title") or title or "")
    if not name:
        fail("the union has no title; pass --name (snake_case enum name, e.g. payment_method)")
    ident(name, "enum name")
    variants, parts = [], {}
    for entry in node[union_key(node)]:
        b = doc.resolve(entry)
        if b.get("type", "object") != "object":
            fail("every union branch must be an object schema")
        props = b.get("properties") or {}
        required = set(b.get("required") or [])
        label = const_value(doc, props[disc])
        v = Variant(label, b.get("title") or ref_name(entry) or pascal(label), [], entry, b)
        if v.part in parts:
            fail(f"variants `{parts[v.part]}` and `{label}` map to the same column name part `{v.part}`")
        parts[v.part] = label
        fparts = set()
        for k, ps in props.items():
            if k == disc:
                continue
            f = Field(k, ps, k not in required or nullable(doc, ps))
            if f.part in fparts:
                fail(f"two fields of `{label}` map to the same column name part `{f.part}`")
            fparts.add(f.part)
            v.fields.append(f)
        variants.append(v)
    return Enum(doc, node, name, disc, variants)


class Table:
    def __init__(self, spec, enum_name):
        parts = [p.strip() for p in spec.split(",")]
        self.name = ident(parts[0], "table")
        self.prefix = enum_name
        self.nullable = False
        for p in parts[1:]:
            if p == "nullable":
                self.nullable = True
            elif p.startswith("prefix="):
                self.prefix = ident(p[len("prefix="):], "prefix")
            else:
                fail(f"unknown table option `{p}` (use nullable or prefix=...)")
        self.vcol = ident(f"{self.prefix}_variant", "column")
        self.check = ident(f"{self.prefix}_variant_check", "constraint")

    def col(self, v, f):
        return ident(f"{self.prefix}_{v.part}_{f.part}", "column")


def columns(e, tb):
    """(variant, field, column, sql type) for every field column, in order."""
    out = []
    for v in e.variants:
        for f in v.fields:
            sql, _ = column_type(e.doc, f.schema, f"{v.label}.{f.key}")
            out.append((v, f, tb.col(v, f), sql))
    return out


def field_enums(e):
    found = {}
    for v in e.variants:
        for f in v.fields:
            _, en = column_type(e.doc, f.schema, f"{v.label}.{f.key}")
            if en:
                typ, labels = en
                if typ in found and found[typ] != labels:
                    fail(f"enum type `{typ}` is used with different values")
                found[typ] = labels
    return found


def create_type(typ, labels):
    return f"CREATE TYPE {typ} AS ENUM ({', '.join(q(x) for x in labels)});"


def check_expr(e, tb, indent):
    cols = columns(e, tb)
    lines = [f"CASE {tb.vcol}"]
    for v in e.variants:
        conds = []
        for cv, f, col, _ in cols:
            if cv is not v:
                conds.append(f"{col} IS NULL")
            elif not f.optional:
                conds.append(f"{col} IS NOT NULL")
        lines.append(f"    WHEN {q(v.label)} THEN {' AND '.join(conds) or 'true'}")
    # ELSE false: a CHECK passes on NULL, so a label added later must not
    # slip through before the constraint knows its shape.
    lines += ["    ELSE false", "END"]
    expr = f"\n{indent}".join(lines)
    if tb.nullable:
        all_null = " AND ".join(f"{c} IS NULL" for _, _, c, _ in cols)
        expr = f"({tb.vcol} IS NULL AND {all_null})\n{indent}OR {expr}"
    return expr


def constraint_def(e, tb):
    return f"CONSTRAINT {tb.check} CHECK (\n        {check_expr(e, tb, '        ')}\n    )"


def column_defs(e, tb):
    defs = [f"{tb.vcol} {e.type}{'' if tb.nullable else ' NOT NULL'}"]
    defs += [f"{col} {sql}" for _, _, col, sql in columns(e, tb)]
    if e.has_fields():
        defs.append(constraint_def(e, tb))
    return defs


def add_constraint(e, tb):
    return [
        f"ALTER TABLE {tb.name} ADD CONSTRAINT {tb.check} CHECK (\n    {check_expr(e, tb, '    ')}\n) NOT VALID;",
        f"ALTER TABLE {tb.name} VALIDATE CONSTRAINT {tb.check};",
    ]


# ---------------------------------------------------------------------------
# Commands


def load(a):
    data = parse_json(sys.stdin.read() if a.schema == "-" else open(a.schema).read(), a.schema)
    doc = Doc(data)
    node, title = find_target(doc, a.defn)
    return doc, node, title, build(doc, node, title, a.name)


def tables_of(a, e):
    if not a.table:
        fail("--table is required (repeat it for every table that stores this enum)")
    return [Table(t, e.name) for t in a.table]


def cmd_show(a):
    _, _, _, e = load(a)
    tb = Table("t", e.name)
    out = {
        "enum": e.name,
        "type": e.type,
        "discriminator_property": e.disc,
        "variant_column": tb.vcol,
        "variants": [
            {
                "label": v.label,
                "fields": [
                    {"field": f.key, "column": col, "sql": sql, "required": not f.optional}
                    for cv, f, col, sql in columns(e, tb)
                    if cv is v
                ],
            }
            for v in e.variants
        ],
        "field_enum_types": field_enums(e),
    }
    if a.json:
        return out
    lines = [f"enum {e.name}: type {e.type}, column {tb.vcol} (JSON property `{e.disc}`)"]
    for v in out["variants"]:
        lines.append(f"  {v['label']}")
        for f in v["fields"]:
            lines.append(f"    {f['column']:<40} {f['sql']:<24} {'required' if f['required'] else 'optional'}")
    for typ, labels in out["field_enum_types"].items():
        lines.append(f"field enum type {typ}: {', '.join(labels)}")
    return "\n".join(lines) + "\n"


def cmd_setup(a):
    _, _, _, e = load(a)
    tables = tables_of(a, e)
    parts, notes = [], []
    if not a.no_types:
        for typ, labels in field_enums(e).items():
            parts.append(f"-- Field enum type; skip if it already exists.\n{create_type(typ, labels)}")
        parts.append(
            f"-- Shared by every table that stores {e.name}; skip if it already exists.\n"
            f"{create_type(e.type, [v.label for v in e.variants])}"
        )
    for tb in tables:
        defs = column_defs(e, tb)
        if a.form == "columns":
            parts.append(f"-- Columns for CREATE TABLE {tb.name} (...):\n    " + ",\n    ".join(defs))
        else:
            adds = [("ADD " if d.startswith("CONSTRAINT ") else "ADD COLUMN ") + d for d in defs]
            parts.append(f"ALTER TABLE {tb.name}\n    " + ",\n    ".join(adds) + ";")
            if not tb.nullable:
                notes.append(
                    f"{tb.name}: adding NOT NULL {tb.vcol} fails if the table has rows; add it without NOT NULL, "
                    "fill the variant and its fields, then SET NOT NULL and add the constraint."
                )
    sql = "\n\n".join(parts) + "\n"
    return {"migrations": [{"name": f"create_{e.name}", "sql": sql}], "notes": notes}


class Tracker:
    """Follows variants and fields through the actions to diff old vs. new."""

    def __init__(self, e):
        self.variants = [
            {"id": i, "label": v.label, "fields": [{"id": (i, f.key), "key": f.key} for f in v.fields]}
            for i, v in enumerate(e.variants)
        ]
        self.counter = 0
        self.backfill = {}

    def new_id(self):
        self.counter += 1
        return ("new", self.counter)

    def variant(self, label):
        return next(t for t in self.variants if t["label"] == label)


def split_target(arg, flag):
    left, sep, right = arg.partition("=")
    if flag.endswith("field") and "." not in left:
        fail(f"{flag} expects LABEL.FIELD, got `{left}`")
    return left.strip(), sep, right


def apply_action(doc, node, title, name, kind, arg, tr, notes):
    cur = build(doc, node, title, name)
    disc = cur.disc
    if kind == "add-variant":
        label, _, js = split_target(arg, kind)
        if not label:
            fail("--add-variant needs a label")
        if any(v.label == label for v in cur.variants):
            fail(f"variant `{label}` already exists")
        branch = parse_json(js, "--add-variant") if js.strip() else {}
        props = dict(branch.get("properties") or {})
        given = props.pop(disc, None)
        if given is not None and const_value(doc, given) != label:
            fail(f"--add-variant: property `{disc}` must be the label `{label}`")
        required = branch.get("required")
        if required is None:
            required = [k for k, p in props.items() if not nullable(doc, p)]
        template = cur.variants[0].branch if cur.variants else {}
        disc_schema = copy.deepcopy(doc.resolve((template.get("properties") or {}).get(disc, {"type": "string"})))
        if "enum" in disc_schema and "const" not in disc_schema:
            disc_schema["enum"] = [label]
        else:
            disc_schema.pop("enum", None)
            disc_schema["const"] = label
        new = {"title": branch.get("title") or pascal(label)}
        for k, v in branch.items():
            if k not in ("title", "properties", "required"):
                new[k] = v
        new.setdefault("type", "object")
        new["properties"] = {disc: disc_schema, **props}
        new["required"] = [disc] + [k for k in required if k != disc]
        if template.get("additionalProperties") is False:
            new.setdefault("additionalProperties", False)
        entries = node[cur.key]
        refs = [ref_name(x) for x in entries]
        if entries and all(refs) and all(x["$ref"].startswith(f"#/{doc.defs_key()}/") for x in entries):
            defs = doc.data.setdefault(doc.defs_key(), {})
            if new["title"] in defs:
                fail(f"{doc.defs_key()} already has an entry `{new['title']}`")
            defs[new["title"]] = new
            entries.append({"$ref": f"#/{doc.defs_key()}/{new['title']}"})
        else:
            entries.append(new)
        tr.variants.append(
            {"id": tr.new_id(), "label": label, "fields": [{"id": tr.new_id(), "key": k} for k in props]}
        )
    elif kind == "remove-variant":
        v = cur.variant(arg.strip())
        node[cur.key].remove(v.entry)
        if ref_name(v.entry):
            notes.append(f"The {doc.defs_key()} entry `{ref_name(v.entry)}` was left in place; delete it if nothing else uses it.")
        tr.variants.remove(tr.variant(v.label))
    elif kind == "rename-variant":
        old, sep, new_label = split_target(arg, kind)
        new_label = new_label.strip()
        if not sep or not new_label:
            fail("--rename-variant expects OLD=NEW")
        v = cur.variant(old)
        if any(x.label == new_label for x in cur.variants):
            fail(f"variant `{new_label}` already exists")
        dp = v.branch["properties"][disc]
        if "$ref" in dp:
            fail(f"the `{disc}` property of `{old}` is a $ref; edit it by hand")
        if "const" in dp:
            dp["const"] = new_label
        else:
            dp["enum"] = [new_label]
        if v.branch.get("title") == pascal(old):
            v.branch["title"] = pascal(new_label)
        tr.variant(old)["label"] = new_label
    elif kind in ("add-field", "remove-field", "rename-field"):
        target, sep, rest = split_target(arg, kind)
        label, key = target.split(".", 1)
        v = cur.variant(label)
        props = v.branch.setdefault("properties", {})
        required = v.branch.setdefault("required", [])
        t = tr.variant(label)
        if kind == "add-field":
            if key in props:
                fail(f"`{label}` already has a field `{key}`")
            if not sep:
                fail("--add-field expects LABEL.FIELD=PROPERTY_JSON")
            ps = parse_json(rest, "--add-field")
            if not isinstance(ps, dict):
                fail("--add-field: the property schema must be a JSON object")
            backfill = ps.pop("x-backfill", None)
            props[key] = ps
            if not nullable(doc, ps):
                required.append(key)
            fid = tr.new_id()
            t["fields"].append({"id": fid, "key": key})
            if backfill is not None:
                tr.backfill[fid] = backfill
        elif kind == "remove-field":
            v.field(key)
            del props[key]
            if key in required:
                required.remove(key)
            t["fields"] = [x for x in t["fields"] if x["key"] != key]
        else:
            new_key = rest.strip()
            if not sep or not new_key:
                fail("--rename-field expects LABEL.OLD=NEW")
            v.field(key)
            if new_key in props:
                fail(f"`{label}` already has a field `{new_key}`")
            items = list(props.items())
            props.clear()
            for k, val in items:
                props[new_key if k == key else k] = val
            v.branch["required"] = [new_key if k == key else k for k in required]
            next(x for x in t["fields"] if x["key"] == key)["key"] = new_key


def cmd_modify(a):
    data0 = parse_json(sys.stdin.read() if a.schema == "-" else open(a.schema).read(), a.schema)
    doc0 = Doc(data0)
    node0, title = find_target(doc0, a.defn)
    old = build(doc0, node0, title, a.name)
    tables = tables_of(a, old)
    if not a.actions:
        fail("modify needs at least one action (--add-variant, --remove-variant, --rename-variant, --add-field, ...)")

    data = copy.deepcopy(data0)
    doc = Doc(data)
    node, _ = find_target(doc, a.defn)
    tr, notes = Tracker(old), []
    for kind, arg in a.actions:
        apply_action(doc, node, title, a.name, kind, arg, tr, notes)
    new = build(doc, node, title, a.name)
    for tb in tables:
        columns(new, tb)  # validates every field type and column name

    old_v = dict(enumerate(old.variants))
    new_v = {t["id"]: new.variant(t["label"]) for t in tr.variants}
    removed = [v for i, v in old_v.items() if i not in new_v]
    added = [t for t in tr.variants if t["id"] not in old_v]
    kept = [(old_v[t["id"]], new_v[t["id"]], t) for t in tr.variants if t["id"] in old_v]
    renamed = [(o.label, n.label) for o, n, _ in kept if o.label != n.label]
    swap = bool(removed)

    # Field-level changes (variant objects from old/new models).
    field_renames, field_drops, field_adds = [], [], []
    for v in removed:
        field_drops += [(v, f) for f in v.fields]
    for t in added:
        nv = new_v[t["id"]]
        field_adds += [(nv, f, None) for f in nv.fields]
    for o, n, t in kept:
        live = {x["id"]: x["key"] for x in t["fields"]}
        for of in o.fields:
            fid = (t["id"], of.key)
            if fid in live:
                field_renames.append((o, of, n, n.field(live[fid])))
            else:
                field_drops.append((o, of))
        for fid, key in live.items():
            if fid[0] == "new":
                nf = n.field(key)
                backfill = tr.backfill.get(fid)
                if not nf.optional and backfill is None:
                    fail(
                        f"`{n.label}.{key}` is required, so existing `{n.label}` rows need a value: add "
                        f"\"x-backfill\": \"<SQL expression>\" to the property, or allow null"
                    )
                field_adds.append((n, nf, backfill))
    shape_changed = swap or bool(added) or bool(field_drops) or bool(field_adds)
    if not (shape_changed or renamed or any(o.part != n.part or of.part != nf.part for o, of, n, nf in field_renames)):
        fail("the actions do not change the database")

    first, body = [], []
    rename_values = [f"ALTER TYPE {old.type} RENAME VALUE {q(x)} TO {q(y)};" for x, y in renamed]
    if added and not swap:
        first += rename_values
        for t in added:
            i = [x["id"] for x in tr.variants].index(t["id"])
            if i > 0:
                first.append(f"ALTER TYPE {old.type} ADD VALUE {q(t['label'])} AFTER {q(tr.variants[i - 1]['label'])};")
            else:
                first.append(f"ALTER TYPE {old.type} ADD VALUE {q(t['label'])} BEFORE {q(tr.variants[1]['label'])};")
    else:
        body += rename_values
    if swap:
        labels = ", ".join(v.label for v in removed)
        for tb in tables:
            body.append(
                "DO $$\nBEGIN\n"
                f"    IF EXISTS (SELECT 1 FROM {tb.name} WHERE {tb.vcol}::text IN ({', '.join(q(v.label) for v in removed)})) THEN\n"
                f"        RAISE EXCEPTION '{tb.name} still has {tb.vcol} rows in ({labels}); migrate or delete them first';\n"
                "    END IF;\nEND $$;"
            )
    old_enum_types = set(field_enums(old))
    for typ, labels in field_enums(new).items():
        if typ not in old_enum_types:
            body.append(f"-- New field enum type; skip if it already exists.\n{create_type(typ, labels)}")
    if shape_changed:
        body += [f"ALTER TABLE {tb.name} DROP CONSTRAINT IF EXISTS {tb.check};" for tb in tables]
    if swap:
        tmp = ident(f"{old.type}_new", "enum type")
        body.append(create_type(tmp, [v.label for v in new.variants]))
        for tb in tables:
            body.append(
                f"ALTER TABLE {tb.name} ALTER COLUMN {tb.vcol} TYPE {tmp} USING {tb.vcol}::text::{tmp};"
            )
        body += [f"DROP TYPE {old.type};", f"ALTER TYPE {tmp} RENAME TO {old.type};"]
    for tb in tables:
        for o, of, n, nf in field_renames:
            if tb.col(o, of) != tb.col(n, nf):
                body.append(f"ALTER TABLE {tb.name} RENAME COLUMN {tb.col(o, of)} TO {tb.col(n, nf)};")
        for v, f in field_drops:
            body.append(f"ALTER TABLE {tb.name} DROP COLUMN {tb.col(v, f)};")
        for v, f, _ in field_adds:
            sql, _ = column_type(doc, f.schema, f"{v.label}.{f.key}")
            body.append(f"ALTER TABLE {tb.name} ADD COLUMN {tb.col(v, f)} {sql};")
        for v, f, backfill in field_adds:
            if backfill is not None:
                body.append(f"UPDATE {tb.name} SET {tb.col(v, f)} = {backfill} WHERE {tb.vcol} = {q(v.label)};")
    if shape_changed and new.has_fields():
        for tb in tables:
            body += add_constraint(new, tb)

    migrations = []
    if first:
        migrations.append({"name": f"add_{old.name}_values", "sql": "\n".join(first) + "\n"})
        notes.insert(
            0,
            "Run the migrations as separate transactions (separate files for sqlx/refinery): a value added with "
            "ADD VALUE cannot be used, not even in the CHECK, until its transaction has committed.",
        )
    if body:
        migrations.append({"name": f"update_{old.name}", "sql": "\n\n".join(body) + "\n"})
    if swap:
        notes.append(
            f"Removing a variant recreates {old.type}. Every table storing it must be passed with --table "
            f"(given: {', '.join(t.name for t in tables)}). Partial indexes, views, functions and defaults that "
            "use these columns or the type must be dropped before this migration and recreated after it."
        )
    if renamed or removed or added:
        notes.append(
            "Labels changed: update SQL that matches on "
            + ", ".join(t.vcol for t in tables)
            + " (CASE, WHERE, partial indexes, views, functions). Postgres does not check CASE exhaustiveness."
        )
    notes.append("Update the application types generated from or mirroring this schema (Rust: rust.md).")

    schema_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    written = None
    if a.write:
        if a.schema == "-":
            fail("--write needs a schema file, not stdin")
        with open(a.schema, "w") as fh:
            fh.write(schema_text)
        written = a.schema
    return {"migrations": migrations, "notes": notes, "schema": data, "schema_text": schema_text, "written": written}


def render(result, as_json):
    if as_json:
        result = {k: v for k, v in result.items() if k != "schema_text"}
        return json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if isinstance(result, str):
        return result
    out = []
    n = len(result["migrations"])
    for i, m in enumerate(result["migrations"], 1):
        out.append(f"-- ===== migration {i} of {n}: {m['name']} =====\n{m['sql']}")
    if result.get("notes"):
        out.append("-- ===== notes =====\n" + "".join(f"-- * {x}\n" for x in result["notes"]))
    if "schema_text" in result:
        if result["written"]:
            out.append(f"-- ===== schema written to {result['written']} =====\n")
        else:
            out.append("-- ===== updated schema =====\n" + result["schema_text"])
    return "\n".join(out)


class ActionAppend(argparse.Action):
    def __call__(self, parser, ns, values, option_string=None):
        acts = getattr(ns, "actions", None) or []
        acts.append((option_string.lstrip("-"), values))
        ns.actions = acts


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("schema", help="JSON Schema file, or - for stdin")
    common.add_argument("--def", dest="defn", help="$defs entry (or root title) of the union")
    common.add_argument("--name", help="snake_case enum name (default: snake_case of the union's title)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("show", parents=[common], help="print variants, columns and SQL types")
    s = sub.add_parser("setup", parents=[common], help="SQL to create the type, columns and constraint")
    s.add_argument("--table", action="append", help="TABLE[,nullable][,prefix=P]; repeatable")
    s.add_argument("--form", choices=["columns", "alter"], default="columns",
                   help="columns: lines for CREATE TABLE (default); alter: ALTER TABLE ADD ...")
    s.add_argument("--no-types", action="store_true", help="omit CREATE TYPE (types already exist)")
    m = sub.add_parser("modify", parents=[common], help="migration SQL and updated schema for changes")
    m.add_argument("--table", action="append", help="TABLE[,nullable][,prefix=P]; every table storing the enum")
    for flag in ("add-variant", "remove-variant", "rename-variant", "add-field", "remove-field", "rename-field"):
        m.add_argument(f"--{flag}", action=ActionAppend, metavar="ARG")
    m.add_argument("--write", action="store_true", help="save the updated schema back to the file")
    m.set_defaults(actions=[])
    a = p.parse_args(argv)
    try:
        result = {"show": cmd_show, "setup": cmd_setup, "modify": cmd_modify}[a.cmd](a)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(render(result, a.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
