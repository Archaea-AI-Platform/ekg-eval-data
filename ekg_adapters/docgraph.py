"""One emitter for every document source: a DocText (ekg_adapters.doctext) -> DocSection, Piece and their links.

The wiki adapter, the stored-files adapter and the repository-docs path of the code adapter all call
emit() for each ContentVersion they make, so a document has the same shape whatever tool it came from:

    owner (File | Document)
      <- VERSION_OF - ContentVersion            (made by the caller; its text is DocText.text)
      <- SECTION_OF | PART_OF - DocSection      one per heading, page, slide, sheet or tab; nested by SECTION_OF
      Piece - PART_OF -> DocSection | owner     the text, cut at block boundaries; `order` is document-global,
      Piece - OF_VERSION -> ContentVersion      `span` is "start-end" in the version's text, `pages` "3" or "3-4"
                                                for a paged source (a PDF: its text flows across pages)
      Piece - NEXT -> Piece                     reading order

Pieces cover all of the text: the spans of one version's pieces, laid end to end, are the version's
text. A table is rendered as `a | b | c` lines, header row first; when a table is longer than one
piece it is cut between rows and every later part starts with the header row again (so there the
piece's text is its span plus that repeated row).

A DocSection is the same record across versions as long as its heading path stays the same; its link
to the document carries the dates it was there (valid_from the version it first appeared in, valid_to
the version it was gone from). The registry has no OF_VERSION for a DocSection, so those dates are
how a section is tied to versions; its pieces are tied exactly, by OF_VERSION.

Extraction quality has no place in the schema, so it is kept beside the graph: per document in
g.ctx["doc_quality"], summed into g.notes, and written to the build report by report().
"""

from __future__ import annotations

import collections
import hashlib
import posixpath
import re

from . import common
from .common import Graph, day, ticket_rx
from .doctext import DocText, render_row

PIECE_CHARS = 4000      # the size a piece aims for; a single block above it is cut by rows / lines
GH_REF = re.compile(r"(?<![\w/&])#(\d{1,6})\b")
# How far a badly read document pulls down what is concluded from its text. A text rule matches words; in a
# document that came out garbled or with pages missing, a match is less sure to mean what it seems to, so the
# rule's confidence is multiplied by the share of the document that was read (Quality.ratio). It is floored at
# 0.5: the words that were read are still words the document holds, so a match never drops to "no idea".
QUALITY_FLOOR = 0.5
STATUS_WORDS = {"draft": "draft", "wip": "draft", "in progress": "draft", "proposed": "draft", "review": "draft",
                "published": "published", "final": "published", "approved": "published", "active": "published",
                "current": "published", "accepted": "published", "stable": "published",
                "deprecated": "deprecated", "obsolete": "deprecated", "superseded": "deprecated",
                "archived": "archived", "retired": "archived"}
OWNER_KEYS = ("owner", "owners", "maintainer", "maintainers", "author", "authors")
DATE_KEYS = ("date", "doc_date", "last_updated", "updated", "last_modified", "lastmod", "modified")


def quality_factor(g: Graph, owner) -> float:
    """What to multiply a text rule's confidence by when its evidence is (a piece of) this document."""
    q = g.ctx.get("doc_quality", {}).get(owner["id"] if isinstance(owner, dict) else owner)
    return max(QUALITY_FLOOR, q["ratio"]) if q else 1.0


# ---------------------------------------------------------------- structure: sections and pieces

