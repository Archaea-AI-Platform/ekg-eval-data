"""Word, PowerPoint and Excel files (.docx, .pptx, .xlsx) -> DocText, with the standard library only.

All three are zip archives of XML parts joined by relationship files (`_rels/*.rels`). Parts are found
through those relationships, never by guessing a file name: a workbook whose sheets were reordered,
or a deck whose slides were, still reads in the order the user sees.
"""

from __future__ import annotations

import datetime as dt
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from .model import Block, DocText, Ref, Unreadable, nonws

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCT = "{http://purl.org/dc/terms/}"
EP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"


MAX_PART = 1 << 30   # one XML part, unpacked: beyond this the archive is treated as damaged (or a zip bomb)


class _Pack:
    def __init__(self, data: bytes):
        try:
            self.z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise Unreadable("not a zip archive (damaged, or an old binary Office file)") from e
        self.names = set(self.z.namelist())

    def xml(self, name):
        if name not in self.names:
            return None
        if self.z.getinfo(name).file_size > MAX_PART:
            raise Unreadable(f"{name}: part larger than {MAX_PART >> 20} MB when unpacked")
        raw = self.z.read(name)
        if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw[:4096]:
            # Office parts never declare a DTD; one that does is an entity-expansion attempt
            raise Unreadable(f"{name}: declares a DTD")
        try:
            return ET.fromstring(raw)
        except ET.ParseError as e:
            raise Unreadable(f"{name}: not valid XML ({e})") from e

    def rels(self, part) -> dict:
        """Relationship id -> {target (archive path, or the address when external), type, external}."""
        folder, base = posixpath.split(part)
        root = self.xml(posixpath.join(folder, "_rels", base + ".rels"))
        out = {}
        for r in root if root is not None else []:
            target, external = r.get("Target", ""), r.get("TargetMode") == "External"
            if not external:
                target = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
            out[r.get("Id")] = {"target": target, "type": r.get("Type", "").rsplit("/", 1)[-1], "external": external}
        return out


def _iso(value):
    """A W3CDTF time from the core properties -> UTC ISO seconds, or None."""
    if not value:
        return None
    try:
        s = re.sub(r"\.\d+", "", value.strip().replace("Z", "+00:00"))
        t = dt.datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _props(pack: _Pack, doc: DocText):
    """docProps/core.xml and app.xml: who made it, who saved it last, when, with what, for which company."""
    core, app = pack.xml("docProps/core.xml"), pack.xml("docProps/app.xml")
    text = lambda root, tag: ((root.findtext(tag) or "").strip() or None) if root is not None else None
    for key, value in (("creator", text(core, DC + "creator")), ("last_modified_by", text(core, CP + "lastModifiedBy")),
                       ("created_at", _iso(text(core, DCT + "created"))), ("modified_at", _iso(text(core, DCT + "modified"))),
                       ("application", text(app, EP + "Application")), ("company", text(app, EP + "Company")),
                       ("subject", text(core, DC + "subject")), ("keywords", text(core, CP + "keywords"))):
        if value:
            doc.meta[key] = value
    doc.title = doc.title or text(core, DC + "title")


# ---------------------------------------------------------------- docx

def _w_text(node, q, links=None, refs=None):
    """The text of a paragraph (or any node): runs, tabs, breaks, hyperlinks. Tracked deletions and the
    fallback copy of a text box are left out, so nothing is said twice."""
    out = []

    def walk(n):
        for c in n:
            tag = c.tag
            if tag == W + "t":
                out.append(c.text or "")
            elif tag == W + "tab":
                out.append("\t")
            elif tag in (W + "br", W + "cr"):
                out.append("\n")
            elif tag == W + "noBreakHyphen":
                out.append("-")
            elif tag in (W + "del", W + "moveFrom"):
                q.dropped["tracked deletion"] += 1
            elif tag == MC + "Fallback" or tag == W + "pPr" or tag == W + "rPr":
                continue
            elif tag == W + "instrText":
                m = re.search(r'HYPERLINK\s+"([^"]+)"', c.text or "")
                if m and refs is not None:
                    refs.append(Ref("url", m.group(1)))
            elif tag == W + "hyperlink":
                at = len(out)
                walk(c)
                rel = (links or {}).get(c.get(R + "id"))
                if rel and rel["external"] and refs is not None:
                    words = "".join(out[at:])
                    url = rel["target"]
                    if url.lower().startswith("mailto:"):
                        refs.append(Ref("email", url[7:].split("?")[0].lower(), words))
                    else:
                        refs.append(Ref("url", url, words))
                        if url not in words:
                            out.append(f" <{url}>")
            elif tag in (W + "drawing", W + "pict", W + "object"):
                q.images += 1
                walk(c)   # a text box inside a drawing still holds text
            else:
                walk(c)

    walk(node)
    return "".join(out)


