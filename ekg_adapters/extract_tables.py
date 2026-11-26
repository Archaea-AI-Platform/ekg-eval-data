"""Tables: two small model calls per table for a COLUMN MAPPING, then code writes every row.

A table (a spreadsheet sheet, a CSV, a table in a PDF, a Word file or a wiki page) arrives as Pieces of
kind "table": its heading line(s), then `cell | cell | cell` lines, header row first; a long table comes in
several pieces that each repeat the header (fill_docs.tables puts them back together). The model never sees
the table row by row. It sees the heading, the header and a sample of rows, twice:

  step 1 (ekg_table_shape)  with the record types (the first sentence of each one's description): the shape, the
                            row type, the composite cells, the name / key / statement columns, and the record type
                            each other column names. A small reply.
  step 2 (ekg_table_map)    with the row type's fields and ONLY the links the registry allows between the table's
                            types, each with its description: each column's field or link.

The decision is made small on purpose: one call over the whole table schema (80,000 characters on ACME) thought for
up to 16,000 tokens and gave another verdict for 5 of 15 tables on an identical rebuild. A step the size or time
guard refuses is asked once more with the shorter tier; an accepted mapping is kept by table signature
(MappingCache) for the next run. The shapes:

  rows        each row is one record, or one relation between records (an event, an assignment, a count per
              person and module). The mapping names: the record type a row is about (row_type), the column that
              names it (name_column, key_column), the columns that are its fields, and the columns that name a
              record it is linked to, each link with the columns that hold values OF THE LINK (a date, a role, a
              count) when the registry gives the link such a field.
  key_value   the table describes ONE record and each ROW is one of its fields or links ("Owner | Suzan",
              "Status | active"): the record's type and name, and which row label is which field or link.
  none        not records of a listed type (figures, a layout table, a glossary): the mapping is rejected and
              the table is read as text by doc_facts with the rest of its document.

Code checks the mapping against the registry and then applies it to every row: a 100,000-row sheet costs one
call. Each row's record is found in the graph (key, name, alias, email, login) or created when its type may be
created from a document; each link cell is resolved the same way, a cell listing several values cut by the
mapping's `split`. Method template_rule, evidence the table piece the row is in, confidence
fill.CONFIDENCE["table"], and a DerivationRun for the pass. Nothing is dropped silently: a row, a cell or a link
that cannot be written is counted by reason, and so is every non-empty cell of a column (or row of a key/value
table) the mapping gives no field: "no field for this column: <header>".

A mapping that is rejected (shape none, or it fails the checks) or whose call fails leaves its table to doc_facts
(fill.run builds the reading windows after the tables are settled), so every table is read once and only once.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import re

from . import common, extract_facts, extract_schema, fill_docs
from .extract_schema import ASPECT, LOOKUP_FIELDS, PERSONAL_NAME, coerce, first_sentence, period, placeholder, year_of
from .fill_tasks import Item, Task, name_key

SAMPLE_HEAD, SAMPLE_REST = 15, 5   # rows the model is shown: the first ones and a few spread over the rest
SPLITS = {"none": None, ",": ",", ";": ";", "/": "/", "space": " "}
SHAPES = ["rows", "key_value", "none"]
MAX_PARTS = 3   # values one composite cell may hold ("Lionel 42", "#174 / pay-auth")
MAPPING_RETRIES = 1   # a mapping a code check rejects is asked again this many times, with the reason
SHAPE_RULES = (
    "- shape: \"rows\" when each row is one record of a listed type or one relation between records (an event, an "
    "assignment, a contact per area, a count per person and module): some column names records of a listed type. "
    "\"key_value\" when the whole table describes ONE record and each ROW is one of its fields or links: a label in "
    "the first column, its value in the second (Owner | ..., Status | ...). \"none\" only when no column names records "
    "of a listed type (figures, a layout table, terms and their meanings): the table is then read as ordinary text. "
    "Decide by the descriptions of the types above, not by how the table looks.\n"
    "- row_type: for \"rows\", the record type each row is about (for a relation, the record at one end: the person "
    "of \"module | person | commits\"), a record one of its columns names, so that name_column or key_column can point "
    "at it. A table that is an export of another tool (a commit log, a merge log) has rows of that tool's type. For "
    "\"key_value\", the type of the one record. \"none\" for shape none.\n"
    "SHAPE rows:\n"
    "- cell_parts: a column whose every cell holds two or three values (\"Lionel 42\" = a person and a count, \"#174 / "
    "pay-auth\" = a pull request and a module): the column, parts (how many values), and either the separator "
    "between them or a regular expression (pattern) with one group per value; the other one empty. Its values are "
    "then columns of their own, named c2.1, c2.2 ..., to use anywhere below. Code splits every row so, and rejects "
    "the mapping if a sample row does not split.\n"
    "- A cell that writes a person's name together with anything else (\"Suzan / @suzan\", \"E004 Suzan eng-payments\", "
    "\"Kim - kim@acme.com\", \"Suzan - PAY-AUTH\") is a composite: split it with cell_parts and give each part its own "
    "column. Code rejects a person's name column whose sample cells all hold more than a name.\n"
    "- name_column: the column that names the row's record, by the field its type is \"Named by\". \"none\" if the type has none.\n"
    "- key_column: a column that identifies the row better than its name (an email address, an id, a number, a sha), "
    "else \"none\".\n"
    "- statement_column: for a type \"stated in\" a field (a decision, a piece of knowledge, a requirement ...), the "
    "column holding that statement, else \"none\".\n"
    "- columns: one entry for every column or column part whose cells name a record of a listed type OTHER than the "
    "row's own record: a person, a team or group, an account (a login or @handle), an email address, a service or "
    "module, a repository, a file or folder (a path), a pull request or ticket (by its number or key) ...: the column "
    "and that type. A column of plain values (a date, a count, a job title, a description) is not listed, and neither "
    "is the row's own name or key column. For key_value give [].\n"
    "SHAPE key_value:\n"
    "- entity_name: the one record's name, copied from the table, its heading or the document title; else \"\".\n"
    "- reason: one sentence.\n")
SHAPE_ASK = ("This is one table: its heading, its header and a sample of its {rows:,} rows (all of them when it is short). "
             "Say what its rows are and what each column names, so that code can read every row the same way. This is "
             "the first of two short steps: the second maps each column to a field or a link, from a short list made for "
             "the types you name here. Your reply describes columns: it never lists rows or copies cell values.\n"
             "{types}\n\nRULES\n" + SHAPE_RULES +
             'Reply with one JSON object: {{"shape", "row_type", "cell_parts": [{{"column", "parts", "separator", "pattern"}}], '
             '"name_column", "key_column", "statement_column", "columns": [{{"column", "type"}}], "entity_name", "reason"}}. '
             "Columns are named c1, c2 ...")
COLUMNS_ASK = (
    "This is one table: its heading, its header and a sample of its {rows:,} rows. An earlier step read its shape:\n"
    "{step1}\n\n"
    "Now say which field or link each column holds, choosing only from the lists below: code made them from the schema "
    "for the record types of this table. Your reply describes columns, never rows.\n"
    "{fields}\n\n{links}\n\n"
    "RULES\n"
    "- key_field: when the key column is the row record's own key, the field of {row_type} it holds (a pull request's "
    "number, a commit's sha, an id), else \"none\"; \"none\" too when it is only a value to look the record up by (an email).\n"
    "- fields: a column (or column part) that holds one of the fields of {row_type} listed above: the column and that "
    "field. A column the first step listed as naming a record may still be one of these fields (a job title, an email) "
    "when no listed link fits it.\n"
    "- links: for a column the first step lists as naming a record: the column, the link type whose description says "
    "what the table states, and row_is: \"from\" when the link goes near end -> the column's record, \"to\" when it goes "
    "the column's record -> near end, as the arrow and the description give it. The near end is the row's own record "
    "(from_column \"row\"), or the record another listed column of the same row names (from_column: that column): a row "
    "can state links between two of its other columns' records too (a module's repository and its file). "
    "other_key_field: when the cells hold the other record's key rather than its name (a pull request's number, a "
    "commit's sha, a file's path), that field of the other type, else \"none\". split: the separator when one cell lists "
    "several (\",\", \";\", \"/\", \"space\"), else \"none\". fields: the columns that hold a value OF THAT LINK (a date, "
    "a count, a role, the part of the other record it is about), each with the link's field name as listed. fixed: a "
    "link field with listed values whose value the column's header or the table's heading states for every row, with "
    "one of those values; else []. period: when a column gives the link's time as a period (a start, an end, or a "
    "range), that column, and the words its cells put before a start (from_words) and before an end (to_words), "
    "copied from the cells as written; column \"none\" and [] otherwise. Code reads its dates: a range is a start and an "
    "end, one date after an end word is the end, any other one date the start. A column may have several entries, "
    "one per link it states.\n"
    "- A link states only what the table says: a count of activity (commits, reviews) does not say who owns, approves "
    "or belongs to something, and a reviewer asked for is not an owner. Choose the link whose description says what "
    "the column's header says.\n"
    "- schema_gap: when a column states something that no listed link or field can hold (for example a count per "
    "person and module when no fitting link has a count field), one sentence saying what, in which columns, and which "
    "link is missing; else \"\". Code reports it, and reads such a table as text as well.\n"
    "- For key_value: label_fields (a row label, as written in the first cell, whose value is a field of the record, and "
    "the field) and label_links (a row label whose value names another record: the label, the link type, the other "
    "record's type, row_is, split). For rows give them as [].\n"
    "- Map a column only when the table makes its meaning plain. Leave the others out (code counts them, and a table "
    "with columns no mapping reads is read as text too). Each list has at most one entry per column, part or label "
    "and link; reason is one sentence.\n"
    'Reply with one JSON object: {{"key_field", "fields": [{{"column", "field"}}], "links": [{{"column", "link", "row_is", '
    '"from_column", "other_key_field", "split", "fields": [{{"column", "field"}}], "fixed": [{{"field", "value"}}], '
    '"period": {{"column", "from_words", "to_words"}}}}], '
    '"schema_gap", "label_fields": [{{"label", "field"}}], "label_links": [{{"label", "link", "other_type", "row_is", '
    '"split"}}], "reason"}}')
TIERS = ("full", "short")   # how much of the registry's descriptions a step shows; a step refused by the size or time
                            # guard is asked once more with the next, shorter one (a smaller list to think about)
MAPPINGS = None   # MappingCache of this run (fill.run sets it): accepted mappings by table signature


def build(v):
    """One task per table. The task's identity covers every row, so a changed table is mapped again. Every table
    piece a task reads is claimed (v.claimed): the reading tasks leave it out unless its mapping is not accepted."""
    cat = v.catalog()
    docs, _ = v.docs()
    v.overhead.setdefault("table_map", overhead(cat, v.budget))
    room = v.room("table_map")
    tasks, skipped = [], collections.Counter()
    for doc in docs:
        for tb in fill_docs.tables(doc):
            lines = [ln for _, ln in tb.lines()]
            if not lines:
                skipped["table with a header and no rows (read as text)"] += 1
                continue
            if len(tb.header) < 2:
                skipped["table with one column (read as text)"] += 1
                continue
            rest = lines[SAMPLE_HEAD:]
            sample = lines[:SAMPLE_HEAD] + ([rest[i * len(rest) // SAMPLE_REST] for i in range(SAMPLE_REST)] if len(rest) > SAMPLE_REST else rest)
            head = tb.head
            while len(sample) > 1 and len(tb.lead) + len(head) + sum(len(s) + 1 for s in sample) > room:
                sample = sample[: len(sample) // 2]  # very wide rows: fewer of them
            cols = "\n".join(f"c{i + 1} = \"{h}\"" for i, h in enumerate(tb.header))
            digest = hashlib.sha1("\n".join(u.text for u in tb.units).encode()).hexdigest()
            ctx = (f"DOCUMENT: {doc.title}\nTABLE: {tb.heading or doc.title} ({len(lines):,} rows, {len(tb.header)} columns)\n"
                   + (f"ABOVE THE TABLE: {tb.lead}\n" if tb.lead else "") + f"COLUMNS\n{cols}")
            tasks.append(Task("table_map", doc.rec["id"], f"Table in {doc.title}" + (f": {tb.heading}" if tb.heading else ""), "",
                              [Item("T1", tb.units[0].id, "header and sample rows", "\n".join([head] + sample), "table")], [],
                              {"doc": doc.rec["id"], "table": tb, "rows": len(lines), "quality": doc.quality,
                               "title": doc.title, "units": [u.id for u in tb.units],
                               "signature": signature(doc.rec, tb)},
                              context=ctx, extra=digest))
            v.claimed.update(u.id for u in tb.units)
    return tasks, skipped


def signature(doc_rec, tb):
    """What makes a table the same table for its mapping: its document, heading, header row and column count. Rows
    may come and go; a mapping describes columns."""
    return hashlib.sha1("\n".join([doc_rec["props"].get("_key") or doc_rec["id"], tb.heading or "", str(len(tb.header)),
                                   " | ".join(tb.header)]).encode()).hexdigest()[:24]


class MappingCache:
    """Accepted mappings by table signature, in <out>/table_mappings.json: a rebuild reuses a table's mapping (held to
    the registry again by check) and asks the model only for a table that is new or changed."""

    def __init__(self, path):
        self.path, self.rows, self.hits = path, {}, 0
        try:
            self.rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            self.rows = {}

    def get(self, t):
        row = self.rows.get(t.write.get("signature"))
        return row["answer"] if row else None

    def put(self, t, answer, model=""):
        self.rows[t.write["signature"]] = {"table": t.question, "model": model,
                                           "answer": {k: v for k, v in answer.items() if k != "_usage"}}
        self.path.write_text(json.dumps(self.rows, indent=1, ensure_ascii=False), encoding="utf-8")


def catalog_text(catalog, budget=None):
    """The whole tables schema (Catalog.schema_text), as one prompt used to carry it: kept for the report's sizes."""
    from . import fill
    return catalog.schema_text("tables", (budget or getattr(catalog, "budget", None) or fill.BUDGET).schema_chars)