def outline(doc: DocText):
    """(sections, section index of each block). A section is a heading or a container (page, slide, sheet, tab);
    headings nest by level inside the container they are in."""
    sections, of_block, stack, seen = [], [], [], collections.Counter()
    for i, b in enumerate(doc.blocks):
        if b.kind in ("container", "heading"):
            if b.kind == "container":
                stack, level, kind = [], 0, b.container
                n = b.meta.get("index")
                label = f"{b.container} {n}" if n is not None else b.container
                heading = (f"{label}: {b.text}" if b.container in ("page", "slide") else b.text) if b.text else label
            else:
                level, kind, heading = b.level, "heading", b.text
                while stack and sections[stack[-1]]["level"] >= level:
                    stack.pop()
            parent = stack[-1] if stack else None
            path = " > ".join([sections[k]["heading"] for k in stack] + [heading])
            seen[(kind, path)] += 1
            sections.append({"heading": heading, "kind": kind, "level": level, "order": len(sections), "path": path,
                             "parent": parent, "nth": seen[(kind, path)], "blocks": []})
            stack.append(len(sections) - 1)
        cur = stack[-1] if stack else None
        of_block.append(cur)
        if cur is not None:
            sections[cur]["blocks"].append(i)
    return sections, of_block


def _cut(text: str, target: int):
    """Offsets that cut `text` into parts of at most `target` characters, at line ends where it can, else at a
    space, else anywhere. Returns [(start, end)], covering the text exactly."""
    out, start, n = [], 0, len(text)
    while n - start > target:
        end = text.rfind("\n", start, start + target) + 1
        if end <= start:
            end = text.rfind(" ", start, start + target) + 1
        if end <= start:
            end = start + target
        out.append((start, end))
        start = end
    out.append((start, n))
    return out


def split_pieces(doc: DocText, target: int = PIECE_CHARS):
    """The document's text as pieces: [{start, end, text, kind, section, refs}], in reading order.

    Blocks are packed up to `target` characters, never across a section boundary; a table stands alone. A block longer than
    `target` is cut on its own (a table between rows, code and text between lines). A piece of a paged source whose
    blocks say their page (a PDF's: meta page / page_end) has `pages` (first, last); else None."""
    sections, of_block = outline(doc)
    groups, cur = [], None     # a group: {"section", "blocks": [...]} or {"section", "block", "part": (s, e), "first"}
    for i, b in enumerate(doc.blocks):
        size = b.end - b.start
        if size > target:
            cur = None
            whole = doc.text[b.start:b.end]
            parts = _cut(whole, target)
            part_refs = [[] for _ in parts]
            for r in b.refs:   # a ref goes with the part that shows it; one no part shows stays with the first
                hit = next((k for k, (s, e) in enumerate(parts) if (r.target and r.target in whole[s:e])
                            or (r.text and r.text in whole[s:e])), 0)
                part_refs[hit].append(r)
            for k, (s, e) in enumerate(parts):
                groups.append({"section": of_block[i], "block": b, "part": (b.start + s, b.start + e), "first": k == 0,
                               "refs": part_refs[k]})
            continue
        # a table is always a piece of its own, never packed with the prose around it: the model step maps a
        # table's columns once and applies that to every row, which needs the table alone
        # (its own heading stays with it)
        table = b.kind == "table"
        if (cur is None or cur["section"] != of_block[i] or (b.end - cur["start"]) > target or cur["table"]
                or (table and cur["prose"])):
            cur = {"section": of_block[i], "blocks": [], "start": b.start, "table": False, "prose": False}
            groups.append(cur)
        cur["table"] = cur["table"] or table
        cur["prose"] = cur["prose"] or b.kind not in ("heading", "container", "table")
        cur["blocks"].append(b)
    starts = [g["part"][0] if "part" in g else g["start"] for g in groups]
    pieces = []
    for k, grp in enumerate(groups):
        start = starts[k]
        end = starts[k + 1] if k + 1 < len(groups) else len(doc.text)   # up to the next piece: the spans tile the text
        body = doc.text[start:end].rstrip("\n")
        if "part" in grp:
            b = grp["block"]
            kinds = {b.kind}
            if b.kind == "table" and b.header and not grp["first"] and b.rows:
                body = render_row(b.rows[0]) + "\n" + body      # the header row again, so the part reads on its own
            refs = grp["refs"]
        else:
            kinds = {b.kind for b in grp["blocks"] if b.kind not in ("heading", "container")}
            refs = [r for b in grp["blocks"] for r in b.refs]
        kind = "table" if kinds == {"table"} else "code" if kinds == {"code"} else "list" if kinds == {"list_item"} else "text"
        held = [grp["block"]] if "part" in grp else grp["blocks"]
        on = [n for b in held for n in (b.meta.get("page"), b.meta.get("page_end")) if isinstance(n, int)]
        pieces.append({"start": start, "end": end, "text": body, "kind": kind, "section": grp["section"], "refs": refs,
                       "pages": (min(on), max(on)) if on else None})
    return sections, pieces