def _docx_table(tbl, q, links, refs):
    rows, header = [], False
    for tr in tbl.findall(W + "tr"):
        pr = tr.find(W + "trPr")
        if not rows and pr is not None and pr.find(W + "tblHeader") is not None:
            header = True
        line = []
        for tc in tr.findall(W + "tc"):
            parts = []
            for el in tc:
                if el.tag == W + "p":
                    parts.append(_w_text(el, q, links, refs).strip())
                elif el.tag == W + "tbl":   # a table inside a cell: its rows, flattened into the cell
                    inner, _ = _docx_table(el, q, links, refs)
                    parts += ["; ".join(c for c in r if c) for r in inner]
            line.append("\n".join(p for p in parts if p))
            span = tc.find(f"{W}tcPr/{W}gridSpan")
            if span is not None and (span.get(W + "val") or "").isdigit():
                line += [""] * (int(span.get(W + "val")) - 1)
        rows.append(line)
    look = tbl.find(f"{W}tblPr/{W}tblLook")
    if look is not None and (look.get(W + "firstRow") == "1" or
                             (re.fullmatch(r"[0-9A-Fa-f]{4}", look.get(W + "val") or "") and int(look.get(W + "val"), 16) & 0x20)):
        header = True
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows if any(c.strip() for c in r)]
    return rows, header and len(rows) > 1