def types_text(catalog, tier="full"):
    """Step 1's list of record types: each with the first sentence of the registry's description ("full") or its
    name alone ("short"), and the field it is named by."""
    out = ["RECORD TYPES (what each is, and the field it is named by). A table may also be an export of another tool "
           "(a commit log, a list of pull requests): its rows are then that tool's records, matched by their key."]
    for t in catalog.table_types:
        named = " or ".join(catalog.names(t)) or "-"
        about = first_sentence(catalog._about_type(t, {"types": "full"})).rstrip(";.") if tier == "full" else ""
        out.append(f"- {t}: {about}. Named by {named}" if about else f"- {t} (named by {named})")
    return "\n".join(out)


def overhead(catalog, budget=None) -> int:
    """Characters of a table_map prompt that are not the table: header, the larger of the two steps' instructions."""
    from .fill import HEADER
    return len(HEADER) + max(len(SHAPE_ASK.format(rows=0, types=types_text(catalog))), len(COLUMNS_ASK) + 12_000) + 600


def _cols(n):
    base = [f"c{i + 1}" for i in range(n)]
    return base, base + [f"{c}.{k}" for c in base for k in range(1, MAX_PARTS + 1)]   # a column's parts (cell_parts)


def prompt(t: Task, catalog, tier="full", note=""):
    """Step 1, the shape: the table and the list of record types; a small reply."""
    from .fill import HEADER, _text_block
    return "\n".join([HEADER, ""] + _text_block(t) + ["", SHAPE_ASK.format(rows=t.write["rows"], types=types_text(catalog, tier))]) + note