# ---------------------------------------------------------------- the emitter

def emit(g: Graph, owner, doc: DocText, version, scope, tool, native_prefix, at=None, current=True,
         piece_chars=None, editor=None, attribute=False, base_path=None, space=None, link_refs=None):
    """Write the sections, pieces and reference links of one ContentVersion of `owner` (a File or a Document).

    version        the ContentVersion record (the caller made it; its text should be doc.text)
    native_prefix  the version's own native id; piece ids are built on it
    at             when this version became current (the pieces' created_at, the sections' valid_from)
    current        this is the version in force now: only then are the document's own claims (front matter,
                   metadata) and, unless link_refs says otherwise, its references turned into links
    editor         the Account that made this version; with attribute=True the sections this version changed
                   get EDITED_BY it (attribute only when the version before is known, or this is the first)
    base_path      the document's own path, to resolve relative links against
    space          the wiki space, to resolve page links that name no space
    Versions of one owner must be emitted oldest first. Returns {"sections": [...], "pieces": [...]}."""
    reg = common._reg
    link_refs = current if link_refs is None else link_refs
    q = doc.quality
    owner_native = owner["props"]["native_id"]
    state = g.ctx.setdefault("_doc_state", {}).setdefault(owner["id"], {"open": {}, "hash": {}})
    if current:
        g.ctx.setdefault("doc_quality", {})[owner["id"]] = {
            "source": tool, "format": doc.format, "name": owner["props"].get("path") or owner["props"].get("title")
            or owner["props"].get("name") or owner_native, "type": owner["type"], "ratio": q.ratio,
            "source_chars": q.source_chars, "kept_chars": q.kept_chars, "text_chars": len(doc.text),
            "reasons": q.reasons(), "quality": q.as_dict()}
    factor = max(QUALITY_FLOOR, q.ratio)
    piece_chars = piece_chars or g.ctx.get("settings", {}).get("piece_chars") or PIECE_CHARS
    sections, pieces = split_pieces(doc, piece_chars)

    # ---- sections: one record per heading path, dated by the versions it is in
    sec_recs, present = [], set()
    for s in sections:
        key = hashlib.sha1(f"{s['kind']}\0{s['path']}\0{s['nth']}".encode("utf-8")).hexdigest()[:16]
        rec = g.rec("DocSection", tool, f"{owner_native}#{key}", {"heading": s["heading"], "kind": s["kind"],
                    "level": s["level"], "order": s["order"], "path": s["path"]}, scope, acl_from=owner)
        if current:   # the record shows the section as it is now
            rec["props"].update({"order": s["order"], "level": s["level"]})
        sec_recs.append(rec)
        present.add(key)
        s["key"] = key
        parent = sec_recs[s["parent"]] if s["parent"] is not None else owner
        ltype = "SECTION_OF" if reg.link_allows("SECTION_OF", "DocSection", parent["type"]) else "PART_OF"
        if not reg.link_allows(ltype, "DocSection", parent["type"]):
            g.notes[f"documents: a section cannot be linked to a {parent['type']} (schema)"] += 1
            continue
        opened = state["open"].get(key)
        if opened is None or opened["b"] != parent["id"]:
            if opened is not None:
                _close(opened, at)
            state["open"][key] = g.link(ltype, rec, parent, "field", order=s["order"], valid_from=at)
        elif current:
            opened["props"]["order"] = s["order"]
    for key in [k for k in state["open"] if k not in present]:   # a section this version no longer has
        _close(state["open"].pop(key), at)
    # ---- who changed which section
    hashes = {}
    for s in sections:
        body = "\n".join(doc.text[doc.blocks[i].start:doc.blocks[i].end] for i in s["blocks"])
        hashes[s["key"]] = hashlib.sha1(body.encode("utf-8")).hexdigest()
    if editor is not None and attribute and at and reg.link_allows("EDITED_BY", "DocSection", editor["type"]):
        for s, rec in zip(sections, sec_recs):
            if state["hash"].get(s["key"]) != hashes[s["key"]]:
                lk = g.link("EDITED_BY", rec, editor, "event", role="editor", evidence=[version], at=at,
                            first_at=at, last_at=at, count=0)
                p = lk["props"]
                p["count"] += 1
                p["first_at"], p["last_at"] = min(p["first_at"], at), max(p["last_at"], at)
                p["at"] = p["last_at"]
    state["hash"] = hashes

    # ---- pieces
    recs, prev = [], None
    key_rx, github_issues = ticket_rx(g), g.ctx.get("settings", {}).get("issue_tracker") == "github"
    for order, pc in enumerate(pieces):
        sec = sec_recs[pc["section"]] if pc["section"] is not None else None
        piece = g.rec("Piece", tool, f"{native_prefix}:{order}", {
            "text": pc["text"], "kind": pc["kind"], "heading": sections[pc["section"]]["path"] if sec else None,
            "order": order, "span": f"{pc['start']}-{pc['end']}", "created_at": at,
            **({"pages": _pages(pc["pages"])} if pc.get("pages") else {})}, scope, acl_from=owner)
        part_of = sec if sec is not None and reg.link_allows("PART_OF", "Piece", "DocSection") else owner
        g.link("PART_OF", piece, part_of, "field", order=order)
        g.link("OF_VERSION", piece, version, "field")
        if prev is not None:
            g.link("NEXT", prev, piece, "field", kind="next")
        prev = piece
        recs.append(piece)
        if link_refs:
            _links(g, owner, piece, pc, scope, tool, at, factor, key_rx, github_issues, base_path, space)
        else:
            g.notes["documents: references in older versions' pieces not turned into links (current version only)"] += \
                len(pc["refs"])
    g.notes[f"documents: {tool} versions read into pieces"] += 1
    g.notes[f"documents: {tool} pieces"] += len(recs)
    if current:
        _claims(g, owner, doc, version, scope, tool, native_prefix, at)
    return {"sections": sec_recs, "pieces": recs}