def read_docx(data: bytes, title=None) -> DocText:
    pack = _Pack(data)
    doc = DocText(title=title, format="docx")
    q = doc.quality
    _props(pack, doc)
    main = next((r["target"] for r in pack.rels("").values() if r["type"] == "officeDocument"), "word/document.xml")
    root = pack.xml(main)
    if root is None:
        raise Unreadable("no main document part")
    links = pack.rels(main)
    # style id -> heading level (by the style's name: "heading 2", "Title"), following basedOn
    styles, based = {}, {}
    sx = pack.xml(next((r["target"] for r in links.values() if r["type"] == "styles"), "word/styles.xml"))
    for st in sx.iter(W + "style") if sx is not None else []:
        sid = st.get(W + "styleId")
        name = (st.find(W + "name").get(W + "val") if st.find(W + "name") is not None else sid) or ""
        m = re.fullmatch(r"heading\s*(\d)", name.strip(), re.I)
        if m:
            styles[sid] = int(m.group(1))
        elif name.strip().lower() == "title":
            styles[sid] = 1
        elif name.strip().lower() == "subtitle":
            styles[sid] = 2
        if st.find(W + "basedOn") is not None:
            based[sid] = st.find(W + "basedOn").get(W + "val")
    for sid, parent in based.items():
        hops = 0
        while sid not in styles and parent and hops < 10:
            if parent in styles:
                styles[sid] = styles[parent]
            parent, hops = based.get(parent), hops + 1
    # numbering: numId -> {level -> ordered?}
    ordered = {}
    nx = pack.xml(next((r["target"] for r in links.values() if r["type"] == "numbering"), "word/numbering.xml"))
    if nx is not None:
        abstract = {}
        for an in nx.findall(W + "abstractNum"):
            abstract[an.get(W + "abstractNumId")] = {
                lvl.get(W + "ilvl"): (lvl.find(W + "numFmt").get(W + "val") if lvl.find(W + "numFmt") is not None else "bullet")
                not in ("bullet", "none") for lvl in an.findall(W + "lvl")}
        for num in nx.findall(W + "num"):
            ref = num.find(W + "abstractNumId")
            if ref is not None:
                ordered[num.get(W + "numId")] = abstract.get(ref.get(W + "val"), {})
    counters = {}

    def body(parent):
        for el in parent:
            if el.tag == W + "p":
                refs = []
                text = _w_text(el, q, links, refs).strip()
                if not text:
                    continue
                q.source_chars += nonws(text)
                ppr = el.find(W + "pPr")
                style = ppr.find(W + "pStyle").get(W + "val") if ppr is not None and ppr.find(W + "pStyle") is not None else None
                outline = ppr.find(W + "outlineLvl") if ppr is not None else None
                num = ppr.find(W + "numPr") if ppr is not None else None
                level = styles.get(style)
                if level is None and outline is not None and (outline.get(W + "val") or "").isdigit() \
                        and int(outline.get(W + "val")) < 9:
                    level = int(outline.get(W + "val")) + 1
                if level:
                    doc.blocks.append(Block("heading", text.replace("\n", " "), level=min(level, 6), refs=refs))
                    if not doc.title and style and style.lower() == "title":
                        doc.title = text
                elif num is not None and num.find(W + "numId") is not None and num.find(W + "numId").get(W + "val") != "0":
                    nid = num.find(W + "numId").get(W + "val")
                    ilvl = num.find(W + "ilvl").get(W + "val") if num.find(W + "ilvl") is not None else "0"
                    meta = {}
                    if ordered.get(nid, {}).get(ilvl):
                        counters[(nid, ilvl)] = counters.get((nid, ilvl), 0) + 1
                        meta = {"ordered": True, "number": counters[(nid, ilvl)]}
                    doc.blocks.append(Block("list_item", text, level=int(ilvl) + 1 if ilvl.isdigit() else 1, refs=refs, meta=meta))
                elif style and re.search(r"quote", style, re.I):
                    doc.blocks.append(Block("quote", text, refs=refs))
                elif style and re.search(r"code|source|html.?pre|macro", style, re.I):
                    if doc.blocks and doc.blocks[-1].kind == "code" and doc.blocks[-1].meta.get("open"):
                        doc.blocks[-1].text += "\n" + text
                    else:
                        doc.blocks.append(Block("code", text, meta={"open": True}))
                    continue
                else:
                    doc.blocks.append(Block("paragraph", text, refs=refs))
            elif el.tag == W + "tbl":
                refs = []
                rows, header = _docx_table(el, q, links, refs)
                if rows:
                    q.source_chars += sum(nonws(c) for r in rows for c in r)
                    doc.blocks.append(Block("table", rows=rows, header=header, refs=refs))
            elif el.tag in (W + "sdt", W + "sdtContent", W + "customXml", W + "smartTag", W + "ins", W + "moveTo"):
                body(el)   # content controls and tracked insertions hold ordinary paragraphs
            if doc.blocks and doc.blocks[-1].kind == "code" and not (el.tag == W + "p"):
                doc.blocks[-1].meta.pop("open", None)

    main_body = root.find(W + "body")
    body(main_body if main_body is not None else root)
    for b in doc.blocks:
        b.meta.pop("open", None)
    # footnotes and endnotes: text the page shows, kept at the end
    for rel in links.values():
        if rel["type"] in ("footnotes", "endnotes"):
            part = pack.xml(rel["target"])
            for note in part if part is not None else []:
                if note.get(W + "type") in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                refs = []
                text = " ".join(t for t in (_w_text(p, q, pack.rels(rel["target"]), refs).strip() for p in note.iter(W + "p")) if t)
                if text:
                    q.source_chars += nonws(text)
                    doc.blocks.append(Block("paragraph", f"[{rel['type'][:-1]}] {text}", refs=refs))
        elif rel["type"] in ("header", "footer"):
            q.dropped["page header/footer part"] += 1
        elif rel["type"] == "comments":
            part = pack.xml(rel["target"])
            for c in part.findall(W + "comment") if part is not None else []:
                text = "\n".join(t for t in (_w_text(p, q).strip() for p in c.iter(W + "p")) if t)
                if text:
                    doc.comments.append({"id": c.get(W + "id"), "author": c.get(W + "author") or None,
                                         "at": _iso(c.get(W + "date")), "text": text})
    return doc.finish()


