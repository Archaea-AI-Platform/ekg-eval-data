"""Tables: one model call per table for a COLUMN MAPPING, then code writes every row.

A table (a spreadsheet sheet, a CSV, a table in a PDF, a Word file or a wiki page) arrives as Pieces of
kind "table": its heading line(s), then `cell | cell | cell` lines, header row first; a long table comes in
several pieces that each repeat the header (fill_docs.tables puts them back together). The model never sees
the table row by row. It sees the heading, the header, a sample of rows and the schema (from the registry), and
says which of three shapes the table has:

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

from . import common, extract_facts, fill_docs
from .extract_schema import LOOKUP_FIELDS, PERSONAL_NAME, coerce, placeholder
from .fill_tasks import Item, Task, name_key

SAMPLE_HEAD, SAMPLE_REST = 15, 5   # rows the model is shown: the first ones and a few spread over the rest
SPLITS = {"none": None, ",": ",", ";": ";", "/": "/", "space": " "}
SHAPES = ["rows", "key_value", "none"]
MAX_PARTS = 3   # values one composite cell may hold ("Lionel 42", "#174 / pay-auth")
MAPPING_RETRIES = 1   # a mapping a code check rejects is asked again this many times, with the reason
ASK = ("This is one table: its heading, its header and a sample of its {rows:,} rows (all of them when it is short). "
       "Say how to read its COLUMNS, so that code can read every row the same way. Your reply describes columns: it "
       "never lists rows or copies cell values, so it is short whatever the table's length.\n"
       "{catalog}\n\n"
       "RULES\n"
       "- shape: \"rows\" when each row is one record of a listed type or one relation between records (an event, an "
       "assignment, a contact per area, a count per person and module): some column names records of a listed type. "
       "\"key_value\" when the whole table describes ONE record and each ROW is one of its fields or links: a label in "
       "the first column, its value in the second (Owner | ..., Status | ...). \"none\" only when no column names records "
       "of a listed type (figures, a layout table, terms and their meanings): the table is then read as ordinary text. "
       "Decide by the descriptions of the types above, not by how the table looks.\n"
       "- row_type: for \"rows\", the record type each row is about (for a relation, the record at one end: the person "
       "of \"module | person | commits\"). A table that is an export of another tool (a commit log, a merge log) has rows "
       "of that tool's type (listed under RECORD TYPES OF OTHER TOOLS). For \"key_value\", the type of the one record. "
       "\"none\" for shape none.\n"
       "SHAPE rows:\n"
       "- cell_parts: a column whose every cell holds two or three values (\"Lionel 42\" = a person and a count, \"#174 / "
       "pay-auth\" = a pull request and a module): the column, parts (how many values), and either the separator "
       "between them or a regular expression (pattern) with one group per value; the other one empty. Its values are "
       "then columns of their own, named c2.1, c2.2 ..., to use anywhere below. Code splits every row so, and rejects "
       "the mapping if a sample row does not split.\n"
       "- A cell that writes a person's name together with anything else (\"Suzan / @suzan\", \"E004 Suzan eng-payments\", "
       "\"Kim - kim@acme.com\", \"Suzan - PAY-AUTH\") is a composite: split it with cell_parts, use the name part as the "
       "name, and map each other part to its own field (an email, an id), key or link (a login: the person's Account; "
       "a module: a Service). Code rejects a person's name column whose sample cells all hold more than a name.\n"
       "- name_column: the column that names the row's record, by the field its type is \"Named by\". \"none\" if the type has none.\n"
       "- key_column: a column that identifies the row better than its name (an email address, an id, a number, a sha), "
       "else \"none\". key_field: when that column is the record's own key, the field of row_type it holds (a pull "
       "request's number, a commit's sha); \"none\" when it is only a value to look the record up by (an email).\n"
       "- statement_column: for a type \"stated in\" a field (a decision, a piece of knowledge, a requirement ...), the "
       "column holding that statement, else \"none\".\n"
       "- fields: a column that holds one of row_type's listed fields: the column and that field's name.\n"
       "- links: one entry for every column (or column part) whose cells name ANOTHER record: the column, the link type, "
       "the other record's type, and row_is: \"from\" when the link goes near end -> other, \"to\" when it goes other -> "
       "near end. The near end is the row's own record (from_column \"row\"), or the record another column of the same "
       "row names (from_column: that column, which has a links entry of its own): a row can state several links, also "
       "between two of its other columns' records (a commit's pull request and the file it changed, a module's "
       "repository and its file). Use the link type whose description says what the table states, in the direction "
       "the description gives. other_key_field: when the cells hold the other record's key rather than its name (a "
       "pull request's number, a commit's sha, a file's path), that field of other_type, else \"none\". split: the "
       "separator when one cell lists several (\",\", \";\", \"/\", \"space\"), else \"none\". fields: the columns that "
       "hold a value OF THAT LINK (a date, a count, a role), each with the link's field name as listed for the link type. "
       "Every column part that names a record gets a links entry, so that nothing a row says is left unlinked.\n"
       "- schema_gap: when a column states something that no listed link or field can hold (for example a count per "
       "person and module when no fitting link has a count field), one sentence saying what, in which columns, and "
       "which link is missing; else \"\". Code reports it, and reads such a table as text as well.\n"
       "SHAPE key_value:\n"
       "- entity_name: the one record's name, copied from the table, its heading or the document title.\n"
       "- label_fields: a row label (the row's first cell, as written) whose value is a field of that record, and the field.\n"
       "- label_links: a row label whose value names another record linked to it: the label, the link type, the other "
       "record's type, row_is (\"from\" when the link goes the record -> other), split.\n"
       "- For the shape you do not use, give empty lists, \"none\" and an empty entity_name.\n"
       "- Map a column or a label only when the table makes its meaning plain. Leave the others out (code counts them, "
       "and a table with columns no mapping reads is read as text too).\n"
       "- Each list has at most one entry per column, part or label; reason is one sentence.\n"
       'Reply with one JSON object: {{"shape", "row_type", "cell_parts": [{{"column", "parts", "separator", "pattern"}}], '
       '"name_column", "key_column", "key_field", "statement_column", "fields": [{{"column", "field"}}], '
       '"links": [{{"column", "link", "other_type", "row_is", "from_column", "other_key_field", "split", '
       '"fields": [{{"column", "field"}}]}}], "schema_gap", '
       '"entity_name", "label_fields": [{{"label", "field"}}], '
       '"label_links": [{{"label", "link", "other_type", "row_is", "split"}}], "reason"}}. Columns are named c1, c2 ...')


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
                               "title": doc.title, "units": [u.id for u in tb.units]},
                              context=ctx, extra=digest))
            v.claimed.update(u.id for u in tb.units)
    return tasks, skipped


def catalog_text(catalog, budget=None):
    """The table prompt's schema section with as many of the registry's descriptions as the budget allows
    (Catalog.schema_text); returns (text, info)."""
    from . import fill
    return catalog.schema_text("tables", (budget or getattr(catalog, "budget", None) or fill.BUDGET).schema_chars)


def overhead(catalog, budget=None) -> int:
    """Characters of a table_map prompt that are not the table: header, schema, rules and reply format."""
    from .fill import HEADER
    return len(HEADER) + len(ASK.format(rows=0, catalog=catalog_text(catalog, budget)[0])) + 600


def prompt(t: Task, catalog):
    from .fill import HEADER, _text_block
    return "\n".join([HEADER, ""] + _text_block(t) + ["", ASK.format(rows=t.write["rows"], catalog=catalog_text(catalog)[0])])


def schema(catalog, n_columns):
    base = [f"c{i + 1}" for i in range(n_columns)]
    cols = base + [f"{c}.{k}" for c in base for k in range(1, MAX_PARTS + 1)]   # a column's parts (cell_parts)
    types = catalog.table_types
    pair = lambda key: {"type": "object", "additionalProperties": False, "required": [key, "field"],
                        "properties": {key: {"type": "string", "enum": cols} if key == "column" else {"type": "string"},
                                       "field": {"type": "string"}}}

    def link(key, with_fields):
        props = {key: {"type": "string", "enum": cols} if key == "column" else {"type": "string"},
                 "link": {"type": "string", "enum": list(catalog.table_links)},
                 "other_type": {"type": "string", "enum": types}, "row_is": {"type": "string", "enum": ["from", "to"]},
                 "split": {"type": "string", "enum": list(SPLITS)}}
        if with_fields:
            props["from_column"] = {"type": "string", "enum": ["row"] + cols}
            props["other_key_field"] = {"type": "string"}
            props["fields"] = {"type": "array", "items": pair("column")}
        return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}

    part = {"type": "object", "additionalProperties": False, "required": ["column", "parts", "separator", "pattern"],
            "properties": {"column": {"type": "string", "enum": base}, "parts": {"type": "integer", "enum": list(range(1, MAX_PARTS + 1))},
                           "separator": {"type": "string"}, "pattern": {"type": "string"}}}
    props = {"shape": {"type": "string", "enum": SHAPES},
             "row_type": {"type": "string", "enum": types + ["none"]},
             "cell_parts": {"type": "array", "items": part},
             "name_column": {"type": "string", "enum": cols + ["none"]},
             "key_column": {"type": "string", "enum": cols + ["none"]},
             "key_field": {"type": "string"},
             "statement_column": {"type": "string", "enum": cols + ["none"]},
             "fields": {"type": "array", "items": pair("column")},
             "links": {"type": "array", "items": link("column", True)},
             "schema_gap": {"type": "string"},
             "entity_name": {"type": "string"},
             "label_fields": {"type": "array", "items": pair("label")},
             "label_links": {"type": "array", "items": link("label", False)},
             "reason": {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


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


def check(t: Task, ans, catalog):
    """The model's mapping, held to the registry: what does not fit is dropped and counted; a mapping with no
    usable row type or nothing to write is rejected (and its table is then read as text)."""
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
    parts, virtual, why = _cell_parts(t, ans.get("cell_parts") or [], n)
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
        links.append({"column": c, "link": ltype, "other": other, "row_from": fwd, "fields": own, "near": near_col,
                      "near_type": near_type, "other_key": other_key,
                      "split": x.get("split") if x.get("split") in SPLITS else "none"})
    if name_col is None and key_col is None and not (tf and tf in used):
        return {"status": "rejected", "reason": "check: mapping names no column for the row's name, key or statement", "dropped": dict(dropped)}
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
    m = {"shape": "rows", "row_type": rtype, "name": name_col, "key": key_col, "key_field": key_field, "parts": parts,
         "fields": fields, "links": links, "identifying": _identifying(catalog, rtype, name_col, key_col, fields, links)}
    unread = [_col_name(i, n, parts) for i in unread_columns(m, n) if any(i < len(row) and row[i] for row in sample)]
    gap = (ans.get("schema_gap") or "").strip()
    if unread or gap:   # what the mapping does not read is read as text as well, so that nothing a cell says is lost
        m["also_text"] = {"unread_columns": unread, **({"schema_gap": gap[:300]} if gap else {})}
    return {"status": "accepted", "mapping": m, "reason": (ans.get("reason") or "")[:300], "dropped": dict(dropped),
            **({"schema_gap": gap[:300]} if gap else {})}


def unread_columns(m, n):
    """The columns (and column parts) of a rows mapping that nothing reads: no name, key, field or link, and no
    link field. A split column is read by its parts."""
    parts = m.get("parts") or []
    width = n + sum(p[1] for p in parts)
    read = {m["name"], m["key"], *(i for i, _ in m["fields"]), *(l["column"] for l in m["links"]),
            *(i for l in m["links"] for i, _ in l.get("fields", []))}
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


async def _ask_bounded(ask, prompt, sch, catalog):
    """One mapping call, bounded in time as well as in tokens: the reply cannot be capped per call (no call passes
    max_tokens), so a reply still running after Budget.mapping_seconds (the token guard at a slow model's pace) is
    given up as one gone round in circles. On ACME v5 one such call ran 11 minutes after the other 14 were done."""
    from . import fill
    budget = getattr(catalog, "budget", None) or fill.BUDGET
    try:
        return await asyncio.wait_for(ask(prompt, "ekg_table_map", sch), timeout=budget.mapping_seconds)
    except asyncio.TimeoutError:
        return {"error": f"reply ran away: no answer after {budget.mapping_seconds:,} seconds"}


async def settle(t: Task, ask, catalog):
    """Ask for the mapping and check it. A mapping a code check rejects (a split that does not parse, a composite
    used as a name ...) is asked again once, with the reason, before the table is left to be read as text. A reply
    too long for a mapping (too_long) is rejected at once: the table is read as text."""
    sch = schema(catalog, len(t.write["table"].header))
    first = prompt(t, catalog)
    ans = await _ask_bounded(ask, first, sch, catalog)
    if (why := too_long(ans)):
        return {"status": "rejected", "reason": f"check: mapping reply too long: {why}", "usage": (ans or {}).get("_usage")}
    e = check(t, ans, catalog)
    for _ in range(MAPPING_RETRIES):
        if e["status"] != "rejected" or not e["reason"].startswith("check:"):
            break
        ans = await _ask_bounded(ask, f"{first}\n\nAN EARLIER MAPPING OF THIS TABLE WAS REJECTED ({e['reason'][7:]}). "
                                 "Give a corrected mapping.", sch, catalog)
        if (why := too_long(ans)):
            return {"status": "rejected", "reason": f"check: mapping reply too long: {why}", "retried_after": e["reason"][:200]}
        e = {**check(t, ans, catalog), "retried_after": e["reason"][:200]}
    if isinstance(ans, dict) and ans.get("_usage"):
        e["usage"] = ans["_usage"]
    return e


PERSON_NOISE = re.compile(r"[^\w\s.'’\-]|_|(?:^|\s)@|\b(?=[^\W\d_]*\d)(?=\d*[^\W\d_])\w+\b")


def _more_than_a_name(cell):
    """Does a cell meant to be a person's name hold something else too: a separator mark (/ · | ( ...), an @login,
    an underscore, or a word mixing letters and digits (an id such as E004)? A name has none of these."""
    return bool(PERSON_NOISE.search(cell))


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


def _cell_parts(t, specs, n):
    """The mapping's composite columns, checked on every sample row the model was shown. Returns
    (parts [[column, count, separator, pattern]], {"c2.1": index of that value in an expanded row}, why rejected)."""
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
        if bad is not None and pattern and (lit := _pattern_separator(pattern, count)):
            # the pattern's own text between its groups, used as the separator: "#(\d+) / (\w+)" fails on
            # "#174 / PAY-AUTH" only because \w has no dash; " / " splits every sample row
            alt = [i, count, lit, ""]
            if all(split_cell(row[i], alt) is not None for row in sample if i < len(row)):
                part, bad = alt, None
        if bad is not None:
            return [], {}, f"the split of {c} does not parse the sample row cell \"{bad[:60]}\""
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

    def cell(cells, i):
        return cells[i].strip() if i is not None and i < len(cells) else ""

    def obtain(ty, key, name, props, unit, what, key_field=None, create=False, same_kind=False):
        """The one record a cell names: found in the graph (by its key field when the mapping gives one, so a tool's
        own record is found by its number or sha), made earlier in this table, or created. (rec, why)."""
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
        got = coerce(reg_fields[f], raw)
        if got is None:
            if raw:
                c[f"cell skipped: {f} value does not fit its type"] += 1
            return
        props[f] = got[0]
        if got[1]:
            props[f"{f}_precision"] = got[1]

    def ends(l, raw, unit, what):
        """The records one link cell names (every value of it, cut by the mapping's split): found by the other
        type's key field when the mapping gives one (a pull request's number, a file's path), else by name, else
        created like a row's record. A cell names the other record; whose record it is (an account, an address)
        never turns into its person (same_kind for identity links)."""
        out = []
        ty, key_field = l["other"], l.get("other_key")
        onf = cat.name_field(ty)
        for val in values(raw, l["split"]):
            if placeholder(val):
                c["placeholder cell (-, n/a ...): no link end, left out"] += 1
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
            other, why = obtain(ty, val, val, props, unit, what, key_field=key_field, create=True,
                                same_kind=l["link"] in v._identity)
            if why:
                c[why] += 1
                continue
            out.append(other)
        return out

    def own_values(l, own_cells):
        own = {}
        lspec = reg.link(l["link"]).fields
        for f, raw_own in own_cells:
            got = coerce(lspec[f], raw_own)
            if got is None:
                if raw_own:
                    c[f"cell skipped: link field {l['link']}.{f} value does not fit its type"] += 1
                continue
            own[f] = got[0]
        return own

    def link(l, rec, raw, unit, own_cells, others=None):
        """Every value of one link cell, linked to `rec` (the row's record, or the record another cell of the row
        names), with the link's own fields from `own_cells`."""
        own = own_values(l, own_cells)
        ltype = l["link"]
        for other in (ends(l, raw, unit, f"{ltype} skipped") if others is None else others):
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
        rec, why = obtain(rtype, key, name, props, unit, "row skipped", key_field if key_field in props else None, create=True)
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
                    raw = cell(cells, i)
                    named[k] = ends({"other": ty, "other_key": key_field, "split": split, "link": ltype}, raw, unit,
                                    f"{ltype} skipped") if raw else []
                return named[k]
            for l in m["links"]:
                if not cell(cells, l["column"]):
                    continue
                others = cell_ends(l["column"], l["other"], l.get("other_key"), l["split"], l["link"])
                own = [(f, cell(cells, i)) for i, f in l.get("fields", [])]
                if l.get("near") is None:
                    link(l, rec, None, unit, own, others)
                    continue
                spec = next((x for x in m["links"] if x["column"] == l["near"]), None)
                nears = cell_ends(l["near"], l["near_type"], spec.get("other_key") if spec else None,
                                  spec["split"] if spec else "none", spec["link"] if spec else l["link"])
                if not nears:
                    c[f"{l['link']} skipped: the cell its near end is in names no record"] += 1
                for near in nears:
                    link(l, near, None, unit, own, others)
        return None
    if getattr(w, "defer_table_links", False):   # fill.run: every table's rows first, then every table's links
        w.table_links.append(links_pass)
        return None
    return links_pass()


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