def _pages(span):
    """(first, last) page -> "3" or "3-4"."""
    return str(span[0]) if span[0] == span[1] else f"{span[0]}-{span[1]}"


def _close(link, at):
    """End a section's link at the version that no longer has it."""
    start = link["props"].get("valid_from")
    if at and (not start or at > start):
        link["props"]["valid_to"] = at


def _links(g, owner, piece, pc, scope, tool, at, factor, key_rx, github_issues, base_path, space):
    """What a piece points at -> links, most through g.ref so the linker can find another tool's record."""
    reg = common._reg
    named = set()
    for r in pc["refs"]:
        if r.kind == "url":
            g.ref(piece, "LINKS_TO", "Url", r.target, "field", 0.95, kind="url", at=at)
            # a page that links another page: the documents are linked too (the piece is the evidence)
            g.ref(owner, "LINKS_TO", "Url", r.target, "field", 0.95, kind="url", at=at, evidence=[piece], only="Document")
        elif r.kind == "page":
            if r.extra.get("id"):
                key, ttype = (tool, r.extra["id"]), "DocumentById"
            elif r.target:
                key, ttype = (tool, r.extra.get("space") or space, r.target), "DocumentByTitle"
            else:
                continue
            g.ref(piece, "LINKS_TO", ttype, key, "field", at=at)
            g.ref(owner, "LINKS_TO", ttype, key, "field", at=at, evidence=[piece], only="Document")
        elif r.kind == "file":
            path = r.target.split("#")[0].split("?")[0]
            if path:
                path = posixpath.normpath(posixpath.join(posixpath.dirname(base_path or ""), path)) if not path.startswith("/") \
                    else path.lstrip("/")
                g.ref(piece, "LINKS_TO", "FileByPath", (tool, path), "field", 0.95, at=at)
        elif r.kind == "attachment" or (r.kind == "image" and r.extra.get("attachment")):
            if not r.target:
                continue
            on = r.extra.get("page")   # an attachment that hangs on another page
            native = f"{tool}:{r.extra.get('space') or space}:{on}:{r.target}" if on else f"{owner['props']['native_id']}:{r.target}"
            att = g.rec("Attachment", tool, native, {"title": r.target, "kind": "diagram" if r.extra.get("diagram")
                        else "image" if r.kind == "image" else "file"}, scope, acl_from=None if on else owner)
            if on:
                g.ref(att, "HAS_ATTACHMENT", "DocumentByTitle", (tool, r.extra.get("space") or space, on), "field", reverse=True)
            elif reg.link_allows("HAS_ATTACHMENT", owner["type"], "Attachment"):
                g.link("HAS_ATTACHMENT", owner, att, "field")
            else:
                g.notes[f"documents: attachment of a {owner['type']} kept without HAS_ATTACHMENT (schema)"] += 1
            g.link("LINKS_TO", piece, att, "field", at=at)
        elif r.kind == "image":
            g.notes["documents: images at an outside address (no record, not linked)"] += 1
        elif r.kind == "ticket":
            named.add(r.target)   # an issue macro: the tool's own reference, not a guess from text
            g.ref(piece, "MENTIONS", "Ticket", r.target, "integration", 1.0, kind="key", at=at)
        elif r.kind == "user":
            if not r.extra.get("setting"):
                g.ref(piece, "MENTIONS", "AccountByKey", (tool, r.target), "field", kind="mention", at=at)
        elif r.kind == "email":
            g.ref(piece, "MENTIONS", "EmailAddress", r.target, "text_rule", round(0.9 * factor, 3), kind="mention", at=at)
    text = pc["text"]
    for k in sorted(set(key_rx.findall(text)) - named):
        g.ref(piece, "MENTIONS", "Ticket", k, "text_rule", round(0.8 * factor, 3), kind="key", at=at)
    if github_issues:
        for n in sorted({int(x) for x in GH_REF.findall(text)}):
            g.ref(piece, "MENTIONS", "GitHubRef", n, "text_rule", round(0.6 * factor, 3), kind="key", at=at)