# ---------------------------------------------------------------- pptx

def _a_text(tx, q, links, refs):
    """[(level, text)] for the paragraphs of a text body."""
    out = []
    for p in tx.findall(A + "p"):
        parts = []
        for el in p:
            if el.tag in (A + "r", A + "fld"):
                t = el.findtext(A + "t") or ""
                parts.append(t)
                click = el.find(f"{A}rPr/{A}hlinkClick")
                rel = links.get(click.get(R + "id")) if click is not None else None
                if rel and rel["external"]:
                    refs.append(Ref("url", rel["target"], t))
                    if rel["target"] not in t:
                        parts.append(f" <{rel['target']}>")
            elif el.tag == A + "br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            ppr = p.find(A + "pPr")
            lvl = int(ppr.get("lvl")) if ppr is not None and (ppr.get("lvl") or "").isdigit() else 0
            out.append((lvl, text))
    return out


def _slide_shapes(tree, q, links):
    """(title, [Block]) for a slide's shape tree: text boxes, placeholders, tables, groups."""
    title, blocks = None, []

    def walk(parent):
        nonlocal title
        for sh in parent:
            if sh.tag == P + "grpSp":
                walk(sh)
            elif sh.tag == P + "sp":
                ph = sh.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
                kind = ph.get("type", "body") if ph is not None else None
                tx = sh.find(P + "txBody")
                if tx is None:
                    continue
                refs = []
                paras = _a_text(tx, q, links, refs)
                if not paras:
                    continue
                q.source_chars += sum(nonws(t) for _, t in paras)
                if kind in ("title", "ctrTitle") and title is None:
                    title = " ".join(t for _, t in paras).replace("\n", " ")
                    title_refs.extend(refs)
                elif kind in ("sldNum", "dt", "ftr"):
                    q.source_chars -= sum(nonws(t) for _, t in paras)
                    q.dropped["slide number / date / footer placeholder"] += 1
                else:
                    listy = kind in ("body", "obj") or any(lvl for lvl, _ in paras)
                    for k, (lvl, t) in enumerate(paras):
                        b = Block("list_item", t, level=lvl + 1) if listy and len(paras) > 1 else Block("paragraph", t)
                        if k == 0:
                            b.refs = refs
                        blocks.append(b)
            elif sh.tag == P + "graphicFrame":
                tbl = next(sh.iter(A + "tbl"), None)
                if tbl is None:
                    q.dropped["chart or diagram frame (no text read)"] += 1
                    continue
                refs, rows = [], []
                for tr in tbl.findall(A + "tr"):
                    rows.append(["\n".join(t for _, t in _a_text(tc.find(A + "txBody"), q, links, refs))
                                 if tc.find(A + "txBody") is not None else "" for tc in tr.findall(A + "tc")])
                width = max((len(r) for r in rows), default=0)
                rows = [r + [""] * (width - len(r)) for r in rows if any(c.strip() for c in r)]
                if rows:
                    q.source_chars += sum(nonws(c) for r in rows for c in r)
                    first = tbl.find(A + "tblPr")
                    blocks.append(Block("table", rows=rows, refs=refs,
                                        header=len(rows) > 1 and (first is None or first.get("firstRow") in ("1", "true"))))
            elif sh.tag == P + "pic":
                q.images += 1

    title_refs = []
    walk(tree)
    return title, title_refs, blocks


