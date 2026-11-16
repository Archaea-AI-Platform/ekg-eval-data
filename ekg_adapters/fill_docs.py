"""Reading a document out of the graph: its current version's pieces in order, its outline, its tables.

The shape the document adapters write (and this tolerates less):

    File | Document  <-VERSION_OF-  ContentVersion          (the newest is the current one)
    DocSection (heading, kind, level, order, path)  -SECTION_OF | PART_OF->  the document or a parent section
    Piece (text, kind text|table|code|list, heading = section path, order)
          -PART_OF-> a DocSection or the document,  -OF_VERSION-> the version

A document is read as its current version's pieces in `order`. One with no pieces is read from the
ContentVersion text. Nothing is skipped for being old or short: every document with text is read, whole,
in as many consecutive windows as the model's window needs (fill_budget.pack), cut only between pieces.
A table is a run of consecutive table pieces under one heading with the same header row. Inside a table
piece, lines before the first line with cells are the table's heading (or caption) lines; the first line with
cells is the header row, which every continuation piece repeats. Only the lines after it are data rows.

What reads what: a table piece that a table_map task maps (View.claimed) is left out of the reading tasks, so it
is never read twice; a table piece no mapping reads (too small to map, or its mapping was rejected) is read as
text with the rest of its document, so nothing is left unread.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

from . import fill_budget

DOC_TYPES = ("Document", "File")
TABLE_KINDS = {"table", "spreadsheet"}
CODE_FILE_KINDS = {"source", "test", "config"}  # a code host's code files: the code adapter reads those
_CELLS = re.compile(r"\s*(?<!\\)\|\s*")


@dataclass
class Unit:
    """One citeable stretch of a document: a Piece, or the document itself when it has no pieces."""
    id: str
    kind: str          # text | table | code | list
    heading: str
    text: str
    order: float
    section: str | None = None   # the DocSection it sits in, when there is one
    part: str = ""               # "2/3" when one piece larger than a whole window had to be cut

    @property
    def size(self):
        return len(self.text) + len(self.heading) + 24


@dataclass
class Table:
    heading: str
    header: list
    units: list
    sep: str
    outer: bool = False   # rows written | like | this |, with a pipe at both ends
    lead: str = ""        # the heading / caption lines above the header row in the first piece

    def lines(self):
        """(unit, line) for every data row line, in order; heading lines and the header row each piece
        repeats are left out."""
        for u in self.units:
            for line in split_table(u.text)[2]:
                if line.strip():
                    yield u, line

    def rows(self):
        """(unit, [cells]) for every data row, in order."""
        for u, line in self.lines():
            yield u, cells(line, self.sep, self.outer)

    @property
    def head(self):
        """The header row as written."""
        return split_table(self.units[0].text)[1]

    @property
    def chars(self):
        return sum(len(u.text) for u in self.units)


def split_table(text):
    """A table piece's text -> (heading lines, header row, [data row lines]). The header row is the first line
    with cells; lines before it are the table's heading or caption. A text with no line with cells -> (text, "", [])."""
    lines = text.split("\n")
    at = next((i for i, ln in enumerate(lines) if _sep(ln)), None)
    if at is None:
        return text, "", []
    return "\n".join(lines[:at]).strip(), lines[at], lines[at + 1:]


def header_line(text):
    """The line a cut table part repeats: its header row (the first line with cells), else its first line."""
    return split_table(text)[1] or text.split("\n", 1)[0]


@dataclass
class Doc:
    rec: dict
    version: dict | None
    title: str
    units: list
    outline: list = field(default_factory=list)   # [(level, heading)]
    quality: float = 1.0                          # the adapter's extraction quality (kept ratio), 1.0 when unknown

    @property
    def chars(self):
        return sum(len(u.text) for u in self.units)


def cells(line, sep, outer=False):
    """One row's cells. An empty cell stays a cell, also the first or last one of a row."""
    if sep == "\t":
        return [c.strip() for c in line.strip("\r\n").split("\t")]
    out = [c.strip().replace("\\|", "|") for c in _CELLS.split(line.strip())]
    return out[1:-1] if outer and len(out) > 2 else out


def _outer(header):
    h = header.strip()
    return len(h) > 2 and h.startswith("|") and h.endswith("|") and not h.endswith("\\|")