def _alias(g, name, tool, scope):
    return g.rec("Alias", tool, f"display_name:{name.strip().lower()}", {"value": name.strip(), "kind": "display_name",
                 "namespace": tool, "normalized_value": name.strip().lower()}, scope)


def _claims(g, owner, doc, version, scope, tool, native_prefix, at):
    """What the document says about itself: file properties (who made it, with what), front matter (status,
    date, owner), review comments."""
    reg = common._reg
    m = doc.meta
    props = {k: m.get(k) for k in ("application", "company", "creator", "last_modified_by") if m.get(k)}
    if props:
        holder = owner if reg.link_allows("HAS_METADATA", owner["type"], "FileMetadata") else version
        md = g.rec("FileMetadata", tool, native_prefix, props, scope, acl_from=owner)
        g.link("HAS_METADATA", holder, md, "field", valid_from=at)
        if m.get("creator"):
            g.link("METADATA_CREATOR", md, _alias(g, m["creator"], tool, scope), "field", at=m.get("created_at") or at)
        if m.get("last_modified_by"):
            g.link("METADATA_LAST_MODIFIED_BY", md, _alias(g, m["last_modified_by"], tool, scope), "field",
                   at=m.get("modified_at") or at)
    fields = reg.record(owner["type"]).fields
    fm = m.get("front_matter") or {}
    stated = {}

    def state(field, value, quote, how="value"):
        if field in fields and value:
            owner["props"][field] = value
            stated[field] = {"value": value, "by": {version["id"]: {how: quote}}}

    if m.get("created_at") and "created_at" in fields:
        owner["props"].setdefault("created_at", m["created_at"])
    raw_status = fm.get("status") or fm.get("state")
    if isinstance(raw_status, str) and raw_status.strip():
        word = STATUS_WORDS.get(raw_status.strip().lower())
        if word:
            state("status", word, f"status: {raw_status}")
        else:
            g.notes["documents: front-matter status not one of draft / published / deprecated / archived (left out)"] += 1
    for k in DATE_KEYS:
        v = fm.get(k)
        dm = re.match(r"(\d{4})-(\d\d)-(\d\d)", v) if isinstance(v, str) else None
        if dm:
            state("doc_date", day(dm.group(0)), f"{k}: {v}", "day")
            if "doc_date" in fields:
                owner["props"]["doc_date_precision"] = "day"
            break
    else:
        if m.get("modified_at") and "doc_date" in fields:   # the file's own "last saved" property
            owner["props"].setdefault("doc_date", m["modified_at"])
    if stated:
        owner["props"]["_stated"] = {**owner["props"].get("_stated", {}), **stated}
    for k in OWNER_KEYS:
        v = fm.get(k)
        for who in (v if isinstance(v, list) else [v] if isinstance(v, str) and v.strip() else []):
            if isinstance(who, str) and who.strip() and reg.link_allows("MAINTAINED_BY", owner["type"], "Person"):
                g.ctx.setdefault("_doc_people", []).append((owner, version, who.strip(), k, owner["props"].get("doc_date") or at))
    for c in doc.comments:
        cm = g.rec("Comment", tool, f"{native_prefix}:c{c.get('id')}", {"text": c["text"], "kind": "doc", "at": c.get("at")},
                   scope, acl_from=owner)
        g.link("ON", cm, owner, "field", at=c.get("at"))
        if c.get("author"):
            g.link("AUTHORED_BY", cm, _alias(g, c["author"], tool, scope), "field", role="author", at=c.get("at"))