def read_pptx(data: bytes, title=None) -> DocText:
    pack = _Pack(data)
    doc = DocText(title=title, format="pptx")
    q = doc.quality
    _props(pack, doc)
    main = next((r["target"] for r in pack.rels("").values() if r["type"] == "officeDocument"), "ppt/presentation.xml")
    pres = pack.xml(main)
    if pres is None:
        raise Unreadable("no presentation part")
    rels = pack.rels(main)
    order = [rels[s.get(R + "id")]["target"] for s in pres.iter(P + "sldId") if s.get(R + "id") in rels]
    for n, part in enumerate(order, 1):
        q.slides += 1
        try:
            slide = pack.xml(part)
            if slide is None:
                raise Unreadable(f"{part} is missing")
            links = pack.rels(part)
            tree = slide.find(f"{P}cSld/{P}spTree")
            stitle, trefs, blocks = _slide_shapes(tree if tree is not None else [], q, links)
        except Unreadable as e:
            q.failed_parts += 1
            q.warn(f"slide {n}: {e}")
            doc.blocks.append(Block("container", container="slide", meta={"index": n}))
            continue
        meta = {"index": n, **({"hidden": True} if slide.get("show") in ("0", "false") else {})}
        doc.blocks.append(Block("container", stitle or "", container="slide", refs=trefs, meta=meta))
        doc.blocks.extend(blocks)
        for rel in links.values():
            if rel["type"] == "notesSlide":
                notes = pack.xml(rel["target"])
                tree = notes.find(f"{P}cSld/{P}spTree") if notes is not None else None
                texts, refs = [], []
                for sh in tree.iter(P + "sp") if tree is not None else []:
                    ph = sh.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
                    if ph is not None and ph.get("type") == "body" and sh.find(P + "txBody") is not None:
                        texts += [t for _, t in _a_text(sh.find(P + "txBody"), q, pack.rels(rel["target"]), refs)]
                if texts:
                    q.source_chars += sum(nonws(t) for t in texts)
                    doc.blocks.append(Block("heading", "Speaker notes", level=2, meta={"notes": True}))
                    doc.blocks += [Block("paragraph", t, refs=refs if k == 0 else []) for k, t in enumerate(texts)]
    if not doc.title:
        doc.title = next((b.text for b in doc.blocks if b.kind == "container" and b.text), None)
    return doc.finish()


# ---------------------------------------------------------------- xlsx

BUILTIN_DATE_FMTS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def _is_date_format(code: str) -> bool:
    """A number format that shows a date or a time: it has y, m, d, h or s outside quotes and brackets."""
    code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.', "", code or "")
    return bool(re.search(r"[ymdhs]", code, re.I)) and "general" not in code.lower()


