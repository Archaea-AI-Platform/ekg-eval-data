"""PDF -> DocText: one container per page, tables as tables, the document's own properties as meta.

With pdfplumber the page's ruled tables are read as tables and the text around them as paragraphs,
in reading order. Without it, pypdf gives the page's text (a table then comes out as its lines).
With neither, the format is unsupported. A page with no text layer (a scan) is counted in
`quality.empty_pages` and warned about; nothing is recognised here, but `ocr` may be passed: a
callable `ocr(pdf_bytes, page_index) -> text` that is asked for exactly those pages.
"""

from __future__ import annotations

import datetime as dt
import io
import re

from .model import Block, DocText, Unreadable, Unsupported, nonws

PDF_DATE = re.compile(r"D:(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?\s*([Zz+-])?(\d\d)?'?(\d\d)?")


def pdf_date(value):
    """A PDF date (D:20261110032941+05'30') -> UTC ISO, or None."""
    m = PDF_DATE.match(str(value or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, sign, oh, om = m.groups()
    try:
        t = dt.datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    if sign in ("+", "-") and oh:
        off = dt.timedelta(hours=int(oh), minutes=int(om or 0))
        t = t - off if sign == "+" else t + off
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _meta(doc: DocText, info: dict):
    """The document information dictionary. /Author is who wrote it; /Creator is the program that made it."""
    get = lambda k: (str(info.get(k) or info.get("/" + k) or "").strip() or None)
    blank = {"(unspecified)", "unknown", "untitled", "(anonymous)"}
    clean = lambda v: None if v is None or v.lower() in blank else v
    doc.title = doc.title or clean(get("Title"))
    for key, value in (("creator", clean(get("Author"))), ("application", clean(get("Creator")) or clean(get("Producer"))),
                       ("created_at", pdf_date(get("CreationDate"))), ("modified_at", pdf_date(get("ModDate"))),
                       ("subject", clean(get("Subject"))), ("keywords", clean(get("Keywords")))):
        if value:
            doc.meta[key] = value


def _paragraphs(text: str):
    return [p.strip("\n") for p in re.split(r"\n\s*\n", text) if p.strip()]


def _columns(page, table, rows):
    """A table ruled between its rows only (no vertical lines) comes out with one cell per row. Its columns are
    then the vertical strips of white that run through every row: a strip no word of any row crosses, at least
    three quarters of a text line wide (an ordinary space between words is about a quarter). A boundary can
    never fall inside a word, so no text is cut; with no such strip the table stays as its rows of text."""
    try:
        lines = [page.crop(r.bbox).extract_words() for r in table.rows]
    except Exception:
        return None
    words = [w for ws in lines for w in ws]
    if len(lines) != len(rows) or len(words) < 2:
        return None
    heights = sorted(w["bottom"] - w["top"] for w in words)
    min_gap = 0.75 * heights[len(heights) // 2]
    spans = []   # the x ranges that hold text in some row, merged
    for x0, x1 in sorted((w["x0"], w["x1"]) for w in words):
        if spans and x0 <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], x1)
        else:
            spans.append([x0, x1])
    cuts = [(a[1] + b[0]) / 2 for a, b in zip(spans, spans[1:]) if b[0] - a[1] >= min_gap]
    if not cuts:
        return None
    out = []
    for ws in lines:
        cells = [[] for _ in range(len(cuts) + 1)]
        for w in ws:
            cells[sum(1 for c in cuts if (w["x0"] + w["x1"]) / 2 > c)].append(w)
        out.append([_cell_lines(c, min_gap) for c in cells])
    return out


def _cell_lines(words, gap):
    """One cell's words as its own lines: words side by side are one line (joined by spaces), a word below the
    line before starts a new one (joined by a line break, as pdfplumber keeps a ruled cell's lines). A cell
    written on two lines ("E003 Lionel" over "eng-orders") so keeps the boundary between its two values."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and w["top"] - rows[-1][0] < gap:   # the same line: its top within a fraction of a line height
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return "\n".join(" ".join(x["text"] for x in sorted(ws, key=lambda x: x["x0"])) for _, ws in rows)


def _plumber_page(page, q):
    """[(top, Block)] for one page: tables with at least two rows and two columns, and the text outside them."""
    tables = []
    for t in page.find_tables():
        rows = [[(c or "").strip() for c in r] for r in (t.extract() or [])]
        if len(rows) >= 2 and max(len(r) for r in rows) == 1:
            rows = _columns(page, t, rows) or rows
        rows = [r for r in rows if any(r)]
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            tables.append((t.bbox, rows))
    boxes = [b for b, _ in tables]

    def outside(obj):
        if "x0" not in obj or "top" not in obj:
            return True
        cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)

    rest = page.filter(outside) if boxes else page
    out = []
    for bbox, rows in tables:
        width = max(len(r) for r in rows)
        out.append((bbox[1], Block("table", rows=[r + [""] * (width - len(r)) for r in rows], header=True)))
    para, last = [], None
    for ln in rest.extract_text_lines():
        text = ln["text"].strip()
        if not text:
            continue
        height = max(ln["bottom"] - ln["top"], 1.0)
        if para and last is not None and ln["top"] - last > 0.7 * height:   # a gap: a new paragraph
            out.append((para[0][0], Block("paragraph", "\n".join(t for _, t in para))))
            para = []
        para.append((ln["top"], text))
        last = ln["bottom"]
    if para:
        out.append((para[0][0], Block("paragraph", "\n".join(t for _, t in para))))
    out.sort(key=lambda x: x[0])
    return [b for _, b in out]


def read_pdf(data: bytes, title=None, ocr=None) -> DocText:
    try:
        import pypdf
    except ImportError:
        pypdf = None
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pypdf is None and pdfplumber is None:
        raise Unsupported("no PDF library installed (pypdf or pdfplumber)")
    doc = DocText(title=title, format="pdf")
    q = doc.quality
    reader = plumb = None
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise Unreadable("encrypted PDF")
            _meta(doc, dict(reader.metadata or {}))
        except Unreadable:
            raise
        except Exception as e:
            if pdfplumber is None:
                raise Unreadable(f"damaged PDF ({type(e).__name__})") from e
            reader = None
    if pdfplumber is not None:
        try:
            plumb = pdfplumber.open(io.BytesIO(data))
            if reader is None:
                _meta(doc, dict(plumb.metadata or {}))
        except Exception as e:
            if reader is None:
                raise Unreadable(f"damaged PDF ({type(e).__name__})") from e
            plumb = None
            q.warn(f"tables not read: pdfplumber could not open the file ({type(e).__name__})")
    else:
        q.warn("tables read as text lines (pdfplumber is not installed)")
    try:
        n_pages = len(plumb.pages) if plumb is not None else len(reader.pages)
        for i in range(n_pages):
            q.pages += 1
            blocks = None
            if plumb is not None:
                try:
                    blocks = _plumber_page(plumb.pages[i], q)
                except Exception as e:   # one bad page: fall back to plain text for it
                    q.warn(f"page {i + 1}: layout not read ({type(e).__name__}), plain text used")
            if blocks is None:
                try:
                    text = (reader.pages[i].extract_text() or "") if reader is not None else ""
                    blocks = [Block("paragraph", p) for p in _paragraphs(text)]
                except Exception as e:
                    q.failed_parts += 1
                    q.warn(f"page {i + 1}: not readable ({type(e).__name__})")
                    blocks = []
                    doc.blocks.append(Block("container", container="page", meta={"index": i + 1}))
                    continue
            if not blocks and ocr is not None:
                text = ocr(data, i) or ""
                blocks = [Block("paragraph", p) for p in _paragraphs(text)]
                if blocks:
                    q.ocr_pages += 1
            doc.blocks.append(Block("container", container="page", meta={"index": i + 1}))
            if not blocks:
                q.empty_pages += 1
                continue
            for b in blocks:
                q.source_chars += nonws(b.text) if b.kind != "table" else sum(nonws(c) for r in b.rows for c in r)
            doc.blocks.extend(blocks)
    finally:
        if plumb is not None:
            plumb.close()
    if q.empty_pages:
        q.warn(f"{q.empty_pages} of {q.pages} pages have no text layer (scanned or image-only): OCR needed")
    return doc.finish()