def same_content(g: Graph, rec, sha256: str):
    """SAME_CONTENT_AS between stored files (or attachments) whose bytes are the same."""
    first = g.ctx.setdefault("_content_hash", {}).setdefault(sha256, rec)
    if first is not rec and first["id"] != rec["id"]:
        g.link("SAME_CONTENT_AS", rec, first, "field", basis="hash")
        g.notes["documents: files with the same content as another (SAME_CONTENT_AS)"] += 1


# ---------------------------------------------------------------- after identity resolution

def finish(g: Graph):
    """The owners a document names in its front matter -> MAINTAINED_BY a Person or Account. Runs after
    identity resolution, because with no directory the Persons only exist by then."""
    queue = g.ctx.pop("_doc_people", [])
    if not queue:
        return {}
    by_id = {r["id"]: r for r in g.records.values()}
    person_of_addr, by_name, by_login, by_display = {}, collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
    addr = {r["props"]["address"].lower(): r for r in g.records.values() if r["type"] == "EmailAddress" and r["props"].get("address")}
    for lk, _, _ in g.links.values():
        if lk["type"] == "HAS_ALIAS" and by_id[lk["a"]]["type"] == "Person":
            person_of_addr[lk["b"]] = by_id[lk["a"]]
    for r in g.records.values():
        if r["type"] == "Person" and r["props"].get("full_name"):
            by_name[r["props"]["full_name"].casefold()].add(r["id"])
        elif r["type"] == "Account" and not r["props"].get("is_bot"):
            if r["props"].get("login"):
                by_login[str(r["props"]["login"]).casefold()].add(r["id"])
            if r["props"].get("display_name"):
                by_display[r["props"]["display_name"].casefold()].add(r["id"])
    stats = collections.Counter()
    one = lambda ids: by_id[next(iter(ids))] if len(ids) == 1 else None
    for owner, version, who, key, since in queue:
        m = re.match(r"^(.*?)\s*<([^<>@\s]+@[^<>\s]+)>$", who)
        name, email = (m.group(1).strip(), m.group(2).lower()) if m else (who, who.lower() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", who) else None)
        target, method, conf = None, "field", 0.95
        if email and email in addr:
            target = person_of_addr.get(addr[email]["id"])
        if target is None and not email:
            target = one(by_login.get(name.lstrip("@").casefold(), ()))
        if target is None and name and not (email and name == who):
            target = one(by_name.get(name.casefold(), ())) or one(by_display.get(name.casefold(), ()))
            # a name match is a text rule (two people can share a name), and it leans on the document being read right
            method, conf = "text_rule", round(0.7 * quality_factor(g, owner), 3)
        if target is None:
            stats["front-matter owner not matched to a person or account"] += 1
            continue
        g.link("MAINTAINED_BY", owner, target, method, conf, evidence=[version], source="front_matter", valid_from=since,
               _stated={"source": {"value": "front_matter", "by": {version["id"]: {"value": f"{key}: {who}"}}}})
        stats[f"front-matter owner -> MAINTAINED_BY {target['type']}"] += 1
    for k, v in stats.items():
        g.notes[f"documents: {k}"] += v
    return dict(stats)


def report(g: Graph) -> dict:
    """The build report's `documents` section: what was read, how well, and the worst of it."""
    rows = list(g.ctx.get("doc_quality", {}).values())
    by_source = collections.defaultdict(lambda: collections.Counter())
    buckets = collections.Counter()
    unknown, dropped = collections.Counter(), collections.Counter()
    for r in rows:
        s = by_source[f"{r['source']} ({r['format']})"]
        s["documents"] += 1
        s["text_chars"] += r["text_chars"]
        s["source_chars"] += r["source_chars"]
        s["kept_chars"] += r["kept_chars"]
        qd = r["quality"]
        for k in ("pages", "slides", "sheets", "empty_pages", "failed_parts", "tables", "images"):
            if qd.get(k):
                s[k] += qd[k]
        ratio = r["ratio"]
        buckets["1.0" if ratio >= 0.9995 else "0.95 - 1" if ratio >= 0.95 else "0.8 - 0.95" if ratio >= 0.8
                else "0.5 - 0.8" if ratio >= 0.5 else "under 0.5"] += 1
        for k, v in qd["unknown"].items():
            unknown[k] += v
        for k, v in qd["dropped"].items():
            dropped[k] += v
    worst = sorted((r for r in rows if r["ratio"] < 0.9995 or r["quality"]["unknown"] or r["quality"]["warnings"]),
                   key=lambda r: (r["ratio"], -sum(r["quality"]["unknown"].values())))[:20]
    counts = {k[len("documents: "):]: v for k, v in g.notes.items() if k.startswith("documents: ")}
    counts.update({k: v for k, v in g.notes.items() if k.startswith(("docs: ", "wiki: "))})
    return {"documents": len(rows), "by_source": {k: dict(v) for k, v in sorted(by_source.items())},
            "kept_ratio": {k: buckets[k] for k in ("1.0", "0.95 - 1", "0.8 - 0.95", "0.5 - 0.8", "under 0.5") if buckets[k]},
            "confidence_rule": f"text-rule links from a document's pieces: confidence x max({QUALITY_FLOOR}, kept ratio)",
            "piece_chars": PIECE_CHARS, "counts": counts,
            "not_understood": dict(unknown.most_common(30)), "left_out_on_purpose": dict(dropped.most_common(30)),
            "worst": [{"name": r["name"], "source": r["source"], "format": r["format"], "kept_ratio": r["ratio"],
                       "reasons": r["reasons"]} for r in worst]}