def excel_date(serial: float, date1904=False) -> str:
    """An Excel serial day number -> ISO: a date, a time of day (serial < 1), or a date and time."""
    if date1904:
        base = dt.datetime(1904, 1, 1)
    else:
        base = dt.datetime(1899, 12, 30)
        if serial < 60:      # Excel counts 29 Feb 1900, a day that never was
            serial += 1
    whole = int(serial // 1)
    seconds = round((serial - whole) * 86400)
    t = base + dt.timedelta(days=whole, seconds=seconds)
    if serial < 1 and not date1904:
        return t.strftime("%H:%M:%S")
    return t.strftime("%Y-%m-%d") if seconds == 0 else t.strftime("%Y-%m-%dT%H:%M:%S")


def _col(ref: str) -> int:
    """'C5' -> 2 (zero-based column)."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _si_text(si) -> str:
    """A shared or inline string: plain <t>, or rich-text runs; phonetic guides are not text."""
    if si is None:
        return ""
    return "".join(t.text or "" for el in si for t in ([el] if el.tag == S + "t" else el.iter(S + "t") if el.tag == S + "r" else []))


def _number(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v
    if f == int(f) and abs(f) < 1e15 and "e" not in v.lower() and "." not in v:
        return v
    return format(f, ".15g")   # drops binary noise such as 0.30000000000000004


def read_xlsx(data: bytes, title=None) -> DocText:
    pack = _Pack(data)
    doc = DocText(title=title, format="xlsx")
    q = doc.quality
    _props(pack, doc)
    main = next((r["target"] for r in pack.rels("").values() if r["type"] == "officeDocument"), "xl/workbook.xml")
    wb = pack.xml(main)
    if wb is None:
        raise Unreadable("no workbook part")
    rels = pack.rels(main)
    by_type = lambda t: next((r["target"] for r in rels.values() if r["type"] == t), None)
    shared = []
    sst = pack.xml(by_type("sharedStrings") or "xl/sharedStrings.xml")
    if sst is not None:
        shared = [_si_text(si) for si in sst.findall(S + "si")]
    date_xf = []   # per cell style index: is it a date format?
    st = pack.xml(by_type("styles") or "xl/styles.xml")
    if st is not None:
        custom = {int(n.get("numFmtId")): n.get("formatCode", "") for n in st.iter(S + "numFmt") if (n.get("numFmtId") or "").isdigit()}
        xfs = st.find(S + "cellXfs")
        for xf in xfs if xfs is not None else []:
            fid = int(xf.get("numFmtId") or 0)
            date_xf.append(fid in BUILTIN_DATE_FMTS or (fid in custom and _is_date_format(custom[fid])))
    pr = wb.find(S + "workbookPr")
    date1904 = pr is not None and pr.get("date1904") in ("1", "true")
    sheets = wb.find(S + "sheets")
    for n, sh in enumerate(sheets if sheets is not None else [], 1):
        name = sh.get("name") or f"Sheet{n}"
        q.sheets += 1
        meta = {"index": n, **({"hidden": True} if sh.get("state") in ("hidden", "veryHidden") else {})}
        doc.blocks.append(Block("container", name, container="sheet", meta=meta))
        rel = rels.get(sh.get(R + "id"))
        if rel is None or rel["type"] != "worksheet":
            q.dropped[f"sheet that is not a worksheet ({rel['type'] if rel else 'no relationship'})"] += 1
            continue
        try:
            ws = pack.xml(rel["target"])
            if ws is None:
                raise Unreadable(f"{rel['target']} is missing")
        except Unreadable as e:
            q.failed_parts += 1
            q.warn(f"sheet {name!r}: {e}")
            continue
        grid, row_no = {}, 0   # (row, col) -> text
        for row in ws.iter(S + "row"):
            row_no = int(row.get("r")) if (row.get("r") or "").isdigit() else row_no + 1
            col = -1
            for c in row.findall(S + "c"):
                col = _col(c.get("r")) if c.get("r") else col + 1   # a cell sits where its address says
                t, v = c.get("t"), c.findtext(S + "v")
                if t == "s":
                    val = shared[int(v)] if v and v.isdigit() and int(v) < len(shared) else ""
                elif t == "inlineStr":
                    val = _si_text(c.find(S + "is"))
                elif t == "b":
                    val = "TRUE" if v == "1" else "FALSE"
                elif t in ("str", "e", "d"):
                    val = v or ""
                    if t == "e":
                        q.unknown["cell holding a formula error"] += 1
                elif v is not None:
                    s = int(c.get("s")) if (c.get("s") or "").isdigit() else -1
                    if 0 <= s < len(date_xf) and date_xf[s]:
                        try:
                            val = excel_date(float(v), date1904)
                        except (ValueError, OverflowError):
                            val = v
                    else:
                        val = _number(v)
                else:
                    val = ""
                    if c.find(S + "f") is not None:   # a formula never calculated: say what it is, not nothing
                        val = "=" + (c.findtext(S + "f") or "")
                        q.unknown["formula with no stored result"] += 1
                if val != "":
                    grid[(row_no, col)] = val
        if not grid:
            q.dropped["empty sheet"] += 1
            continue
        cols = sorted({c for _, c in grid})
        rows = [[grid.get((r, c), "") for c in cols] for r in sorted({r for r, _ in grid})]
        q.source_chars += sum(nonws(v) for v in grid.values())
        merged = [m.get("ref") for m in ws.iter(S + "mergeCell") if m.get("ref")]
        doc.blocks.append(Block("table", rows=rows, header=len(rows) > 1, meta={"merged": merged} if merged else {}))
        if merged:
            q.dropped[f"merged cell ranges (value kept in the first cell)"] += len(merged)
    return doc.finish()