def shape_schema(catalog, n_columns):
    base, cols = _cols(n_columns)
    types = catalog.table_types
    part = {"type": "object", "additionalProperties": False, "required": ["column", "parts", "separator", "pattern"],
            "properties": {"column": {"type": "string", "enum": base}, "parts": {"type": "integer", "enum": list(range(1, MAX_PARTS + 1))},
                           "separator": {"type": "string"}, "pattern": {"type": "string"}}}
    col = {"type": "object", "additionalProperties": False, "required": ["column", "type"],
           "properties": {"column": {"type": "string", "enum": cols}, "type": {"type": "string", "enum": types}}}
    props = {"shape": {"type": "string", "enum": SHAPES}, "row_type": {"type": "string", "enum": types + ["none"]},
             "cell_parts": {"type": "array", "items": part},
             "name_column": {"type": "string", "enum": cols + ["none"]}, "key_column": {"type": "string", "enum": cols + ["none"]},
             "statement_column": {"type": "string", "enum": cols + ["none"]},
             "columns": {"type": "array", "items": col}, "entity_name": {"type": "string"}, "reason": {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def column_types(shape):
    """{column: the record type step 1 says its cells name}."""
    out = {}
    for x in shape.get("columns") or []:
        if isinstance(x, dict) and isinstance(x.get("column"), str) and x.get("type") in common._reg.records:
            out.setdefault(x["column"], x["type"])
    return out


def candidate_links(catalog, rtype, col_types, key_value=False):
    """The links step 2 may choose from, made by code from the registry: for a rows table, those the registry allows
    between two of the table's record types, the row's and those its columns name (one end always a column's);
    for a key/value table, those with one end the record's type. {link: (from types, to types)}."""
    if key_value:
        return {n: (a, b) for n, (a, b) in catalog.table_links.items() if rtype in a or rtype in b}
    ends = list(dict.fromkeys([rtype, *col_types.values()]))
    cols = list(col_types.items())
    pairs = {(rtype, ty) for _, ty in cols} | {(t1, t2) for c1, t1 in cols for c2, t2 in cols if c1 != c2}
    out = {}
    # the registry's own end types only (catalog.table_links): a link open to "any" record at one end (ABOUT) is
    # offered for the types it names, never for every pair of the table's types
    for n, (froms, tos) in catalog.table_links.items():
        hits = [(a, b) for x, y in pairs for a, b in ((x, y), (y, x)) if a in froms and b in tos]
        if hits:
            out[n] = (sorted({a for a, _ in hits}, key=ends.index), sorted({b for _, b in hits}, key=ends.index))
    return out


def _fields_block(catalog, rtype, tier):
    fs = catalog.fields(rtype)
    if not fs:
        return f"FIELDS OF {rtype}: none a column can fill"
    rows = []
    for n, s in fs.items():
        about = (s.about if tier == "full" else "") or ""
        rows.append(f"  - {catalog._field_text(n, s)}" + (f": {about}" if about else ""))
    return f"FIELDS OF {rtype} (a column holding one of these is that field):\n" + "\n".join(rows)


def _links_block(catalog, cands, tier):
    if not cands:
        return "LINKS: none joins the record types of this table (name a schema_gap if a column states a relation)"
    detail = {"links": "full", "link_fields": "full"} if tier == "full" else {"links": "first"}
    out = ["LINKS between the record types of this table (A LINK B: A is the record on the left; stored only in this "
           "direction, between these types):"]
    aspect = None
    for n, (a, b) in cands.items():
        fs = catalog.link_value_fields(n)
        aspect = aspect or fs.get(ASPECT)
        fs = {f: s for f, s in fs.items() if f != ASPECT}   # every link has it: said once, below
        own = catalog._fields_text(fs, "link_fields", detail, ", ".join(catalog._field_text(f, s) for f, s in fs.items()))
        out.append(f"- {n}: {' | '.join(a)} -> {' | '.join(b)}{catalog._about_link(n, detail)}" + own)
    if aspect is not None:
        out.append(f"Every link above also has the field {ASPECT}: {aspect.about}")
    return "\n".join(out)


def step1_text(shape, col_types):
    rtype = shape.get("row_type")
    if (shape.get("shape") or "rows") == "key_value":
        return (f"The whole table describes ONE {rtype} named \"{shape.get('entity_name') or ''}\"; each row is one of its "
                "fields or links, its label in the first column.")
    parts = [f"Each row is one {rtype} record" + "".join(
        f"; {what} column {shape[k]}" for k, what in (("name_column", "its name"), ("key_column", "its key"),
                                                     ("statement_column", "its statement")) if shape.get(k) not in (None, "", "none"))]
    for p in shape.get("cell_parts") or []:
        if isinstance(p, dict):
            how = f"the separator \"{p.get('separator')}\"" if p.get("separator") else f"the pattern {p.get('pattern')}"
            parts.append(f"column {p.get('column')} holds {p.get('parts')} values each, split by {how} into "
                         + ", ".join(f"{p.get('column')}.{k + 1}" for k in range(int(p.get('parts') or 0))))
    parts.append("columns that name records: " + (", ".join(f"{c} = {ty}" for c, ty in col_types.items()) or "none"))
    return ".\n".join(parts) + "."


def columns_prompt(t: Task, catalog, shape, tier="full", note=""):
    """Step 2, the columns: the row type's fields and the links code found between this table's types."""
    from .fill import HEADER, _text_block
    rtype, cts = shape["row_type"], column_types(shape)
    cands = candidate_links(catalog, rtype, cts, (shape.get("shape") or "rows") == "key_value")
    ask = COLUMNS_ASK.format(rows=t.write["rows"], step1=step1_text(shape, cts), row_type=rtype,
                             fields=_fields_block(catalog, rtype, tier), links=_links_block(catalog, cands, tier))
    return "\n".join([HEADER, ""] + _text_block(t) + ["", ask]) + note, cands


def columns_schema(catalog, n_columns, rtype, cands):
    _, cols = _cols(n_columns)
    names = list(cands) or ["none"]
    offered = sorted(set(catalog.fields(rtype)) | {f for f in (catalog.name_field(rtype), catalog.text_field(rtype)) if f})
    pair = lambda key, field_enum=None: {
        "type": "object", "additionalProperties": False, "required": [key, "field"],
        "properties": {key: {"type": "string", "enum": cols} if key == "column" else {"type": "string"},
                       "field": {"type": "string", "enum": field_enum} if field_enum else {"type": "string"}}}
    fixed = {"type": "object", "additionalProperties": False, "required": ["field", "value"],
             "properties": {"field": {"type": "string"}, "value": {"type": "string"}}}
    per = {"type": "object", "additionalProperties": False, "required": ["column", "from_words", "to_words"],
           "properties": {"column": {"type": "string", "enum": cols + ["none"]},
                          "from_words": {"type": "array", "items": {"type": "string"}},
                          "to_words": {"type": "array", "items": {"type": "string"}}}}
    lk = {"type": "object", "additionalProperties": False,
          "required": ["column", "link", "row_is", "from_column", "other_key_field", "split", "fields", "fixed", "period"],
          "properties": {"column": {"type": "string", "enum": cols}, "link": {"type": "string", "enum": names},
                         "row_is": {"type": "string", "enum": ["from", "to"]}, "from_column": {"type": "string", "enum": ["row"] + cols},
                         "other_key_field": {"type": "string"}, "split": {"type": "string", "enum": list(SPLITS)},
                         "fields": {"type": "array", "items": pair("column")}, "fixed": {"type": "array", "items": fixed},
                         "period": per}}
    lab = {"type": "object", "additionalProperties": False, "required": ["label", "link", "other_type", "row_is", "split"],
           "properties": {"label": {"type": "string"}, "link": {"type": "string", "enum": names},
                          "other_type": {"type": "string", "enum": catalog.table_types},
                          "row_is": {"type": "string", "enum": ["from", "to"]}, "split": {"type": "string", "enum": list(SPLITS)}}}
    props = {"key_field": {"type": "string", "enum": sorted(catalog.key_fields(rtype)) + ["none"]},
             "fields": {"type": "array", "items": pair("column", offered or None)},
             "links": {"type": "array", "items": lk}, "schema_gap": {"type": "string"},
             "label_fields": {"type": "array", "items": pair("label")}, "label_links": {"type": "array", "items": lab},
             "reason": {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def merge(shape, cols):
    """The two steps' answers as one mapping for check(): step 1's shape and columns, step 2's fields and links, each
    link given the record type step 1 says its column names (unless the link names one itself). A link put on a whole
    column whose parts step 1 typed alike (c3 for c3.1, c3.2) takes that type."""
    cts = column_types(shape)
    links = []
    for x in cols.get("links") or []:
        if isinstance(x, dict) and x.get("link") not in (None, "none"):
            c = x.get("column")
            ty = x.get("other_type") or cts.get(c)
            if ty is None and isinstance(c, str):
                part_types = {t for k, t in cts.items() if k.startswith(c + ".")}
                ty = part_types.pop() if len(part_types) == 1 else None
            links.append({**x, "other_type": ty})
    out = {k: shape.get(k) for k in ("shape", "row_type", "cell_parts", "name_column", "key_column", "statement_column",
                                      "entity_name", "columns")}
    out.update({k: cols.get(k) for k in ("key_field", "fields", "schema_gap", "label_fields", "label_links")})
    out["links"] = links
    out["label_links"] = [x for x in out.get("label_links") or [] if isinstance(x, dict) and x.get("link") not in (None, "none")]
    out["reason"] = " ".join(x for x in (shape.get("reason"), cols.get("reason")) if x)
    return out


def schema(catalog, n_columns):
    """Step 1's reply schema (the first call a table makes)."""
    return shape_schema(catalog, n_columns)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


def labels(tb):
    """{normalised label: label as written} for a key/value table: the first cell of the header and of every row."""
    out = {}
    for c in [tb.header] + [cells for _, cells in tb.rows()]:
        if c and c[0].strip():
            out.setdefault(_norm(c[0]), c[0].strip())
    return out


def _link_ok(reg, catalog, x, rtype, dropped):
    """A proposed link held to the registry: (link type, other type, row_from) or None (counted)."""
    ltype, other = x.get("link"), x.get("other_type")
    if ltype not in catalog.table_links or other not in reg.records:
        dropped[f"mapping: {ltype} to {other} is not offered by the schema"] += 1
        return None
    fwd = x.get("row_is") == "from"
    ok = lambda forward: reachable(reg, ltype, rtype, other, forward)
    if not ok(fwd) and ok(not fwd):   # the link exists only the other way round: the registry fixes direction
        dropped[f"mapping: {ltype} direction turned to the one the registry allows"] += 1
        fwd = not fwd
    if not ok(fwd):
        dropped[f"mapping: {ltype} cannot join a {rtype} and a {other}"] += 1
        return None
    return ltype, other, fwd


def check(t: Task, ans, catalog, lenient_split=False):
    """The model's mapping, held to the registry: what does not fit is dropped and counted; a mapping with no
    usable row type or nothing to write is rejected (and its table is then read as text). lenient_split: a split
    that parses some sample cells but not every one is kept (settle uses it once the corrected mapping still fails
    the same way): the cells it does not parse are skipped and counted, and the table is read as text too."""
    from .fill import _error
    if (err := _error(ans)):
        return {"status": "error", "reason": err}
    reg, tb = common._reg, t.write["table"]
    n = len(tb.header)
    rtype, shape = ans.get("row_type"), ans.get("shape") or "rows"
    if shape == "none" or rtype not in catalog.table_types:
        return {"status": "rejected", "reason": f"llm: not a table of records ({(ans.get('reason') or '')[:200]})"}
    dropped = collections.Counter()
    nf, tf = catalog.name_field(rtype), catalog.text_field(rtype)
    offered = set(catalog.fields(rtype)) | {f for f in (nf, tf) if f}
    if shape == "key_value":
        return _check_key_value(t, ans, catalog, rtype, offered, nf, dropped)
    unsplit = [] if lenient_split else None
    parts, virtual, why = _cell_parts(t, ans.get("cell_parts") or [], n, unsplit)
    if why:
        return {"status": "rejected", "reason": f"check: {why}", "dropped": dict(dropped)}

    def col(c):
        if isinstance(c, str) and re.fullmatch(r"c[1-9]\d*", c) and int(c[1:]) <= n:
            return int(c[1:]) - 1
        return virtual.get(c)
    width = n + len(virtual)   # columns and column parts: no list of a mapping names more entries than that
    if any(len(ans.get(k) or []) > width for k in ("fields", "links", "cell_parts")):
        return {"status": "rejected", "reason": "check: the mapping lists more entries than the table has columns (a mapping "
                "names columns, never rows)", "dropped": dict(dropped)}
    name_col, key_col = col(ans.get("name_column")), col(ans.get("key_column"))
    key_field = ans.get("key_field") if key_col is not None and ans.get("key_field") not in (None, "", "none") else None
    if key_field is not None and key_field not in catalog.key_fields(rtype):
        dropped[f"mapping: {key_field} is not a key field of {rtype} (a plain text or number field)"] += 1
        key_field = None
    fields, links, used = [], [], set()
    if tf and (sc := col(ans.get("statement_column"))) is not None:
        used.add(tf)
        fields.append([sc, tf])
    for f in ans.get("fields") or []:
        c = col(f.get("column")) if isinstance(f, dict) else None
        if c is None:
            dropped["mapping: column that is not in the table"] += 1
        elif f.get("field") not in offered:
            dropped[f"mapping: {f.get('field')} is not a field of {rtype} in the registry"] += 1
        elif f["field"] in used:
            dropped[f"mapping: field {f['field']} mapped twice"] += 1
        else:
            used.add(f["field"])
            fields.append([c, f["field"]])
    if name_col is None and nf in used:   # the name given as a field mapping
        name_col = next(c for c, f in fields if f == nf)
    fields = [[c, f] for c, f in fields if f != nf]
    raw_links = [x for x in ans.get("links") or [] if isinstance(x, dict)]
    # the record type each link column names, so that another link can start at it (from_column)
    named_by = {}
    for x in raw_links:
        c = col(x.get("column"))
        if c is not None and x.get("other_type") in reg.records:
            named_by.setdefault(c, x.get("other_type"))
    # links from the row first: a link that starts at another column's record is written after that column's own
    for x in sorted(raw_links, key=lambda x: (x.get("from_column") or "row") != "row"):
        c = col(x.get("column"))
        if c is None:
            dropped["mapping: column that is not in the table"] += 1
            continue
        near_col = None
        if (x.get("from_column") or "row") not in ("row", "none"):
            near_col = col(x.get("from_column"))
            if near_col == c:   # a link cannot start at its own cell: the column named itself, so the near end is the row
                dropped["mapping: from_column named the link's own column, read as the row"] += 1
                near_col = None
            elif near_col is None or near_col not in named_by:
                dropped["mapping: from_column names no other column with a record type of its own"] += 1
                continue
            if near_col in (name_col, key_col):   # the row's own name or key: the row itself
                near_col = None
        joins = lambda ty: x.get("link") in catalog.table_links and x.get("other_type") in reg.records and any(
            reachable(reg, x["link"], ty, x["other_type"], fwd) for fwd in (True, False))
        if near_col is None and not joins(rtype):
            # the link cannot start at the row's record, but it can at the record exactly one other column of the row
            # names (a reviewer is APPROVER_FOR the row's repository, not the row's file): that record is the near end
            fit = [k for k, ty in named_by.items() if k != c and k not in (name_col, key_col) and joins(ty)]
            if len(fit) == 1:
                near_col = fit[0]
                dropped[f"mapping: {x['link']} starts at the record of {_col_name(fit[0], n, parts)}, the one column of the "
                        "row whose type it can join"] += 1
        near_type = rtype if near_col is None else named_by[near_col]
        got = _link_ok(reg, catalog, x, near_type, dropped)
        if got is None:
            continue
        ltype, other, fwd = got
        other_key = x.get("other_key_field") if x.get("other_key_field") not in (None, "", "none") else None
        if other_key is not None and other_key not in catalog.key_fields(other):
            dropped[f"mapping: {other_key} is not a key field of {other} (a plain text or number field)"] += 1
            other_key = None
        own, offered_own = [], catalog.link_value_fields(ltype)
        for f in x.get("fields") or []:
            fc = col(f.get("column")) if isinstance(f, dict) else None
            if fc is None:
                dropped["mapping: column that is not in the table"] += 1
            elif f.get("field") not in offered_own:
                dropped[f"mapping: {f.get('field')} is not a field of link {ltype} in the registry"] += 1
            elif any(f["field"] == g for _, g in own):
                dropped[f"mapping: link field {f['field']} mapped twice"] += 1
            else:
                own.append([fc, f["field"]])
        fixed = {}   # a link field the header states for every row: one of the field's listed values only
        for f in x.get("fixed") or []:
            spec = offered_own.get(f.get("field")) if isinstance(f, dict) else None
            got = coerce(spec, f.get("value")) if spec is not None and spec.enum else None
            if got is None:
                dropped["mapping: a fixed value that is not one of the link field's listed values"] += 1
            elif f["field"] in fixed or any(f["field"] == g for _, g in own):
                dropped[f"mapping: link field {f['field']} mapped twice"] += 1
            else:
                fixed[f["field"]] = got[0]
        per = _period_ok(x.get("period"), col, offered_own, own, tb, parts, dropped)
        links.append({"column": c, "link": ltype, "other": other, "row_from": fwd, "fields": own, "near": near_col,
                      "near_type": near_type, "other_key": other_key, **({"fixed": fixed} if fixed else {}),
                      **({"period": per} if per else {}),
                      "split": x.get("split") if x.get("split") in SPLITS else "none"})
    if name_col is None and key_col is None and not (tf and tf in used):
        return {"status": "rejected", "reason": f"check: {NO_NAME}", "dropped": dict(dropped)}
    if rtype not in catalog.creatable and not ((name_col is not None and nf) or key_field):   # a tool's record type
        return {"status": "rejected", "reason": f"check: a {rtype} row needs its name or its key field to be matched to the tool's record",
                "dropped": dict(dropped)}
    if not fields and not links and rtype in catalog.resolvable:
        return {"status": "rejected", "reason": "check: mapping has nothing to write", "dropped": dict(dropped)}
    sample = [expand(fill_docs.cells(line, tb.sep, tb.outer), parts) for line in t.sources[0].text.split("\n")[1:] if line.strip()]
    names = [(name_col, rtype, "none")] + [(l["column"], l["other"], l["split"]) for l in links]
    for c, ty, split in names:
        if c is not None and catalog.name_field(ty) == PERSONAL_NAME:
            cells = [x for row in sample if c < len(row) for x in values(row[c], split)]
            if cells and all(_more_than_a_name(x) for x in cells):
                return {"status": "rejected", "reason": f"check: column {_col_name(c, n, parts)} names a {ty} but every sample cell holds "
                        f"more than a name (\"{cells[0][:40]}\"): split it with cell_parts", "dropped": dict(dropped)}
    if (why := _shared_identity(tb, parts, name_col, key_col, links, n)):
        return {"status": "rejected", "reason": f"check: {why}", "dropped": dict(dropped)}
    m = {"shape": "rows", "row_type": rtype, "name": name_col, "key": key_col, "key_field": key_field, "parts": parts,
         "fields": fields, "links": links, "identifying": _identifying(catalog, rtype, name_col, key_col, fields, links)}
    unread = [_col_name(i, n, parts) for i in unread_columns(m, n) if any(i < len(row) and row[i] for row in sample)]
    gap = (ans.get("schema_gap") or "").strip()
    if unread or gap or unsplit:   # what the mapping does not read is read as text as well, so nothing a cell says is lost
        m["also_text"] = {"unread_columns": unread, **({"schema_gap": gap[:300]} if gap else {}),
                          **({"cells_that_do_not_split": unsplit[:10]} if unsplit else {})}
    return {"status": "accepted", "mapping": m, "reason": (ans.get("reason") or "")[:300], "dropped": dict(dropped),
            **({"schema_gap": gap[:300]} if gap else {})}


PERIOD_FIELDS = ("valid_from", "valid_to")   # the registry's interval fields a period column fills


def _period_ok(p, col, offered_own, own, tb, parts, dropped):
    """A link's period column, held to the table: a column of it, the link has the interval fields, and every word
    given is in that column's cells as written (the table's own words: no language is assumed here)."""
    if not isinstance(p, dict) or p.get("column") in (None, "", "none"):
        return None
    c = col(p.get("column"))
    if c is None or not all(f in offered_own for f in PERIOD_FIELDS):
        dropped["mapping: a period on a column that is not in the table, or a link with no valid_from/valid_to"] += 1
        return None
    if any(f in PERIOD_FIELDS for _, f in own):
        dropped["mapping: a period and a valid_from/valid_to column for one link: the column mapping is kept"] += 1
        return None
    rows = [expand(cells, parts) for _, cells in tb.rows()]
    text = "\n".join(r[c] for r in rows if c < len(r)).casefold()
    words = {k: [w.strip() for w in p.get(k) or [] if isinstance(w, str) and w.strip()] for k in ("from_words", "to_words")}
    bad = {w for ws in words.values() for w in ws if w.casefold() not in text}
    if bad:
        dropped["mapping: a period word that is not in the column's cells, left out"] += len(bad)
    return {"column": c, **{k: [w for w in ws if w not in bad] for k, ws in words.items()}}


def unread_columns(m, n):
    """The columns (and column parts) of a rows mapping that nothing reads: no name, key, field or link, and no
    link field. A split column is read by its parts."""
    parts = m.get("parts") or []
    width = n + sum(p[1] for p in parts)
    read = {m["name"], m["key"], *(i for i, _ in m["fields"]), *(l["column"] for l in m["links"]),
            *(i for l in m["links"] for i, _ in l.get("fields", [])),
            *(l["period"]["column"] for l in m["links"] if l.get("period"))}
    split_cols = {p[0] for p in parts}
    return [i for i in range(width) if i not in read and i not in split_cols]


def _check_key_value(t, ans, catalog, rtype, offered, nf, dropped):
    reg, tb = common._reg, t.write["table"]
    name = (ans.get("entity_name") or "").strip()
    shown = _norm("\n".join([t.write.get("title") or "", tb.heading or "", tb.lead or ""] + [u.text for u in tb.units]))
    if not name or _norm(name) not in shown:
        return {"status": "rejected", "reason": "check: the record's name is not in the table, its heading or the document title",
                "dropped": dict(dropped)}
    known = labels(tb)
    fields, links, used, named = [], [], set(), []
    for f in ans.get("label_fields") or []:
        lab = _norm(f.get("label")) if isinstance(f, dict) else ""
        if lab not in known:
            dropped["mapping: label that is not in the table"] += 1
        elif f.get("field") not in offered:
            dropped[f"mapping: {f.get('field')} is not a field of {rtype} in the registry"] += 1
        elif f["field"] in used or lab in {x for x, _ in fields}:
            dropped["mapping: field or label mapped twice"] += 1
        elif f["field"] == nf:  # the row that names the record: entity_name holds it
            named.append(lab)
        else:
            used.add(f["field"])
            fields.append([lab, f["field"]])
    for x in ans.get("label_links") or []:
        lab = _norm(x.get("label")) if isinstance(x, dict) else ""
        if lab not in known:
            dropped["mapping: label that is not in the table"] += 1
            continue
        got = _link_ok(reg, catalog, x, rtype, dropped)
        if got:
            links.append({"label": lab, "link": got[0], "other": got[1], "row_from": got[2], "fields": [],
                          "split": x.get("split") if x.get("split") in SPLITS else "none"})
    if not fields and not links:
        return {"status": "rejected", "reason": "check: key/value mapping has nothing to write", "dropped": dict(dropped)}
    return {"status": "accepted", "mapping": {"shape": "key_value", "row_type": rtype, "entity": name, "fields": fields,
                                              "links": links, "name_labels": named},
            "reason": (ans.get("reason") or "")[:300], "dropped": dict(dropped)}


def reachable(reg, ltype, rtype, other, forward):
    """Can this link join a row record and the other record in this direction, directly or through the other
    kinds of record the same identity has (a Person's Account, an EmailAddress's Account ...)?"""
    identity = {n for n, lk in reg.links.items() if lk.relation in ("ALIAS_OF", "HAS_ACCOUNT")}
    rows = {rtype} | extract_facts.family(reg, rtype, identity)
    others = {other} | extract_facts.family(reg, other, identity)
    return any(reg.link_allows(ltype, a, b) if forward else reg.link_allows(ltype, b, a) for a in rows for b in others)


def too_long(ans, budget=None):
    """Why a mapping reply is a failed mapping by its size alone, or None. A mapping names columns, never rows, so
    its reply is small by construction: one that ran into the reply limit, or took more tokens (thinking included)
    than Budget.mapping_reply_tokens, went round in circles. Such a table is read as text, and the verdict stands
    (a rejection, not an error asked again next run)."""
    from . import fill
    limit = (budget or fill.BUDGET).mapping_reply_tokens
    if isinstance(ans, dict) and "error" in ans and re.search(r"window full|cut off|reply limit|ran away", str(ans["error"]), re.I):
        return f"the reply ran into the limit ({str(ans['error'])[:120]})"
    used = ((ans or {}).get("_usage") or {}).get("completion_tokens") if isinstance(ans, dict) else None
    if isinstance(used, (int, float)) and used > limit:
        return f"the reply took {int(used):,} tokens, over the {limit:,} a mapping may take"
    return None


async def _ask_bounded(ask, prompt, sch, catalog, name="ekg_table_map", patient=False):
    """One mapping call, bounded in time as well as in tokens: the reply cannot be capped per call (no call passes
    max_tokens), so a reply still running after Budget.mapping_seconds (the token guard at a slow model's pace) is
    given up as one gone round in circles. On ACME v5 one such call ran 11 minutes after the other 14 were done.
    The clock starts when the call is sent (Responses waits for its own gate first), and the pace is the one this
    run's calls really had (fill.slow_pace): on ACME v8, three runs at once made the provider slower than the fixed
    40 tokens/s, and six sound mappings were given up at 307 seconds. `patient`: the call is the shorter tier asked
    after the fuller one ran past this limit, and may take MAPPING_SECONDS_MAX: a provider slow at the start of a run
    (no call finished yet to measure its pace) gave up 5 of 15 sound mappings on ACME v11 and v12, two runs at once."""
    from . import fill, fill_budget

    def limit():
        if patient:
            return fill_budget.MAPPING_SECONDS_MAX
        b = getattr(catalog, "budget", None) or fill.BUDGET
        pace = fill.slow_pace(fill_budget.SLOW_TOKENS_PER_SECOND)
        return min(max(b.mapping_seconds, int(b.mapping_reply_tokens / max(pace, 1.0))), fill_budget.MAPPING_SECONDS_MAX) \
            if pace < fill_budget.SLOW_TOKENS_PER_SECOND else b.mapping_seconds
    return await ask(prompt, name, sch, timeout=limit)


def _add_usage(total, ans):
    for k, n in ((ans or {}).get("_usage") or {}).items() if isinstance(ans, dict) else ():
        if isinstance(n, (int, float)):
            total[k] = round(total.get(k, 0) + n, 8)


async def _step(ask, make, name, sch, catalog, usage, info):
    """One step, at the fuller tier first; a reply the size or time guard refuses (too_long) is asked once more with
    the shorter tier (a smaller list of candidates to think about) before the step gives up. When every tier came
    back with a whole answer that only took more tokens than the guard allows (it did finish: no error, no cut, in
    time), the shortest of those answers is kept and held to check() like any other: on ACME v9 the steward table's
    step 2 gave the same, sound mapping in 11,950, 13,004 and 15,575 tokens, and only the first was kept. A reply cut
    or gone past the time guard is never kept. Returns (answer, why)."""
    why, whole = None, []
    for tier in TIERS:
        prompt_text = make(tier)
        ans = await _ask_bounded(ask, prompt_text, sch, catalog, name, patient=bool(why and "ran away" in why))
        _add_usage(usage, ans)
        info.setdefault("prompt_chars", {})[name] = len(prompt_text)
        info.setdefault("tier", {})[name] = tier
        if not (why := too_long(ans)):
            return ans, None
        info.setdefault("refused", []).append(f"{name} ({tier}): {why}"[:200])
        if isinstance(ans, dict) and "error" not in ans and why.startswith("the reply took"):
            whole.append((ans["_usage"]["completion_tokens"], tier, ans))
    if len(whole) == len(TIERS):
        tokens, tier, ans = min(whole, key=lambda x: x[0])
        info["tier"][name] = tier
        info.setdefault("kept_over_the_guard", []).append(f"{name} ({tier}): a whole answer in {int(tokens):,} tokens")
        return ans, None
    return ans, why


async def _two_steps(t, ask, catalog, note, usage, info, shape=None):
    """Step 1 (shape, row type, name and key columns, what each column names), then step 2 (each column's field or
    link, from the candidates code found for those types). `shape`: step 1's answer as code settled it (step 1 is
    then not asked). Returns (one mapping for check(), why refused)."""
    from .fill import _error
    n = len(t.write["table"].header)
    if shape is None:
        shape, why = await _step(ask, lambda tier: prompt(t, catalog, tier, note), "ekg_table_shape",
                                 shape_schema(catalog, n), catalog, usage, info)
        if why or _error(shape):
            return shape, why
    if (shape.get("shape") or "rows") == "none" or shape.get("row_type") not in catalog.table_types:
        return shape, None   # not a table of records: check() rejects it, no second step
    if (shape.get("shape") or "rows") == "rows" and all(shape.get(k) in (None, "", "none") for k in
                                                         ("name_column", "key_column", "statement_column")):
        return {**shape, "fields": [], "links": []}, None   # check() rejects it at once (no column names the row's
        # record), and step 1 is asked again with that reason before any step 2 is paid for
    holder = {}

    def make(tier):
        text, holder["cands"] = columns_prompt(t, catalog, shape, tier, note)
        return text
    make("full")
    sch = columns_schema(catalog, n, shape["row_type"], holder["cands"])
    cols, why = await _step(ask, make, "ekg_table_map", sch, catalog, usage, info)
    if why or _error(cols):
        return cols, why
    info["candidate_links"] = len(holder["cands"])
    return merge(shape, cols), None


async def settle(t: Task, ask, catalog):
    """A table's mapping: from the mapping cache when this table (by signature) was mapped before and that mapping
    still passes check(); else asked in two small steps (_two_steps). A mapping a code check rejects (a split that
    does not parse, a composite used as a name, one account given to several rows ...) is asked again once, with the
    reason, before the table is left to be read as text. A step refused by the size or time guard at both tiers is
    a rejection: the table is read as text."""
    from . import fill
    from .fill import _error
    if MAPPINGS is not None and (hit := MAPPINGS.get(t)) is not None:
        e = check(t, hit, catalog, lenient_split=bool(hit.get(LENIENT)))
        if e["status"] == "accepted" or hit.get(VERDICT) == "rejected":
            MAPPINGS.hits += 1
            return {**e, "answer": hit, "mapping_from": f"cache: this table's {hit.get(VERDICT) or 'accepted'} mapping from an earlier run",
                    **({"split_kept_for_the_rows_it_parses": True} if hit.get(LENIENT) and e["status"] == "accepted" else {})}
    usage, info, note, retried, failed = {}, {}, "", None, []
    for attempt in range(MAPPING_RETRIES + 1):
        ans, why = await _two_steps(t, ask, catalog, note, usage, info)
        if why:
            e = {"status": "rejected", "reason": f"check: mapping reply too long: {why}"}
        else:
            e = check(t, ans, catalog)
            if e["status"] == "rejected" and e["reason"].startswith("check:"):
                failed.insert(0, (ans, e["reason"]))   # the latest answer first
        if retried:
            e["retried_after"] = retried
        if why or e["status"] != "rejected" or not e["reason"].startswith("check:") or attempt == MAPPING_RETRIES:
            break
        retried = e["reason"][:200]
        note = (f"\n\nAN EARLIER MAPPING OF THIS TABLE WAS REJECTED ({e['reason'][7:]}). Give a corrected answer.")
    if retried and e["status"] == "rejected":
        # asked again and still rejected (on the same fault, another one, or the guard): the verdict does not hang on
        # the second answer. An answer (the corrected one first, else the first) whose only faults are a split that
        # misses a cell or two ("Jonathan; Ben for infrastructure issues" beside "Mahesh · mahesh@acme.com") or an
        # identity link column that gives one value to several rows (a team code read as everyone's account) is kept
        # without them: the split for the rows it parses (the rest counted, the table read as text too), the rest of
        # the mapping without that column (left unread, so read as text too)
        for got, reason in failed:
            got, kept = _without_shared_identity(got, reason), {}
            again = check(t, got, catalog)
            if again["status"] == "rejected" and "does not parse the sample row cell" in again["reason"]:
                again, kept = check(t, got, catalog, lenient_split=True), {LENIENT: True}
            if again["status"] == "accepted":
                e = {**again, "retried_after": retried, **({"split_kept_for_the_rows_it_parses": True} if kept else {}),
                     **({"identity_column_left_out": reason[7:200]} if "gives one value" in reason else {})}
                ans = {**got, **kept}
                break
    if retried and e["status"] == "rejected" and NO_NAME in e["reason"] and isinstance(ans, dict) \
            and (fixed := _row_from_columns(ans, catalog)) is not None:
        # asked again and step 1 still names no column for its row type (a relation table read as a Handover or a
        # PropertyClaim, whose name no column holds): the rule step 1 was given says a relation's row is the record at
        # one end, one a column names. Code takes the first column step 1 typed as that record, and asks step 2 once
        again, again_why = await _two_steps(t, ask, catalog, "", usage, info, shape=fixed)
        if not again_why:
            got = check(t, again, catalog)
            if got["status"] == "accepted":
                e, ans = {**got, "retried_after": retried,
                          "row_type_from_columns": f"{ans.get('row_type')} -> {fixed['row_type']} named by {fixed['name_column']}"}, again
    if usage:
        e["usage"] = usage
    e["steps"] = info
    if e["status"] == "accepted":
        e["answer"] = ans   # the answer as checked: share_columns reads it, the mapping cache keeps it
        if MAPPINGS is not None:
            MAPPINGS.put(t, ans, getattr(fill, "MODEL_NAME", ""))
    elif MAPPINGS is not None and isinstance(ans, dict) and not _error(ans) and "too long" not in e["reason"]:
        # the model's own verdict (not a table of records) or one the checks gave its answers: kept too, so that a
        # rebuild asks nothing for this table; a reply the guard refused or a failed call is asked again next time
        MAPPINGS.put(t, {**ans, VERDICT: "rejected"}, getattr(fill, "MODEL_NAME", ""))
    return e


VERDICT = "_verdict"   # a kept answer the checks rejected (its table is read as text): "rejected"
NO_NAME = "mapping names no column for the row's name, key or statement"


def _row_from_columns(shape, catalog):
    """Step 1's answer with its row taken as the record the first typed column names (in the table's column order),
    when its own row type has no name, key or statement column; None when no typed column names a type a document
    may create by its name. On ACME's 3-row change table ("Effective | Change | Supporting GitHub record") the only
    typed column is a pull request reference, so that table stays rejected (read as text), as in every build."""
    def order(c):
        return tuple(int(x) for x in re.findall(r"\d+", c))
    typed = sorted(((c, ty) for c, ty in column_types(shape).items() if re.fullmatch(r"c\d+(?:\.\d+)?", c)),
                   key=lambda x: order(x[0]))
    # a type a document may create and that is found by its name (a person, a service): a tool's record (a pull
    # request named by a phrase like "Order-state PR #221") is matched by its key, never by such a name
    pick = next(((c, ty) for c, ty in typed if ty in catalog.creatable and catalog.name_field(ty)), None)
    if pick is None:
        return None
    return {**shape, "shape": "rows", "row_type": pick[1], "name_column": pick[0], "key_column": "none",
            "statement_column": "none", "columns": [x for x in shape.get("columns") or [] if isinstance(x, dict)
                                                    and x.get("column") != pick[0]]}


def _without_shared_identity(ans, reason):
    """The answer without the identity link of the column _shared_identity named in `reason`, else unchanged."""
    m = re.search(r"column (c\d+(?:\.\d+)?) gives one value", reason or "")
    if not m:
        return ans
    identity = {x for x, lk in common._reg.links.items() if lk.relation in ("ALIAS_OF", "HAS_ACCOUNT")}
    keep = [x for x in ans.get("links") or [] if not (isinstance(x, dict) and x.get("column") == m.group(1)
                                                       and x.get("link") in identity)]
    return {**ans, "links": keep}


LENIENT = "_split_kept_for_the_rows_it_parses"   # a kept answer whose split was accepted for the rows it parses


def _used_columns(ans):
    """Every column or column part an answer reads: name, key, statement, fields, link columns, their near ends,
    their own fields and period columns."""
    used = {ans.get(k) for k in ("name_column", "key_column", "statement_column")}
    used |= {f.get("column") for f in ans.get("fields") or [] if isinstance(f, dict)}
    for x in ans.get("links") or []:
        if isinstance(x, dict):
            used |= {x.get("column"), x.get("from_column")} | {f.get("column") for f in x.get("fields") or [] if isinstance(f, dict)}
            used.add((x.get("period") or {}).get("column") if isinstance(x.get("period"), dict) else None)
    return {c for c in used if isinstance(c, str) and c not in ("", "none", "row")}


def share_columns(tasks, entries, catalog):
    """The tables of one document read alike, so that one table's answer does not leave out what another's reads.
    When a table's accepted mapping reads a part of a composite column as a link from the row (the third value of
    "E003 Lionel / eng-orders" as MEMBER_OF a Group), and another accepted table of the same document, with the same
    row type, has a column of the same header that the same split cuts into the same values, while its own mapping
    leaves that part unread ("E018 Charolette / pm-orders"), the link is added to that table's answer and the answer is
    checked again (check): kept only when it still passes. Code only, no call; decided by the document's own
    headers and cells. Each table is read against the others' answers as the model gave them, in task order, so
    the result does not depend on the order the answers came back. entries: {task id: ledger entry}, changed in place."""
    by_doc = collections.defaultdict(list)
    for t in tasks:
        e = entries.get(t.id)
        if t.kind == "table_map" and e and e.get("status") == "accepted" and isinstance(e.get("answer"), dict) \
                and (e.get("mapping") or {}).get("shape") == "rows":
            by_doc[t.write["doc"]].append((t, e))
    for tabs in by_doc.values():
        for t, e in tabs:
            ans, tb = e["answer"], t.write["table"]
            heads = [_norm(h) for h in tb.header]
            cells_of = lambda i: [cells[i] if i < len(cells) else "" for _, cells in tb.rows()]
            used, parts = _used_columns(ans), [dict(p) for p in ans.get("cell_parts") or [] if isinstance(p, dict)]
            links, cols, notes = list(ans.get("links") or []), list(ans.get("columns") or []), []
            for s, se in tabs:
                if s is t or se["mapping"]["row_type"] != e["mapping"]["row_type"]:
                    continue
                sa, sh = se["answer"], s.write["table"].header
                for x in sa.get("links") or []:
                    m = re.fullmatch(r"c([1-9]\d*)\.([1-9]\d*)", str(x.get("column") if isinstance(x, dict) else ""))
                    if not m or (x.get("from_column") or "row") not in ("row", "none") or int(m.group(1)) > len(sh):
                        continue
                    sc, k = int(m.group(1)), int(m.group(2))
                    if not any(l["link"] == x.get("link") and _col_name(l["column"], len(sh), se["mapping"]["parts"]) == x["column"]
                               for l in se["mapping"]["links"]):   # the source mapping really writes it
                        continue
                    # the splits as check() applies them (a pattern it repaired, a separator it read off a pattern)
                    spec_s = next((p for p in se["mapping"].get("parts") or [] if p[0] == sc - 1), None)
                    if spec_s is None or _norm(sh[sc - 1]) not in heads:
                        continue
                    ct = heads.index(_norm(sh[sc - 1])) + 1
                    target = f"c{ct}.{k}"
                    if target in used:
                        continue
                    spec_t = next((p for p in e["mapping"].get("parts") or [] if p[0] == ct - 1), None)
                    cells = [c for c in cells_of(ct - 1) if not placeholder(c)]
                    by_s = [split_cell(c, [ct - 1, *spec_s[1:]]) for c in cells]
                    if not cells or any(v is None for v in by_s):
                        continue   # the other table's split does not cut every cell of this column
                    if spec_t is not None:
                        if spec_t[1] != spec_s[1] or any(split_cell(c, spec_t) != v for c, v in zip(cells, by_s)):
                            continue   # this table cuts the column otherwise
                    elif f"c{ct}" in used:
                        continue   # the whole cell is read as something else
                    else:
                        parts.append({"column": f"c{ct}", "parts": spec_s[1], "separator": spec_s[2], "pattern": spec_s[3]})
                    links.append({**x, "column": target, "from_column": "row"})
                    cols.append({"column": target, "type": x.get("other_type")})
                    used.add(target)
                    notes.append(f"{target} {x.get('link')} {x.get('other_type')}, as \"{s.question}\" reads column "
                                 f"\"{sh[sc - 1]}\""[:240])
            if not notes:
                continue
            got = check(t, {**ans, "cell_parts": parts, "links": links, "columns": cols}, catalog,
                        lenient_split=bool(ans.get(LENIENT)))
            if got["status"] == "accepted":
                e["mapping"] = got["mapping"]
                e["columns_from_other_tables"] = notes
    return None


def _shared_identity(tb, parts, name_col, key_col, links, n):
    """Why an identity link column cannot be one (or None): an account, address or alias is one identity and belongs
    to one record, so a column that gives the same value to the records of several rows (two people both "support")
    names something else (a group, a team). Checked on every row of the table, not only the sample."""
    identity = {x for x, lk in common._reg.links.items() if lk.relation in ("ALIAS_OF", "HAS_ACCOUNT")}
    rows = [expand(cells, parts) for _, cells in tb.rows()]
    get = lambda row, i: row[i].strip() if i is not None and i < len(row) else ""
    for l in links:
        if l["link"] not in identity or l.get("near") is not None:
            continue
        owners = collections.defaultdict(set)
        for row in rows:
            me = name_key(get(row, key_col) or get(row, name_col))
            for val in values(get(row, l["column"]), l["split"]):
                if me and not placeholder(val):
                    owners[name_key(val)].add(me)
        shared = next(((val, who) for val, who in owners.items() if len(who) > 1), None)
        if shared:
            return (f"column {_col_name(l['column'], n, parts)} gives one value (\"{shared[0][:30]}\") to {len(shared[1])} different "
                    f"rows' records: an {l['other']} is one identity and belongs to one record, so this column is not their "
                    f"{l['link']} (a value several records share is a group, a team or a role)")
    return None


PERSON_NOISE = extract_schema.PERSON_NOISE


def _more_than_a_name(cell):
    """Does a cell meant to be a person's name hold something else too: a separator mark (/ · | ( ...), an @login,
    an underscore, or a word mixing letters and digits (an id such as E004)? A name has none of these."""
    return extract_schema.more_than_a_name(cell)


def _col_name(i, n, parts):
    if i < n:
        return f"c{i + 1}"
    k = i - n
    for p in parts:
        if k < p[1]:
            return f"c{p[0] + 1}.{k + 1}"
        k -= p[1]
    return f"c{i + 1}"


def _identifying(catalog, rtype, name_col, key_col, fields, links):
    """How many columns of the mapping identify the row's record (name, key, lookup and id fields, identity links).
    The table that identifies its records best is written first, so the others resolve to what it made."""
    from .extract_schema import LOOKUP_FIELDS
    identity = {n for n, lk in catalog.reg.links.items() if lk.relation in ("ALIAS_OF", "HAS_ACCOUNT")}
    return (name_col is not None) + (key_col is not None) + sum(1 for _, f in fields if f in LOOKUP_FIELDS or f.endswith("_id")) \
        + sum(1 for l in links if l["link"] in identity)


def split_cell(raw, part):
    """One composite cell -> its `parts` values, by the separator or the pattern the mapping states; None when the
    cell does not split that way. An empty cell -> empty values."""
    _, n, sep, pattern = part
    raw = (raw or "").strip()
    if placeholder(raw):   # an empty cell, or one that stands for nothing ("-", "—", "n/a"): no values, not a bad split
        return [""] * n
    if pattern:
        m = re.fullmatch(pattern, raw)
        got = [g.strip() for g in m.groups()] if m and len(m.groups()) == n else None
    else:
        got = [x.strip() for x in raw.split(sep, n - 1)] if sep else None
        got = got if got and len(got) == n else None
    return got if got and all(got) else None


def _pattern_separator(pattern, count):
    r"""The one plain text a pattern writes between each of its groups (" / " in "#(\d+) / (\w+)"), or None when the
    texts between groups differ or hold anything but literal characters."""
    if count < 2:
        return None
    between = re.split(r"\((?:\\.|[^()\\])*\)", pattern)
    if len(between) != count + 1:
        return None
    mids = {re.sub(r"\\(.)", r"\1", b) for b in between[1:-1]}
    if len(mids) != 1:
        return None
    lit = mids.pop()
    return lit if lit.strip() and not re.search(r"[\\.*+?\[\]{}|^$]", lit) else None


def _cell_parts(t, specs, n, unsplit=None):
    """The mapping's composite columns, checked on every sample row the model was shown. Returns
    (parts [[column, count, separator, pattern]], {"c2.1": index of that value in an expanded row}, why rejected).
    With `unsplit` (a list), a split that parses some sample cells but not all is kept, and the cells it does not
    parse are listed there instead (apply counts them; the table is read as text too)."""
    tb = t.write["table"]
    sample = [fill_docs.cells(line, tb.sep, tb.outer) for line in t.sources[0].text.split("\n")[1:] if line.strip()]
    parts, virtual = [], {}
    for s in specs:
        c = s.get("column") if isinstance(s, dict) else None
        i = int(c[1:]) - 1 if isinstance(c, str) and re.fullmatch(r"c[1-9]\d*", c) and int(c[1:]) <= n else None
        count, sep, pattern = s.get("parts"), s.get("separator") or "", s.get("pattern") or ""
        if i is None or count not in range(1, MAX_PARTS + 1) or not (sep or pattern) or any(p[0] == i for p in parts):
            return [], {}, f"cell_parts for {c} names no column, no separator or pattern, or the column twice"
        if pattern:
            try:
                if re.compile(pattern).groups != count:
                    return [], {}, f"the pattern for {c} has not {count} groups"
            except re.error as ex:
                return [], {}, f"the pattern for {c} is not a regular expression ({ex})"
        part = [i, count, sep, pattern]
        bad = next((row[i] for row in sample if i < len(row) and split_cell(row[i], part) is None), None)
        if bad is not None and pattern and "\\\\" in pattern:
            # a pattern escaped twice ("(\\w+)\\s+..." matches a backslash, never a cell): the JSON reply quoted it
            # once too often. Read once unescaped, it is kept only if it then splits every sample row
            try:
                once = pattern.replace("\\\\", "\\")
                alt = [i, count, "", once] if re.compile(once).groups == count else None
            except re.error:
                alt = None
            if alt and all(split_cell(row[i], alt) is not None for row in sample if i < len(row)):
                part, bad = alt, None
        if bad is not None and pattern and (lit := _pattern_separator(pattern, count)):
            # the pattern's own text between its groups, used as the separator: "#(\d+) / (\w+)" fails on
            # "#174 / PAY-AUTH" only because \w has no dash; " / " splits every sample row
            alt = [i, count, lit, ""]
            if all(split_cell(row[i], alt) is not None for row in sample if i < len(row)):
                part, bad = alt, None
        if bad is not None:
            fits = [row[i] for row in sample if i < len(row) and split_cell(row[i], part) is not None and not placeholder(row[i])]
            if unsplit is None or not fits:
                return [], {}, f"the split of {c} does not parse the sample row cell \"{bad[:60]}\""
            unsplit += [f"{c}: {row[i][:60]}" for row in sample if i < len(row) and split_cell(row[i], part) is None]
        for k in range(count):
            virtual[f"{c}.{k + 1}"] = n + len(virtual)
        parts.append(part)
    return parts, virtual, None


def expand(cells, parts, counter=None):
    """A row's cells with its composite cells' values appended, in the order the mapping numbered them."""
    out = list(cells)
    for part in parts:
        got = split_cell(cells[part[0]] if part[0] < len(cells) else "", part)
        if got is None:
            if counter is not None:
                counter[f"cell skipped: c{part[0] + 1} does not split as the mapping states"] += 1
            got = [""] * part[1]
        out += got
    return out


def values(raw, split):
    """The values one cell lists: cut by the mapping's separator, each stripped; empty ones left out."""
    sep = SPLITS.get(split)
    parts = raw.split() if sep == " " else raw.split(sep) if sep else [raw]
    return [p.strip() for p in parts if p.strip()]


def apply(w, t: Task, e):
    """Apply an accepted mapping to every row of the table, in code."""
    cat, v, reg, c = w.v.catalog(), w.v, common._reg, w.facts
    m, tb, doc = e["mapping"], t.write["table"], w.by_id[t.subject]
    tool, quality, rtype = doc["props"]["source_tool"], t.write.get("quality", 1.0), m["row_type"]
    reg_fields, nf, tf = reg.record(rtype).fields, cat.name_field(rtype), cat.text_field(rtype)
    v._index()
    fam = {ty: {ty} | extract_facts.family(reg, ty, v._identity) for ty in {rtype, *(l["other"] for l in m["links"])}}
    # a date written without its year ("Before Oct 03") is in the year of the table's own full dates, else of the
    # document's (the latest year either writes), else of the document's date or title; without one it is no date
    whole = next((d for d in v.docs()[0] if d.rec["id"] == doc["id"]), None)
    year = (year_of([u.text for u in tb.units]) or year_of([u.text for u in whole.units] if whole else [])
            or year_of([doc["props"].get(f) for f in ("doc_date", "title", "name")]))

    def cell(cells, i):
        return cells[i].strip() if i is not None and i < len(cells) else ""

    def obtain(ty, key, name, props, unit, what, key_field=None, create=False, same_kind=False, never_create=False):
        """The one record a cell names: found in the graph (by its key field when the mapping gives one, so a tool's
        own record is found by its number or sha), made earlier in this table, or created; never created when
        `never_create` (a bare first name: THIN is returned instead). (rec, why).
        A record with a path in its repository (a File) is found and made by its path read with its leading "/" as
        the root (repo_path): "/src/a.py" in one table and "src/a.py" in another are one file, one record."""
        if extract_schema.has_path(reg, ty):
            nfield = cat.name_field(ty)
            key, name = extract_schema.repo_path(key), extract_schema.repo_path(name)
            props = {f: extract_schema.repo_path(x) if f in (extract_schema.PATH_FIELD, nfield) else x for f, x in props.items()}
        found = v.by_field(ty, key_field, props.get(key_field, key)) if key_field else []
        if not found:
            found = None
            for f, val in strong(ty, props):   # an id, an email, a login on the row, before its name
                hits = v.by_field(ty, f, val) if f.endswith("_id") else v.resolve(str(val), {ty}, tool)
                if hits:
                    found = hits
                    break
            if found is None:
                found = v.resolve(key, {ty}, tool) or (v.resolve(name, {ty}, tool) if name and name != key else [])
            if not found and (name or key):   # "payments-platform" -> the one "acme-commerce/payments-platform"
                found = v.by_name_tail(name or key, {ty})
            if not found and name and cat.name_field(ty) == PERSONAL_NAME:   # "Lionel" -> "Lionel eng-orders" (team eng-orders)
                found = v.by_name_prefix(name, {ty})
            if not found and not extract_facts.is_name(ty) and not same_kind:   # known as the other kind of record (a person's account)
                found = v.resolve(key, fam.get(ty, {ty}) - {ty}, tool)
        if len(found) > 1:
            return None, f"{what}: {len(found)} records match, not linked"
        if found:
            rec = found[0]
            if rec["type"] == ty and props:   # an existing record gains only the fields it lacks
                gaps = {k: val for k, val in props.items() if k not in rec["props"]}
                if gaps:
                    before = json.dumps(rec["props"])
                    rec["props"].update(gaps)
                    if common._vrec(rec).ok:
                        if rec["props"]["_key"] in w.old_records:
                            w.patched.add(rec["props"]["_key"])
                    else:
                        rec["props"] = json.loads(before)
            return rec, None
        if never_create:
            return None, THIN
        if ty not in cat.creatable and not (create and ty in cat.table_types):
            return None, f"{what}: no such {ty} in the graph (not a type created from a document)"
        from .fill import DEFAULT_STATUS
        src = w.by_id[unit.id]
        native = f"table:{key_field}={props[key_field]}" if key_field else "table:" + name_key(key)
        if ty in DEFAULT_STATUS and not props.get("status"):
            props = {**props, "status": DEFAULT_STATUS[ty]}
        rec, why = w.new_record(ty, tool, native, props, src)
        if why:
            return None, f"{what}: {why}"
        if ty in cat.stated:   # a stated record's origin (R41): the row's piece, or else its document
            origin = cat.origin_for(ty, [src["type"], doc["type"]])
            why = f"schema: no origin link from a {ty} to a {doc['type']}" if not origin else \
                w._link(origin[0], rec, src if origin[1] == src["type"] else doc, [src["id"]], t.kind, "table", quality,
                        method="template_rule")
            if why:
                w.drop_record(rec)
                return None, f"{what}: {ty} not created, {why[:100]}"
        for f in [key_field] + [f for f in rec["props"] if f.endswith("_id")]:
            if f:
                v.index_field(rec, f)
        c[f"{ty}: created from a table row" + ("" if ty in cat.creatable else " (no such record from its tool yet)")] += 1
        return rec, None

    def strong(ty, props):
        """The row's identifying values other than its name: id fields and lookup fields (an email, a handle)
        whose value is an id, an address or a login (a digit, @, . or _ in it)."""
        nfield = cat.name_field(ty)
        return [(f, val) for f, val in props.items() if f != nfield and isinstance(val, (str, int))
                and (f.endswith("_id") or (f in LOOKUP_FIELDS and extract_facts._IDENTIFYING.search(str(val))))]

    def mention(unit, rec):
        src = w.by_id[unit.id]
        if src["type"] == "Piece" and src["id"] != rec["id"]:
            w._link("MENTIONS", src, rec, [src["id"]], t.kind, "table", quality, method="template_rule", kind="name")

    def put(props, f, raw):
        got = coerce(reg_fields[f], raw, year)
        if got is None:
            if raw:
                c[f"cell skipped: {f} value does not fit its type"] += 1
            return
        props[f] = got[0]
        if got[1]:
            props[f"{f}_precision"] = got[1]

    def ends(l, raw, unit, what, thin=None):
        """The records one link cell names (every value of it, cut by the mapping's split): found by the other
        type's key field when the mapping gives one (a pull request's number, a file's path), else by name, else
        created like a row's record. A cell names the other record; whose record it is (an account, an address)
        never turns into its person (same_kind for identity links). A bare first name ("Noah") is never created: it
        goes to `thin`, for resolve_thin once every table's links are written."""
        out = []
        ty, key_field = l["other"], l.get("other_key")
        onf = cat.name_field(ty)
        # the other type's one number key (a pull request's number), for a column the mapping gave no key field: a
        # cell written in that key's form ("#246", "246") names the existing record of that number. It only finds:
        # with no such record the cell is read as before (by name), and a person's name column is never read so
        numbered = [f for f, s in cat.key_fields(ty).items() if s.type == "number" and f != onf] \
            if not key_field and onf != PERSONAL_NAME else []
        for val in values(raw, l["split"]):
            if placeholder(val):
                c["placeholder cell (-, n/a ...): no link end, left out"] += 1
                continue
            num = _NUMBER_KEY.fullmatch(val.strip()) if len(numbered) == 1 else None
            hits = v.by_field(ty, numbered[0], int(num.group(1))) if num else []
            if len(hits) == 1:
                c[f"{ty}: a link cell written as its {numbered[0]}, the existing record of that {numbered[0]}"] += 1
                out.append(hits[0])
                continue
            props = {onf: val.lstrip("@")} if onf else {}
            if key_field:
                got = coerce(reg.record(ty).fields[key_field], val.lstrip("@")) if key_field in reg.record(ty).fields else None
                if got is None:
                    c[f"{what}: the cell is not a {ty} {key_field}"] += 1
                    continue
                props = {key_field: got[0]} if key_field != onf else {onf: got[0]}
            if not props:
                c[f"{what}: {ty} has no field to name it by"] += 1
                continue
            bare = onf == PERSONAL_NAME and not key_field and extract_facts.thin(cat, ty, {"name": val.lstrip("@"), "fields": []})
            # a cell value that holds more than a name ("Jonathan; Ben for infrastructure issues"): found if it is one,
            # never made a person of its own (the column check rejects a column of such cells; this is one cell)
            noisy = onf == PERSONAL_NAME and not key_field and _more_than_a_name(val.lstrip("@"))
            # a role or another record's name in a person column ("Product Manager"): found if it is one, never made
            role = onf == PERSONAL_NAME and not key_field and not bare and not noisy \
                and extract_facts.not_a_personal_name(v, cat, ty, val.lstrip("@"))
            other, why = obtain(ty, val, val, props, unit, what, key_field=key_field, create=True,
                                same_kind=l["link"] in v._identity, never_create=bare or noisy or bool(role))
            if why == THIN and noisy and not bare:
                c[f"{ty}: a cell that holds more than a name (\"{val[:30]}\"), not created and not linked"] += 1
                continue
            if why == THIN and role:
                c[f"{ty}: not created from a table cell, {role}"] += 1
                continue
            if why == THIN:
                if thin is not None:
                    thin.append(val.lstrip("@"))
                else:
                    c[f"{ty}: a bare first name, not created and not linked"] += 1
                continue
            if why:
                c[why] += 1
                continue
            out.append(other)
        return out

    def own_values(l, own_cells):
        own = {}
        lspec = reg.link(l["link"]).fields
        for f, raw_own in own_cells:
            got = coerce(lspec[f], raw_own, year)
            if got is None:
                if raw_own:
                    c[f"cell skipped: link field {l['link']}.{f} value does not fit its type"] += 1
                continue
            own[f] = got[0]
        return own

    def link(l, rec, raw, unit, own_cells, others=None):
        """Every value of one link cell, linked to `rec` (the row's record, or the record another cell of the row
        names), with the link's own fields from `own_cells`."""
        own = {**own_values(l, own_cells), **(l.get("fixed") or {})}
        ltype = l["link"]
        for other in (ends(l, raw, unit, f"{ltype} skipped") if others is None else others):
            mention(unit, other)   # the piece names it: a record result can point to the row that says so
            a, b = (rec, other) if l["row_from"] else (other, rec)
            if not reg.link_allows(ltype, a["type"], b["type"]):
                lk = reg.link(ltype)
                if not any(reg.is_a(a["type"], x) for x in lk.from_types):
                    a = extract_facts.other_identity(w, a, lambda ty: any(reg.is_a(ty, x) for x in lk.from_types), tool)
                if a is not None and not any(reg.is_a(b["type"], x) for x in lk.to_types):
                    b = extract_facts.other_identity(w, b, lambda ty: any(reg.is_a(ty, x) for x in lk.to_types), tool)
                if a is None or b is None:
                    c[f"{ltype} skipped: the schema needs another kind of record at one end, and there is not exactly one"] += 1
                    continue
            why = w._link(ltype, a, b, [unit.id], t.kind, "table", quality, method="template_rule", **own)
            if why and own and why.startswith("schema"):   # the link stands without the values the schema refused
                why = w._link(ltype, a, b, [unit.id], t.kind, "table", quality, method="template_rule")
                c[f"{ltype}: link field values the schema refused, left out"] += 1
            c[f"{ltype}: {'written from a table' if why is None else 'skipped: ' + why[:100]}"] += 1

    if m.get("shape") == "key_value":
        return _apply_key_value(w, t, m, tb, cat, c, obtain, mention, put, link, nf)

    # every non-empty cell of a column the mapping does not read is counted, never dropped silently
    parts, key_field = m.get("parts") or [], m.get("key_field")
    heads = list(tb.header) + [f"{tb.header[p[0]]} (part {k + 1})" for p in parts for k in range(p[1])]
    unread = unread_columns(m, len(tb.header))
    # the other fields a record is named or looked up by (a preferred name, a display name, a handle): the row's name
    # column already gives its name, and the same cell copied into one of these would state a second name the table
    # does not give. A field of another kind from that cell (a file's path, which its name is) is still written
    # (the fields the name index reads: fill_tasks.View.add_name)
    also_names = {f for f in (*extract_schema.names_of(reg.record(rtype)), *LOOKUP_FIELDS) if f != nf}
    # pass 1: every row's own record, so that a later column can name a row further down (a manager)
    rows = []
    for unit, cells in tb.rows():
        c["table rows read"] += 1
        cells = expand(cells, parts, c)
        for i in unread:
            if cell(cells, i):
                c[f"no field for this column: {heads[i]}"] += 1
        name = cell(cells, m["name"])
        if name and placeholder(name):
            c["placeholder cell (-, n/a ...): not a name, left out"] += 1
            name = ""
        key = cell(cells, m["key"])
        key = "" if placeholder(key) else key
        key = key or name
        props = {}
        if nf and name:
            props[nf] = name
        if key_field and cell(cells, m["key"]):
            put(props, key_field, cell(cells, m["key"]))
            if key_field not in props:
                c["row skipped: its key does not fit the key field's type"] += 1
                continue
        for i, f in m["fields"]:
            if i == m["name"] and nf and f in also_names:   # one cell, one value: the name is not a second name too
                if cell(cells, i):
                    c[f"cell skipped: {f} is the row's name column (its name), not a name of its own"] += 1
                continue
            put(props, f, cell(cells, i))
        if not key:
            if not (tf and props.get(tf)):
                c["row skipped: no value in the name or key column"] += 1
                continue
            key = hashlib.sha1(f"{unit.id}|{props[tf]}".encode()).hexdigest()[:16]
        if nf and not name and not key_field and not (tf and props.get(tf)) and rtype in cat.creatable \
                and not v.resolve(key, fam[rtype], tool):
            c["row skipped: no value in the name column"] += 1
            continue
        bare = nf == PERSONAL_NAME and not key_field and name and key == name \
            and extract_facts.thin(cat, rtype, {"name": name, "fields": list(props.items())})
        if bare:   # a login or address the row gives for its own record (an identity link cell) identifies it
            ids = [x for l in m["links"] if l["link"] in v._identity and l.get("near") is None
                   for x in values(cell(cells, l["column"]), l["split"]) if not placeholder(x)]
            hits = {r["id"]: r for x in ids for r in v.resolve(x, {rtype}, tool)}
            if len(hits) == 1:   # the record that login already belongs to
                key = next(iter(hits.values()))["props"].get(nf) or key
            bare = not ids
        rec, why = obtain(rtype, key, name, props, unit, "row skipped", key_field if key_field in props else None, create=True,
                          never_create=bare)
        if why == THIN:
            c["row skipped: a bare first name (no full name, email, login or id): not created"] += 1
            continue
        if why:
            c[why] += 1
            continue
        c["table rows written"] += 1
        mention(unit, rec)
        rows.append((unit, cells, rec))
    # pass 2: the link columns, each with the values of its own fields. A link may start at the record another
    # cell of the row names (near): that cell is resolved once per row, as its own link column resolves it
    def links_pass():
        for unit, cells, rec in rows:
            named = {}   # (column, type, key field) -> the records that cell names, resolved once per row

            def cell_ends(i, ty, key_field, split, ltype):
                k = (i, ty, key_field)
                if k not in named:
                    raw, thin = cell(cells, i), []
                    named[k] = (ends({"other": ty, "other_key": key_field, "split": split, "link": ltype}, raw, unit,
                                     f"{ltype} skipped", thin) if raw else [], thin)
                return named[k]
            for l in m["links"]:
                if not cell(cells, l["column"]):
                    continue
                others, thin = cell_ends(l["column"], l["other"], l.get("other_key"), l["split"], l["link"])
                own = [(f, cell(cells, i)) for i, f in l.get("fields", [])]
                if l.get("period"):   # the link's interval, from the period column (extract_schema.period)
                    pr = l["period"]
                    for f, day in zip(PERIOD_FIELDS, period(cell(cells, pr["column"]), pr["from_words"], pr["to_words"], year, c)):
                        if day:
                            own.append((f, day))
                if l.get("near") is None:
                    nears = [rec]
                else:
                    spec = next((x for x in m["links"] if x["column"] == l["near"]), None)
                    nears, near_thin = cell_ends(l["near"], l["near_type"], spec.get("other_key") if spec else None,
                                                 spec["split"] if spec else "none", spec["link"] if spec else l["link"])
                    if not nears:
                        c[f"{l['link']} skipped: the cell its near end is in names no record"] += 1
                for near in nears:
                    link(l, near, None, unit, own, others)
                    for val in thin:   # settled once every table's links are in (resolve_thin)
                        w.thin_pending.append((l, near, val, unit, own, link))
        return None
    if getattr(w, "defer_table_links", False):   # fill.run: every table's rows first, then every table's links
        w.table_links.append(links_pass)
        return None
    links_pass()
    resolve_thin(w)
    return None


_NUMBER_KEY = re.compile(r"#?(\d+)")   # a cell written as a number key: "#246", "246"
THIN = "a bare first name"   # obtain()'s answer for a name it may not create a record from


def resolve_thin(w):
    """The bare first names table link cells gave ("Noah 26" in a module's row), after every table's links are
    written. Within one tenant, a single word that is the first word of exactly ONE record's personal name, and no
    other record's name, login or alias of that identity, is that record when the link's other end (the row's
    record, or the near end) is already linked to it or to its accounts: the table corroborates what the graph
    holds. Any other bare name is left unlinked and counted; it never becomes a record of its own."""
    v, c, reg = w.v, w.facts, common._reg
    pending, w.thin_pending = getattr(w, "thin_pending", []), []
    for l, near, val, unit, own, link in pending:
        ty = l["other"]
        fam = {ty} | extract_facts.family(reg, ty, v._identity)
        cands = v.by_first_name(val, {ty})
        taken = [i for i in v._index().get(name_key(val), ()) if v.rec[i]["type"] in fam]
        if len(cands) != 1 or taken:
            c[f"{ty}: a bare first name that {'several records' if len(cands) > 1 or taken else 'no record'} could be, "
              "not linked (not created)"] += 1
            continue
        if not v.linked(near, cands[0]):
            c[f"{ty}: a bare first name, one record could be it but nothing links it to the row's record yet: not linked "
              "(not created)"] += 1
            continue
        c[f"{ty}: a bare first name, the one record whose name starts with it, already linked to the row's record"] += 1
        link(l, near, None, unit, own, [cands[0]])


def _apply_key_value(w, t, m, tb, cat, c, obtain, mention, put, link, nf):
    """One record for the whole table: each mapped row label is one of its fields or links."""
    rtype, unit = m["row_type"], tb.units[0]
    fields, links = dict(m["fields"]), {}
    for l in m["links"]:
        links.setdefault(l["label"], []).append(l)
    pairs = [(tb.units[0], tb.header, True)] + [(u, cells, False) for u, cells in tb.rows()]
    props = {nf: m["entity"]} if nf else {}
    todo = []
    for u, cells, is_header in pairs:
        if not is_header:
            c["table rows read"] += 1
        lab, raw = _norm(cells[0]) if cells else "", (cells[1].strip() if len(cells) > 1 else "")
        for i in range(2, len(cells)):
            if cells[i].strip() and not is_header:
                c[f"no field for this column: {tb.header[i]}"] += 1
        if lab in m.get("name_labels", ()):
            continue
        if lab in fields:
            put(props, fields[lab], raw)
        elif lab in links:
            todo.append((u, raw, links[lab]))
        elif raw and not is_header:   # a header row the mapping does not read is only the table's header
            c[f"no field for this row: {cells[0].strip()}"] += 1
    rec, why = obtain(rtype, m["entity"], m["entity"], props, unit, "key/value table skipped")
    if why:
        c[why] += 1
        return None
    c["key/value table: one record written"] += 1
    mention(unit, rec)
    for u, raw, ls in todo:
        for l in ls:
            if raw:
                link(l, rec, raw, u, [])
    return None