def _sep(header):
    return "|" if _CELLS.search(header) else "\t" if "\t" in header else None


def title_of(rec):
    p = rec["props"]
    return p.get("title") or p.get("name") or p.get("path") or p.get("native_id") or "?"


def _order(rec, link=None):
    o = rec["props"].get("order")
    if o is None and link is not None:
        o = link["props"].get("order")
    return o if isinstance(o, (int, float)) else 1e12


def read(v, d) -> Doc | None:
    """The document as units in reading order, or None when it holds no text."""
    versions = [(lk["props"].get("order") or 0, v.rec[lk["a"]]["props"].get("valid_from") or "", lk["a"])
                for lk in v.inn[d["id"]] if lk["type"] == "VERSION_OF" and v.rec[lk["a"]]["type"] == "ContentVersion"]
    current = v.rec[max(versions)[2]] if versions else None
    # sections, at any depth
    sections, queue = {}, [d["id"]]
    while queue:
        parent = queue.pop()
        for lk in v.inn[parent]:
            s = v.rec[lk["a"]]
            if lk["type"] in ("SECTION_OF", "PART_OF") and s["type"] == "DocSection" and s["id"] not in sections:
                sections[s["id"]] = (s, parent, _order(s, lk))
                queue.append(s["id"])

    def path(sid):
        s, parent, _ = sections[sid]
        if s["props"].get("path"):
            return s["props"]["path"]
        own = s["props"].get("heading") or ""
        return f"{path(parent)} > {own}" if parent in sections else own

    pieces = {}
    for holder in [d["id"], *sections]:
        for lk in v.inn[holder]:
            p = v.rec[lk["a"]]
            if lk["type"] == "PART_OF" and p["type"] == "Piece":
                pieces[p["id"]] = (p, holder if holder in sections else None, _order(p, lk))
    versioned = {}
    for _, _, vid in versions:
        for lk in v.inn[vid]:
            p = v.rec[lk["a"]]
            if lk["type"] == "OF_VERSION" and p["type"] == "Piece":
                versioned[p["id"]] = vid
                pieces.setdefault(p["id"], (p, None, _order(p)))
    # the current version's pieces; a piece tied to no version belongs to the document as it is now
    mine = [x for pid, x in pieces.items() if current is None or versioned.get(pid, current["id"]) == current["id"]]
    mine.sort(key=lambda x: (x[2], x[0]["props"].get("chunk_index") or 0, x[0]["id"]))
    units, heads = [], []
    for p, sec, order in mine:
        pp = p["props"]
        kind = pp.get("kind") or "text"
        heading = pp.get("heading") or (path(sec) if sec else "")
        if heading and (not heads or heads[-1][1] != heading):
            m = re.fullmatch(r"h([1-6])", kind)
            heads.append((int(m.group(1)) if m else heading.count(" > ") + 1, heading))
        if (pp.get("text") or "").strip():
            units.append(Unit(p["id"], "table" if kind in TABLE_KINDS else kind if kind in ("code", "list") else "text",
                              heading, pp["text"].strip("\n"), order, sec))
    if not units and current is not None and (current["props"].get("text") or "").strip():
        # no pieces: the version's text, cited to the document itself
        units = [Unit(d["id"], "text", "", current["props"]["text"].strip(), 0)]
    if not units:
        return None
    if sections:
        outline = [(s["props"].get("level") or path(sid).count(" > ") + 1, s["props"].get("heading") or path(sid))
                   for sid, (s, _, _) in sorted(sections.items(), key=lambda kv: (kv[1][2], kv[0]))]
    else:
        outline = heads
    return Doc(d, current, title_of(d), units, outline)


def documents(v, sample: int | None = None, quality: dict | None = None, only=None):
    """Every document with text -> ([Doc], skipped counter). `sample` keeps that many, evenly spaced (test runs);
    `only` keeps the documents it names (a record id, key, native id, path or title, exactly)."""
    skipped, found = collections.Counter(), []
    for t in DOC_TYPES:
        for d in v.typed(t):
            p = d["props"]
            if t == "File" and p.get("kind") in CODE_FILE_KINDS and p.get("source_tool") != "docs":
                continue
            found.append(d)
    found.sort(key=lambda d: d["props"]["_key"])
    if only:
        names = {str(x).strip() for x in only}
        keep = [d for d in found if names & {d["id"], *(str(d["props"].get(k)) for k in ("_key", "native_id", "path", "title", "name"))}]
        skipped["not read: outside --only"] += len(found) - len(keep)
        found = keep
    docs = []
    for d in found:
        doc = read(v, d)
        if doc is None:
            skipped["no text stored"] += 1
            continue
        doc.quality = quality_of(d, quality or {})
        docs.append(doc)
    if sample is not None and len(docs) > sample:
        keep = {round(i * (len(docs) - 1) / max(sample - 1, 1)) for i in range(sample)} if sample > 0 else set()
        skipped["not read: outside the --docs sample"] += len(docs) - len(keep)
        docs = [doc for i, doc in enumerate(docs) if i in keep]
    return docs, skipped


def quality_of(d, quality: dict) -> float:
    """The document adapter's extraction quality for this document (its kept ratio), 1.0 when it gave none."""
    p = d["props"]
    for key in (d["id"], p.get("_key"), p.get("native_id"), p.get("path"), p.get("name"), p.get("title")):
        q = quality.get(key) if key else None
        if isinstance(q, dict):
            q = next((q[k] for k in ("kept_ratio", "kept ratio", "kept", "quality", "ratio") if k in q), None)
        if isinstance(q, (int, float)) and not isinstance(q, bool):
            return min(max(float(q), 0.0), 1.0)
    return 1.0


def load_quality(report: dict) -> dict:
    """A {document: kept ratio} map from the graph's report.json, wherever the document adapter put it."""
    found = {}

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 4:
            return
        for k, val in node.items():
            if k in ("doc_quality", "document_quality", "kept_ratio", "kept ratio") and isinstance(val, dict):
                found.update(val)
            else:
                walk(val, depth + 1)
    walk(report or {})
    return found


def tables(doc: Doc):
    """Runs of consecutive table pieces with one heading and one header row."""
    out, cur = [], None
    for u in doc.units:
        if u.kind != "table":
            cur = None
            continue
        lead, first, _ = split_table(u.text)
        sep = _sep(first) if first else None
        if sep is None:
            cur = None
            continue
        header = cells(first, sep, _outer(first))
        # a continuation part: same heading, same header row repeated (its own heading lines, if any, repeat too)
        if cur is not None and cur.heading == u.heading and cur.header == header:
            cur.units.append(u)
        else:
            cur = Table(u.heading, header, [u], sep, _outer(first), lead)
            out.append(cur)
    return out


def text_units(doc: Doc, claimed=()):
    """What the reading tasks read: every unit but the table pieces a table mapping reads (`claimed`)."""
    return [u for u in doc.units if not (u.kind == "table" and u.id in claimed)]


def windows(units, room: int):
    """Consecutive windows of whole units. A unit larger than a whole window is cut at line ends first
    (a table's header row repeated); returns (windows, how many units had to be cut)."""
    flat, cut = [], 0
    for u in units:
        if u.size <= room:
            flat.append(u)
            continue
        cut += 1
        repeat = header_line(u.text) if u.kind == "table" else ""
        parts = fill_budget.split_text(u.text, room - len(u.heading) - 24, repeat)
        flat += [Unit(u.id, u.kind, u.heading, part, u.order, u.section, f"{i + 1}/{len(parts)}") for i, part in enumerate(parts)]
    return fill_budget.pack(flat, room, size=lambda u: u.size), cut


def outline_text(doc: Doc, room: int):
    """The document's headings, indented by level; the deepest levels are left out when they would not fit."""
    rows = doc.outline
    depth = max((lv for lv, _ in rows), default=0)
    while rows and sum(len(h) + 2 * lv + 1 for lv, h in rows) > room and depth > 1:
        depth -= 1
        rows = [r for r in doc.outline if r[0] <= depth]
    text = "\n".join(f"{'  ' * (max(lv, 1) - 1)}{h}" for lv, h in rows)
    if len(text) > room:
        text = text[:room].rsplit("\n", 1)[0]
    return text + ("\n(deeper headings are left out here; each passage below still names its own)" if len(rows) < len(doc.outline) else "")
